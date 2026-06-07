"""Tool registry for dynamic tool management."""

from __future__ import annotations

from typing import Any

from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.base import Tool


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

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

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
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            if fallback_skill_loader:
                skill = fallback_skill_loader.load_skill(name)
                if skill:
                    return f"Error: You can't execute the skill `{name}` as a tool. The description of the skill is:\n{skill}"
            return f"Error: Tool '{name}' not found."

        from nanobot.security import safety_bypass

        token = safety_bypass.set(bypass_safety_check) if bypass_safety_check else None

        try:
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)

            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _HINT
        finally:
            if token is not None:
                safety_bypass.reset(token)

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

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
