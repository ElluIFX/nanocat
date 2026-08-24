"""Configuration loading utilities."""

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from nanocat.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None

# Global runtime config — set once at startup, then read by all subsystems.
_runtime_config: Config | None = None


class ConfigLoadError(RuntimeError):
    """Raised when an existing configuration cannot be used safely."""


_SPECIAL_ENV_PATHS = {
    "NANOCAT_WEB_HOST": "channels.web.host",
    "NANOCAT_WEB_PORT": "channels.web.port",
}


def persisted_defaults_payload() -> dict[str, Any]:
    """Materialize schema defaults without consulting BaseSettings sources."""
    return Config.model_construct().model_dump(by_alias=True, mode="json")


def validate_persisted_config(raw: Mapping[str, Any]) -> Config:
    """Validate file-backed values after filling every field from pure defaults."""
    payload = persisted_defaults_payload()

    def overlay(target: dict[str, Any], values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            current = target.get(key)
            if isinstance(current, dict) and isinstance(value, Mapping):
                overlay(current, value)
            else:
                target[key] = deepcopy(value)

    overlay(payload, raw)
    return Config.model_validate(payload)


def _normalized_env_segment(value: str) -> str:
    return value.casefold().replace("-", "").replace("_", "")


def _payload_path(payload: Mapping[str, Any], segments: list[str]) -> tuple[str, ...] | None:
    current: Any = payload
    resolved: list[str] = []
    for segment in segments:
        if not isinstance(current, Mapping):
            return None
        normalized = _normalized_env_segment(segment)
        key = next(
            (
                str(candidate)
                for candidate in current
                if _normalized_env_segment(str(candidate)) == normalized
            ),
            None,
        )
        if key is None:
            return None
        resolved.append(key)
        current = current[key]
    return tuple(resolved)


def _get_payload_path(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for part in path:
        current = current[part]
    return current


def _set_payload_path(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = payload
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = deepcopy(value)


def _provided_payload_values(
    effective: Mapping[str, Any],
    provided: Mapping[str, Any],
    prefix: tuple[str, ...],
) -> dict[str, Any]:
    """Project explicitly supplied JSON keys onto the parsed environment view."""
    values: dict[str, Any] = {}
    for raw_key, raw_value in provided.items():
        resolved = _payload_path(effective, [str(raw_key)])
        if resolved is None:
            continue
        key = resolved[0]
        effective_value = effective[key]
        path = (*prefix, key)
        if isinstance(raw_value, Mapping) and isinstance(effective_value, Mapping):
            values.update(_provided_payload_values(effective_value, raw_value, path))
        else:
            values[".".join(path)] = deepcopy(effective_value)
    return values


def environment_override_paths() -> dict[str, Any]:
    """Return effective environment overrides using canonical config paths."""
    env_config = Config()
    env_payload = env_config.model_dump(by_alias=True, mode="json")
    overrides: dict[str, Any] = {}
    for name in os.environ:
        if not name.startswith("NANOCAT_") or name in _SPECIAL_ENV_PATHS:
            continue
        suffix = name.removeprefix("NANOCAT_")
        if "__" in suffix:
            path = _payload_path(env_payload, suffix.split("__"))
            if path is not None:
                overrides[".".join(path)] = deepcopy(_get_payload_path(env_payload, path))
            continue
        path = _payload_path(env_payload, [suffix])
        if path is None:
            continue
        effective_value = _get_payload_path(env_payload, path)
        try:
            provided = json.loads(os.environ[name])
        except json.JSONDecodeError:
            provided = os.environ[name]
        if isinstance(provided, Mapping) and isinstance(effective_value, Mapping):
            overrides.update(
                _provided_payload_values(effective_value, provided, path)
            )
        else:
            overrides[".".join(path)] = deepcopy(effective_value)
    web_host = os.environ.get("NANOCAT_WEB_HOST", "").strip()
    if web_host:
        overrides["channels.web.host"] = web_host
    web_port = os.environ.get("NANOCAT_WEB_PORT", "").strip()
    if web_port:
        try:
            overrides["channels.web.port"] = int(web_port)
        except ValueError as exc:
            raise ValueError("NANOCAT_WEB_PORT must be an integer") from exc
    return overrides


def apply_environment_overrides(config: Config) -> Config:
    """Overlay process-local environment settings without persisting them."""
    payload = config.model_dump(by_alias=True, mode="json")
    for dotted_path, value in environment_override_paths().items():
        path = tuple(dotted_path.split("."))
        if _payload_path(payload, list(path)) == path:
            _set_payload_path(payload, path, value)
    return validate_persisted_config(payload)


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path.

    Defaults to ``./config.json`` in the current working directory. Runtime
    composition binds this path from the selected workdir before loading.
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
            if not isinstance(data, dict):
                raise ValueError("configuration root must be a JSON object")
            config = validate_persisted_config(data)
            return apply_environment_overrides(config)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ConfigLoadError(f"Failed to load existing config from {path}: {exc}") from exc

    return apply_environment_overrides(validate_persisted_config({}))


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
