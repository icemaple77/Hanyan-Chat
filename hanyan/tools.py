"""
Hanyan Chat — LLM 工具调用层
================================
让角色在回复前可以主动使用工具：搜网页、读链接、搜图、下载图片/表情包、
搜 GitHub、查时间。纯 stdlib，零外部依赖。

协议（对模型的约定，见 TOOL_SPEC）：模型想用工具时单独输出一行
    <tool>{"name":"web_search","args":{"query":"..."}}</tool>
bot 解析执行后把结果以【工具结果】喂回，模型再继续（最多 max_calls_per_turn 轮）。

搜索后端可插拔：
- 默认 DuckDuckGo HTML 版（免 key，中文一般，偶尔限流）
- config 里配了 tools.searxng_url 则自动切换 SearXNG（自建聚合搜索，
  反 robot 检测由 SearXNG 层处理，且支持图片搜索）

调试标记：本模块所有关键路径都打 [CKPT:tool_*] 日志，出问题按标记回查。
"""

import hashlib
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from . import config, fs_access, links
from .emotion import EMOTION_FOLDER_MAP

logger = logging.getLogger("hanyan.tools")

DOWNLOAD_DIR = os.path.join(config.ROOT_DIR, "data", "downloads")
EMOJI_DIR = os.path.join(config.ROOT_DIR, "emojis")

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 有效的表情包情绪文件夹（download_image 的 emotion 参数校验用）
_VALID_EMOTION_FOLDERS = set(EMOTION_FOLDER_MAP.values()) | {"reminded", "tired"}

TOOL_SPEC = (
    "【工具】当你需要实时信息、想找图/表情包、或用户明确要求时，可以使用工具。\n"
    "用法：单独输出一行（这一行不要夹任何别的字）：\n"
    '<tool>{"name":"工具名","args":{…}}</tool>\n'
    "可用工具：\n"
    '- web_search {"query":"…"} 搜索网络\n'
    '- fetch_url {"url":"…"} 读取某个网页的正文\n'
    '- search_images {"query":"…"} 搜索图片，返回图片直链列表\n'
    '- download_image {"url":"…","emotion":"happy"} 下载图片发给用户；'
    "emotion 可选（happy/sad/angry/surprised/loved/confused/evasive），"
    "填了就把这张图收藏进你的表情包库以后也能用\n"
    '- github_search {"query":"…"} 搜 GitHub 开源项目\n'
    '- get_time {} 查看现在的日期时间\n'
    '- fs_list {"path":"…"} 列出本机某个目录的内容（只读）\n'
    '- fs_read {"path":"…"} 读取本机某个文件的内容（只读）\n'
    '- fs_write {"path":"…","content":"…"} 写文件。你的工作区（data/workspace）内直接生效；'
    "工作区外会生成审批单，需要用户 /批准 后才执行\n"
    '- fs_delete {"path":"…"} 删除文件，审批规则同 fs_write\n'
    "工具结果会以【工具结果】发给你，看完后用你的角色口吻自然地回复用户，"
    "不要向用户提及“工具”这个词。不需要工具时就直接正常回复。"
    "涉及文件操作时要如实转达审批单编号。"
)

# 模型输出里的工具调用匹配：<tool>{...}</tool>，容忍前后空白和代码块包裹
_TOOL_PATTERN = re.compile(r"<tool>\s*(\{.*?\})\s*</tool>", re.DOTALL)


def parse_tool_call(reply: str) -> Optional[tuple[str, dict]]:
    """从模型回复中解析工具调用。没有则返回 None。"""
    if not reply:
        return None
    m = _TOOL_PATTERN.search(reply)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        name = data.get("name", "")
        args = data.get("args", {}) or {}
        if isinstance(name, str) and name and isinstance(args, dict):
            return name, args
    except (json.JSONDecodeError, AttributeError):
        logger.info("[CKPT:tool_parse_fail] unparseable tool call: %.100s", m.group(1))
    return None


def strip_tool_calls(reply: str) -> str:
    """把回复里残留的 <tool>…</tool> 全部剥掉（模型偶尔会把工具调用和正文混着输出）。"""
    return _TOOL_PATTERN.sub("", reply or "").strip()


def execute(name: str, args: dict, user_id: str = "") -> dict:
    """执行工具。返回 {"text": 给模型看的结果文本, "image": 可选的本地图片路径}。
    任何异常都被捕获并转成文字结果，绝不让工具错误冒泡打断聊天流程。"""
    logger.info("[CKPT:tool_exec] %s args=%s", name, json.dumps(args, ensure_ascii=False)[:200])
    try:
        if name.startswith("fs_"):
            if not config.get("fs.enabled", True):
                return {"text": "（文件系统访问功能已被关闭）"}
            path = str(args.get("path", ""))
            if name == "fs_list":
                return {"text": fs_access.fs_list(path)}
            if name == "fs_read":
                return {"text": fs_access.fs_read(path)}
            if name == "fs_write":
                return {"text": fs_access.fs_write(path, str(args.get("content", "")), user_id)}
            if name == "fs_delete":
                return {"text": fs_access.fs_delete(path, user_id)}
        if name == "web_search":
            return {"text": _web_search(str(args.get("query", ""))[:100])}
        if name == "fetch_url":
            text = links.fetch_and_extract_text(str(args.get("url", "")))
            return {"text": text[:2000] if text else "（抓取失败或该页面没有可读正文）"}
        if name == "search_images":
            return {"text": _search_images(str(args.get("query", ""))[:100])}
        if name == "download_image":
            return _download_image(str(args.get("url", "")), args.get("emotion"))
        if name == "github_search":
            return {"text": _github_search(str(args.get("query", ""))[:100])}
        if name == "get_time":
            now = datetime.now()
            weekdays = "一二三四五六日"
            return {"text": now.strftime(f"%Y年%m月%d日 星期{weekdays[now.weekday()]} %H:%M:%S")}
        return {"text": f"（没有叫 {name} 的工具）"}
    except Exception as e:
        logger.warning("[CKPT:tool_error] %s failed: %s", name, e, exc_info=True)
        return {"text": f"（工具执行出错了：{e}）"}


# ── 搜索后端 ─────────────────────────────────────────────────────

def _searxng_url() -> str:
    return (config.get("tools.searxng_url", "") or "").rstrip("/")


def _http_get(url: str, timeout: int = 12, data: Optional[bytes] = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(1_000_000)


def _web_search(query: str) -> str:
    if not query:
        return "（搜索词是空的）"
    sx = _searxng_url()
    if sx:
        return _searxng_search(query, "general")
    return _ddg_search(query)


def _searxng_search(query: str, category: str) -> str:
    url = f"{_searxng_url()}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "categories": category, "language": "zh-CN"}
    )
    try:
        data = json.loads(_http_get(url).decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning("[CKPT:search_searxng_fail] %s", e)
        return _ddg_search(query) if category == "general" else "（图片搜索暂时不可用）"
    results = data.get("results", [])[:5]
    if not results:
        return "（没搜到结果）"
    if category == "images":
        lines = [f"{i+1}. {r.get('title','')[:40]} 图片直链: {r.get('img_src','')}" for i, r in enumerate(results)]
    else:
        lines = [
            f"{i+1}. {r.get('title','')[:60]}\n   {r.get('content','')[:120]}\n   链接: {r.get('url','')}"
            for i, r in enumerate(results)
        ]
    return "\n".join(lines)


_DDG_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>',
    re.DOTALL,
)


def _ddg_search(query: str) -> str:
    """DuckDuckGo HTML 版搜索（免 key）。被限流/结构变化时优雅降级。"""
    try:
        body = urllib.parse.urlencode({"q": query, "kl": "cn-zh"}).encode()
        raw = _http_get("https://html.duckduckgo.com/html/", data=body).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("[CKPT:search_ddg_fail] %s", e)
        return "（搜索服务暂时连不上，稍后再试）"
    out = []
    for m in _DDG_RESULT_RE.finditer(raw):
        href, title, snippet = m.groups()
        # DDG 的结果链接是 /l/?uddg=<真实URL编码> 的跳转格式
        real = href
        if "uddg=" in href:
            real = urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet)).strip()
        out.append(f"{len(out)+1}. {title[:60]}\n   {snippet[:120]}\n   链接: {real}")
        if len(out) >= 5:
            break
    if not out:
        logger.info("[CKPT:search_ddg_empty] query=%s len=%d", query, len(raw))
        return "（没搜到结果，可能被临时限流了）"
    return "\n".join(out)


def _search_images(query: str) -> str:
    if not query:
        return "（搜索词是空的）"
    if _searxng_url():
        return _searxng_search(query, "images")
    return (
        "（图片搜索需要配置自建的 SearXNG（config: tools.searxng_url）。"
        "你可以先 web_search 找到相关网页，再 fetch_url 从页面里找图片地址。）"
    )


# ── 图片下载 ─────────────────────────────────────────────────────

_EXT_BY_MIME = {"image/gif": ".gif", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def _download_image(url: str, emotion: Optional[str] = None) -> dict:
    if not url.startswith(("http://", "https://")):
        return {"text": "（图片链接不合法）"}
    max_bytes = int(config.get("tools.max_image_mb", 5)) * 1024 * 1024
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            mime = (resp.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            if mime not in _EXT_BY_MIME:
                return {"text": f"（这个链接不是图片，Content-Type={mime or '未知'}）"}
            data = resp.read(max_bytes + 1)
    except Exception as e:
        return {"text": f"（下载失败：{e}）"}
    if len(data) > max_bytes:
        return {"text": f"（图片超过 {max_bytes // 1024 // 1024}MB 上限，放弃下载）"}

    ext = _EXT_BY_MIME[mime]
    fname = "dl_" + hashlib.md5(data).hexdigest()[:10] + ext

    # emotion 合法 → 收藏进表情包库（自我扩充）；否则放临时下载目录（定期清理）
    folder = DOWNLOAD_DIR
    saved_note = ""
    if emotion:
        folder_name = EMOTION_FOLDER_MAP.get(str(emotion), str(emotion))
        if folder_name in _VALID_EMOTION_FOLDERS:
            folder = os.path.join(EMOJI_DIR, folder_name)
            saved_note = f"，并收藏进了表情包库（{folder_name}）"
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, fname)
    with open(path, "wb") as f:
        f.write(data)
    logger.info("[CKPT:tool_image_saved] %s (%d bytes)", path, len(data))
    return {"text": f"（图片已下载{saved_note}，会随回复一起发给用户）", "image": path}


# ── GitHub 搜索 ──────────────────────────────────────────────────

def _github_search(query: str) -> str:
    if not query:
        return "（搜索词是空的）"
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": query, "sort": "stars", "per_page": 5}
    )
    try:
        data = json.loads(_http_get(url).decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning("[CKPT:search_github_fail] %s", e)
        return "（GitHub 搜索暂时不可用）"
    items = data.get("items", [])[:5]
    if not items:
        return "（没搜到相关仓库）"
    lines = [
        f"{i+1}. {r.get('full_name','')} ⭐{r.get('stargazers_count',0)}\n"
        f"   {(r.get('description') or '')[:100]}\n   链接: {r.get('html_url','')}"
        for i, r in enumerate(items)
    ]
    return "\n".join(lines)


# ── 自我清理 ─────────────────────────────────────────────────────

def cleanup_downloads(max_age_days: int = 7) -> int:
    """清理临时下载目录里过期的文件。返回删除数量。（表情包库不清理）"""
    if not os.path.isdir(DOWNLOAD_DIR):
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for fn in os.listdir(DOWNLOAD_DIR):
        p = os.path.join(DOWNLOAD_DIR, fn)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("[CKPT:cleanup] removed %d expired download(s)", removed)
    return removed
