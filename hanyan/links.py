"""
Hanyan Chat — 链接内容提取
纯 stdlib（urllib + html.parser），不引入 bs4/lxml 依赖。
"""

import logging
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.links")

URL_PATTERN = re.compile(r'https?://[^\s<>"]+|www\.[^\s<>"]+')


class _TextExtractHTMLParser(HTMLParser):
    """极简正文提取：跳过 script/style/noscript 标签内容，其余文本节点拼接。"""

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.chunks.append(text)

    def get_text(self) -> str:
        return "\n".join(self.chunks)


def fetch_and_extract_text(url: str) -> Optional[str]:
    """抓取链接并提取正文纯文本。失败一律返回 None，调用方应静默回退到原始消息。"""
    timeout = config.get("link_fetch.timeout", 10)
    max_length = config.get("link_fetch.max_length", 2000)
    user_agent = config.get("link_fetch.user_agent", "Mozilla/5.0 (compatible; HanyanChat/1.0)")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "") or ""
            if "html" not in content_type.lower():
                return None
            raw = resp.read(200_000)  # 硬上限 200KB，避免大文件拖慢

        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            html_text = raw.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            html_text = raw.decode("utf-8", errors="replace")

        parser = _TextExtractHTMLParser()
        parser.feed(html_text)
        text = re.sub(r"\n{2,}", "\n", parser.get_text()).strip()
        return text[:max_length] if text else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        logger.debug("Link fetch failed for %s: %s", url, e)
        return None


def build_message_with_link_context(text: str) -> str:
    """如果消息里带链接，抓取正文摘要拼进即将发给 LLM 的内容里。
    只影响本轮送去给 LLM 的文本，session/记忆里仍然保存用户的原始输入。"""
    if not config.get("link_fetch.enabled", True):
        return text
    match = URL_PATTERN.search(text)
    if not match:
        return text
    url = match.group(0)
    if url.startswith("www."):
        url = "http://" + url
    content = fetch_and_extract_text(url)
    if not content:
        return text
    return (
        f'用户发送了消息："{text}"\n'
        f"其中包含的链接的主要内容摘要如下（可能不完整）：\n---\n{content}\n---\n"
    )
