"""Immutable path bindings for one NanoCat runtime instance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """All persistent and transient runtime paths derived from one config file."""

    config_file: Path
    data_dir: Path
    workspace: Path
    sessions_dir: Path
    cron_dir: Path
    logs_dir: Path
    media_dir: Path
    restart_notification: Path

    @classmethod
    def from_config_path(cls, config_file: Path, workspace: Path | None = None) -> "RuntimePaths":
        """Build paths without creating directories or consulting global state."""
        config_file = config_file.expanduser().resolve()
        data_dir = config_file.parent
        workspace = workspace or data_dir / "workspace"
        workspace = workspace.expanduser().resolve()
        return cls(
            config_file=config_file,
            data_dir=data_dir,
            workspace=workspace,
            sessions_dir=data_dir / "sessions",
            cron_dir=data_dir / "cron",
            logs_dir=data_dir / "logs",
            media_dir=workspace / "media",
            restart_notification=data_dir / ".pending_restart_notify.json",
        )
