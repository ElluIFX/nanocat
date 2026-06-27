"""Tool registry for dynamic tool management."""

from __future__ import annotations

from typing import Any

from nanocat.agent.skills import SkillsLoader
from nanocat.agent.tools.base import Tool, tool_err


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        bypass_safety_check: bool = False,
        fallback_skill_loader: SkillsLoader | None = None,
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

        from nanocat.security import safety_bypass

        token = safety_bypass.set(bypass_safety_check) if bypass_safety_check else None

        try:
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)

            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                return tool_err(
                    f"Invalid parameters for tool '{name}': " + "; ".join(errors),
                    hint=retry_hint,
                )
            # Tools self-report success/failure in their JSON envelope; pass through.
            return await tool.execute(**params)
        except Exception as e:
            return tool_err(f"Error executing {name}: {str(e)}", hint=retry_hint)
        finally:
            if token is not None:
                safety_bypass.reset(token)

    def filtered(self, exclude: frozenset[str] | set[str]) -> ToolRegistry:
        """Return a new registry with the same tool instances except those in *exclude*."""
        clone = ToolRegistry()
        for name, tool in self._tools.items():
            if name not in exclude:
                clone._tools[name] = tool
        return clone

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
