# -*- coding: utf-8 -*-
"""
DocMaster Agent - LLM 接口封装
统一封装 LLM 调用，兼容所有 OpenAI SDK 兼容的 API 平台：
  - Google Gemini (generativelanguage.googleapis.com)
  - 智谱 GLM (open.bigmodel.cn)
  - DeepSeek (api.deepseek.com)
  - 硅基流动 SiliconFlow (api.siliconflow.cn)
  - 通义千问 via 阿里云百炼
  - 任何 OpenAI 兼容 API

特性：
  - 多 Key 自动轮换：api_key 支持逗号分隔多个密钥，
    失效/过期时自动切换到下一个可用 Key
  - 429 限流自动重试（指数退避）
"""

import json
import time
from typing import Optional

from core.logger import logger
from core.schema import Message, Role, ToolCall


def parse_api_keys(api_key: str) -> list[str]:
    """
    解析 API Key 配置，支持逗号分隔的多 Key。

    示例：
      "sk-abc123"             → ["sk-abc123"]
      "sk-abc123, sk-def456"  → ["sk-abc123", "sk-def456"]
    """
    keys = [k.strip() for k in api_key.split(",") if k.strip()]
    return keys if keys else [""]


class LLM:
    """大语言模型调用封装（支持多 Key 自动 Failover）"""

    # Key 失效的错误码/关键词（触发切换到下一个 Key）
    _KEY_ERROR_SIGNALS = [
        "invalid api key", "invalid_api_key",
        "API_KEY_INVALID", "expired", "invalid x-goog-api-key",
        "PERMISSION_DENIED", "UNAUTHENTICATED",
        "unauthorized", "authentication",
    ]

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
        **kwargs,
    ):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "请安装 openai 库: pip install openai"
            )

        self.model = model
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

        # 解析多 Key
        self._api_keys = parse_api_keys(api_key)
        self._current_key_index = 0

        # 用第一个 Key 初始化客户端
        self.client = OpenAI(
            base_url=base_url,
            api_key=self._api_keys[self._current_key_index],
            timeout=self.timeout,
        )
        # 异步客户端延迟初始化（chat_stream 首次调用时创建）
        self._async_client = None

    def _switch_to_next_key(self) -> bool:
        """
        切换到下一个 API Key。

        Returns:
            True 如果成功切换，False 如果已经没有更多 Key 可用
        """
        if len(self._api_keys) <= 1:
            return False

        self._current_key_index = (
            (self._current_key_index + 1) % len(self._api_keys)
        )

        from openai import OpenAI
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self._api_keys[self._current_key_index],
            timeout=self.timeout,
        )
        # Key 切换后异步客户端需要重建
        self._async_client = None

        # 只显示 Key 的前8个字符用于调试
        key_preview = self._api_keys[self._current_key_index][:8] + "..."
        logger.warning(
            "   [LLM] 已切换到 Key #%d (%s)",
            self._current_key_index + 1, key_preview,
        )
        return True
    
    def _ensure_async_client(self):
        """延迟创建 AsyncOpenAI 客户端（chat_stream 专用）。"""
        if self._async_client is None:
            from openai import AsyncOpenAI
            self._async_client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self._api_keys[self._current_key_index],
                timeout=self.timeout,
            )
        return self._async_client

    async def chat_stream(self, messages: list[dict], tools=None):
        """
        异步流式调用 LLM（返回 AsyncStream，可 async for 消费）。

        错误处理策略（与 chat() 一致）：
          1. Key 失效 → 自动切换到下一个 Key（最多尝试所有 Key）
          2. 429 限流 → 指数退避重试（同一个 Key 最多 3 次）

        关键：必须使用 AsyncOpenAI 客户端。
        同步 OpenAI 的 .create(stream=True) 返回同步 Stream 对象，
        不能 await 也不能 async for，会直接 TypeError。
        """
        import asyncio as _asyncio

        kwargs = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        keys_tried = 0
        max_keys = len(self._api_keys)

        while keys_tried < max_keys:
            max_retries = 3
            for attempt in range(max_retries + 1):
                try:
                    client = self._ensure_async_client()
                    return await client.chat.completions.create(**kwargs)

                except Exception as e:
                    if self._is_key_error(e) and "429" not in str(e):
                        logger.warning(
                            "   [LLM-stream] Key 不可用: %s", str(e)[:80]
                        )
                        if self._switch_to_next_key():
                            keys_tried += 1
                            break
                        else:
                            raise

                    if "429" in str(e) and attempt < max_retries:
                        wait = 2 ** attempt * 5
                        logger.warning(
                            "   [LLM-stream] 触发限流，%d秒后自动重试 (%d/%d)...",
                            wait, attempt + 1, max_retries,
                        )
                        await _asyncio.sleep(wait)
                        continue

                    raise
            else:
                continue

        raise RuntimeError(
            f"所有 {max_keys} 个 API Key 均不可用，请检查配置或更换 Key"
        )

    def _is_key_error(self, error: Exception) -> bool:
        """判断错误是否是 Key 失效/过期类型（字符串匹配 + HTTP 状态码检查）"""
        # 检查 OpenAI 异常的 HTTP 状态码（401=未授权, 403=禁止访问）
        status_code = getattr(error, 'status_code', None)
        if status_code in (401, 403):
            return True
        error_str = str(error).lower()
        return any(sig.lower() in error_str for sig in self._KEY_ERROR_SIGNALS)

    def chat(
        self,
        messages: list[Message],
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[dict | str] = None,
    ) -> Message:
        """
        发送对话消息给 LLM，返回响应 Message。

        错误处理策略：
          1. Key 失效 → 自动切换到下一个 Key（最多尝试所有 Key）
          2. 429 限流 → 指数退避重试（同一个 Key 最多 3 次）

        Args:
            messages: 对话历史
            tools: 可用工具列表（OpenAI function calling 格式）

        Returns:
            LLM 回复的 Message（可能包含 tool_calls 或纯文本 content）
        """
        # 构建请求参数
        api_messages = [m.to_dict() for m in messages]
        kwargs = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        # 多 Key 轮换 + 429 重试
        keys_tried = 0
        max_keys = len(self._api_keys)

        while keys_tried < max_keys:
            max_retries = 3
            for attempt in range(max_retries + 1):
                try:
                    response = self.client.chat.completions.create(**kwargs)
                    # 成功！解析并返回
                    return self._parse_response(response)

                except Exception as e:
                    # Key 失效/过期 → 切换 Key
                    if self._is_key_error(e) and "429" not in str(e):
                        logger.warning(
                            "   [LLM] Key 不可用: %s", str(e)[:80]
                        )
                        if self._switch_to_next_key():
                            keys_tried += 1
                            break  # 跳出 retry 循环，用新 Key 重新开始
                        else:
                            raise  # 只有一个 Key，无法切换

                    # 429 限流 → 指数退避重试
                    if "429" in str(e) and attempt < max_retries:
                        wait = 2 ** attempt * 5  # 5s, 10s, 20s
                        logger.warning(
                            "   [LLM] 触发限流，%d秒后自动重试 (%d/%d)...",
                            wait, attempt + 1, max_retries,
                        )
                        time.sleep(wait)
                        continue

                    # 其他错误或重试耗尽 → 直接抛出
                    raise
            else:
                # retry 循环正常结束（未 break） → 不应该到这里
                continue

        # 所有 Key 都试过了
        raise RuntimeError(
            f"所有 {max_keys} 个 API Key 均不可用，请检查配置或更换 Key"
        )

    @staticmethod
    def _parse_response(response) -> Message:
        """解析 API 响应为 Message"""
        choice = response.choices[0]
        resp_msg = choice.message

        # 解析工具调用
        tool_calls = []
        if resp_msg.tool_calls:
            for tc in resp_msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {"raw": tc.function.arguments}

                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=args,
                    )
                )

        return Message(
            role=Role.ASSISTANT,
            content=resp_msg.content,
            tool_calls=tool_calls,
        )

    def test_connection(self) -> str:
        """测试 LLM 连通性，返回模型回复"""
        msg = Message(role=Role.USER, content="你好，请回复'连接成功'四个字。")
        resp = self.chat([msg])
        return resp.content or "(无回复)"
