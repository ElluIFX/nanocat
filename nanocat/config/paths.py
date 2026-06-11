"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from nanocat.config.loader import get_config_path
from nanocat.utils.helpers import ensure_dir


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory under the agent workspace, optionally per channel."""
    from nanocat.config.loader import get_runtime_config

    base = ensure_dir(get_runtime_config().workspace_path / "media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_runtime_subdir("logs")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanocat" / "workspace"
    return ensure_dir(path)


def get_restart_notify_path() -> Path:
    """Return the path to the pending post-restart notification file."""
    return get_data_dir() / ".pending_restart_notify.json"


def get_sessions_dir() -> Path:
    """Return the sessions storage directory."""
    return get_runtime_subdir("sessions")


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".nanocat" / "sessions"
