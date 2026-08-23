"""Runtime bootstrap for non-CLI NanoCat execution."""

from __future__ import annotations

from typing import Any

__all__ = ["main", "run_service", "run_service_async"]


def __getattr__(name: str) -> Any:
    """Load launcher exports only when explicitly requested."""
    if name == "main":
        from nanocat.runtime.launcher import main

        return main
    if name == "run_service":
        from nanocat.runtime.launcher import run_service

        return run_service
    if name == "run_service_async":
        from nanocat.runtime.launcher import run_service_async

        return run_service_async
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
