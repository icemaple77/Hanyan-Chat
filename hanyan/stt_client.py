"""
Hanyan Chat — STT 客户端（语音转文字，本地 whisper-mlx）
============================================================
用 mlx-whisper（Apple Silicon 上用 MLX 框架跑 Whisper，比 CPU 版 faster-whisper
明显快）把用户发来的语音消息转成文字，转出来的文字直接进入和普通文字消息一样的
处理流程（角色路由、记忆、命令、LLM 回复）——对齐 KouriChat 里"语音输入 = 微信
自动转文字后当文字处理"的行为。

依赖：
    pip install mlx-whisper
    （仅支持 Apple Silicon Mac；模型首次使用时会从 HuggingFace 自动下载到本地缓存）

不满足这些条件时（比如部署在 Linux 服务器上）：把 config.json 的 stt.enabled
设为 false，语音消息会被静默跳过（不影响文字聊天）。想在非 Apple Silicon 环境
用本地 STT，可以把 hanyan/stt_client.py 换成 faster-whisper 实现，接口
（transcribe(audio_path) -> Optional[str]）保持不变即可，上层代码不用改。
"""

import logging
import os
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.stt")


class STTClient:
    """本地 whisper-mlx STT 客户端。模型懒加载（第一次转写时才真正加载/下载）。"""

    def __init__(self):
        self._load_config()
        self._import_error_logged = False

    def _load_config(self):
        cfg = config.get("stt")
        self.enabled = cfg.get("enabled", True)
        self.model = cfg.get("model", "mlx-community/whisper-large-v3-turbo")
        self.language = cfg.get("language", "zh")

    def reload_config(self):
        """热加载配置。"""
        self._load_config()

    def transcribe(self, audio_path: str) -> Optional[str]:
        """
        转写本地音频文件（wav/ogg/mp3/m4a 均可，内部用 ffmpeg 解码，
        与 tts_client 的 OGG 压缩共用同一个 ffmpeg 系统依赖）。

        Returns:
            转写文字，失败/未启用/依赖未安装时返回 None（调用方应静默跳过，
            不要因为语音转写失败就崩掉整条消息处理链路）。
        """
        if not self.enabled:
            return None
        if not os.path.exists(audio_path):
            logger.warning("STT: audio file not found: %s", audio_path)
            return None

        try:
            import mlx_whisper
        except ImportError:
            if not self._import_error_logged:
                logger.error(
                    "mlx_whisper 未安装，STT 不可用（语音消息会被跳过）。"
                    "安装: pip install mlx-whisper （仅支持 Apple Silicon）"
                )
                self._import_error_logged = True
            return None

        try:
            result = mlx_whisper.transcribe(
                audio_path,
                path_or_hf_repo=self.model,
                language=None if self.language in ("auto", "", None) else self.language,
            )
            text = (result.get("text") or "").strip()
            return text or None
        except Exception as e:
            logger.error("STT transcription failed for %s: %s", audio_path, e)
            return None

    def health_check(self) -> bool:
        """检查 STT 依赖是否可用（不实际跑一次转写，只检查能否 import）。"""
        if not self.enabled:
            return False
        try:
            import mlx_whisper  # noqa: F401
            return True
        except ImportError:
            return False


# 全局单例
_client: Optional[STTClient] = None


def get_client() -> STTClient:
    global _client
    if _client is None:
        _client = STTClient()
    return _client
