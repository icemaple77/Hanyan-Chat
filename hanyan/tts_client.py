"""
Hanyan Chat — TTS 客户端（文字转语音）
调用 SAES/GPT-SoVITS 网关，返回音频文件路径；网关不可用时静默降级（不发语音，
不影响文字回复）。
"""

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.tts")

_DATA_DIR = os.path.join(config.ROOT_DIR, "data")


class TTSClient:
    """SAES/GPT-SoVITS TTS 客户端"""

    def __init__(self):
        self._load_config()

    def _load_config(self):
        cfg = config.get("tts")
        self.enabled = cfg.get("enabled", True)
        self.base_url = cfg.get("base_url", "http://127.0.0.1:9100").rstrip("/")
        self.endpoint = cfg.get("endpoint", "/hanyan/stream")
        self.timeout = cfg.get("timeout", 30)
        self.engine = cfg.get("engine", "gptsovits")
        self.max_files = cfg.get("max_files", 200)
        self.max_age_days = cfg.get("max_age_days", 7)

    def reload_config(self):
        """热加载配置。"""
        self._load_config()

    def synthesize(self, text: str) -> Optional[str]:
        """
        将文本合成为语音。

        Returns:
            音频文件路径（.wav 或 .ogg），失败或未启用返回 None
        """
        if not self.enabled:
            return None

        url = f"{self.base_url}{self.endpoint}"
        payload = {"text": text, "engine": self.engine}
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                audio_data = resp.read()
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            logger.error("TTS HTTP %d: %s", e.code, body)
            return None
        except urllib.error.URLError as e:
            logger.error("TTS connection failed: %s", e.reason)
            return None
        except TimeoutError:
            logger.error("TTS timeout (%ds)", self.timeout)
            return None

        if not audio_data:
            logger.warning("TTS returned empty audio")
            return None

        out_dir = os.path.join(_DATA_DIR, "tts_output")
        os.makedirs(out_dir, exist_ok=True)

        ext = ".wav"
        if "ogg" in content_type or "opus" in content_type:
            ext = ".ogg"
        elif "mp3" in content_type:
            ext = ".mp3"

        text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
        out_path = os.path.join(out_dir, f"tts_{text_hash}.wav")

        with open(out_path, "wb") as f:
            f.write(audio_data)

        # WAV → OGG 压缩（Matrix 小气泡要求，体积缩小 10-20 倍）
        if ext == ".wav":
            ogg_path = out_path.replace(".wav", ".ogg")
            try:
                import subprocess
                subprocess.run(
                    ["ffmpeg", "-y", "-i", out_path, "-c:a", "libopus", "-b:a", "24k", ogg_path],
                    capture_output=True, timeout=30,
                )
                if os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
                    os.remove(out_path)
                    out_path = ogg_path
                    logger.info("TTS compressed to OGG: %s (%d bytes)", ogg_path, os.path.getsize(ogg_path))
            except Exception as e:
                logger.debug("TTS OGG conversion skipped (ffmpeg missing/failed?): %s", e)

        logger.info("TTS saved: %s", out_path)
        self._cleanup_old_files(out_dir)
        return out_path

    def _cleanup_old_files(self, out_dir: str):
        """清理 tts_output 缓存目录，避免长期运行下磁盘无限增长。"""
        try:
            now = time.time()
            max_age_seconds = self.max_age_days * 86400
            entries = []
            for name in os.listdir(out_dir):
                path = os.path.join(out_dir, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if now - mtime > max_age_seconds:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                entries.append((mtime, path))

            if len(entries) > self.max_files:
                entries.sort(key=lambda x: x[0])
                for _, path in entries[: len(entries) - self.max_files]:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        except OSError as e:
            logger.debug("TTS cache cleanup skipped: %s", e)

    def health_check(self) -> bool:
        """检查 TTS 服务是否在线。"""
        if not self.enabled:
            return False
        url = f"{self.base_url}/hanyan/stream"
        payload = json.dumps({"text": "测试"}).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False


# 全局单例
_client: Optional[TTSClient] = None


def get_client() -> TTSClient:
    global _client
    if _client is None:
        _client = TTSClient()
    return _client
