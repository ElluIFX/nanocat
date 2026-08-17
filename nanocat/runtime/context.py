"""Runtime-scoped configuration snapshot and dependency binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nanocat.config.schema import Config
from nanocat.runtime.paths import RuntimePaths


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Validated configuration plus paths captured at runtime construction."""

    config: Config
    paths: RuntimePaths
    revision: int = 1
    source: str | None = None

    @property
    def provider_view(self) -> Any:
        """Expose a future read-only provider view without changing Config yet."""
        return self.config.providers

    @property
    def tools_view(self) -> Any:
        """Expose a future read-only tool/security view without global lookup."""
        return self.config.tools


@dataclass
class RuntimeContext:
    """Runtime composition object kept free of concrete service imports."""

    config: Config
    bus: Any
    session_manager: Any
    cron: Any
    agent: Any
    channels: Any
    heartbeat: Any
    system_turns: Any | None = None
    paths: RuntimePaths | None = None
    config_snapshot: ConfigSnapshot | None = None
    supervisor: Any | None = None
    log_sink_id: int | None = None
    intervention: Any | None = None
    command_dispatcher: Any | None = None
