"""Transactional persisted-configuration service for HTTP settings clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from nanocat.config.loader import (
    environment_override_paths,
    persisted_defaults_payload,
    save_config,
    validate_persisted_config,
)
from nanocat.config.schema import Config
from nanocat.observability.redaction import redact_value

_READ_ONLY_PATHS = frozenset({"schemaVersion"})
_NULLABLE_EMPTY_PATHS = frozenset(
    {
        "api.authToken",
        "channels.web.password",
        "memory.apiKey",
    }
)

_RESTART_PATHS = frozenset(
    {
        "api.enabled",
        "api.host",
        "api.port",
        "channels.web.enabled",
        "channels.web.host",
        "channels.web.port",
    }
)
_RECONNECT_PREFIXES = (
    "providers.",
    "channels.",
    "tools.mcpServers.",
    "memory.",
)


def setting_apply_mode(path: str) -> str:
    """Return the narrowest runtime boundary required by one setting path."""
    if path in _RESTART_PATHS:
        return "restart"
    if path.startswith("channels.web.") or path in {
        "channels.sendProgress",
        "channels.sendToolHints",
        "channels.outboundMaxAttempts",
        "channels.outboundRetryDelayS",
    }:
        return "live"
    if path == "tools.mcpServers" or path.startswith(_RECONNECT_PREFIXES):
        return "reconnect"
    if path.startswith(("agents.", "runtime.", "runtimeFiles.", "heartbeat.", "tools.", "transcription.")):
        return "next_turn"
    return "live"


def classify_setting_paths(paths: tuple[str, ...] | list[str]) -> dict[str, list[str]]:
    """Group changed paths by their documented runtime application boundary."""
    grouped = {
        "appliedPaths": [],
        "nextTurnPaths": [],
        "reconnectedPaths": [],
        "restartRequiredPaths": [],
    }
    targets = {
        "live": "appliedPaths",
        "next_turn": "nextTurnPaths",
        "reconnect": "reconnectedPaths",
        "restart": "restartRequiredPaths",
    }
    for path in paths:
        grouped[targets[setting_apply_mode(path)]].append(path)
    return grouped


def restart_setting_paths() -> list[str]:
    """Return the exact listener-topology settings that require process restart."""
    return sorted(_RESTART_PATHS)


class ConfigurationError(RuntimeError):
    """Base error for persisted configuration operations."""


class ConfigurationConflictError(ConfigurationError):
    """Raised when a caller updates a stale configuration revision."""


class ConfigurationValidationError(ConfigurationError):
    """Raised when a requested update does not form a valid Config."""


@dataclass(frozen=True, slots=True)
class ConfigurationState:
    """One validated persisted snapshot and its stable revision."""

    config: Config
    revision: int


class ConfigurationService:
    """Validate and atomically replace the workdir configuration."""

    def __init__(
        self,
        path: Path,
        *,
        effective_config: Config,
        channel_defaults: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._path = path.resolve()
        self._effective_config = effective_config
        self._channel_defaults = {
            str(name): deepcopy(dict(values))
            for name, values in (channel_defaults or {}).items()
            if str(name).strip()
        }
        enriched = self._materialize_channel_defaults(effective_config)
        self._effective_config.channels = enriched.channels
        self._lock = asyncio.Lock()
        self._runtime_applier: (
            Callable[[Config, tuple[str, ...]], Awaitable[None]] | None
        ) = None

    def set_runtime_applier(
        self,
        applier: Callable[[Config, tuple[str, ...]], Awaitable[None]],
    ) -> None:
        """Bind the composition-root callback that updates live resource owners."""
        self._runtime_applier = applier

    @staticmethod
    def _revision(config: Config) -> int:
        payload = json.dumps(
            config.model_dump(by_alias=True, mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
        return value & ((1 << 53) - 1) or 1

    @staticmethod
    def _persisted_defaults_payload() -> dict[str, Any]:
        """Materialize schema defaults without consulting BaseSettings sources."""
        return persisted_defaults_payload()

    @classmethod
    def _validate_persisted(cls, raw: Mapping[str, Any]) -> Config:
        """Validate persisted values after filling missing keys from pure defaults."""
        return validate_persisted_config(raw)

    @classmethod
    def _persisted_defaults(cls) -> Config:
        """Build validated schema defaults without consulting BaseSettings sources."""
        return cls._validate_persisted({})

    @staticmethod
    def _overlay_missing(target: dict[str, Any], defaults: Mapping[str, Any]) -> None:
        for key, value in defaults.items():
            current = target.get(key)
            if isinstance(current, dict) and isinstance(value, Mapping):
                ConfigurationService._overlay_missing(current, value)
            elif key not in target:
                target[key] = deepcopy(value)

    def _materialize_channel_defaults(self, config: Config) -> Config:
        if not self._channel_defaults:
            return config
        payload = config.model_dump(by_alias=True, mode="json")
        channels = payload.setdefault("channels", {})
        if not isinstance(channels, dict):
            raise ConfigurationValidationError("Channels configuration must be an object")
        for name, defaults in self._channel_defaults.items():
            section = channels.setdefault(name, {})
            if not isinstance(section, dict):
                continue
            self._overlay_missing(section, defaults)
        return Config.model_validate(payload)

    def _read_sync(self) -> ConfigurationState:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("configuration root must be a JSON object")
                config = self._materialize_channel_defaults(self._validate_persisted(raw))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ConfigurationError(
                    f"Unable to read the persisted configuration: {exc}"
                ) from exc
        else:
            config = self._materialize_channel_defaults(self._persisted_defaults())
        return ConfigurationState(config=config, revision=self._revision(config))

    @staticmethod
    def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
        if path in _READ_ONLY_PATHS:
            raise ConfigurationValidationError(f"Setting `{path}` is read-only")
        parts = path.split(".")
        if not parts or any(not part or part.startswith("_") for part in parts):
            raise ConfigurationValidationError(f"Setting path `{path}` is invalid")
        cursor: Any = payload
        for part in parts[:-1]:
            if not isinstance(cursor, dict) or part not in cursor:
                raise ConfigurationValidationError(f"Unknown setting `{path}`")
            cursor = cursor[part]
        leaf = parts[-1]
        if not isinstance(cursor, dict) or leaf not in cursor:
            raise ConfigurationValidationError(f"Unknown setting `{path}`")
        cursor[leaf] = None if path in _NULLABLE_EMPTY_PATHS and value == "" else value

    def _runtime_overrides(self) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        for path, value in environment_override_paths().items():
            normalized = path.casefold()
            if any(
                marker in normalized
                for marker in (
                    "key",
                    "token",
                    "password",
                    "secret",
                    "credential",
                    "cookie",
                    "headers",
                    ".env",
                )
            ):
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                overrides[path] = {
                    "configured": value not in (None, "", {}, []),
                    "fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12],
                }
                continue
            overrides[path] = redact_value(value)
        return overrides

    @staticmethod
    async def _finish_thread_call(
        function: Any,
        *args: Any,
        on_complete: Callable[[Any], None] | None = None,
    ) -> Any:
        """Finish a worker and publish its committed result before cancellation."""
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    if task.cancelled():
                        raise RuntimeError("runtime configuration owner was cancelled")
                    result = task.result()
                    break
        if on_complete is not None:
            on_complete(result)
        if cancelled:
            raise asyncio.CancelledError
        return result

    @staticmethod
    async def _finish_async_call(awaitable: Awaitable[Any]) -> Any:
        """Let an owner transition reach a deterministic terminal state on cancellation."""
        task = asyncio.create_task(awaitable)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    result = task.result()
                    break
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def snapshot(self) -> dict[str, Any]:
        """Return the current persisted settings and concurrency metadata."""
        async with self._lock:
            state = await self._finish_thread_call(self._read_sync)
        return {
            "config": state.config,
            "revision": state.revision,
            "readOnlyPaths": sorted(_READ_ONLY_PATHS),
            "effectiveOverrides": self._runtime_overrides(),
        }

    def _update_sync(
        self,
        values: Mapping[str, Any],
        expected_revision: int | None,
    ) -> tuple[ConfigurationState, tuple[str, ...]]:
        current = self._read_sync()
        if expected_revision is not None and current.revision != expected_revision:
            raise ConfigurationConflictError(
                "The configuration changed after it was loaded; refresh and retry"
            )
        state, changed_paths = self._build_update(current, values)
        save_config(state.config, self._path)
        return state, changed_paths

    def _build_update(
        self,
        current: ConfigurationState,
        values: Mapping[str, Any],
    ) -> tuple[ConfigurationState, tuple[str, ...]]:
        """Validate path updates against one already-read configuration state."""
        payload = current.config.model_dump(by_alias=True, mode="json")
        changed_paths: list[str] = []
        for path, value in values.items():
            if not isinstance(path, str):
                raise ConfigurationValidationError("Setting paths must be strings")
            self._set_path(payload, path, value)
            changed_paths.append(path)
        try:
            updated = Config.model_validate(payload)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors(include_url=False)
            )
            raise ConfigurationValidationError(detail or "Configuration is invalid") from exc
        return (
            ConfigurationState(config=updated, revision=self._revision(updated)),
            tuple(changed_paths),
        )

    def _prepare_runtime_update_sync(
        self,
        values: Mapping[str, Any],
    ) -> tuple[ConfigurationState, tuple[str, ...], Config]:
        """Validate persisted and effective runtime views before committing either."""
        current = self._read_sync()
        state, changed_paths = self._build_update(current, values)
        effective_state = ConfigurationState(
            config=self._effective_config,
            revision=self._revision(self._effective_config),
        )
        effective, _ = self._build_update(effective_state, values)
        save_config(state.config, self._path)
        return state, changed_paths, effective.config

    def _mutate_runtime_sync(
        self,
        mutator: Callable[[Config], Mapping[str, Any]],
    ) -> tuple[ConfigurationState, tuple[str, ...], Config]:
        """Derive and commit a runtime update from the latest persisted state."""
        current = self._read_sync()
        values = dict(mutator(current.config.model_copy(deep=True)))
        if not values:
            return current, (), self._effective_config.model_copy(deep=True)
        state, changed_paths = self._build_update(current, values)
        effective_state = ConfigurationState(
            config=self._effective_config,
            revision=self._revision(self._effective_config),
        )
        effective, _ = self._build_update(effective_state, values)
        save_config(state.config, self._path)
        return state, changed_paths, effective.config

    def _replace_effective_roots(
        self,
        updated: Config,
        changed_paths: tuple[str, ...],
    ) -> None:
        """Replace only runtime-updated root sections and retain environment overrides."""
        roots = {path.split(".", 1)[0] for path in changed_paths}
        for name, field in Config.model_fields.items():
            alias = field.alias or name
            if alias in roots:
                setattr(self._effective_config, name, getattr(updated, name))

    @staticmethod
    def _get_path(payload: Mapping[str, Any], path: str) -> Any:
        cursor: Any = payload
        for part in path.split("."):
            if not isinstance(cursor, Mapping) or part not in cursor:
                raise ConfigurationValidationError(f"Unknown setting `{path}`")
            cursor = cursor[part]
        return cursor

    def _apply_effective_paths(
        self,
        updated: Config,
        changed_paths: tuple[str, ...],
    ) -> None:
        """Apply non-topology fields while retaining environment overrides."""
        override_paths = set(environment_override_paths())
        runtime_paths = tuple(
            path
            for path in changed_paths
            if setting_apply_mode(path) != "restart" and path not in override_paths
        )
        if not runtime_paths:
            return
        payload = self._effective_config.model_dump(by_alias=True, mode="json")
        source = updated.model_dump(by_alias=True, mode="json")
        for path in runtime_paths:
            self._set_path(payload, path, self._get_path(source, path))
        effective = Config.model_validate(payload)
        self._replace_effective_roots(effective, runtime_paths)

    async def update(
        self,
        values: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Apply a compare-and-swap update and atomically persist it."""
        if not values:
            raise ConfigurationValidationError("At least one setting is required")
        async with self._lock:
            previous = await self._finish_thread_call(self._read_sync)
            previous_effective = self._effective_config.model_copy(deep=True)
            state, changed_paths = await self._finish_thread_call(
                self._update_sync,
                dict(values),
                expected_revision,
            )
            self._apply_effective_paths(state.config, changed_paths)
            try:
                if self._runtime_applier is not None:
                    await self._finish_async_call(
                        self._runtime_applier(self._effective_config, changed_paths)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._finish_thread_call(save_config, previous.config, self._path)
                self._replace_effective_roots(previous_effective, changed_paths)
                if self._runtime_applier is not None:
                    try:
                        await self._finish_async_call(
                            self._runtime_applier(previous_effective, changed_paths)
                        )
                    except BaseException:
                        pass
                raise ConfigurationError(
                    f"Unable to apply the runtime configuration: {type(exc).__name__}"
                ) from exc
        classified = classify_setting_paths(changed_paths)
        return {
            "config": state.config,
            "revision": state.revision,
            "changedPaths": list(changed_paths),
            **classified,
            "restartRequired": bool(classified["restartRequiredPaths"]),
            "effectiveOverrides": self._runtime_overrides(),
        }

    async def update_runtime(
        self,
        values: Mapping[str, Any],
        *,
        on_applied: Callable[[Config], None] | None = None,
    ) -> int:
        """Merge runtime-owned fields into the latest persisted snapshot.

        Runtime actions do not hold a browser revision, but they must share the
        same serialization boundary so they cannot replace unrelated settings
        with a stale startup snapshot.
        """
        if not values:
            raise ConfigurationValidationError("At least one setting is required")
        async with self._lock:
            applied: tuple[ConfigurationState, tuple[str, ...], Config] | None = None

            def apply_result(result: Any) -> None:
                nonlocal applied
                state, changed_paths, effective = result
                self._replace_effective_roots(effective, changed_paths)
                if on_applied is not None:
                    on_applied(self._effective_config)
                applied = (state, changed_paths, effective)

            await self._finish_thread_call(
                self._prepare_runtime_update_sync,
                dict(values),
                on_complete=apply_result,
            )
            assert applied is not None
            state, _, _ = applied
        return state.revision

    async def mutate_runtime(
        self,
        mutator: Callable[[Config], Mapping[str, Any]],
        *,
        on_applied: Callable[[Config], None] | None = None,
    ) -> ConfigurationState:
        """Atomically derive runtime-owned updates from the latest persisted state."""
        async with self._lock:
            applied: ConfigurationState | None = None

            def apply_result(result: Any) -> None:
                nonlocal applied
                state, changed_paths, effective = result
                self._replace_effective_roots(effective, changed_paths)
                if on_applied is not None:
                    on_applied(self._effective_config)
                applied = state

            await self._finish_thread_call(
                self._mutate_runtime_sync,
                mutator,
                on_complete=apply_result,
            )
            assert applied is not None
        return applied
