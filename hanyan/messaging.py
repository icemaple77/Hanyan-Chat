"""
Hanyan Chat — 消息模板系统（KouriChat 风格）
[tickle]/[tickle_self]/[recall] 标记 + $ / \\ 断句，把一条 LLM 回复拆成一串
发送动作，模拟真人分几条发消息、拍一拍、撤回的效果。
"""

import random
import re

_MARKER_PATTERN = re.compile(r"(\[tickle\]|\[tickle_self\]|\[recall\])")
_BRACKET_OPEN = set("({[（【｛")
_BRACKET_CLOSE = set(")}]）】｝")


def _split_on_hard_boundaries(text: str) -> list[str]:
    """按"空行"（连续 2 个以上换行，即真正的段落分隔）或连续 3 个以上反斜线切分。

    之前这里对单个 \\n 就直接切成新消息——这是照抄 KouriChat 的假设：它的角色
    提示词里明确要求模型"不要用句号逗号，改用 \\ 分句"，所以模型输出里出现的换行
    本来就很少、而且基本等价于"这是新的一句"。但不是所有角色卡都会写这条约定
    （比如没有照搬 KouriChat 风格、纯自然语言写的角色卡），这类角色配合会自然
    换行的模型（比如叙述里习惯分段）时，随手一个换行就会被拆成单独一条 Matrix
    消息，结果就是"发一句话，收到四五条回复"——换行只是模型的排版习惯，不代表
    真的想分开发。现在只有连续空行（真正的段落分隔）才会触发分条，单个换行会
    留在同一条消息里，Matrix 客户端本来就能正常显示消息内的换行。"""
    parts = re.split(r"(?:\\{3,}|\n{2,})", text)
    return [p for p in parts if p != ""]


def _split_single_backslash(text: str) -> list[str]:
    """按单个 \\ 切分成短句，但跳过颜文字里的 \\（例如 (・\\・)）——
    判定方法：\\ 前后各看 10 个字符，如果左边出现过左括号且右边出现过右括号，
    就认为这是颜文字的一部分，不切分。"""
    if "\\" not in text:
        return [text] if text else []
    result = []
    buf: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "\\":
            before = text[max(0, i - 10):i]
            after = text[i + 1:i + 11]
            in_emoticon = (
                any(c in _BRACKET_OPEN for c in before)
                and any(c in _BRACKET_CLOSE for c in after)
            )
            if in_emoticon:
                buf.append(ch)
            else:
                seg = "".join(buf).strip()
                if seg:
                    result.append(seg)
                buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        result.append(tail)
    return result


def split_reply(text: str) -> list[tuple[str, str]]:
    """
    将 LLM 回复解析成一串 (动作类型, 内容) 的发送序列：
    - [tickle] / [tickle_self] / [recall] 作为独立动作标记
    - 其余文本依次按 $ → 换行/连续 3+ 个 \\（硬边界） → 单个 \\（跳过颜文字）拆成多条消息
    动作类型: "text" | "tickle" | "tickle_self" | "recall"
    """
    if not text:
        return []
    actions: list[tuple[str, str]] = []
    for seg in _MARKER_PATTERN.split(text):
        if not seg:
            continue
        if seg in ("[tickle]", "[tickle_self]", "[recall]"):
            actions.append((seg[1:-1], ""))
            continue
        for dollar_chunk in seg.split("$"):
            if not dollar_chunk:
                continue
            for hard_chunk in _split_on_hard_boundaries(dollar_chunk):
                for sentence in _split_single_backslash(hard_chunk):
                    cleaned = sentence.strip()
                    if cleaned:
                        actions.append(("text", cleaned))
    return actions


def typing_delay(text: str) -> float:
    """模拟打字节奏的发送间隔，文本越长停顿略长，封顶避免真人等太久。"""
    base = min(3.0, max(0.4, len(text) * 0.06))
    return base + random.uniform(0.15, 0.4)
