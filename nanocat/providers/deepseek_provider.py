"""DeepSeek direct provider — bypasses LiteLLM for full control over thinking mode and reasoning chain."""

from __future__ import annotations

from typing import Any

import json_repair
from openai import AsyncOpenAI

from nanocat.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# Models known to support thinking mode (deepseek-v4 series).
_THINKING_MODELS = frozenset({"deepseek-v4-pro", "deepseek-v4-flash"})


def _supports_thinking(model: str) -> bool:
    """Return True if the model supports the thinking parameter."""
    return model in _THINKING_MODELS or model.startswith("deepseek-v4")


class DeepSeekProvider(LLMProvider):
    """Direct DeepSeek API provider.

    Key differences from the LiteLLM path:
    - ``thinking`` is sent via ``extra_body`` (not a top-level param) to
      control DeepSeek's native thinking mode.
    - ``reasoning_content`` is extracted from responses and preserved
      in message history so multi-turn tool-call conversations work.
    - Thinking mode ignores ``temperature`` / ``top_p`` (not sent).
    - ``frequency_penalty`` / ``presence_penalty`` are deprecated by
      DeepSeek and never sent.
    - The API is text-only: image blocks are replaced with a stable text path
      reference before sending (see ``supports_vision``).
    """

    # DeepSeek has no multimodal support; strip images to text proactively.
    supports_vision = False

    _DEFAULT_BASE_URL = "https://api.deepseek.com"

    @staticmethod
    def _strip_model(model: str) -> str:
        """Strip provider prefix so 'deepseek/deepseek-v4-pro' → 'deepseek-v4-pro'."""
        return model.split("/")[-1] if "/" in model else model

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "deepseek-v4-pro",
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = self._strip_model(default_model)
        self._client = AsyncOpenAI(
            api_key=api_key or "no-key",
            base_url=api_base or self._DEFAULT_BASE_URL,
            default_headers=extra_headers or {},
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = 4096,
        temperature: float | None = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        resolved_model = self._strip_model(model or self.default_model)

        # Text-only API: turn image blocks into stable [image: {path}] text first.
        messages = self._enforce_vision_policy(messages)

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": self._sanitize_empty_content(messages),
        }

        if max_tokens is not None:
            kwargs["max_tokens"] = max(1, max_tokens)

        # --- thinking mode ---
        thinking_enabled = _supports_thinking(resolved_model) and reasoning_effort is not None
        if thinking_enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            if reasoning_effort:
                # Map OpenAI-compatible values to DeepSeek's supported values.
                # low/medium → high, xhigh → max.
                _mapped = reasoning_effort
                if reasoning_effort in ("low", "medium"):
                    _mapped = "high"
                elif reasoning_effort == "xhigh":
                    _mapped = "max"
                kwargs["reasoning_effort"] = _mapped
        else:
            # Non-thinking mode: temperature is accepted.
            if reasoning_effort is not None:
                kwargs["reasoning_effort"] = reasoning_effort

        # temperature: only send in non-thinking mode (ignored otherwise).
        if not thinking_enabled and temperature is not None:
            kwargs["temperature"] = temperature

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        try:
            response = await self._client.chat.completions.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling DeepSeek API: {e}",
                finish_reason="error",
            )

    def _parse_response(self, response: Any) -> LLMResponse:
        if not response.choices:
            return LLMResponse(
                content="Error: DeepSeek returned empty choices.",
                finish_reason="error",
            )
        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or "stop"

        tool_calls: list[ToolCallRequest] = []
        for tc in msg.tool_calls or []:
            args = tc.function.arguments
            if isinstance(args, str):
                args = json_repair.loads(args)
            tool_calls.append(
                ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                )
            )

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        reasoning_content = getattr(msg, "reasoning_content", None) or None

        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content=reasoning_content,
        )

    def get_default_model(self) -> str:
        return self.default_model
