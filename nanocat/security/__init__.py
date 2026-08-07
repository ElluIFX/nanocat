"""Security authorization contracts shared by policy and tool adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolAuthorization:
    """Explicit authorization attached to exactly one approved tool call."""

    capability: str
    call_fingerprint: str
    scope: str = "once"
    tool_name: str = ""


__all__ = ["ToolAuthorization"]
