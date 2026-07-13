"""
Hanyan Chat — 自我进化（成长档案 / 兴趣演化 / 检查点回溯）
==========================================================
安全边界：进化只发生在**数据层**（成长档案、兴趣、表情包库、提案），
永远不自动修改代码。想要的新功能她会写进 proposals.md 留给人审核。

三样东西，都存在 data/growth/<user>_<char>/ 下：
- profile.md    成长档案：她对用户的了解、相处模式、自己的性格变化。
                每天由 LLM 自我反思更新一次，每轮对话注入 system prompt，
                所以性格和了解会随时间真实演化。
- interests.json 兴趣清单 [{topic, weight}]：反思时提取，旧兴趣按 0.85 衰减、
                低于 0.2 淘汰——兴趣会自然转移，主动消息可以聊"最近在研究的东西"。
- proposals.md  功能提案：她用 github_search 搜到的项目自动归档在这里，
                反思时也可以补充"想要的能力"，由人来决定是否实施。

看门狗/回溯：所有对档案文件的写入都走 _checkpoint_write() ——
先备份 → 写临时文件 → 校验 → 原子替换；校验失败自动还原备份，
全过程打 [CKPT:evo_*] 日志（data/hanyan.log），出问题可按标记回查 + 手动
从 *.bak 恢复。
"""

import json
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Callable, Optional

from . import config, memory

logger = logging.getLogger("hanyan.evolution")

GROWTH_DIR = os.path.join(config.ROOT_DIR, "data", "growth")

_INTEREST_DECAY = 0.85
_INTEREST_DROP = 0.2
_INTEREST_MAX = 12
_PROFILE_MAX_BYTES = 100_000


def _key_dir(user_id: str, character_name: str) -> str:
    safe = re.sub(r"[^\w一-鿿.-]", "_", f"{user_id}_{character_name}")
    d = os.path.join(GROWTH_DIR, safe)
    os.makedirs(d, exist_ok=True)
    return d


# ── 检查点写入（备份→写入→校验→失败回滚）──────────────────────────

def _checkpoint_write(path: str, content: str, validator: Optional[Callable[[str], bool]] = None) -> bool:
    """带回溯的安全写入。返回是否成功。失败时文件保持原样。"""
    def _default_valid(c: str) -> bool:
        return bool(c.strip()) and len(c.encode("utf-8")) < _PROFILE_MAX_BYTES

    valid = validator or _default_valid
    if not valid(content):
        logger.warning("[CKPT:evo_reject] validation failed for %s, keeping old version", path)
        return False

    backup = path + ".bak"
    try:
        if os.path.exists(path):
            shutil.copy2(path, backup)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        # 复核落盘内容
        with open(tmp, encoding="utf-8") as f:
            if not valid(f.read()):
                raise ValueError("post-write validation failed")
        os.replace(tmp, path)
        logger.info("[CKPT:evo_write] %s updated (%d bytes)", os.path.basename(path), len(content.encode("utf-8")))
        return True
    except Exception as e:
        logger.error("[CKPT:evo_rollback] write %s failed (%s), restoring backup", path, e)
        try:
            if os.path.exists(backup):
                shutil.copy2(backup, path)
        except OSError as re_err:
            logger.error("[CKPT:evo_rollback_fail] %s", re_err)
        return False


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# ── 对外接口：prompt 注入 ─────────────────────────────────────────

def build_context_block(user_id: str, character_name: str) -> str:
    """每轮对话注入 system prompt 的动态上下文：当前时间 + 成长档案 + 近期兴趣。"""
    d = _key_dir(user_id, character_name)
    now = datetime.now()
    weekdays = "一二三四五六日"
    parts = [f"【当前时间】{now.strftime(f'%Y年%m月%d日 星期{weekdays[now.weekday()]} %H:%M')}"]

    profile = _read(os.path.join(d, "profile.md")).strip()
    if profile:
        parts.append(f"【你的成长档案（你自己写的，据此保持性格连贯）】\n{profile[:1500]}")

    interests = load_interests(user_id, character_name)
    if interests:
        top = "、".join(i["topic"] for i in interests[:4])
        parts.append(f"【你最近感兴趣的话题】{top}（主动聊天时可以自然提起）")
    return "\n\n".join(parts)


def load_interests(user_id: str, character_name: str) -> list[dict]:
    d = _key_dir(user_id, character_name)
    try:
        data = json.loads(_read(os.path.join(d, "interests.json")) or "[]")
        return sorted(
            [i for i in data if isinstance(i, dict) and i.get("topic")],
            key=lambda x: -float(x.get("weight", 0)),
        )
    except (json.JSONDecodeError, TypeError):
        logger.warning("[CKPT:evo_interests_corrupt] resetting interests for %s", user_id)
        return []


# ── 提案归档（github_search 的结果自动收集）──────────────────────

def append_proposal(user_id: str, character_name: str, source: str, content: str):
    d = _key_dir(user_id, character_name)
    path = os.path.join(d, "proposals.md")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## {stamp} · {source}\n\n{content.strip()[:2000]}\n"
    old = _read(path)
    # 提案文件只增不删，超过 50KB 时裁掉最旧的一半
    merged = old + entry
    if len(merged.encode("utf-8")) > 50_000:
        merged = merged[len(merged) // 2:]
    _checkpoint_write(path, merged)


def read_proposals(user_id: str, character_name: str) -> str:
    return _read(os.path.join(_key_dir(user_id, character_name), "proposals.md"))


# ── 自我反思（每日一次，LLM 更新成长档案 + 兴趣）────────────────

_REFLECT_PROMPT = """你是{char}。下面是你和 {user} 最近的聊天记录摘录，以及你之前写的成长档案。
请你以第一人称重写成长档案（这是写给你自己看的备忘，不是给用户看的），要求：
1. markdown 格式，500 字以内，包含三节：# 关于他 / # 我们的相处 / # 我的变化
2. 保留旧档案里仍然成立的认知，融入新的观察；矛盾之处以新的为准
3. 最后单独输出一行兴趣清单（从聊天里提取你和他都感兴趣的话题，1-5 个）：
INTERESTS: ["话题1", "话题2"]

【旧档案】
{old_profile}

【最近聊天】
{recent}
"""


def reflect(llm, user_id: str, character_name: str) -> bool:
    """执行一次自我反思：更新 profile.md 和 interests.json。返回是否成功。
    llm 需要有 .chat(messages, temperature=...) 接口（LLMClient 或 Router 均可）。"""
    d = _key_dir(user_id, character_name)
    memories = memory.load_memory(user_id, character_name)
    if len(memories) < 10:
        logger.info("[CKPT:evo_skip] not enough messages for reflection (%d)", len(memories))
        return False

    recent = "\n".join(
        f"{'他' if m.get('role') == 'user' else '我'}: {m.get('content', '')[:80]}"
        for m in memories[-40:]
    )
    old_profile = _read(os.path.join(d, "profile.md")) or "（还没有档案，这是第一次反思）"
    prompt = _REFLECT_PROMPT.format(
        char=character_name, user=user_id.split(":")[0].lstrip("@"),
        old_profile=old_profile[:1500], recent=recent[:3000],
    )
    raw = llm.chat(
        [{"role": "system", "content": "你在做每日自我反思，认真、诚实、简洁。"},
         {"role": "user", "content": prompt}],
        temperature=0.5,
    )
    if not raw or raw.startswith("["):  # LLM 降级文案以 [ 开头
        logger.warning("[CKPT:evo_reflect_fail] LLM unavailable")
        return False

    # 拆出兴趣行
    new_topics: list[str] = []
    m = re.search(r"INTERESTS:\s*(\[.*?\])", raw, re.DOTALL)
    if m:
        try:
            new_topics = [str(t)[:30] for t in json.loads(m.group(1)) if str(t).strip()][:5]
        except json.JSONDecodeError:
            pass
        raw = raw[: m.start()].strip()

    ok = _checkpoint_write(os.path.join(d, "profile.md"), raw.strip())

    # 兴趣演化：旧的衰减，新的注入
    old = {i["topic"]: float(i.get("weight", 0)) for i in load_interests(user_id, character_name)}
    merged = {t: w * _INTEREST_DECAY for t, w in old.items()}
    for t in new_topics:
        merged[t] = min(1.0, merged.get(t, 0) + 0.5)
    items = [{"topic": t, "weight": round(w, 3)} for t, w in merged.items() if w >= _INTEREST_DROP]
    items.sort(key=lambda x: -x["weight"])
    _checkpoint_write(
        os.path.join(d, "interests.json"),
        json.dumps(items[:_INTEREST_MAX], ensure_ascii=False, indent=1),
        validator=lambda c: isinstance(json.loads(c), list),
    )
    if ok:
        _mark_reflected(d)
        logger.info("[CKPT:evo_reflect_ok] %s/%s profile updated, %d interests", user_id, character_name, len(items))
    return ok


def _mark_reflected(d: str):
    with open(os.path.join(d, "last_reflect.txt"), "w") as f:
        f.write(datetime.now().strftime("%Y-%m-%d"))


def should_reflect(user_id: str, character_name: str) -> bool:
    """今天还没反思过 → True。由后台线程每小时调一次。"""
    if not config.get("evolution.enabled", True):
        return False
    d = _key_dir(user_id, character_name)
    last = _read(os.path.join(d, "last_reflect.txt")).strip()
    return last != datetime.now().strftime("%Y-%m-%d")
