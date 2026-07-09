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
        self.provider = cfg.get("provider", "saes")  # "saes" | "siliconflow"
        self.base_url = cfg.get("base_url", "http://127.0.0.1:9100").rstrip("/")
        self.endpoint = cfg.get("endpoint", "/hanyan/stream")
        self.timeout = cfg.get("timeout", 30)
        self.engine = cfg.get("engine", "gptsovits")
        self.api_key = cfg.get("api_key", "")
        self.model = cfg.get("model", "FunAudioLLM/CosyVoice2-0.5B")
        self.voice = cfg.get("voice", "FunAudioLLM/CosyVoice2-0.5B:anna")
        self.speed = cfg.get("speed", 1.0)
        self.max_files = cfg.get("max_files", 200)
        self.max_age_days = cfg.get("max_age_days", 7)

    def reload_config(self):
        """热加载配置。"""
        self._load_config()

    def synthesize(self, text: str) -> Optional[str]:
        """
        将文本合成为语音（SAES 网关 或 SiliconFlow API，取决于 tts.provider）。

        Returns:
            音频文件路径（.ogg，除非 ffmpeg 不可用则退化为原始格式），
            失败或未启用返回 None
        """
        if not self.enabled:
            return None

        if self.provider == "siliconflow":
            result = self._request_siliconflow(text)
        else:
            result = self._request_saes(text)
        if result is None:
            return None
        audio_data, ext = result

        out_dir = os.path.join(_DATA_DIR, "tts_output")
        os.makedirs(out_dir, exist_ok=True)

        text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
        # 文件名后缀必须和实际写入的字节内容一致——之前这里曾经不管探测到的
        # ext 是什么，一律硬编码存成 .wav，导致比如 SiliconFlow 返回的其实是
        # mp3 字节，却被存成 xxx.wav，send_voice() 按文件名猜 mimetype 时会猜
        # 成 audio/wav，造成 Matrix 客户端按 WAV 解码 MP3 数据、播放失败/异常。
        out_path = os.path.join(out_dir, f"tts_{text_hash}{ext}")
        with open(out_path, "wb") as f:
            f.write(audio_data)

        out_path = self._compress_to_ogg(out_path, ext)

        logger.info("TTS saved: %s", out_path)
        self._cleanup_old_files(out_dir)
        return out_path

    def _request_saes(self, text: str) -> Optional[tuple[bytes, str]]:
        """SAES/GPT-SoVITS 网关：POST {base_url}{endpoint}，返回 (音频字节, 猜测的扩展名)。"""
        url = f"{self.base_url}{self.endpoint}"
        payload = {"text": text, "engine": self.engine}
        headers = {"Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                audio_data = resp.read()
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            logger.error("TTS (SAES) HTTP %d: %s", e.code, body)
            return None
        except urllib.error.URLError as e:
            logger.error("TTS (SAES) connection failed: %s", e.reason)
            return None
        except TimeoutError:
            logger.error("TTS (SAES) timeout (%ds)", self.timeout)
            return None

        if not audio_data:
            logger.warning("TTS (SAES) returned empty audio")
            return None

        ext = ".wav"
        if "ogg" in content_type or "opus" in content_type:
            ext = ".ogg"
        elif "mp3" in content_type:
            ext = ".mp3"
        return audio_data, ext

    def _request_siliconflow(self, text: str) -> Optional[tuple[bytes, str]]:
        """SiliconFlow /audio/speech（OpenAI 风格，Bearer 认证）。固定请求 wav 格式，
        复用下面统一的 WAV → OGG 压缩管线，不用为 mp3/opus 分别处理转码。
        参考: https://docs.siliconflow.cn/cn/api-reference/audio/create-speech"""
        if not self.api_key:
            logger.error("TTS (siliconflow): tts.api_key 未配置")
            return None
        url = f"{self.base_url}/audio/speech"
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "wav",
            "speed": self.speed,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                audio_data = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            logger.error("TTS (siliconflow) HTTP %d: %s", e.code, body)
            return None
        except urllib.error.URLError as e:
            logger.error("TTS (siliconflow) connection failed: %s", e.reason)
            return None
        except TimeoutError:
            logger.error("TTS (siliconflow) timeout (%ds)", self.timeout)
            return None

        if not audio_data:
            logger.warning("TTS (siliconflow) returned empty audio")
            return None
        return audio_data, ".wav"

    def _compress_to_ogg(self, path: str, ext: str) -> str:
        """WAV → OGG/Opus 压缩（Matrix 语音气泡 MSC3245 推荐格式，体积也小 10-20 倍）。
        源文件已经是 ogg/opus 就直接跳过；ffmpeg 缺失或转码失败就原样返回源文件——
        语音消息仍然能发出去，只是不是压缩过的格式，不会因为转码失败就整条静默丢弃。"""
        if ext == ".ogg":
            return path
        ogg_path = os.path.splitext(path)[0] + ".ogg"
        try:
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-c:a", "libopus", "-b:a", "24k", ogg_path],
                capture_output=True, timeout=30,
            )
            if os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
                os.remove(path)
                logger.info("TTS compressed to OGG: %s (%d bytes)", ogg_path, os.path.getsize(ogg_path))
                return ogg_path
        except Exception as e:
            logger.debug("TTS OGG conversion skipped (ffmpeg missing/failed?): %s", e)
        return path

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
        """检查 TTS 服务是否在线。按 provider 探测不同的地址/协议——
        之前这里不管 provider 都硬编码探测 SAES 的 /hanyan/stream，对 SiliconFlow
        来说那是个不存在的路径，永远会返回不在线。"""
        if not self.enabled:
            return False
        try:
            if self.provider == "siliconflow":
                if not self.api_key:
                    return False
                req = urllib.request.Request(
                    f"{self.base_url}/audio/speech",
                    data=json.dumps({
                        "model": self.model, "input": "测试", "voice": self.voice,
                        "response_format": "wav",
                    }).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    method="POST",
                )
            else:
                req = urllib.request.Request(
                    f"{self.base_url}{self.endpoint}",
                    data=json.dumps({"text": "测试", "engine": self.engine}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            with urllib.request.urlopen(req, timeout=8) as resp:
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
