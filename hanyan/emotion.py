"""
Hanyan Chat — 情绪检测 + 表情包
"""

import logging
import os
import random
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.emotion")

EMOJI_DIR = os.path.join(config.ROOT_DIR, "emojis")

# 情绪 → 表情包文件夹映射（对应 emojis/ 下的目录名）
EMOTION_FOLDER_MAP: dict[str, str] = {
    "开心": "happy",
    "难过": "sad",
    "生气": "angry",
    "惊讶": "surprised",
    "撒娇": "loved",
    "懵逼": "confused",
    "尴尬": "evasive",
    "得意": "happy",
}

# 情绪关键词映射（KouriChat 风格）。
# 注：原来 "得意" 里也有一个 "哼"，但字典遍历顺序里 "撒娇" 排在 "得意" 前面，
# "哼" 永远先命中 "撒娇"，"得意" 那个 "哼" 是死代码，故删除去重。
EMOTION_KEYWORDS: dict[str, list[str]] = {
    "开心": ["开心", "高兴", "快乐", "哈哈哈", "哈哈", "好开心", "棒", "太棒了", "耶",
             "嘻嘻", "嘿嘿", "笑死", "笑死了", "好笑", "有趣", "好玩", "太好啦"],
    "难过": ["难过", "伤心", "哭了", "呜呜", "委屈", "悲伤", "心碎", "好难过",
             "不开心", "哭", "泪", "叹气", "sigh"],
    "生气": ["生气", "愤怒", "气死", "烦", "烦死了", "无语", "恶心", "讨厌",
             "滚", "走开", "受不了", "怒"],
    "惊讶": ["惊讶", "震惊", "天哪", "哇塞", "不会吧", "真的吗", "不可能", "吓到",
             "惊", "卧槽", "我去", "啊?"],
    "撒娇": ["撒娇", "哼", "不要嘛", "讨厌啦", "人家", "好不好嘛", "亲亲",
             "抱抱", "么么", "爱你", "想你"],
    "懵逼": ["懵逼", "啥", "什么情况", "晕", "迷茫", "搞不懂", "懵", "不理解",
             "啥意思", "什么意思"],
    "尴尬": ["尴尬", "汗", "囧", "好尴尬", "社死", "丢人", "糗", "不好意思"],
    "得意": ["得意", "那当然", "厉害吧", "我赢了", "超厉害", "牛逼",
             "帅", "最美", "最棒"],
}


def detect_emotion(text: str) -> Optional[str]:
    """检测回复文本中的情绪关键词，返回情绪分类名称，未检测到返回 None。"""
    text_lower = text.lower()
    for emotion, keywords in EMOTION_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                logger.debug("Detected emotion '%s' via keyword '%s'", emotion, kw)
                return emotion
    return None


def pick_emoji(emotion: str) -> Optional[str]:
    """从 emojis/{情绪对应文件夹}/ 随机选取一个表情包文件。"""
    folder_name = EMOTION_FOLDER_MAP.get(emotion, emotion)
    folder = os.path.join(EMOJI_DIR, folder_name)
    if not os.path.isdir(folder):
        logger.debug("Emoji folder not found: %s", folder)
        return None
    try:
        files = [
            f for f in os.listdir(folder)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        ]
        if not files:
            logger.debug("No emoji files in %s", folder)
            return None
        return os.path.join(folder, random.choice(files))
    except OSError as e:
        logger.warning("Failed to list emoji dir %s: %s", folder, e)
        return None
