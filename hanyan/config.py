"""
Hanyan Chat — 配置模块
支持热加载：调用 reload() 重新读取配置文件。

- load() 与默认配置深度合并，磁盘配置缺失的 key 不会导致下游 AttributeError
- load()/save() 原子写入 + 异常兜底，损坏的 config.json 不会让进程崩溃
- 线程安全（WebUI 与主循环可能并发访问）
"""

import json
import logging
import os
import threading
from copy import deepcopy
from typing import Optional

logger = logging.getLogger("hanyan.config")

# 项目根目录 = hanyan/ 包所在目录的上一级（config.json、prompts/、data/ 都放在根目录，
# 不是包内部，这样用户部署时只需要在根目录下操作，不用深入 hanyan/ 包）。
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")

_DEFAULT_CONFIG = {
    # Matrix
    "matrix": {
        "homeserver": "https://matrix.chenyun.org",
        "user_id": "@hanyan:matrix.chenyun.org",
        # 生产环境建议通过环境变量 HANYAN_MATRIX_PASSWORD 覆盖，而不是提交明文密码。
        "password": os.environ.get("HANYAN_MATRIX_PASSWORD", "CHANGE_ME"),
        "device_name": "HanyanChat",
    },
    # 本地 LLM（Ollama 原生协议，或任意 OpenAI 兼容端点）
    "llm": {
        "base_url": "http://localhost:11434",
        "model": "qwen3:8b",
        "temperature": 0.7,
        "timeout": 60,
        "api_key": "",
        # Qwen3/3.5 这类混合推理模型默认可能会输出 <think>...</think> 思考过程，
        # 混进正文会污染消息拆句和 JSON 解析（提醒/记忆摘要都要求纯 JSON 输出）。
        # 这个 app 是纯聊天场景，不需要推理链，默认关闭。只在原生 Ollama 协议
        # （没配 api_key 时）生效——OpenAI 兼容协议没有这个参数。
        "think": False,
    },
    # TTS（文字转语音）。provider 支持两种：
    # - "saes"：本地 SAES/GPT-SoVITS 网关，用 base_url + endpoint + engine
    # - "siliconflow"：SiliconFlow 云端 API（/audio/speech），用 base_url + api_key
    #   + model + voice + speed，本地没有 TTS 网关时的备选方案
    "tts": {
        "enabled": True,
        "provider": "saes",
        "base_url": "http://127.0.0.1:9100",
        "endpoint": "/hanyan/stream",
        "timeout": 30,
        "engine": "gptsovits",
        # 以下三项只在 provider="siliconflow" 时用到
        "api_key": "",
        "model": "FunAudioLLM/CosyVoice2-0.5B",
        "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
        "speed": 1.0,
        "max_files": 200,       # data/tts_output 下最多保留的文件数
        "max_age_days": 7,      # 超过这个天数的缓存音频会被清理
    },
    # STT（语音转文字，本地 whisper-mlx，Apple Silicon 优化）
    "stt": {
        "enabled": True,
        # mlx-community 在 HuggingFace 上发布的 MLX 格式 Whisper 模型，首次使用会自动下载。
        # 可选：whisper-tiny-mlx（最快） / whisper-large-v3-turbo（推荐，速度与精度平衡）
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "zh",       # 设为 "auto" 交给模型自动检测语言
    },
    # 主动消息
    "proactive": {
        "interval_minutes": 30,
        "quiet_start": "23:00",
        "quiet_end": "07:00",
    },
    # 记忆
    "memory": {
        "max_entries": 50,          # 滚动短期记忆（对话轮数）上限
        "storage_dir": os.path.join(ROOT_DIR, "data", "memories"),
        "promote_threshold": 30,    # 滚动记忆消息条数达到该值时触发摘要 → 核心记忆
        "core_memory_max": 50,      # 核心记忆条目上限（超出按重要度/时间衰减淘汰）
        "use_llm_summary": True,
    },
    # 角色
    "character": {
        "prompts_dir": os.path.join(ROOT_DIR, "prompts"),
        "default_character": "角色1",
        # 多用户独立角色分配：{"@friend:matrix.example.org": "角色2"}
        "user_map": {},
    },
    # 链接内容提取
    "link_fetch": {
        "enabled": True,
        "timeout": 10,
        "max_length": 2000,
        "user_agent": "Mozilla/5.0 (compatible; HanyanChat/1.0)",
    },
    # 聊天内命令
    "commands": {
        "enabled": True,
        "prefix": "/",
    },
    # WebUI（可选）
    "webui": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 5001,
        "username": "admin",
        "password": os.environ.get("HANYAN_WEBUI_PASSWORD", "CHANGE_ME"),
        "secret_key": os.environ.get("HANYAN_WEBUI_SECRET", "change-this-secret-key"),
    },
}

_config: Optional[dict] = None
_lock = threading.RLock()


def _get_config_path() -> str:
    return _CONFIG_PATH


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：以 base（默认配置）为骨架，override（磁盘配置）覆盖已有值，
    但保留 base 中 override 未提供的 key。返回新 dict，不修改入参。"""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load() -> dict:
    """加载配置。首次调用创建默认配置。磁盘配置会与默认配置深度合并，
    缺失的新 key 自动补全；损坏的配置文件会回退到默认配置而不是让进程崩溃。"""
    global _config
    with _lock:
        path = _get_config_path()
        if not os.path.exists(path):
            _config = deepcopy(_DEFAULT_CONFIG)
            save()
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
                if not isinstance(on_disk, dict):
                    raise ValueError("config.json 根节点不是对象")
                _config = _deep_merge(_DEFAULT_CONFIG, on_disk)
            except (json.JSONDecodeError, OSError, ValueError) as e:
                logger.error(
                    "Failed to load config.json (%s); falling back to defaults. "
                    "损坏的配置文件不会被覆盖，请手动检查。", e,
                )
                _config = deepcopy(_DEFAULT_CONFIG)
        return deepcopy(_config)


def save():
    """将当前配置原子写入文件（tmp + rename，避免写入中途崩溃导致文件损坏）。"""
    global _config
    with _lock:
        path = _get_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_config, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except OSError as e:
            logger.error("Failed to save config: %s", e)


def reload() -> dict:
    """热加载：重新从磁盘读取配置。"""
    return load()


def get(key: str, default=None):
    """获取配置值，支持点号分隔的嵌套 key。"""
    global _config
    with _lock:
        if _config is None:
            load()
        keys = key.split(".")
        val = _config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return deepcopy(val) if isinstance(val, (dict, list)) else val


def set(key: str, value):
    """设置配置值，支持点号分隔的嵌套 key。自动保存。"""
    global _config
    with _lock:
        if _config is None:
            load()
        keys = key.split(".")
        target = _config
        for k in keys[:-1]:
            if k not in target or not isinstance(target[k], dict):
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value
        save()


def reset():
    """重置为默认配置。"""
    global _config
    with _lock:
        _config = deepcopy(_DEFAULT_CONFIG)
        save()


def get_character_for_user(user_id: str) -> str:
    """多用户独立角色分配：根据 character.user_map 查找该用户应使用的角色名，
    找不到则回退默认角色。"""
    user_map = get("character.user_map", {}) or {}
    return user_map.get(user_id) or get("character.default_character", "角色1")


# 自动加载
load()
