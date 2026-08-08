"""Fast, local-only context budgeting and emergency history trimming."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nanocat.utils.helpers import estimate_prompt_tokens_fast


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    estimated_tokens: int
    usable_tokens: int
    target_tokens: int
    over_budget: bool


class ContextBudget:
    def __init__(self, config: Any):
        self._config = config
        defaults = config.agents.defaults
        self.context_window = max(0, int(defaults.context_window_tokens))
        configured_output = defaults.max_tokens
        output_reserve = int(configured_output or 4096)
        self.output_reserve = max(4096, output_reserve)
        self.safety_margin = max(2048, int(self.context_window * 0.05))

    def inspect(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> BudgetSnapshot:
        estimated = estimate_prompt_tokens_fast(messages, tools)
        usable = max(1024, self.context_window - self.output_reserve - self.safety_margin)
        target = max(4096, int(usable * 0.45))
        return BudgetSnapshot(estimated, usable, target, estimated > usable)

    @staticmethod
    def _message_size(message: dict[str, Any]) -> int:
        content = message.get("content")
        if isinstance(content, str):
            return len(content)
        return len(str(content or "")) + len(str(message.get("tool_calls") or ""))

    @staticmethod
    def _is_checkpoint(message: dict[str, Any]) -> bool:
        content = message.get("content")
        return isinstance(content, str) and (
            "<SESSION-CHECKPOINT>" in content or "<COMPACTED-MEMORY>" in content
        )

    def trim(self, messages: list[dict[str, Any]], target_tokens: int) -> list[dict[str, Any]]:
        """Keep system/checkpoint context and the newest complete user groups."""
        if len(messages) <= 2:
            return messages

        prefix: list[dict[str, Any]] = []
        start = 0
        if messages and messages[0].get("role") == "system":
            prefix.append(messages[0])
            start = 1
        if start < len(messages) and self._is_checkpoint(messages[start]):
            prefix.append(messages[start])
            start += 1

        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        previous_role: str | None = None
        for message in messages[start:]:
            role = message.get("role")
            if role == "user" and current and previous_role != "tool":
                groups.append(current)
                current = []
            current.append(message)
            previous_role = role
        if current:
            groups.append(current)
        if not groups:
            return messages

        budget_chars = max(4096, target_tokens * 4)
        selected: list[list[dict[str, Any]]] = []
        used_chars = sum(self._message_size(message) for message in prefix)
        for group in reversed(groups):
            group_chars = sum(self._message_size(message) for message in group)
            if selected and used_chars + group_chars > budget_chars:
                break
            selected.append(group)
            used_chars += group_chars
        selected.reverse()

        result = [*prefix, *(message for group in selected for message in group)]
        if result == messages:
            return result
        return self._legalize(result)

    @staticmethod
    def _legalize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        declared: set[str] = set()
        output: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                calls = message.get("tool_calls") or []
                for call in calls:
                    if isinstance(call, dict) and call.get("id"):
                        declared.add(str(call["id"]))
                output.append(message)
                continue
            if role == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if call_id not in declared:
                    continue
                output.append(message)
                continue
            output.append(message)
        return output
