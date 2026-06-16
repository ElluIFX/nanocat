"""Configuration loading utilities."""

import json
from pathlib import Path

from nanocat.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None

# Global runtime config — set once at startup, then read by all subsystems.
_runtime_config: Config | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path.

    Defaults to ``./config.json`` in the current working directory; pass
    ``--config`` to point elsewhere (which relocates all derived runtime dirs).
    """
    if _current_config_path:
        return _current_config_path
    return Path.cwd() / "config.json"


def set_runtime_config(config: Config) -> None:
    """Store the active runtime configuration for global access."""
    global _runtime_config
    _runtime_config = config


def get_runtime_config() -> Config:
    """Return the active runtime configuration.

    Loads from disk if not yet set (lazy init for early imports).
    """
    global _runtime_config
    if _runtime_config is None:
        _runtime_config = load_config()
    return _runtime_config


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            config = Config.model_validate(data)
            save_config(config, path)
            return config
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    fs_cfg = tools.setdefault("filesystem", {})

    # Move tools.exec.restrictToWorkspace → tools.filesystem.restrictToWorkspace
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in fs_cfg:
        fs_cfg["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Move tools.restrictToWorkspace → tools.filesystem.restrictToWorkspace
    if "restrictToWorkspace" in tools and "restrictToWorkspace" not in fs_cfg:
        fs_cfg["restrictToWorkspace"] = tools.pop("restrictToWorkspace")
    elif "restrictToWorkspace" in tools:
        tools.pop("restrictToWorkspace")

    return data
