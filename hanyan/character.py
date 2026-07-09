"""
Hanyan Chat — 角色系统
从 prompts/ 目录加载 .md 文件作为角色提示词，支持多角色切换与按用户分配角色。
兼容 KouriChat 风格的无 frontmatter 纯 Markdown 角色文件，也支持可选的
`---name/description---` YAML frontmatter。
"""

import logging
import os
import threading
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.character")


class Character:
    """单个角色的定义。"""

    def __init__(self, name: str, prompt: str, description: str = ""):
        self.name = name
        self.prompt = prompt
        self.description = description or name

    def system_message(self) -> dict:
        """返回用于 LLM 的 system message。"""
        return {"role": "system", "content": self.prompt}


class CharacterManager:
    """角色管理器 — 加载、切换角色。线程安全（WebUI 编辑和主循环可能并发访问）。"""

    def __init__(self):
        self._prompts_dir = config.get("character.prompts_dir", "prompts")
        self._characters: dict[str, Character] = {}
        self._current: Optional[Character] = None
        self._lock = threading.RLock()
        self._load_all()

    # ── 加载 ─────────────────────────────────────────────────

    def _load_all(self):
        """扫描 prompts 目录加载所有 .md 文件。"""
        with self._lock:
            self._characters.clear()
            if not os.path.isdir(self._prompts_dir):
                logger.warning("Prompts dir not found: %s", self._prompts_dir)
                return

            for filename in sorted(os.listdir(self._prompts_dir)):
                if not filename.endswith(".md") and not filename.endswith(".txt"):
                    continue
                path = os.path.join(self._prompts_dir, filename)
                name = os.path.splitext(filename)[0]
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    # 解析 YAML frontmatter（如果有），否则整份文件内容就是提示词
                    # （兼容 KouriChat 的纯 Markdown 角色文件，没有 frontmatter）。
                    description = ""
                    prompt_text = content
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            frontmatter = parts[1].strip()
                            prompt_text = parts[2].strip()
                            for line in frontmatter.split("\n"):
                                if line.startswith("description:"):
                                    description = line.split(":", 1)[1].strip()
                                elif line.startswith("name:"):
                                    name = line.split(":", 1)[1].strip()

                    character = Character(name=name, prompt=prompt_text, description=description)
                    self._characters[name] = character
                    logger.info("Loaded character: %s", name)
                except OSError as e:
                    logger.error("Failed to load character %s: %s", filename, e)

            # 设置默认角色
            default_name = config.get("character.default_character", "角色1")
            self._current = self._characters.get(default_name)
            if self._current is None and self._characters:
                self._current = next(iter(self._characters.values()))

    def reload(self):
        """热加载角色文件。"""
        self._load_all()

    # ── 切换 ─────────────────────────────────────────────────

    def switch(self, name: str) -> bool:
        """切换全局默认角色。返回是否成功。"""
        name = self._normalize_name(name)
        with self._lock:
            if name in self._characters:
                self._current = self._characters[name]
                logger.info("Switched to character: %s", name)
                return True
        logger.warning("Character not found: %s", name)
        return False

    @property
    def current(self) -> Optional[Character]:
        with self._lock:
            return self._current

    @property
    def current_name(self) -> str:
        with self._lock:
            return self._current.name if self._current else "unknown"

    def list_characters(self) -> list[dict]:
        """列出所有可用角色。"""
        with self._lock:
            current_name = self._current.name if self._current else None
            return [
                {
                    "name": ch.name,
                    "description": ch.description,
                    "is_current": (ch.name == current_name),
                }
                for ch in self._characters.values()
            ]

    def get(self, name: str) -> Optional[Character]:
        name = self._normalize_name(name)
        with self._lock:
            return self._characters.get(name)

    def get_for_user(self, user_id: str) -> Optional[Character]:
        """按用户解析角色（多用户独立角色分配）：
        依次尝试 user_map 中为该用户配置的角色 → 全局当前角色 → 任意已加载角色。"""
        name = config.get_character_for_user(user_id)
        char = self.get(name) if name else None
        if char is not None:
            return char
        with self._lock:
            if self._current is not None:
                return self._current
            if self._characters:
                return next(iter(self._characters.values()))
        return None

    @staticmethod
    def _normalize_name(name: str) -> str:
        """标准化角色名（去除路径、扩展名）。"""
        name = os.path.basename(name)
        name = os.path.splitext(name)[0]
        return name


# 全局单例
_manager: Optional[CharacterManager] = None


def get_manager() -> CharacterManager:
    """获取全局角色管理器。"""
    global _manager
    if _manager is None:
        _manager = CharacterManager()
    return _manager


def reload_characters():
    """热加载所有角色。"""
    get_manager().reload()
