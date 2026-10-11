from __future__ import annotations

from typing import Any

from nanocat.providers.base import LLMResponse
from nanocat.providers.custom_provider import CustomProvider


class KimiProvider(CustomProvider):
    """Use the configured Kimi billing endpoint with OpenAI-compatible messages."""

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
        effort = {"medium": "high", "xhigh": "max"}.get(reasoning_effort, reasoning_effort)
        messages = self._sanitize_request_messages(
            messages,
            frozenset(
                {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"}
            ),
        )
        return await super().chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=effort,
            tool_choice=tool_choice,
        )
