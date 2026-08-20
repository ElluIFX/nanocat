"""Tool registry for dynamic tool management."""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any

from nanocat.agent.skills import SkillsLoader
from nanocat.agent.tools.base import Tool, tool_err
from nanocat.core.runtime import CapabilityDescriptor

if TYPE_CHECKING:
    from nanocat.application.tool_executor import ToolExecutionContext


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self, locks: MutableMapping[str, asyncio.Lock] | None = None):
        self._tools: dict[str, Tool] = {}
        self._locks = locks if locks is not None else {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    def get_descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        """Return immutable capability metadata without exposing tool instances."""
        return tuple(tool.descriptor for tool in self._tools.values())

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        fallback_skill_loader: SkillsLoader | None = None,
        authorization: Any | None = None,
        *,
        execution_context: ToolExecutionContext | None = None,
    ) -> str:
        """Execute a tool by name with given parameters."""
        retry_hint = "Analyze the error and try a different approach."

        tool = self._tools.get(name)
        if not tool:
            if fallback_skill_loader:
                skill = fallback_skill_loader.load_skill(name)
                if skill:
                    return tool_err(
                        f"'{name}' is a skill, not a tool, and cannot be executed as one.",
                        hint=f"Skill description:\n{skill}",
                    )
            return tool_err(f"Tool '{name}' not found.")

        try:
            if "_security_authorization" in params:
                return tool_err("reserved security authorization parameter is not accepted")
            if authorization is not None and getattr(authorization, "tool_name", name) not in {
                "",
                name,
            }:
                return tool_err("security authorization does not match the requested tool")
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)

            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                return tool_err(
                    f"Invalid parameters for tool '{name}': " + "; ".join(errors),
                    hint=retry_hint,
                )
            # Authorization is an execution concern and must never cross the tool boundary.
            # External adapters receive only the validated tool parameters.
            async def _execute_bound() -> str:
                self._bind_context(tool, execution_context)
                result = await tool.execute(**params)
                if execution_context is not None:
                    tool.record_execution(execution_context, params, result)
                return result

            if tool.name != "todo":
                return await _execute_bound()

            lock_key = f"todo:{execution_context.session_key}" if execution_context else "todo"
            lock = self._locks.setdefault(lock_key, asyncio.Lock())
            async with lock:
                return await _execute_bound()
        except Exception as e:
            return tool_err(f"Error executing {name}: {str(e)}", hint=retry_hint)

    @staticmethod
    def _bind_context(tool: Tool, context: ToolExecutionContext | None) -> None:
        """Bind legacy routing fields only while the registry owns this call."""
        if context is None or not hasattr(tool, "set_context"):
            return

        channel = context.conversation.channel
        chat_id = context.conversation.chat_id
        if tool.name == "subagent_spawn":
            tool.set_context(channel, chat_id, context.principal_id)  # type: ignore[attr-defined]
        elif tool.name == "message":
            tool.set_context(channel, chat_id, context.message_id)  # type: ignore[attr-defined]
        elif tool.name == "todo":
            tool.set_context(channel, chat_id, context.session)  # type: ignore[attr-defined]
        elif tool.name == "cron":
            tool.set_context(channel, chat_id, context.principal_id)  # type: ignore[attr-defined]
        else:
            tool.set_context(channel, chat_id)  # type: ignore[attr-defined]

        if hasattr(tool, "set_session_key"):
            tool.set_session_key(context.session_key)  # type: ignore[attr-defined]

    def filtered(self, exclude: frozenset[str] | set[str]) -> ToolRegistry:
        """Return a new registry with the same tool instances except those in *exclude*."""
        clone = ToolRegistry(self._locks)
        for name, tool in self._tools.items():
            if name not in exclude:
                clone._tools[name] = tool
        return clone

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
