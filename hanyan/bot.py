"""
Hanyan Chat — 主逻辑（KouriChat on Matrix，本地 LLM + TTS + STT）
====================================================================
把 config / character / matrix_client / llm_client / tts_client / stt_client /
session / memory / emotion / messaging / links / reminders / commands 这些
子模块编排起来的顶层 Bot 类。本文件只负责"流程编排"，具体逻辑都下沉到对应
子模块里，方便单独阅读/测试/替换（比如以后想换 STT 引擎，只用改 stt_client.py，
这里的编排逻辑完全不用动）。
"""

import asyncio
import fcntl
import json
import logging
import logging.handlers
import os
import random
import re
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime
from typing import Optional

from . import agent, commands, config, emotion, evolution, links, llm_client, memory, messaging, stt_client, tools, tts_client
from .character import get_manager as get_character_manager
from .matrix_client import MatrixClient
from .reminders import ReminderSystem
from .session import Session, SessionManager

logger = logging.getLogger("hanyan.bot")

DATA_DIR = os.path.join(config.ROOT_DIR, "data")
LOG_FILE = os.path.join(DATA_DIR, "hanyan.log")


def setup_logging():
    """配置日志：同时输出到文件和控制台。
    文件用 RotatingFileHandler（单文件 10MB × 3 备份）——之前用普通 FileHandler
    且 root 是 DEBUG 级，mautrix 的每个 HTTP 请求都写两行日志，几天就把
    hanyan.log 撑到上百 MB。同时把 mau.http 压到 WARNING，HTTP 流水日志
    对排查业务问题没用，真出网络错误 WARNING 以上照样能看到。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)

    logging.getLogger("mau.http").setLevel(logging.WARNING)
    logging.getLogger("mau").setLevel(logging.INFO)


_REMINDER_KEYWORDS = ["提醒", "提醒我", "分钟后", "小时后", "定时", "每天", "每日", "叫我", "叫我起床"]


class _RouterAsChat:
    """把 LLMRouter 适配成 LLMClient.chat 的签名（固定 purpose），
    给 evolution.reflect / memory 摘要这类只认 .chat() 接口的模块用。"""

    def __init__(self, router, purpose: str):
        self._router = router
        self._purpose = purpose

    def chat(self, messages, temperature=None, **kwargs):
        return self._router.chat(messages, temperature=temperature, purpose=self._purpose)


class HanyanBot:
    """Hanyan Chat 主程序。"""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.matrix = MatrixClient()
        self.llm = llm_client.get_client()
        self.router = llm_client.get_router()  # 本地+云端双模型路由（云端未配置时等价单模型）
        self.tts = tts_client.get_client()
        self.stt = stt_client.get_client()
        self.char_mgr = get_character_manager()
        self.session_manager = SessionManager()
        self.reminder = ReminderSystem(
            send_callback=self._send_reminder,
            get_room_id=lambda uid: (self.session_manager.get(uid).room_id if self.session_manager.get(uid) else None),
        )
        self._shutdown_event: Optional[asyncio.Event] = None  # 事件循环已运行时才创建

        self._auto_message_enabled = True
        self._auto_message_thread: Optional[threading.Thread] = None
        self._auto_message_running = False
        # 主动消息限频：房间维度记录上次发送时间（两个 session 指向同一房间时
        # 不能各发一条）；用户维度记录当天已发条数（date_str, count）。
        self._proactive_last_room: dict[str, float] = {}
        self._proactive_daily: dict[str, tuple[str, int]] = {}

        self._memory_manager_thread: Optional[threading.Thread] = None
        self._memory_manager_running = False

        self._emoji_enabled = True
        self._emoji_probability = 40  # %

        # 任务执行层：多步任务的规划/执行/验证。send 回调把消息从后台线程
        # 丢回主事件循环发出去；start_task 工具由这里注册进 tools。
        self.task_runner = agent.TaskRunner(self.router, self._send_text_from_thread)
        tools.set_task_starter(self._start_task_from_tool)

        self._running = False

    # ── 给 commands.py 用的接口 ──────────────────────────────────

    def schedule_restart(self):
        """1 秒后重启进程（os.execv 原地替换进程镜像，适用于 POSIX；
        失败就退出让外部 supervisor/systemd 接管重启）。"""
        def _do():
            time.sleep(1)
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except OSError as e:
                logger.error("Restart via execv failed (%s); exiting instead", e)
                os._exit(1)
        threading.Thread(target=_do, daemon=True).start()

    def set_auto_message_enabled(self, enabled: bool):
        self._auto_message_enabled = enabled

    def _send_text_from_thread(self, room_id: str, text: str):
        """线程安全的发消息（任务执行器等后台线程用）。"""
        if not self._loop:
            logger.warning("Cannot send from thread: event loop not ready")
            return
        fut = asyncio.run_coroutine_threadsafe(self.matrix.send_text(room_id, text), self._loop)
        fut.result(timeout=30)

    def _start_task_from_tool(self, user_id: str, goal: str) -> str:
        """start_task 工具入口：从 session 找到用户所在房间后启动任务。"""
        session = self.session_manager.get(user_id)
        if not session or session.room_id == "webui":
            return "（找不到你的聊天房间，暂时没法后台执行任务）"
        return self.task_runner.start(user_id, session.room_id, session.character_name, goal)

    # ── STT：语音转文字回调（跑在线程池里，同步函数）──────────────

    def _transcribe_voice(self, audio_bytes: bytes, mime_type: str) -> Optional[str]:
        ext = ".ogg"
        if "wav" in mime_type:
            ext = ".wav"
        elif "mp3" in mime_type:
            ext = ".mp3"
        elif "m4a" in mime_type or "mp4" in mime_type:
            ext = ".m4a"
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(audio_bytes)
                tmp_path = f.name
            return self.stt.transcribe(tmp_path)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ── 消息处理 ─────────────────────────────────────────────────

    async def _on_message(self, room_id: str, sender: str, text: str, event, was_voice: bool = False):
        """Matrix 消息回调 — 处理用户消息（文字消息 + 已经过 STT 转写的语音消息）。"""
        logger.info("Message from %s in %s: %.60s", sender, room_id, text)
        # 立刻更新 last_active：LLM 生成可能要几十秒，如果等 add_message 才更新，
        # 主动消息线程可能在生成期间误判"用户很久没说话"插进来一条。
        session = self.session_manager.get_or_create(sender, room_id)
        session.last_active = time.time()
        character_name = config.get_character_for_user(sender)
        session.character_name = character_name
        character = self.char_mgr.get(character_name) or self.char_mgr.current

        # 命令优先于一切
        if await commands.dispatch(self, session, room_id, sender, text):
            return

        memories = memory.load_memory(sender, character_name)
        core_memories = memory.load_core_memory(sender, character_name)

        messages = []
        if character:
            messages.append(character.system_message())
        core_block = memory.format_core_memory_for_prompt(core_memories)
        if core_block:
            messages.append({"role": "system", "content": core_block})
        # 动态上下文：当前时间 + 成长档案 + 近期兴趣（自我进化的注入点）
        dyn_block = evolution.build_context_block(sender, character_name)
        if dyn_block:
            messages.append({"role": "system", "content": dyn_block})
        if config.get("tools.enabled", True):
            messages.append({"role": "system", "content": tools.TOOL_SPEC})

        max_history = 20
        if memories:
            messages.extend(memories[-max_history:])

        # 链接内容提取：只影响送去给 LLM 的这一条消息内容，原始文本仍用于记忆/会话
        llm_text = await asyncio.get_event_loop().run_in_executor(
            None, links.build_message_with_link_context, text
        )
        messages.append({"role": "user", "content": llm_text})
        session.add_message("user", text)

        is_reminder = any(kw in text for kw in _REMINDER_KEYWORDS)
        if is_reminder and await self._try_handle_reminder(session, text):
            return

        try:
            reply, tool_images = await self._chat_with_tools(messages, sender, character_name)
        except Exception as e:
            logger.error("LLM chat error for %s: %s", sender, e, exc_info=True)
            reply, tool_images = "[嗯，我现在有点累，稍后再聊好吗？]", []

        reply = tools.strip_tool_calls(reply) or reply
        if not reply:
            reply = "[嗯，我现在有点累，稍后再聊好吗？]"

        session.add_message("assistant", reply)
        memory.append_memory(sender, character_name, text, reply)

        actions = messaging.split_reply(reply)
        if not actions:
            actions = [("text", reply)]
        logger.debug("LLM reply: %d chars -> %d action(s)", len(reply), len(actions))

        emoji_path = None
        if self._emoji_enabled and random.randint(0, 100) < self._emoji_probability:
            detected = emotion.detect_emotion(reply)
            if detected:
                emoji_path = emotion.pick_emoji(detected)

        await self._send_actions(room_id, actions, was_voice)

        # 工具下载的图片（表情包/图片）随回复发出
        for img in tool_images:
            try:
                await self.matrix.send_image(room_id, img)
            except Exception as e:
                logger.warning("Failed to send tool image: %s", e)

        if emoji_path:
            try:
                await self.matrix.send_image(room_id, emoji_path)
            except Exception as e:
                logger.warning("Failed to send emoji: %s", e)

    async def _chat_with_tools(self, messages: list[dict], sender: str, character_name: str) -> tuple[str, list[str]]:
        """带工具调用循环的 LLM 对话（实际逻辑在 tools.chat_loop，和 WebUI 聊天共用）。"""
        return await asyncio.get_event_loop().run_in_executor(
            None, tools.chat_loop, self.router, messages, sender, character_name
        )

    async def _send_actions(self, room_id: str, actions: list[tuple[str, str]], was_voice: bool = False):
        """把 split_reply() 输出的动作序列发送出去（文字/拍一拍/撤回）。
        语音输入 → 每一条文字动作都合成语音发出去（而不仅仅是第一条——早期版本
        只给第一条文字合成语音，`\\` 拆出来的第二、三句会被直接丢弃，用户发语音
        问问题、bot 回复被拆成好几句时会莫名其妙"漏话"）。
        文字输入 → 只发文字，第一条文字额外触发一次 TTS 语音合成（图文并茂但不用
        每句话都等语音合成，兼顾体验和延迟）。"""
        first_text_tts_done = False
        for i, (action_type, content) in enumerate(actions):
            if action_type == "tickle":
                await self.matrix.send_tickle(room_id)
                await asyncio.sleep(random.uniform(1.0, 2.0))
            elif action_type == "tickle_self":
                await self.matrix.send_tickle_self(room_id)
                await asyncio.sleep(random.uniform(1.0, 2.0))
            elif action_type == "recall":
                await self.matrix.redact_last_sent(room_id)
                await asyncio.sleep(random.uniform(1.0, 2.0))
            elif action_type == "text" and content:
                if was_voice:
                    # 语音输入 → 语音回复：这一条也合成语音发出去，不发文字
                    if len(content) > 2:
                        try:
                            audio_path = await asyncio.get_event_loop().run_in_executor(
                                None, self.tts.synthesize, content
                            )
                            if audio_path:
                                await self.matrix.send_voice(room_id, audio_path)
                            else:
                                # TTS 失败时兜底发文字，总不能语音输入却完全没回复
                                await self.matrix.send_text(room_id, content)
                        except Exception as e:
                            logger.debug("TTS skipped, falling back to text: %s", e)
                            await self.matrix.send_text(room_id, content)
                    else:
                        await self.matrix.send_text(room_id, content)
                if not was_voice:
                    # 文字输入 → 文字回复，第一条顺带合成一次语音
                    logger.debug("SENDING action %d/%d: %d chars", i + 1, len(actions), len(content))
                    await self.matrix.send_text(room_id, content)
                    if not first_text_tts_done and len(content) > 2:
                        first_text_tts_done = True
                        try:
                            audio_path = await asyncio.get_event_loop().run_in_executor(
                                None, self.tts.synthesize, content
                            )
                            if audio_path:
                                await self.matrix.send_voice(room_id, audio_path)
                        except Exception as e:
                            logger.debug("TTS skipped: %s", e)
                await asyncio.sleep(messaging.typing_delay(content))

    async def _try_handle_reminder(self, session: Session, text: str) -> bool:
        """尝试解析并设置提醒。使用 LLM 分类提醒请求。返回 True 表示已处理。"""
        now = datetime.now()
        prompt = f"""请分析用户的提醒或定时请求。
当前时间是: {now.strftime("%Y-%m-%d %A %H:%M:%S")}.
用户的请求是: "{text}"

请判断这个请求属于以下哪种类型，并计算相关时间：
A) **重复性每日提醒**：例如 "每天早上8点叫我起床", "提醒我每天晚上10点睡觉"。
B) **一次性提醒 (延迟 > 10分钟)**：例如 "1小时后提醒我", "今天下午3点开会", "明天早上叫我"。
C) **一次性提醒 (延迟 <= 10分钟)**：例如 "5分钟后提醒我"。
D) **非提醒请求**：例如 "今天天气怎么样?", "取消提醒"。

请严格按照以下格式返回 JSON 对象，不要添加任何其他文字：
- A: {{"type": "recurring", "time_str": "HH:MM", "message": "提醒内容"}}
- B: {{"type": "one-off", "target_datetime_str": "YYYY-MM-DD HH:MM", "message": "提醒内容"}}
- C: {{"type": "short", "delay_seconds": 300, "message": "提醒内容"}}
- D: null"""

        try:
            raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.llm.chat(
                    [{"role": "system", "content": "你是一个提醒解析助手。只返回 JSON。"},
                     {"role": "user", "content": prompt}],
                    temperature=0.1,
                ),
            )
        except Exception:
            return False

        if not raw or "null" in raw.strip().lower():
            return False

        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            return False
        try:
            data = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            return False

        reminder_type = data.get("type")
        msg = data.get("message", "").strip()
        if not msg:
            return False

        confirm_text: Optional[str] = None

        if reminder_type == "short":
            delay = int(data.get("delay_seconds", 300))
            if delay <= 0:
                delay = 60
            self.reminder.set_short_reminder(session.user_id, session.room_id, delay, msg)
            confirm_text = f"好嘞，{delay}秒后提醒你：{msg}"

        elif reminder_type == "one-off":
            target_str = data.get("target_datetime_str", "")
            try:
                target_dt = datetime.strptime(target_str, "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                return False
            self.reminder.set_long_reminder(session.user_id, session.room_id, target_dt, msg)
            confirm_text = f"好嘞！我会在 {target_dt.strftime('%Y年%m月%d日 %H:%M')} 提醒你：{msg}"

        elif reminder_type == "recurring":
            time_str = data.get("time_str", "")
            try:
                datetime.strptime(time_str, "%H:%M")
            except (ValueError, TypeError):
                return False
            self.reminder.set_recurring_reminder(session.user_id, session.room_id, time_str, msg)
            confirm_text = f"好嘞！我每天 {time_str} 都会提醒你：{msg}"

        if confirm_text is None:
            return False

        await self.matrix.send_text(session.room_id, confirm_text)
        # session/记忆也要记一笔 bot 的确认回复，不然下一轮对话 bot"忘了"自己刚答应过这件事。
        session.add_message("assistant", confirm_text)
        memory.append_memory(session.user_id, session.character_name, text, confirm_text)
        return True

    # ── 主动消息 ─────────────────────────────────────────────────

    def _auto_message_loop(self):
        """后台线程：检查用户超时，发送主动消息；同时顺带回收空闲 session。"""
        if hasattr(self, '_auto_message_started') and self._auto_message_started:
            return
        self._auto_message_started = True
        self._auto_message_running = True
        interval_minutes = config.get("proactive.interval_minutes", 30)
        quiet_start_str = config.get("proactive.quiet_start", "23:00")
        quiet_end_str = config.get("proactive.quiet_end", "07:00")
        last_evict = time.time()

        while self._auto_message_running:
            try:
                if time.time() - last_evict >= 3600:
                    self.session_manager.evict_idle()
                    tools.cleanup_downloads(config.get("tools.download_max_age_days", 7))
                    last_evict = time.time()

                if not self._auto_message_enabled:
                    time.sleep(10)
                    continue
                if self._is_quiet_time(quiet_start_str, quiet_end_str):
                    time.sleep(60)
                    continue

                now = time.time()
                timeout = interval_minutes * 60
                max_per_day = config.get("proactive.max_per_day", 24)
                today = datetime.now().strftime("%Y-%m-%d")
                for session in self.session_manager.all_sessions():
                    if now - session.last_active >= timeout:
                        # 跳过机器人账号的 session，避免给 @hermes 也发主动消息
                        if session.user_id.startswith("@hermes:") or session.user_id.startswith("@serena:"):
                            continue
                        # 每用户每天上限：主动消息是"想你了"不是"轰炸"，之前没有
                        # 上限时一天能给同一个人发几十条。
                        day, count = self._proactive_daily.get(session.user_id, (today, 0))
                        if day == today and count >= max_per_day:
                            continue
                        # 同一房间限频：多个 session 可能落在同一个房间（比如房间里
                        # 还有别的 bot 也建了 session），不能各自触发各发一条。
                        last_room_ts = self._proactive_last_room.get(session.room_id, 0)
                        if now - last_room_ts < timeout:
                            continue
                        logger.info("Proactive message for %s (idle %.0fs)", session.user_id, now - session.last_active)
                        try:
                            session.last_active = time.time()  # 先更新时间戳，避免 LLM 生成期间再次触发
                            self._proactive_last_room[session.room_id] = time.time()
                            self._proactive_daily[session.user_id] = (today, count + 1 if day == today else 1)
                            self._send_proactive_message(session)
                        except Exception as e:
                            logger.error("Proactive message error for %s: %s", session.user_id, e, exc_info=True)
                time.sleep(30)
            except Exception as e:
                logger.error("Auto-message loop error: %s", e, exc_info=True)
                time.sleep(30)

    def _is_quiet_time(self, start_str: str, end_str: str) -> bool:
        try:
            sh, sm = (int(x) for x in start_str.split(":"))
            eh, em = (int(x) for x in end_str.split(":"))
            start_min, end_min = sh * 60 + sm, eh * 60 + em
        except (ValueError, IndexError):
            return False
        now = datetime.now()
        current_min = now.hour * 60 + now.minute
        if start_min <= end_min:
            return start_min <= current_min <= end_min
        return current_min >= start_min or current_min <= end_min

    def _send_proactive_message(self, session: Session):
        """向用户发送一条主动消息（在后台线程里同步执行，用 run_coroutine_threadsafe
        把实际发送丢回主事件循环）。"""
        character = self.char_mgr.get(session.character_name) or self.char_mgr.current
        chat_messages = []
        if character:
            chat_messages.append(character.system_message())
        # 主动消息也带上时间/成长档案/兴趣——她可以聊"最近在研究的东西"
        dyn_block = evolution.build_context_block(session.user_id, session.character_name)
        if dyn_block:
            chat_messages.append({"role": "system", "content": dyn_block})
        chat_messages.extend(session.messages[-4:])

        prompt_text = random.choice([
            "（用户一阵子没说话了。自然地发一条过去，语气参照你们的聊天氛围。）",
            "（用户好像消失了。发条消息试试，可以撒娇也可以关心。）",
            "（主动找用户聊聊。说说你在干嘛，或者问问ta在干嘛。）",
            "（半天没动静了。发个消息过去，语气轻松自然一点。）",
        ])
        chat_messages.append({"role": "user", "content": prompt_text})

        try:
            reply = self.llm.chat(chat_messages, temperature=0.8)
        except Exception as e:
            logger.error("Proactive chat error: %s", e)
            return
        if not reply:
            return

        session.add_message("assistant", reply)
        memory.append_memory(session.user_id, session.character_name, prompt_text, reply)

        actions = messaging.split_reply(reply)
        if not actions:
            actions = [("text", reply)]

        def _send_async(coro):
            if self._loop:
                fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
                fut.result(timeout=30)

        try:
            for action_type, content in actions:
                if action_type == "tickle":
                    _send_async(self.matrix.send_tickle(session.room_id))
                elif action_type == "tickle_self":
                    _send_async(self.matrix.send_tickle_self(session.room_id))
                elif action_type == "recall":
                    _send_async(self.matrix.redact_last_sent(session.room_id))
                elif action_type == "text" and content:
                    _send_async(self.matrix.send_text(session.room_id, content))
                time.sleep(random.uniform(0.3, 0.8))

            if self._emoji_enabled and random.randint(0, 100) < self._emoji_probability:
                detected = emotion.detect_emotion(reply)
                if detected:
                    emoji_path = emotion.pick_emoji(detected)
                    if emoji_path:
                        _send_async(self.matrix.send_image(session.room_id, emoji_path))
        except Exception as e:
            logger.error("Proactive send error: %s", e)

    # ── 记忆管理后台线程 ─────────────────────────────────────────

    def _memory_manager_loop(self):
        """周期性检查各用户滚动记忆是否达到晋升阈值，达到就摘要成核心记忆；
        同时负责每日自我反思（成长档案 + 兴趣演化）的调度。"""
        self._memory_manager_running = True
        interval = 60
        while self._memory_manager_running:
            try:
                if config.get("memory.use_llm_summary", True):
                    threshold = config.get("memory.promote_threshold", 30)
                    for user_id in self.session_manager.all_user_ids():
                        session = self.session_manager.get(user_id)
                        if not session:
                            continue
                        try:
                            memories = memory.load_memory(user_id, session.character_name)
                            if len(memories) >= threshold * 2:
                                memory.summarize_dynamic_memory(self.llm, user_id, session.character_name)
                        except Exception as e:
                            logger.error("Memory promotion failed for %s/%s: %s", user_id, session.character_name, e, exc_info=True)

                # 每日自我反思：过了 reflect_hour（默认凌晨4点）且今天还没反思过就做一次
                reflect_hour = int(config.get("evolution.reflect_hour", 4))
                if datetime.now().hour >= reflect_hour:
                    for session in self.session_manager.all_sessions():
                        if session.user_id.startswith(("@hermes:", "@serena:")):
                            continue
                        try:
                            if evolution.should_reflect(session.user_id, session.character_name):
                                logger.info("[CKPT:evo_reflect_start] %s/%s", session.user_id, session.character_name)
                                evolution.reflect(
                                    _RouterAsChat(self.router, "reflection"),
                                    session.user_id, session.character_name,
                                )
                        except Exception as e:
                            logger.error("[CKPT:evo_reflect_error] %s: %s", session.user_id, e, exc_info=True)
                time.sleep(interval)
            except Exception as e:
                logger.error("Memory manager loop error: %s", e, exc_info=True)
                time.sleep(interval)

    # ── 发送提醒消息 ─────────────────────────────────────────────

    def _send_reminder(self, room_id: str, user_id: str, message: str):
        if self._loop:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self.matrix.send_text(room_id, f"🔔 提醒：{message}"), self._loop,
                )
                fut.result(timeout=30)
                logger.info("Reminder sent to %s: %s", user_id, message)
            except Exception as e:
                logger.error("Failed to send reminder to %s: %s", user_id, e)

    # ── 启动 / 停止 ─────────────────────────────────────────────

    async def start(self):
        """启动 Bot。"""
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()
        logger.info("=" * 50)
        logger.info("Hanyan Chat starting...")
        logger.info("=" * 50)

        await self.matrix.connect()
        self.matrix.on_message(self._on_message)
        self.matrix.set_stt_callback(self._transcribe_voice)
        logger.info("Message handler + STT callback registered")

        for d in [DATA_DIR, config.get("memory.storage_dir"), os.path.join(config.ROOT_DIR, "emojis")]:
            if d:
                os.makedirs(d, exist_ok=True)

        self.reminder.start()

        self._auto_message_thread = threading.Thread(target=self._auto_message_loop, name="AutoMessage", daemon=True)
        self._auto_message_thread.start()
        logger.info("Auto-message checker started")

        self._memory_manager_thread = threading.Thread(target=self._memory_manager_loop, name="MemoryManager", daemon=True)
        self._memory_manager_thread.start()
        logger.info("Memory manager started")

        # 内嵌 WebUI（webui.enabled=true 时随 bot 一起启动，共用 session/记忆/路由器，
        # 网页聊天和 Matrix 聊天是同一段对话）
        if config.get("webui.enabled", False):
            from . import webui
            threading.Thread(target=webui.run_embedded, args=(self,), name="WebUI", daemon=True).start()
            logger.info(
                "Embedded WebUI starting on http://%s:%s",
                config.get("webui.host", "127.0.0.1"), config.get("webui.port", 5001),
            )

        self._running = True
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._shutdown(s)))
            except NotImplementedError:
                pass  # Windows 不支持 add_signal_handler

        logger.info("Hanyan Chat is running. Press Ctrl+C to stop.")
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown(signal.SIGTERM)

    async def _shutdown(self, sig):
        """优雅关闭。"""
        if not self._running:
            return
        self._running = False
        logger.info("Shutting down (signal %s)...", sig)

        self._auto_message_running = False
        self._memory_manager_running = False
        self.reminder.stop()

        await self.matrix.disconnect()
        if self._shutdown_event:
            self._shutdown_event.set()
        logger.info("Shutdown complete.")


_INSTANCE_LOCK_FILE = None  # 模块级引用，防止被 GC 回收导致锁提前释放


def acquire_single_instance_lock() -> bool:
    """单实例锁：flock 排他锁保证同一时刻只有一个 bot 进程在跑。

    背景：实测出现过 3-4 个旧进程同时登录同一 Matrix 账号，用户发一句话，
    每个进程都独立回复一遍，表现为"一条消息回来一大串"。flock 是进程存活
    期间自动持有、进程死掉（哪怕 kill -9）自动释放的，不会有 stale pidfile
    问题。schedule_restart 的 os.execv 会关掉这个 fd（Python fd 默认
    CLOEXEC），新进程镜像重新走 main() 重新拿锁，也是安全的。"""
    global _INSTANCE_LOCK_FILE
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_path = os.path.join(DATA_DIR, "hanyan.lock")
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    f.write(str(os.getpid()))
    f.flush()
    _INSTANCE_LOCK_FILE = f
    return True


def main():
    """启动 Hanyan Chat。"""
    setup_logging()
    logger.info("Initializing Hanyan Chat...")

    if not acquire_single_instance_lock():
        logger.error(
            "另一个 Hanyan Chat 实例已经在运行（data/hanyan.lock 被占用）。"
            "请先停掉旧进程：pkill -f 'Hanyan-Chat.*main.py'"
        )
        sys.exit(1)

    bot = HanyanBot()

    async def _run():
        try:
            await bot.start()
            if bot._shutdown_event:
                await bot._shutdown_event.wait()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.critical("Fatal error: %s", e, exc_info=True)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
