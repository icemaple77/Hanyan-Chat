"""
Hanyan Chat — 本地文件系统访问（带权限边界和审批流）
=====================================================
权限模型（三层）：
1. 她的主目录（fs.home_dir，默认 data/home/）→ 自由读/写/删
2. 只读范围（fs.read_roots，默认用户家目录）→ 只能读和列目录
3. 主目录外的写/删 → 生成审批单，用户在聊天里 /批准 <id> 后才执行，
   默认 15 分钟过期；/拒绝 <id> 或超时自动作废

安全机制：
- 所有路径先 expanduser + realpath 解析（防 ../ 和符号链接逃逸）再做前缀判定
- 敏感路径硬黑名单（.ssh/密钥/token/Keychains 等）任何层级都直接拒绝，
  不受配置影响——防止角色被话术诱导去读私钥
- 单文件读取上限 fs.max_read_kb（默认 256KB），给模型的摘要截断到 2000 字
- 全部操作写审计日志 data/fs_audit.log + 主日志 [CKPT:fs_*] 标记
"""

import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.fs")

AUDIT_LOG = os.path.join(config.ROOT_DIR, "data", "fs_audit.log")

# 任何层级出现这些片段就直接拒绝（大小写不敏感）。硬编码，不受配置削弱。
_DENY_PARTS = (
    ".ssh", ".gnupg", ".aws", ".kube", "keychains", "id_rsa", "id_ed25519",
    ".env", "access_token", "credentials", "secring", ".netrc", "cookies",
    "signing.key", "homeserver.db", "password",
)

_MAX_WRITE_BYTES = 200_000


def _home_dir() -> str:
    d = config.get("fs.home_dir", "") or os.path.join(config.ROOT_DIR, "data", "home")
    d = os.path.realpath(os.path.expanduser(d))
    os.makedirs(d, exist_ok=True)
    return d


def _read_roots() -> list[str]:
    roots = config.get("fs.read_roots", None) or ["~"]
    return [os.path.realpath(os.path.expanduser(r)) for r in roots]


def _audit(line: str):
    logger.info("[CKPT:fs_audit] %s", line)
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _resolve(path: str) -> Optional[str]:
    """展开并规范化路径；命中敏感黑名单返回 None。"""
    if not path or not isinstance(path, str):
        return None
    real = os.path.realpath(os.path.expanduser(path))
    lowered = real.lower()
    if any(part in lowered for part in _DENY_PARTS):
        _audit(f"DENY(blacklist) {real}")
        return None
    return real


def _in_home(real: str) -> bool:
    home = _home_dir()
    return real == home or real.startswith(home + os.sep)


def _readable(real: str) -> bool:
    if _in_home(real):
        return True
    return any(real == r or real.startswith(r + os.sep) for r in _read_roots())


# ── 读 / 列目录（只读范围内自由使用）─────────────────────────────

def fs_list(path: str) -> str:
    real = _resolve(path)
    if not real:
        return "（这个路径不允许访问）"
    if not _readable(real):
        return "（超出了允许的只读范围）"
    if not os.path.isdir(real):
        return "（不是目录或不存在）"
    try:
        entries = sorted(os.listdir(real))[:50]
    except OSError as e:
        return f"（读取失败：{e}）"
    lines = []
    for name in entries:
        p = os.path.join(real, name)
        mark = "/" if os.path.isdir(p) else f" ({os.path.getsize(p)}B)" if os.path.isfile(p) else ""
        lines.append(name + mark)
    _audit(f"LIST {real}")
    return f"{real} 下共 {len(entries)} 项（最多显示50）：\n" + "\n".join(lines) if lines else "（空目录）"


def fs_read(path: str) -> str:
    real = _resolve(path)
    if not real:
        return "（这个路径不允许访问）"
    if not _readable(real):
        return "（超出了允许的只读范围）"
    if not os.path.isfile(real):
        return "（文件不存在）"
    max_bytes = int(config.get("fs.max_read_kb", 256)) * 1024
    if os.path.getsize(real) > max_bytes:
        return f"（文件超过 {max_bytes // 1024}KB，太大了不读）"
    try:
        with open(real, "rb") as f:
            raw = f.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
    except OSError as e:
        return f"（读取失败：{e}）"
    _audit(f"READ {real}")
    return f"{real} 内容（截断到2000字）：\n{text[:2000]}"


# ── 写 / 删（主目录内自由；主目录外走审批）───────────────────────

class _PendingOp:
    def __init__(self, op: str, path: str, content: Optional[str], user_id: str):
        self.op = op
        self.path = path
        self.content = content
        self.user_id = user_id
        self.created = time.time()


_pending: dict[int, _PendingOp] = {}
_pending_lock = threading.Lock()
_next_id = 1


def _add_pending(op: str, path: str, content: Optional[str], user_id: str) -> int:
    global _next_id
    timeout = int(config.get("fs.approval_timeout_min", 15)) * 60
    with _pending_lock:
        # 顺手清掉过期的
        for pid in [p for p, v in _pending.items() if time.time() - v.created > timeout]:
            _pending.pop(pid, None)
        pid = _next_id
        _next_id += 1
        _pending[pid] = _PendingOp(op, path, content, user_id)
    _audit(f"PENDING#{pid} {op} {path} (requested)")
    return pid


def _do_write(real: str, content: str) -> str:
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w", encoding="utf-8") as f:
            f.write(content)
        _audit(f"WRITE {real} ({len(content)}B)")
        return f"（已写入 {real}）"
    except OSError as e:
        return f"（写入失败：{e}）"


def _do_delete(real: str) -> str:
    try:
        if os.path.isfile(real):
            os.remove(real)
            _audit(f"DELETE {real}")
            return f"（已删除 {real}）"
        return "（只支持删除单个文件，且文件要存在）"
    except OSError as e:
        return f"（删除失败：{e}）"


def fs_write(path: str, content: str, user_id: str) -> str:
    real = _resolve(path)
    if not real:
        return "（这个路径不允许访问）"
    if len((content or "").encode("utf-8")) > _MAX_WRITE_BYTES:
        return "（内容太大，超过写入上限）"
    if _in_home(real):
        return _do_write(real, content or "")
    pid = _add_pending("write", real, content or "", user_id)
    return (
        f"（这个位置在你的主目录之外，已生成审批单 #{pid}：写入 {real}。"
        f"告诉用户需要他回复 /批准 {pid} 才会执行，或 /拒绝 {pid} 作废）"
    )


def fs_delete(path: str, user_id: str) -> str:
    real = _resolve(path)
    if not real:
        return "（这个路径不允许访问）"
    if _in_home(real):
        return _do_delete(real)
    pid = _add_pending("delete", real, None, user_id)
    return (
        f"（这个位置在你的主目录之外，已生成审批单 #{pid}：删除 {real}。"
        f"告诉用户需要他回复 /批准 {pid} 才会执行，或 /拒绝 {pid} 作废）"
    )


# ── 审批接口（commands.py 调用）─────────────────────────────────

def approve(pid: int, user_id: str) -> str:
    timeout = int(config.get("fs.approval_timeout_min", 15)) * 60
    with _pending_lock:
        op = _pending.get(pid)
        if op is None:
            return f"没有找到审批单 #{pid}（可能已过期/已处理）。"
        # 先鉴权再摘单：不能让别人的失败尝试把审批单弄丢
        if op.user_id != user_id:
            _audit(f"PENDING#{pid} approve attempt by wrong user {user_id}")
            return "这个审批单不是你发起的会话产生的，忽略。"
        _pending.pop(pid, None)
    if time.time() - op.created > timeout:
        _audit(f"PENDING#{pid} expired")
        return f"审批单 #{pid} 已过期，让她重新操作一次吧。"
    _audit(f"PENDING#{pid} approved by {user_id}")
    if op.op == "write":
        return _do_write(op.path, op.content or "")
    return _do_delete(op.path)


def reject(pid: int, user_id: str) -> str:
    with _pending_lock:
        op = _pending.get(pid)
        if op is None:
            return f"没有找到审批单 #{pid}。"
        if op.user_id != user_id:
            return "这个审批单不是你发起的会话产生的，忽略。"
        _pending.pop(pid, None)
    _audit(f"PENDING#{pid} rejected by {user_id}")
    return f"已拒绝审批单 #{pid}（{op.op} {op.path}）。"


def list_pending() -> str:
    timeout = int(config.get("fs.approval_timeout_min", 15)) * 60
    with _pending_lock:
        items = [
            f"#{pid} {v.op} {v.path}（{int((timeout - (time.time() - v.created)) / 60)}分钟后过期）"
            for pid, v in sorted(_pending.items())
            if time.time() - v.created <= timeout
        ]
    return "待批准的操作：\n" + "\n".join(items) if items else "当前没有待批准的操作。"
