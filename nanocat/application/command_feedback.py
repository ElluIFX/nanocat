"""Uniform command result rendering for the legacy outbound boundary."""

from __future__ import annotations

from nanocat.core.commands import CommandResult


class CommandFeedback:
    """Render structured results without allowing handlers to touch channels."""

    @staticmethod
    def render(result: CommandResult) -> str:
        lines: list[str] = []
        if result.title:
            lines.append(result.title)
        if result.message:
            lines.append(result.message)
        if not result.ok and result.code:
            lines.append(f"Code: `{result.code}`")
        if result.usage:
            lines.append(f"Usage: `{result.usage}`")
        if result.examples:
            examples = "\n\n".join(f"- `{item}`" for item in result.examples)
            lines.append(f"Examples:\n\n{examples}")
        if result.suggestions:
            lines.append("Try: " + ", ".join(f"`{item}`" for item in result.suggestions))
        return "\n\n".join(lines) or ("Command completed." if result.ok else "Command failed.")
