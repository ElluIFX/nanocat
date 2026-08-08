"""Runtime bootstrap for non-CLI NanoCat execution."""

from __future__ import annotations

from typing import Any

__all__ = ["main", "run_gateway"]


def __getattr__(name: str) -> Any:
    """Load the legacy launcher facade only when it is explicitly requested."""
    if name == "main":
        from nanocat.runtime.launcher import main

        return main
    if name == "run_gateway":
        from nanocat.runtime.launcher import run_gateway

        return run_gateway
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
