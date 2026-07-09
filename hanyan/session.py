"""
Hanyan Chat — Session 管理（多用户独立对话状态）
"""

import logging
import threading
import time
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.session")

# 单个 session 在内存里最多保留多少条消息（防止长期运行、从不清空导致无限增长）。
# 磁盘记忆另有 memory.max_entries 控制，这里只是内存态的安全上限。
SESSION_MAX_MESSAGES = 200
# session 空闲超过这么久（秒）就从内存里回收；不影响磁盘记忆，下次消息来了会重新创建。
SESSION_IDLE_EVICT_SECONDS = 48 * 3600


class Session:
    """单个用户的对话 session。"""

    def __init__(self, user_id: str, room_id: str, character_name: Optional[str] = None):
        self.user_id = user_id          # Matrix 用户 ID
        self.room_id = room_id          # DM 房间 ID
        self.character_name = character_name or config.get("character.default_character", "角色1")
        self.last_active = time.time()
        self._messages: list[dict] = []  # 聊天历史 [{role, content}, ...]
        self._lock = threading.Lock()    # 主循环线程 + AutoMessage 线程都会读写这个列表

    @property
    def messages(self) -> list[dict]:
        with self._lock:
            return list(self._messages)

    def add_message(self, role: str, content: str):
        with self._lock:
            self._messages.append({"role": role, "content": content})
            if len(self._messages) > SESSION_MAX_MESSAGES:
                self._messages = self._messages[-SESSION_MAX_MESSAGES:]
        self.last_active = time.time()

    def clear_history(self):
        with self._lock:
            self._messages.clear()


class SessionManager:
    """管理所有用户的 session。"""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get_or_create(self, user_id: str, room_id: str) -> Session:
        with self._lock:
            if user_id not in self._sessions:
                character_name = config.get_character_for_user(user_id)
                self._sessions[user_id] = Session(user_id, room_id, character_name)
            self._sessions[user_id].room_id = room_id
            return self._sessions[user_id]

    def get(self, user_id: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(user_id)

    def remove(self, user_id: str):
        with self._lock:
            self._sessions.pop(user_id, None)

    def all_sessions(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def all_user_ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def evict_idle(self, max_idle_seconds: float = SESSION_IDLE_EVICT_SECONDS) -> int:
        """回收长时间空闲的 session（只影响内存态，磁盘记忆不受影响）。返回回收数量。"""
        now = time.time()
        evicted = 0
        with self._lock:
            stale = [uid for uid, s in self._sessions.items() if now - s.last_active >= max_idle_seconds]
            for uid in stale:
                self._sessions.pop(uid, None)
                evicted += 1
        if evicted:
            logger.info("Evicted %d idle session(s)", evicted)
        return evicted
