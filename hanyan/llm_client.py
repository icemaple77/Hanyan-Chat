"""
Hanyan Chat — LLM 调用模块
纯 urllib，零外部依赖。支持两种后端：
- Ollama 原生协议（POST /api/chat）—— 本地 qwen3:8b 等模型走这条路
- OpenAI 兼容协议（POST /chat/completions）—— 配置了 llm.api_key 时自动切换
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from . import config

logger = logging.getLogger("hanyan.llm")


class LLMClient:
    """本地/远程 LLM 聊天客户端（Ollama 协议 或 OpenAI 兼容协议，自动判断）。"""

    def __init__(self):
        self._load_config()

    def _load_config(self):
        cfg = config.get("llm")
        self.base_url = cfg.get("base_url", "http://localhost:11434").rstrip("/")
        self.model = cfg.get("model", "qwen3:8b")
        self.temperature = cfg.get("temperature", 0.7)
        self.timeout = cfg.get("timeout", 120)
        self.api_key = cfg.get("api_key", "")
        self.think = cfg.get("think", False)

    def reload_config(self):
        """热加载配置。"""
        self._load_config()

    def _build_request(self, messages: list[dict], temperature: Optional[float], stream: bool):
        """根据是否配置了 api_key 决定走 OpenAI 兼容端点还是原生 Ollama 端点。
        两条路径的 payload/headers 形状不同，统一在这里构造，chat() 和 chat_stream()
        共用，避免出现「chat_stream 忘记按同样规则组 headers」这类问题。"""
        if self.api_key:
            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": stream,
                "temperature": temperature if temperature is not None else self.temperature,
                "max_tokens": 512,
                # SiliconFlow（以及其它同样支持混合推理的 OpenAI 兼容网关，比如 DeepSeek
                # 系列）用 enable_thinking 控制思考模式，和原生 Ollama 协议的 "think" 字段
                # 是同一个意图、不同字段名。之前只在原生协议分支加了 think:false，OpenAI
                # 兼容分支完全没管——如果模型（比如 DeepSeek-V4-Flash）默认开着思考模式，
                # 每次请求都会先生成一整段推理过程，而且这里是非流式调用（stream 由调用方
                # 决定，chat() 默认非流式），要等推理 + 正文全部生成完才返回一个字，这是
                # "感觉每次都要等一会"最常见的原因之一。不支持这个字段的网关会直接忽略，
                # 不影响兼容性。
                "enable_thinking": self.think,
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
        else:
            url = f"{self.base_url}/api/chat"
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": stream,
                # 关闭混合推理模型（Qwen3/3.5 等）的思考模式：这个 app 是纯聊天场景，
                # 不需要推理链，且 <think>...</think> 混进正文会破坏消息拆句和 JSON
                # 解析（提醒/记忆摘要都要求纯 JSON 输出）。旧模型/不支持该参数的模型
                # 会直接忽略这个字段，不影响兼容性。
                "think": self.think,
                "options": {
                    "temperature": temperature if temperature is not None else self.temperature,
                    "num_predict": 512,
                    "stop": ["<|im_end|>", "\nuser:"],
                },
            }
            headers = {"Content-Type": "application/json"}
        return url, payload, headers

    def chat(
        self,
        messages: list[dict],
        temperature: Optional[float] = None,
        stream: bool = False,
    ) -> Optional[str]:
        """
        发送聊天请求。

        Args:
            messages: [{"role": "...", "content": "..."}, ...]
            temperature: 覆盖默认 temperature
            stream: 是否流式返回（默认 False，一次返回完整结果）

        Returns:
            回复文本，失败返回降级文案（不会返回 None，调用方不需要额外判空）
        """
        url, payload, headers = self._build_request(messages, temperature, stream)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            logger.error("LLM HTTP %d: %s", e.code, body)
            return _fallback_reply()
        except urllib.error.URLError as e:
            logger.error("LLM connection failed: %s", e.reason)
            return _fallback_reply()
        except TimeoutError:
            logger.error("LLM timeout (%ds)", self.timeout)
            return _fallback_reply()
        except json.JSONDecodeError as e:
            logger.error("LLM JSON decode error: %s", e)
            return _fallback_reply()

        if self.api_key:
            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0].get("message", {}).get("content", "")
                if content:
                    return content.strip()
        else:
            if "message" in result and "content" in result["message"]:
                return result["message"]["content"].strip()

        logger.error("Unexpected LLM response: %s", json.dumps(result)[:200])
        return _fallback_reply()

    def chat_stream(self, messages: list[dict], temperature: Optional[float] = None):
        """
        流式调用（生成器，逐 token yield）。目前没有调用方在用（消息模板系统
        依赖完整回复做拆句），保留是为了后续做打字机效果的语音/文本流式发送。
        """
        url, payload, headers = self._build_request(messages, temperature, stream=True)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for line in resp:
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8").strip())
                    except json.JSONDecodeError:
                        continue
                    if "message" in chunk and "content" in chunk["message"]:
                        yield chunk["message"]["content"]
                    if chunk.get("done", False):
                        break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            logger.error("LLM stream HTTP %d: %s", e.code, body)
            yield _fallback_reply()
        except urllib.error.URLError as e:
            logger.error("LLM stream connection failed: %s", e.reason)
            yield _fallback_reply()
        except TimeoutError:
            logger.error("LLM stream timeout (%ds)", self.timeout)
            yield _fallback_reply()

    def health_check(self) -> bool:
        """检查 LLM 服务是否在线。"""
        try:
            with urllib.request.urlopen(self.base_url, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False


def _fallback_reply() -> str:
    """LLM 不可用时的降级回复。"""
    return "[嗯，我现在有点累，稍后再聊好吗？]"


# 全局单例
_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
