"""Bounded persistent activity records for one runtime instance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from nanocat.observability.redaction import redact_mapping, redact_value

_DEFAULT_SEGMENT_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_EVENT_BYTES = 1024 * 1024
_DEFAULT_MAX_SESSION_BYTES = 128 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_RETENTION_DAYS = 30
_MAX_SUMMARY_CHARS = 2_000
_MAX_VALUE_CHARS = 32_000
_MAX_ARTIFACT_REFS = 64
_MAX_BLOCKED_SCOPES = 65_536
_MAX_ACTIVITY_STATES = 4_096
_MAX_SEGMENT_BOUNDS = 32_768


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bounded_value(value: Any, max_chars: int = _MAX_VALUE_CHARS) -> Any:
    """Return redacted JSON data with an explicit preview for large values."""
    redacted = redact_value(value)
    try:
        encoded = json.dumps(redacted, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = json.dumps(str(redacted), ensure_ascii=False)
    if len(encoded) <= max_chars:
        try:
            return json.loads(encoded)
        except json.JSONDecodeError:
            return str(redacted)
    return {
        "truncated": True,
        "totalChars": len(encoded),
        "preview": encoded[:max_chars],
    }


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One redacted event in a session-scoped append-only activity stream."""

    type: str
    source: str
    status: str | None = None
    event_id: str | None = None
    sequence: int = 0
    recorded_at: datetime = field(default_factory=_utc_now)
    turn_id: str | None = None
    request_id: str | None = None
    node_id: str | None = None
    parent_id: str | None = None
    source_id: str | None = None
    phase: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    summary: str = ""
    redacted_input: Any = None
    redacted_output: Any = None
    artifact_refs: tuple[str, ...] = ()
    model: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        event_type = str(self.type).strip()
        source = str(self.source).strip()
        if not event_type:
            raise ValueError("activity event type cannot be empty")
        if not source:
            raise ValueError("activity event source cannot be empty")
        object.__setattr__(self, "type", event_type)
        object.__setattr__(self, "source", source)
        safe_summary = redact_value(str(self.summary))
        object.__setattr__(self, "summary", str(safe_summary)[:_MAX_SUMMARY_CHARS])
        object.__setattr__(self, "redacted_input", _bounded_value(self.redacted_input))
        object.__setattr__(self, "redacted_output", _bounded_value(self.redacted_output))
        object.__setattr__(
            self,
            "artifact_refs",
            tuple(str(item) for item in self.artifact_refs[:_MAX_ARTIFACT_REFS]),
        )
        safe_usage = _bounded_value(redact_mapping(self.usage))
        safe_metadata = _bounded_value(redact_mapping(self.metadata))
        object.__setattr__(self, "usage", safe_usage if isinstance(safe_usage, Mapping) else {})
        object.__setattr__(
            self,
            "metadata",
            safe_metadata if isinstance(safe_metadata, Mapping) else {},
        )

    def to_dict(self, *, session_scope: str | None = None) -> dict[str, Any]:
        """Serialize with a stable camelCase wire shape."""
        value: dict[str, Any] = {
            "schemaVersion": 1,
            "eventId": self.event_id,
            "sequence": self.sequence,
            "recordedAt": _isoformat(self.recorded_at),
            "type": self.type,
            "source": self.source,
            "status": self.status,
            "turnId": self.turn_id,
            "requestId": self.request_id,
            "nodeId": self.node_id,
            "parentId": self.parent_id,
            "sourceId": self.source_id,
            "phase": self.phase,
            "startedAt": _isoformat(self.started_at),
            "endedAt": _isoformat(self.ended_at),
            "durationMs": self.duration_ms,
            "summary": self.summary,
            "redactedInput": self.redacted_input,
            "redactedOutput": self.redacted_output,
            "artifactRefs": list(self.artifact_refs),
            "model": self.model,
            "usage": dict(self.usage),
            "metadata": dict(self.metadata),
        }
        if session_scope is not None:
            value["sessionScope"] = session_scope
        return {key: item for key, item in value.items() if item not in (None, "", [], {})}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActivityEvent":
        """Load one known schema version and ignore future additive fields."""
        return cls(
            type=str(value.get("type") or "unknown"),
            source=str(value.get("source") or "unknown"),
            status=str(value["status"]) if value.get("status") is not None else None,
            event_id=str(value["eventId"]) if value.get("eventId") else None,
            sequence=int(value.get("sequence") or 0),
            recorded_at=_parse_datetime(value.get("recordedAt")) or _utc_now(),
            turn_id=str(value["turnId"]) if value.get("turnId") else None,
            request_id=str(value["requestId"]) if value.get("requestId") else None,
            node_id=str(value["nodeId"]) if value.get("nodeId") else None,
            parent_id=str(value["parentId"]) if value.get("parentId") else None,
            source_id=str(value["sourceId"]) if value.get("sourceId") else None,
            phase=str(value["phase"]) if value.get("phase") else None,
            started_at=_parse_datetime(value.get("startedAt")),
            ended_at=_parse_datetime(value.get("endedAt")),
            duration_ms=(int(value["durationMs"]) if value.get("durationMs") is not None else None),
            summary=str(value.get("summary") or ""),
            redacted_input=value.get("redactedInput"),
            redacted_output=value.get("redactedOutput"),
            artifact_refs=tuple(str(item) for item in value.get("artifactRefs") or ()),
            model=str(value["model"]) if value.get("model") else None,
            usage=value.get("usage") if isinstance(value.get("usage"), Mapping) else {},
            metadata=(value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}),
        )


@dataclass(frozen=True, slots=True)
class ActivityPage:
    """A forward-only page over a session activity stream."""

    events: tuple[ActivityEvent, ...]
    next_cursor: int | None
    has_more: bool
    oldest_sequence: int | None = None
    latest_sequence: int | None = None


@dataclass(slots=True)
class _SessionState:
    next_segment: int
    next_sequence: int
    current_segment: Path | None = None


class ActivityJournal:
    """Persist bounded segmented JSONL streams under ``data/activity``."""

    def __init__(
        self,
        root: Path,
        *,
        segment_bytes: int = _DEFAULT_SEGMENT_BYTES,
        max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
        max_session_bytes: int = _DEFAULT_MAX_SESSION_BYTES,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.root = root.expanduser().resolve()
        if not 0 < max_event_bytes <= segment_bytes <= max_session_bytes <= max_total_bytes:
            raise ValueError(
                "activity limits must satisfy max_event_bytes <= segment_bytes <= "
                "max_session_bytes <= max_total_bytes"
            )
        if retention_days <= 0:
            raise ValueError("activity retention_days must be positive")
        self.segment_bytes = segment_bytes
        self.max_event_bytes = max_event_bytes
        self.max_session_bytes = max_session_bytes
        self.max_total_bytes = max_total_bytes
        self.retention = timedelta(days=retention_days)
        self._lock = threading.RLock()
        self._states: OrderedDict[str, _SessionState] = OrderedDict()
        self._segment_bound_cache: OrderedDict[
            Path, tuple[int, int, int, int]
        ] = OrderedDict()
        self._blocked_scopes: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._remove_incomplete_files_locked()
            self._enforce_retention_locked()
            for directory in self.root.iterdir():
                if directory.is_dir():
                    self._enforce_session_quota_locked(directory.name, protected=None)
            self._enforce_global_quota_locked(protected=None)

    @staticmethod
    def scope_for(session_key: str) -> str:
        """Return a stable non-reversible directory name for one session."""
        return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]

    def append(self, session_key: str, event: ActivityEvent) -> ActivityEvent | None:
        """Append one event and return its assigned identities, or ``None`` on capacity failure."""
        if not session_key:
            raise ValueError("session_key cannot be empty")
        scope = self.scope_for(session_key)
        with self._lock:
            self._ensure_open_locked()
            if scope in self._blocked_scopes:
                return None
            state = self._state_locked(scope)
            sequence = state.next_sequence
            event_id = event.event_id or self._stable_id(
                "event", session_key, event.type, event.turn_id, event.request_id, sequence
            )
            node_id = event.node_id or self._stable_id(
                "node",
                session_key,
                event.type,
                event.turn_id,
                event.request_id,
                event.source_id,
                event.parent_id,
                sequence,
            )
            assigned = replace(
                event,
                event_id=event_id,
                node_id=node_id,
                sequence=sequence,
            )
            payload = assigned.to_dict(session_scope=scope)
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            encoded = line.encode("utf-8")
            if len(encoded) > self.max_event_bytes:
                return None

            path = self._target_segment_locked(scope, state, len(encoded))
            if not self._reserve_capacity_locked(scope, protected=path, incoming=len(encoded)):
                return None
            try:
                with path.open("ab") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                return None
            state.next_sequence += 1
            return assigned

    def read_page(
        self,
        session_key: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        event_types: Iterable[str] | None = None,
        statuses: Iterable[str] | None = None,
    ) -> ActivityPage:
        """Read a bounded forward page; the numeric cursor survives segment rotation."""
        if not 1 <= limit <= 1_000:
            raise ValueError("activity page limit must be between 1 and 1000")
        scope = self.scope_for(session_key)
        type_filter = frozenset(event_types or ())
        status_filter = frozenset(statuses or ())
        with self._lock:
            self._ensure_open_locked()
            segments = self._segments_locked(scope)
            events: list[ActivityEvent] = []
            bounded_segments = [
                (path, bounds)
                for path in segments
                if (bounds := self._segment_bounds_locked(path)) is not None
            ]
            oldest = bounded_segments[0][1][0] if bounded_segments else None
            latest = bounded_segments[-1][1][1] if bounded_segments else None
            for path, (_, segment_latest) in bounded_segments:
                if segment_latest <= after_sequence:
                    continue
                for event in self._read_segment(path):
                    if event.sequence <= after_sequence:
                        continue
                    if type_filter and event.type not in type_filter:
                        continue
                    if status_filter and (event.status or "") not in status_filter:
                        continue
                    events.append(event)
                    if len(events) > limit:
                        break
                self._mark_accessed(path)
                if len(events) > limit:
                    break
            has_more = len(events) > limit
            page_events = tuple(events[:limit])
            next_cursor = page_events[-1].sequence if has_more and page_events else None
            return ActivityPage(
                events=page_events,
                next_cursor=next_cursor,
                has_more=has_more,
                oldest_sequence=oldest,
                latest_sequence=latest,
            )

    def read_recent(self, session_key: str, *, limit: int = 100) -> ActivityPage:
        """Return the newest events while preserving chronological order."""
        if not 1 <= limit <= 1_000:
            raise ValueError("activity page limit must be between 1 and 1000")
        scope = self.scope_for(session_key)
        with self._lock:
            self._ensure_open_locked()
            segments = [
                (path, bounds)
                for path in self._segments_locked(scope)
                if (bounds := self._segment_bounds_locked(path)) is not None
            ]
            newest_first: list[ActivityEvent] = []
            for path, _ in reversed(segments):
                found = self._read_segment(path)
                self._mark_accessed(path)
                for event in reversed(found):
                    newest_first.append(event)
                    if len(newest_first) > limit:
                        break
                if len(newest_first) > limit:
                    break
            selected = tuple(reversed(newest_first[:limit]))
            return ActivityPage(
                events=selected,
                next_cursor=None,
                has_more=len(newest_first) > limit,
                oldest_sequence=segments[0][1][0] if segments else None,
                latest_sequence=segments[-1][1][1] if segments else None,
            )

    def cleanup_scope(self, session_key: str) -> bool:
        """Best-effort removal of persistent activity owned by one deleted session."""
        scope = self.scope_for(session_key)
        directory = self.root / scope
        with self._lock:
            self._blocked_scopes[scope] = None
            self._blocked_scopes.move_to_end(scope)
            while len(self._blocked_scopes) > _MAX_BLOCKED_SCOPES:
                self._blocked_scopes.popitem(last=False)
            self._states.pop(scope, None)
            for path in tuple(self._segment_bound_cache):
                if path.parent == directory:
                    self._segment_bound_cache.pop(path, None)
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return True

    def has_events(self, session_key: str) -> bool:
        """Return whether a session has any readable activity segment."""
        scope = self.scope_for(session_key)
        with self._lock:
            self._ensure_open_locked()
            return any(self._read_segment(path) for path in self._segments_locked(scope))

    async def close(self) -> None:
        """Close the owner idempotently; append and read calls are rejected afterwards."""
        with self._lock:
            self._closed = True
            self._states.clear()
            self._segment_bound_cache.clear()

    @staticmethod
    def _stable_id(prefix: str, *parts: object) -> str:
        encoded = "\x1f".join("" if part is None else str(part) for part in parts).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()[:24]
        return f"{prefix}_{digest}"

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("activity journal is closed")

    def _scope_dir_locked(self, scope: str) -> Path:
        directory = self.root / scope
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _state_locked(self, scope: str) -> _SessionState:
        if state := self._states.get(scope):
            self._states.move_to_end(scope)
            return state
        segments = self._segments_locked(scope)
        next_segment = 1
        next_sequence = 1
        if segments:
            next_segment = int(segments[-1].stem.split("-")[-1]) + 1
            for path in reversed(segments):
                found = self._read_segment(path)
                if found:
                    next_sequence = max(event.sequence for event in found) + 1
                    break
        state = _SessionState(next_segment=next_segment, next_sequence=next_sequence)
        self._states[scope] = state
        while len(self._states) > _MAX_ACTIVITY_STATES:
            self._states.popitem(last=False)
        return state

    def _target_segment_locked(
        self,
        scope: str,
        state: _SessionState,
        event_bytes: int,
    ) -> Path:
        current = state.current_segment
        if current is not None:
            try:
                if current.stat().st_size + event_bytes <= self.segment_bytes:
                    return current
            except OSError:
                pass
        directory = self._scope_dir_locked(scope)
        current = directory / f"segment-{state.next_segment:08d}.jsonl"
        state.next_segment += 1
        state.current_segment = current
        return current

    def _segments_locked(self, scope: str) -> list[Path]:
        directory = self.root / scope
        if not directory.is_dir():
            return []
        return sorted(directory.glob("segment-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].jsonl"))

    def _segment_bounds_locked(self, path: Path) -> tuple[int, int] | None:
        """Read and cache only the first and last valid sequence in one segment."""
        try:
            stat = path.stat()
        except OSError:
            self._segment_bound_cache.pop(path, None)
            return None
        cached = self._segment_bound_cache.get(path)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            self._segment_bound_cache.move_to_end(path)
            return cached[2], cached[3]
        oldest: int | None = None
        latest: int | None = None
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                        if not isinstance(value, Mapping):
                            continue
                        sequence = int(value.get("sequence") or 0)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if sequence <= 0:
                        continue
                    if oldest is None:
                        oldest = sequence
                    latest = sequence
        except OSError:
            return None
        if oldest is None or latest is None:
            return None
        self._segment_bound_cache[path] = (
            stat.st_mtime_ns,
            stat.st_size,
            oldest,
            latest,
        )
        self._segment_bound_cache.move_to_end(path)
        while len(self._segment_bound_cache) > _MAX_SEGMENT_BOUNDS:
            self._segment_bound_cache.popitem(last=False)
        return oldest, latest

    @staticmethod
    def _read_segment(path: Path) -> list[ActivityEvent]:
        events: list[ActivityEvent] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                        if isinstance(value, Mapping):
                            events.append(ActivityEvent.from_dict(value))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
        except OSError:
            return []
        return events

    @staticmethod
    def _mark_accessed(path: Path) -> None:
        try:
            stat = path.stat()
            os.utime(path, (datetime.now().timestamp(), stat.st_mtime))
        except OSError:
            pass

    def _all_segments_locked(self) -> list[Path]:
        return [path for path in self.root.glob("*/segment-*.jsonl") if path.is_file()]

    @staticmethod
    def _access_ns(path: Path) -> int:
        try:
            stat = path.stat()
        except OSError:
            return 0
        return max(stat.st_atime_ns, stat.st_mtime_ns)

    @staticmethod
    def _unlink(path: Path) -> int:
        try:
            size = path.stat().st_size
            path.unlink()
            return size
        except OSError:
            return 0

    @staticmethod
    def _path_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _reserve_capacity_locked(self, scope: str, *, protected: Path, incoming: int) -> bool:
        segments = self._segments_locked(scope)
        session_total = sum(self._path_size(path) for path in segments) + incoming
        for path in segments:
            if session_total <= self.max_session_bytes:
                break
            if path == protected:
                continue
            session_total -= self._unlink(path)
        if session_total > self.max_session_bytes:
            return False

        segments = self._all_segments_locked()
        total = sum(self._path_size(path) for path in segments) + incoming
        for path in sorted(segments, key=self._access_ns):
            if total <= self.max_total_bytes:
                break
            if path == protected:
                continue
            total -= self._unlink(path)
        return total <= self.max_total_bytes

    def _enforce_session_quota_locked(
        self,
        scope: str,
        *,
        protected: Path | None,
    ) -> None:
        segments = self._segments_locked(scope)
        total = sum(self._path_size(path) for path in segments)
        for path in segments:
            if total <= self.max_session_bytes:
                break
            if path == protected:
                continue
            total -= self._unlink(path)

    def _enforce_global_quota_locked(self, *, protected: Path | None) -> bool:
        segments = self._all_segments_locked()
        total = sum(self._path_size(path) for path in segments)
        for path in sorted(segments, key=self._access_ns):
            if total <= self.max_total_bytes:
                break
            if protected is not None and path == protected:
                continue
            total -= self._unlink(path)
        return total <= self.max_total_bytes

    def _enforce_retention_locked(self) -> None:
        cutoff = (_utc_now() - self.retention).timestamp()
        for path in self._all_segments_locked():
            try:
                last_access = max(path.stat().st_atime, path.stat().st_mtime)
            except OSError:
                continue
            if last_access < cutoff:
                self._unlink(path)

    def _remove_incomplete_files_locked(self) -> None:
        for path in self.root.rglob("*.tmp"):
            try:
                path.unlink()
            except OSError:
                pass
