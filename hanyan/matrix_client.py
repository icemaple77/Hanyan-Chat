"""
Hanyan Chat — Matrix 通信层（替代 KouriChat 的 wxauto）
基于 mautrix SDK，负责：
- 登录 Matrix 服务器（token 优先，失效自动回退密码登录）
- 手动 sync 循环，监听 DM 文字消息 + 语音消息（语音会先过 STT 回调转成文字）
- 发送文字/图片/语音/表情/reaction（[tickle]/[tickle_self]/[recall] 的载体）
"""

import asyncio
import collections
import json
import logging
import os
import time
import mimetypes
from typing import Callable, Optional

from mautrix.client import Client
from mautrix.client.state_store import FileStateStore
from mautrix.types import (
    EventType,
    Membership,
    MessageEvent,
    TextMessageEventContent,
    MediaMessageEventContent,
    MessageType,
    RoomID,
    ContentURI,
    ImageInfo,
    AudioInfo,
    FileInfo,
)

from . import config

logger = logging.getLogger("hanyan.matrix")

_TOKEN_PATH = os.path.join(config.ROOT_DIR, "data", "access_token.txt")

# 认证失效时 Matrix 返回的错误码/关键字（用于区分"需要重新登录"与"临时网络故障"）
_AUTH_ERROR_MARKERS = ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN", "401")

_SEEN_EVENTS_PATH = os.path.join(config.ROOT_DIR, "data", "seen_events.json")

# 同一个发送者，短时间内发来内容完全相同的消息，大概率是客户端网络卡顿后的
# 自动重发（重发时会生成新的 event_id，_mark_seen 按 event_id 去重完全拦不住，
# 实测日志里出现过同一句话在同一秒内被当成 3 条不同事件、各自触发一次完整
# LLM 回复的情况）。这里按"发送者+文字内容"做一层内容级去重兜底。
_DUPLICATE_BODY_WINDOW_SECONDS = 60.0  # LLM 回复慢，窗口要大于一次 LLM 生成时间

# 不是所有 mautrix-python 版本都在 EventType 上预置了 REACTION 常量，
# 用 find() 兜底，保证 [tickle]/[tickle_self] 在任意版本下都不会因为
# AttributeError 崩掉（此时会被 send_tickle 里的 try/except 捕获，退化为文字）。
try:
    _REACTION_EVENT_TYPE = EventType.REACTION
except AttributeError:
    _REACTION_EVENT_TYPE = EventType.find("m.reaction", EventType.Class.MESSAGE)


class MatrixClient:
    """Matrix 客户端 — 消息收发层"""

    def __init__(self):
        self.client: Optional[Client] = None
        self._event_handlers = []
        self._logged_in = False
        self._stop_future: Optional[asyncio.Future] = None
        self._sync_task: Optional[asyncio.Task] = None
        # 已处理的事件 ID 去重：deque 保证有界 + FIFO 淘汰，set 用于 O(1) 查找。
        self._seen_events_order: collections.deque = collections.deque(maxlen=10000)
        self._seen_events: set = set()
        self._seen_dirty = False  # 只在真的有新事件时才落盘，避免空闲时每次 sync 都无意义写文件
        self._load_seen_events()
        # 用于 [tickle]/[recall] 等动作标记：记录每个房间最近一次收到/发出的消息事件。
        self._last_sent_event: dict = {}      # room_id -> event_id（bot 自己发的最后一条文本）
        self._last_received_event: dict = {}  # room_id -> event_id（用户发的最后一条消息）
        # 内容级去重兜底：sender -> (body, monotonic_timestamp)。见 _DUPLICATE_BODY_WINDOW_SECONDS 注释。
        self._recent_body_cache: dict = {}
        # 语音转文字回调：set_stt_callback() 注入，签名 (audio_bytes, mime_type) -> Optional[str]。
        # 放在这一层而不是上层 bot 逻辑里判断 msgtype，是因为"语音先转文字再走正常流程"
        # 本质上是传输层的预处理，转完之后 handler 完全不需要知道这条消息原本是语音。
        self._stt_callback: Optional[Callable[[bytes, str], Optional[str]]] = None
        # DM 缓存，避免每次 sync 都调 get_joined_members（那个会吃限流）
        self._dm_cache: dict[str, bool] = {}
        self._dm_cache_time: dict[str, float] = {}

    def set_stt_callback(self, callback: Callable[[bytes, str], Optional[str]]):
        """注册语音转文字回调（同步函数，内部会丢进线程池执行，不阻塞事件循环）。"""
        self._stt_callback = callback

    # ── 连接 / 断开 ──────────────────────────────────────────

    async def connect(self):
        """登录 Matrix 服务器并开始同步。"""
        self.client = Client(
            base_url=config.get("matrix.homeserver"),
            state_store=FileStateStore(path=os.path.join(config.ROOT_DIR, "data", "state_store.json")),
        )

        # 尝试用存储的 token 登录，失败则走密码登录
        if os.path.exists(_TOKEN_PATH):
            try:
                with open(_TOKEN_PATH) as f:
                    saved_token = f.read().strip()
                if not saved_token:
                    raise ValueError("empty token file")
                self.client.api.token = saved_token
                self.client.mxid = config.get("matrix.user_id")
                # 验证 token 是否仍然有效，避免带着已失效的 token 直接进同步循环，
                # 那样要等 sync 报错才发现，还会被误判成网络故障反复重试。
                await self.client.whoami()
                self._logged_in = True
                logger.info("Logged in via saved token")
            except Exception as e:
                logger.warning("Saved token invalid/expired (%s); falling back to password login", e)
                self._logged_in = False
        if not self._logged_in:
            await self._login_with_password()

        # 注册事件处理器（供 mautrix 内建 dispatcher 使用；实际消息路径走下面的
        # 手动 _sync_loop，这两条路径互不冲突）
        self.client.add_event_handler(EventType.ROOM_MEMBER, self._on_room_member, wait_sync=True)
        # 消息处理走手动 sync 循环（_sync_loop），不注册 mautrix 内建 dispatcher，
        # 避免每条消息被处理两次。

        # 开始同步
        self._stop_future = asyncio.get_event_loop().create_future()
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("Sync started")

    async def _login_with_password(self):
        """用配置里的密码登录，并把新 token 落盘。"""
        hs = config.get("matrix.homeserver")
        user_id = config.get("matrix.user_id")
        password = config.get("matrix.password")
        device_name = config.get("matrix.device_name", "HanyanChat")

        logger.info("Logging in to %s as %s ...", hs, user_id)
        resp = await self.client.login(
            identifier=user_id,
            password=password,
            device_name=device_name,
        )
        self._logged_in = True
        logger.info("Logged in as %s (device: %s)", resp.user_id, resp.device_id)

        try:
            os.makedirs(os.path.dirname(_TOKEN_PATH), exist_ok=True)
            with open(_TOKEN_PATH, "w") as f:
                f.write(self.client.api.token)
        except OSError as e:
            logger.warning("Failed to save access token: %s", e)

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        """粗略判断一个同步异常是不是"token 失效"而不是网络抖动。"""
        msg = str(exc)
        return any(marker in msg for marker in _AUTH_ERROR_MARKERS)

    async def _reauthenticate(self):
        """token 失效时：删除旧 token，重新走密码登录。"""
        logger.warning("Matrix token appears invalid; re-authenticating with password")
        try:
            os.remove(_TOKEN_PATH)
        except OSError:
            pass
        self._logged_in = False
        await self._login_with_password()

    async def disconnect(self):
        """断开并停止同步。"""
        if self._stop_future and not self._stop_future.done():
            self._stop_future.set_result(None)
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        if self.client:
            try:
                result = self.client.stop()
                if result is not None:
                    await result
            except Exception:
                pass
        self._logged_in = False
        logger.info("Disconnected")

    # ── 去重（持久化） ──────────────────────────────────────────
    #
    # 之前的实现只在内存里去重，进程重启后这个集合是空的——如果 mautrix 的
    # state store 没能可靠地记住 sync token，重启后可能会短暂重放最近的一批
    # 事件。落盘这个去重集合作为额外一层保险。只在真的有新事件时才写文件
    # （_seen_dirty 标记），而不是每次 sync 循环跑一圈就无条件写一次——
    # 5 秒一次的短轮询空闲时也会跑很多圈，无条件写盘会变成持续的无意义 I/O。

    def _load_seen_events(self):
        """从文件加载已处理的事件 ID。"""
        if not os.path.exists(_SEEN_EVENTS_PATH):
            return
        try:
            with open(_SEEN_EVENTS_PATH) as f:
                events = json.load(f)
            self._seen_events = set(events)
            self._seen_events_order = collections.deque(events, maxlen=10000)
            logger.info("Loaded %d seen events from disk", len(self._seen_events))
        except Exception as e:
            logger.warning("Failed to load seen events: %s", e)

    def _save_seen_events(self):
        """将已处理的事件 ID 保存到文件（仅当有新增时才真正写盘）。"""
        if not self._seen_dirty:
            return
        try:
            os.makedirs(os.path.dirname(_SEEN_EVENTS_PATH), exist_ok=True)
            tmp_path = _SEEN_EVENTS_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(list(self._seen_events), f)
            os.replace(tmp_path, _SEEN_EVENTS_PATH)
            self._seen_dirty = False
        except Exception as e:
            logger.debug("Failed to save seen events: %s", e)

    # ── 去重（已处理事件检测） ──────────────────────────────────

    def _mark_seen(self, event_id: str) -> bool:
        """去重。返回 True 表示这是新事件（应处理），False 表示重复。
        用有界 deque 做 FIFO 淘汰，而不是达到上限就整体 clear()。"""
        if not event_id or event_id in self._seen_events:
            return False
        if len(self._seen_events_order) >= self._seen_events_order.maxlen:
            oldest = self._seen_events_order.popleft()
            self._seen_events.discard(oldest)
        self._seen_events_order.append(event_id)
        self._seen_events.add(event_id)
        self._seen_dirty = True
        return True

    # ── 语音转文字 ───────────────────────────────────────────

    async def _maybe_transcribe_voice(self, content: dict) -> Optional[str]:
        """如果这是一条语音消息（m.audio）且注册了 STT 回调，下载媒体并转写。
        任何一步失败都返回 None，让调用方静默跳过这条消息，不影响其它消息处理。"""
        if content.get("msgtype") != "m.audio" or not self._stt_callback:
            return None
        mxc = content.get("url")
        if not mxc:
            return None
        try:
            download = getattr(self.client, "download_media", None) or self.client.api.download_media
            audio_bytes = await download(mxc)
        except Exception as e:
            logger.warning("Failed to download voice message media: %s", e)
            return None
        mime_type = (content.get("info") or {}).get("mimetype", "audio/ogg")
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._stt_callback, audio_bytes, mime_type)
        except Exception as e:
            logger.error("STT callback failed: %s", e)
            return None

    # ── 手动 sync 消息处理 ───────────────────────────────────

    async def _on_message_raw(self, event):
        """处理来自手动 sync 的原始消息事件（文字 + 语音都走这里）。"""
        if event.get("type") != "m.room.message":
            return
        room_id = event.get("room_id", "")
        sender = event.get("sender", "")
        if not sender or sender == self.client.mxid:
            return
        # 只处理 DM（跳过成员数 > 2 的群聊）
        if not await self._is_dm(room_id):
            return
        event_id = event.get("event_id", "")
        if not self._mark_seen(event_id):
            return
        self._last_received_event[room_id] = event_id

        content = event.get("content", {})
        msgtype = content.get("msgtype", "")

        body = ""
        if msgtype == "m.audio":
            # 语音消息：不能直接把 body（通常是文件名，比如 "voice-message.ogg"）
            # 当成聊天内容发给 LLM——那样等于在瞎聊文件名。必须先过 STT。
            body = await self._maybe_transcribe_voice(content)
            if not body:
                logger.info("Voice message from %s could not be transcribed, skipping", sender)
                return
            logger.info("Voice->text from %s in %s: %.50s", sender.split(":")[0], room_id[:12], body)
        else:
            body = content.get("body", "").strip()
            if not body:
                return
            logger.info("Raw msg from %s in %s: %.50s", sender.split(":")[0], room_id[:12], body)

        # 内容级去重：同一发送者短时间内发来完全相同的文字
        # （事件 event_id 不同，_mark_seen 挡不住），不是真的想再问一遍同一句话。
        now = time.monotonic()
        cached = self._recent_body_cache.get(sender)
        if cached and cached[0] == body and (now - cached[1]) < _DUPLICATE_BODY_WINDOW_SECONDS:
            logger.info(
                "Duplicate message body from %s within %.1fs, skipping: %.50s",
                sender.split(":")[0], now - cached[1], body,
            )
            return
        self._recent_body_cache[sender] = (body, now)

        for handler in self._event_handlers:
            try:
                was_voice = msgtype == "m.audio"
                await handler(room_id, sender, body, event, was_voice)
            except Exception as e:
                # 单个 handler 出错绝不能冒泡回 _sync_loop 的外层 try，
                # 否则会被误判为"同步/网络故障"，触发不必要的重连退避。
                logger.exception("Handler error for event %s: %s", event_id, e)

    async def _process_timeline_event(self, room_id: str, event: dict):
        """处理单条 timeline 事件。异常在这里被吞掉并记录，绝不冒泡到
        _sync_loop 的外层 try，这样一条消息处理失败不会导致整批 sync 结果
        （其 sync token 已经前进）被误判为连接失败，也不会丢失同批次
        其它房间/事件的处理。"""
        try:
            event["room_id"] = room_id
            await self._on_message_raw(event)
        except Exception as e:
            logger.exception("Error processing event in room %s: %s", room_id, e)

    async def _sync_loop(self):
        """持续同步循环，手动处理 sync 数据。"""
        logger.info("Sync loop started")
        retry_delay = 1
        max_retry = 60
        while not self._stop_future.done():
            try:
                sync_data = await self.client.sync(timeout=5000, full_state=False)
                for room_id, room in (sync_data.get("rooms", {}) or {}).get("join", {}).items():
                    for event in (room.get("timeline", {}) or {}).get("events", []):
                        await self._process_timeline_event(room_id, event)
                    for event in (room.get("state", {}) or {}).get("events", []):
                        if event.get("type") == "m.room.member" and event.get("state_key") == self.client.mxid:
                            if event.get("content", {}).get("membership") == "invite":
                                logger.info("Invite to %s, joining...", room_id)
                                try:
                                    await self.client.join_room(room_id)
                                except Exception as e:
                                    logger.error("Join failed %s: %s", room_id, e)
                for room_id in (sync_data.get("rooms", {}) or {}).get("invite", {}):
                    logger.info("Invited to %s", room_id)
                    try:
                        await self.client.join_room(room_id)
                    except Exception as e:
                        logger.error("Join failed %s: %s", room_id, e)
                retry_delay = 1
                self._save_seen_events()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._is_auth_error(e):
                    try:
                        await self._reauthenticate()
                        retry_delay = 1
                        continue
                    except Exception as reauth_err:
                        logger.error("Re-authentication failed: %s", reauth_err)
                logger.error("Sync error: %s (retry in %ds)", e, retry_delay)
                try:
                    await self._wait_with_cancel(retry_delay)
                except asyncio.TimeoutError:
                    pass
                retry_delay = min(retry_delay * 2, max_retry)
        logger.info("Sync loop ended")

    async def _wait_with_cancel(self, delay: float):
        """等待，但可被 _stop_future 取消。"""
        try:
            await asyncio.wait([self._stop_future], timeout=delay)
        except (asyncio.CancelledError, ValueError):
            pass

    # ── mautrix 内建 dispatcher 路径（备用，当前实际消息走上面的手动 sync）──

    async def _on_room_member(self, event):
        """自动接受房间邀请。"""
        if event.content.membership == Membership.INVITE and event.state_key == self.client.mxid:
            try:
                await self.client.join_room(event.room_id)
                logger.info("Auto-joined room %s", event.room_id)
            except Exception as e:
                logger.error("Failed to join room %s: %s", event.room_id, e)

    async def _on_message(self, event: MessageEvent):
        """收到 Matrix 消息事件（mautrix 内建 dispatcher 路径）。"""
        if not await self._is_dm(event.room_id):
            return
        if event.sender == self.client.mxid:
            return
        content = event.content
        if isinstance(content, TextMessageEventContent):
            text = content.body.strip()
            if not text:
                return
            for handler in self._event_handlers:
                try:
                    await handler(event.room_id, event.sender, text, event)
                except Exception as e:
                    logger.exception("Handler error: %s", e)

    async def _is_dm(self, room_id: RoomID) -> bool:
        """判断房间是否为 DM（只有自己和另一个用户）。
        结果缓存 5 分钟——这个方法每条消息都会被调一次，不缓存的话每条消息
        都要打一次 get_joined_members API（__init__ 里早就声明了 _dm_cache，
        但之前忘了在这里真正用上）。"""
        now = time.monotonic()
        cached_at = self._dm_cache_time.get(room_id, 0)
        if room_id in self._dm_cache and now - cached_at < 300:
            return self._dm_cache[room_id]
        try:
            members = await self.client.get_joined_members(room_id)
            others = [u for u in members if u != self.client.mxid]
            result = len(others) == 1
        except Exception:
            return False  # 查询失败不缓存，下次重试
        self._dm_cache[room_id] = result
        self._dm_cache_time[room_id] = now
        return result

    def on_message(self, handler: Callable):
        """注册消息处理回调。handler(room_id, sender, text, event)"""
        self._event_handlers.append(handler)

    # ── 发送消息 ─────────────────────────────────────────────

    async def send_text(self, room_id: RoomID, text: str) -> Optional[str]:
        """发送文字消息。返回 event_id。"""
        if not self._ensure_connected():
            return None
        try:
            resp = await self.client.send_text(room_id, text)
            logger.debug("Sent text to %s: %.50s", room_id, text)
            if resp:
                self._last_sent_event[room_id] = resp
            return resp
        except Exception as e:
            logger.error("send_text failed: %s", e)
            return None

    async def send_tickle(self, room_id: RoomID) -> bool:
        """[tickle] 的等价实现：Matrix 没有"拍一拍"原生功能，用 m.reaction
        贴到用户最后一条消息上模拟（贴不了就退化成一句文字）。"""
        if not self._ensure_connected():
            return False
        target_event = self._last_received_event.get(room_id)
        if target_event:
            try:
                await self.client.send_message_event(
                    room_id,
                    _REACTION_EVENT_TYPE,
                    {"m.relates_to": {"rel_type": "m.annotation", "event_id": target_event, "key": "👋"}},
                )
                return True
            except Exception as e:
                logger.debug("send_tickle reaction failed, falling back to text: %s", e)
        await self.send_text(room_id, "*拍了拍你*")
        return True

    async def send_tickle_self(self, room_id: RoomID) -> bool:
        """[tickle_self] 的等价实现：给 bot 自己最后一条发出的消息加反应。"""
        if not self._ensure_connected():
            return False
        target_event = self._last_sent_event.get(room_id)
        if target_event:
            try:
                await self.client.send_message_event(
                    room_id,
                    _REACTION_EVENT_TYPE,
                    {"m.relates_to": {"rel_type": "m.annotation", "event_id": target_event, "key": "🙈"}},
                )
                return True
            except Exception as e:
                logger.debug("send_tickle_self reaction failed: %s", e)
        return False

    async def redact_last_sent(self, room_id: RoomID, reason: str = "撤回") -> bool:
        """[recall] 的等价实现：撤回 bot 自己最后发出的一条消息。"""
        if not self._ensure_connected():
            return False
        target_event = self._last_sent_event.get(room_id)
        if not target_event:
            logger.debug("redact_last_sent: no last-sent event tracked for %s", room_id)
            return False
        try:
            await self.client.redact(room_id, target_event, reason=reason)
            self._last_sent_event.pop(room_id, None)
            return True
        except Exception as e:
            logger.error("redact_last_sent failed: %s", e)
            return False

    async def send_image(self, room_id: RoomID, filepath: str) -> Optional[str]:
        """发送图片/表情包。自动上传 media 到 Matrix 仓库。"""
        if not self._ensure_connected():
            return None
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            mime_type, _ = mimetypes.guess_type(filepath)
            mime_type = mime_type or "image/png"

            mxc = await self.client.upload_media(data, mime_type=mime_type, filename=os.path.basename(filepath))
            info = ImageInfo(mimetype=mime_type, size=len(data))
            resp = await self.client.send_image(room_id, url=mxc, info=info, file_name=os.path.basename(filepath))
            logger.debug("Sent image to %s: %s", room_id, filepath)
            return resp
        except Exception as e:
            logger.error("send_image failed: %s", e)
            return None

    async def send_voice(self, room_id: RoomID, audio_path: str) -> Optional[str]:
        """发送语音消息（m.audio + m.voice flag）。"""
        if not self._ensure_connected():
            return None
        try:
            with open(audio_path, "rb") as f:
                data = f.read()
            mime_type = "audio/ogg"
            if audio_path.endswith(".wav"):
                mime_type = "audio/wav"
            elif audio_path.endswith(".mp3"):
                mime_type = "audio/mpeg"

            mxc = await self.client.upload_media(data, mime_type=mime_type, filename=os.path.basename(audio_path))
            msg_content = {
                "msgtype": "m.audio",
                "body": "语音消息.ogg",
                "url": str(mxc),
                "info": {"mimetype": mime_type, "size": len(data)},
                "org.matrix.msc3245.voice": {},
            }
            resp = await self.client.send_message_event(room_id, EventType.ROOM_MESSAGE, msg_content)
            logger.debug("Sent voice to %s: %s", room_id, audio_path)
            return resp
        except Exception as e:
            logger.error("send_voice failed: %s", e)
            return None

    async def send_sticker(self, room_id: RoomID, filepath: str) -> Optional[str]:
        """发送 sticker (表情包)。"""
        if not self._ensure_connected():
            return None
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            mime_type, _ = mimetypes.guess_type(filepath)
            mime_type = mime_type or "image/png"
            mxc = await self.client.upload_media(data, mime_type=mime_type, filename=os.path.basename(filepath))
            info = ImageInfo(mimetype=mime_type, size=len(data))
            return await self.client.send_sticker(room_id, url=mxc, info=info)
        except Exception as e:
            logger.error("send_sticker failed: %s", e)
            return None

    # ── 工具 ─────────────────────────────────────────────────

    @property
    def user_id(self) -> Optional[str]:
        if self.client:
            return self.client.mxid
        return None

    def _ensure_connected(self) -> bool:
        if not self._logged_in or not self.client:
            logger.warning("Not connected to Matrix")
            return False
        return True
