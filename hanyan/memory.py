"""
Hanyan Chat — 记忆系统：动态（滚动）记忆 + 核心（长期）记忆
========================================================
- 动态记忆：逐轮追加的滚动对话，达到阈值后由 LLM 摘要，晋升为一条核心记忆
- 核心记忆：{timestamp, summary, importance} 的 JSON 数组，按重要度/时间衰减淘汰

记忆文件按 (user_id, character_name) 联合键存储，与 KouriChat 的
"{user}_{character}_..." 命名方式对齐，保证同一用户切换角色后记忆互不串。
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.memory")

MEMORY_DIR = config.get("memory.storage_dir") or os.path.join(config.ROOT_DIR, "data", "memories")

# 记忆文件是 "读取全量 → 修改 → 整体写回" 的模式，没有锁的话，同一用户几乎同时
# 收到主循环消息 + 主动消息/后台摘要时可能互相覆盖丢一条记录。用一把全局锁
# 粗粒度地串行化所有记忆写操作——写频率低（每轮对话一次），代价可以忽略。
_write_lock = threading.Lock()


def _sanitize_key_part(value: str) -> str:
    """将任意字符串转为安全的文件名片段。"""
    safe = re.sub(r'[<>:"/\\|?*@!]', "_", value or "")
    safe = safe.strip(" .")
    return (safe or "unknown")[:80]


def _memory_key(user_id: str, character_name: str) -> str:
    return f"{_sanitize_key_part(user_id)}__{_sanitize_key_part(character_name)}"


def _memory_path(user_id: str, character_name: str) -> str:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return os.path.join(MEMORY_DIR, f"{_memory_key(user_id, character_name)}.json")


def _core_memory_path(user_id: str, character_name: str) -> str:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return os.path.join(MEMORY_DIR, f"{_memory_key(user_id, character_name)}_core.json")


def _atomic_write_json(path: str, data) -> bool:
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except OSError as e:
        logger.error("Failed to write %s: %s", path, e)
        return False


# ── 滚动（短期）记忆 ─────────────────────────────────────────────

def load_memory(user_id: str, character_name: str) -> list[dict]:
    path = _memory_path(user_id, character_name)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load memory for %s: %s", user_id, e)
    return []


def save_memory(user_id: str, character_name: str, memories: list[dict]):
    _atomic_write_json(_memory_path(user_id, character_name), memories)


def append_memory(user_id: str, character_name: str, user_msg: str, bot_reply: str):
    """追加一轮对话到滚动记忆，并裁剪到 max_entries。
    是否晋升为核心记忆由后台的 MemoryManager 线程周期性检查，不阻塞这里。"""
    with _write_lock:
        max_entries = config.get("memory.max_entries", 50)
        memories = load_memory(user_id, character_name)
        memories.append({"role": "user", "content": user_msg})
        memories.append({"role": "assistant", "content": bot_reply})
        if len(memories) > max_entries * 2:
            memories = memories[-max_entries * 2:]
        save_memory(user_id, character_name, memories)


# ── 核心（长期）记忆 ─────────────────────────────────────────────

def load_core_memory(user_id: str, character_name: str) -> list[dict]:
    """加载核心记忆：[{timestamp, summary, importance}, ...]"""
    path = _core_memory_path(user_id, character_name)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load core memory for %s: %s", user_id, e)
    return []


def save_core_memory(user_id: str, character_name: str, memories: list[dict]):
    _atomic_write_json(_core_memory_path(user_id, character_name), memories)


def add_core_memory_entry(user_id: str, character_name: str, summary: str, importance: int):
    """新增一条核心记忆，并立即做容量淘汰。"""
    importance = max(1, min(5, int(importance)))
    with _write_lock:
        memories = load_core_memory(user_id, character_name)
        memories.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
            "importance": importance,
        })
        save_core_memory(user_id, character_name, memories)
    cleanup_core_memory(user_id, character_name)


def cleanup_core_memory(user_id: str, character_name: str):
    """按重要度和时间衰减淘汰核心记忆，只保留 top memory.core_memory_max 条
    （重要度权重 0.6，时间衰减权重 0.4，衰减单位是"天"）。"""
    cap = config.get("memory.core_memory_max", 50)
    with _write_lock:
        memories = load_core_memory(user_id, character_name)
        if len(memories) <= cap:
            return
        now = datetime.now()

        def _score(entry: dict) -> float:
            try:
                ts = datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M:%S")
            except (KeyError, ValueError):
                ts = now
            age_days = max(0.0, (now - ts).total_seconds() / 86400)
            importance = entry.get("importance", 3)
            return 0.6 * importance - 0.4 * age_days

        memories.sort(key=_score, reverse=True)
        save_core_memory(user_id, character_name, memories[:cap])


def format_core_memory_for_prompt(memories: list[dict], limit: int = 8) -> str:
    """把核心记忆格式化为一段可以拼进 system prompt 的文字块。"""
    if not memories:
        return ""
    ranked = sorted(memories, key=lambda m: m.get("importance", 0), reverse=True)[:limit]
    lines = [f"- [{m.get('timestamp', '')}] {m.get('summary', '')}" for m in ranked if m.get("summary")]
    if not lines:
        return ""
    return "以下是你和这位用户之间一些重要的长期记忆，请自然地体现在对话里：\n" + "\n".join(lines)


def summarize_dynamic_memory(llm_client, user_id: str, character_name: str) -> bool:
    """用 LLM 把滚动记忆摘要成一条核心记忆条目，然后截断滚动记忆（保留最近几轮，
    避免摘要后对话完全断档）。返回是否成功晋升了一条核心记忆。

    llm_client: 任何有 .chat(messages, temperature=...) -> str 方法的对象
    （解耦自具体的 bot 实例，方便单独测试/复用）。"""
    memories = load_memory(user_id, character_name)
    if len(memories) < 4:
        return False

    convo_text = "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}" for m in memories)
    prompt = (
        "请阅读下面这段对话，用一到两句话总结其中值得长期记住的关键信息"
        "（例如用户的喜好、重要事件、约定、情绪状态等），并给这条记忆的重要性打分"
        "（1-5的整数，5表示非常重要）。严格只返回 JSON，不要添加任何其它文字：\n"
        '{"summary": "...", "importance": 3}\n\n'
        f"对话内容：\n{convo_text}"
    )
    try:
        raw = llm_client.chat(
            [
                {"role": "system", "content": "你是一个记忆摘要助手，只输出 JSON，不要有多余文字。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
    except Exception as e:
        logger.error("Memory summarization LLM call failed for %s/%s: %s", user_id, character_name, e)
        return False

    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        logger.warning("Memory summarization returned no JSON for %s/%s", user_id, character_name)
        return False
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return False

    summary = (data.get("summary") or "").strip()
    if not summary:
        return False
    try:
        importance = int(data.get("importance", 3))
    except (TypeError, ValueError):
        importance = 3

    add_core_memory_entry(user_id, character_name, summary, importance)

    with _write_lock:
        keep_tail = memories[-4:]
        save_memory(user_id, character_name, keep_tail)
    logger.info("Promoted memory to core for %s/%s: %s", user_id, character_name, summary)
    return True
