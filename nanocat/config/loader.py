"""Configuration loading utilities."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nanocat.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None

# Global runtime config — set once at startup, then read by all subsystems.
_runtime_config: Config | None = None


def _unknown_config_paths(raw: Any, parsed: Any, prefix: str = "") -> list[str]:
    """Find config keys ignored by the validated schema before normalization."""
    if not isinstance(raw, dict) or not isinstance(parsed, BaseModel):
        return []
    if parsed.__class__.__name__ == "ChannelsConfig":
        return []

    fields = parsed.model_fields
    aliases = {
        alias
        for field in fields.values()
        if (alias := field.alias) is not None
    }
    accepted = set(fields) | aliases
    unknown = [
        f"{prefix}.{key}" if prefix else str(key)
        for key in raw
        if key not in accepted
    ]
    paths = list(unknown)
    for field_name, field in fields.items():
        raw_key = field.alias if field.alias in raw else field_name
        if raw_key not in raw:
            continue
        value = getattr(parsed, field_name, None)
        raw_value = raw[raw_key]
        if isinstance(value, BaseModel):
            nested_prefix = f"{prefix}.{raw_key}" if prefix else raw_key
            paths.extend(_unknown_config_paths(raw_value, value, nested_prefix))
        elif isinstance(value, dict) and isinstance(raw_value, dict):
            for item_key, item_value in value.items():
                item = raw_value.get(item_key)
                if isinstance(item, dict) and isinstance(item_value, BaseModel):
                    item_prefix = f"{prefix}.{raw_key}.{item_key}" if prefix else f"{raw_key}.{item_key}"
                    paths.extend(_unknown_config_paths(item, item_value, item_prefix))
    return paths


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
            config = Config.model_validate(data)
            unknown_paths = _unknown_config_paths(data, config)
            if unknown_paths:
                print(
                    "Warning: Unsupported config keys will be discarded when saved: "
                    + ", ".join(sorted(unknown_paths))
                )
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
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
