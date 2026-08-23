"""Runtime-owned, agent-readable files stored inside the workspace."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

RuntimeFileKind = Literal["tools", "compaction", "context", "images", "http"]

_VALID_KINDS = frozenset({"tools", "compaction", "context", "images", "http"})
_DEFAULT_INLINE_CHARS = 6000
_DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_SESSION_BYTES = 256 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_PREVIEW_CHARS = 1200
_MAX_BLOCKED_SCOPES = 65_536


@dataclass(frozen=True, slots=True)
class RuntimeFileRef:
    """One runtime file with both model-facing and owner-facing paths."""

    scope: str
    kind: RuntimeFileKind
    relative_path: str
    absolute_path: Path
    metadata_path: Path
    size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _StoredRecord:
    ref: RuntimeFileRef
    access_ns: int


class RuntimeFileStore:
    """Own bounded, disposable files beneath ``workspace/_runtime_temp``."""

    def __init__(
        self,
        workspace: Path,
        *,
        runtime_dir: Path | None = None,
        inline_chars: int = _DEFAULT_INLINE_CHARS,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        max_session_bytes: int = _DEFAULT_MAX_SESSION_BYTES,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.root = (
            runtime_dir or self.workspace / "_runtime_temp"
        ).expanduser().resolve()
        if not self.root.is_relative_to(self.workspace):
            raise ValueError("runtime_dir must be inside the workspace")
        if not 0 < max_file_bytes <= max_session_bytes <= max_total_bytes:
            raise ValueError(
                "runtime file limits must satisfy max_file_bytes <= "
                "max_session_bytes <= max_total_bytes"
            )
        self.inline_chars = max(512, inline_chars)
        self.max_file_bytes = max_file_bytes
        self.max_session_bytes = max_session_bytes
        self.max_total_bytes = max_total_bytes
        self._lock = threading.RLock()
        self._allocations: dict[Path, RuntimeFileRef] = {}
        self._blocked_scopes: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup_incomplete_files()

    def apply_limits(
        self,
        *,
        max_file_bytes: int,
        max_session_bytes: int,
        max_total_bytes: int,
    ) -> None:
        """Apply validated quotas to subsequent allocations under the owner lock."""
        if not 0 < max_file_bytes <= max_session_bytes <= max_total_bytes:
            raise ValueError(
                "runtime file limits must satisfy max_file_bytes <= "
                "max_session_bytes <= max_total_bytes"
            )
        with self._lock:
            self.max_file_bytes = max_file_bytes
            self.max_session_bytes = max_session_bytes
            self.max_total_bytes = max_total_bytes

    @staticmethod
    def scope_for(session_key: str) -> str:
        """Return the stable, non-reversible directory name for a session."""
        return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _validate_kind(kind: RuntimeFileKind) -> RuntimeFileKind:
        if kind not in _VALID_KINDS:
            raise ValueError(f"unsupported runtime file kind: {kind}")
        return kind

    def _kind_dir(self, session_key: str, kind: RuntimeFileKind) -> tuple[str, Path]:
        kind = self._validate_kind(kind)
        scope = self.scope_for(session_key)
        if self._closed:
            raise OSError("runtime file store is closed")
        if scope in self._blocked_scopes:
            raise OSError("runtime file scope was deleted")
        path = self.root / "sessions" / scope / kind
        path.mkdir(parents=True, exist_ok=True)
        return scope, path

    @staticmethod
    def _normalized_suffix(suffix: str) -> str:
        suffix = suffix.strip()
        if not suffix:
            return ".bin"
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        if len(suffix) > 24 or any(char in suffix for char in ("/", "\\", "\0")):
            raise ValueError("invalid runtime file suffix")
        return suffix

    def _new_ref(
        self,
        session_key: str,
        kind: RuntimeFileKind,
        suffix: str,
    ) -> RuntimeFileRef:
        scope, directory = self._kind_dir(session_key, kind)
        suffix = self._normalized_suffix(suffix)
        absolute_path = directory / f"rf_{uuid.uuid4().hex}{suffix}"
        relative_path = absolute_path.relative_to(self.workspace).as_posix()
        return RuntimeFileRef(
            scope=scope,
            kind=kind,
            relative_path=relative_path,
            absolute_path=absolute_path,
            metadata_path=absolute_path.with_name(f"{absolute_path.name}.meta.json"),
        )

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    @classmethod
    def _atomic_write_json(cls, path: Path, value: dict[str, Any]) -> None:
        cls._atomic_write_bytes(
            path,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    @staticmethod
    def _strip_success_markers(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: RuntimeFileStore._strip_success_markers(item)
                for key, item in value.items()
                if not (key == "ok" and item is True)
            }
        if isinstance(value, list):
            return [RuntimeFileStore._strip_success_markers(item) for item in value]
        return value

    @classmethod
    def _normalized_result(cls, result: Any) -> Any:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                return result
        return cls._strip_success_markers(result)

    @classmethod
    def inline_result(cls, result: Any) -> str:
        """Normalize a tool result for direct model injection."""
        value = cls._normalized_result(result)
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    @classmethod
    def serialize_tool_result(cls, result: Any) -> str:
        """Render complete top-level fields with multiline strings kept readable."""
        value = cls._normalized_result(result)
        if not isinstance(value, dict):
            if isinstance(value, str):
                return value
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)

        sections: list[str] = []
        for key, item in value.items():
            sections.append(f"## {key}")
            if isinstance(item, str):
                sections.append(item)
            else:
                sections.append(json.dumps(item, ensure_ascii=False, indent=2, default=str))
        return "\n\n".join(sections)

    @staticmethod
    def _preview(text: str) -> str:
        if len(text) <= _PREVIEW_CHARS * 2:
            return text
        omitted = len(text) - _PREVIEW_CHARS * 2
        return f"{text[:_PREVIEW_CHARS]}\n...[{omitted} chars omitted]...\n{text[-_PREVIEW_CHARS:]}"

    def capture(
        self,
        session_key: str,
        tool_name: str,
        tool_call_id: str | None,
        result: Any,
    ) -> str:
        """Return an inline result or a bounded reference to its complete file."""
        inline_text = self.inline_result(result)
        if len(inline_text) <= self.inline_chars:
            return inline_text

        readable_text = self.serialize_tool_result(result)
        try:
            ref = self.snapshot(
                session_key,
                "tools",
                readable_text,
                source_name=tool_name,
                source_id=tool_call_id,
            )
        except Exception:
            ref = None
        response: dict[str, Any] = {
            "status": "stored" if ref else "not_stored",
            "tool": tool_name,
            "total_chars": len(readable_text),
            "total_lines": readable_text.count("\n") + 1,
            "preview": self._preview(readable_text),
        }
        if ref:
            response["path"] = ref.relative_path
            response["hint"] = (
                "Use read_file with offset and limit, or grep_file with this path."
            )
        else:
            response["hint"] = "The complete result could not be stored."
        return json.dumps(response, ensure_ascii=False)

    def snapshot(
        self,
        session_key: str,
        kind: RuntimeFileKind,
        content: str,
        *,
        source_name: str | None = None,
        source_id: str | None = None,
        suffix: str = ".txt",
    ) -> RuntimeFileRef | None:
        """Atomically store UTF-8 text and apply the configured quotas."""
        return self.write_bytes(
            session_key,
            kind,
            content.encode("utf-8"),
            source_name=source_name,
            source_id=source_id,
            suffix=suffix,
        )

    def write_bytes(
        self,
        session_key: str,
        kind: RuntimeFileKind,
        content: bytes,
        *,
        source_name: str | None = None,
        source_id: str | None = None,
        suffix: str = ".bin",
    ) -> RuntimeFileRef | None:
        """Atomically store bytes and apply the configured quotas."""
        if len(content) > self.max_file_bytes:
            return None
        with self._lock:
            ref = self._new_ref(session_key, kind, suffix)
            try:
                self._atomic_write_bytes(ref.absolute_path, content)
                return self._finalize_locked(
                    ref,
                    source_name=source_name,
                    source_id=source_id,
                )
            except OSError:
                self._discard_locked(ref)
                return None

    def write_auxiliary_bytes(
        self,
        relative_path: str,
        content: bytes,
        *,
        max_group_bytes: int,
    ) -> bool:
        """Atomically write a runtime-owner file under the shared physical quota."""
        if len(content) > self.max_file_bytes or max_group_bytes <= 0:
            return False
        target = (self.root / relative_path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("auxiliary runtime path escaped its root")
        with self._lock:
            if self._closed:
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            existing_size = target.stat().st_size if target.is_file() else 0
            try:
                group_size = sum(
                    item.stat().st_size
                    for item in target.parent.iterdir()
                    if item.is_file() and not item.name.startswith(".")
                )
            except OSError:
                return False
            if group_size - existing_size + len(content) > max_group_bytes:
                return False
            if not self._ensure_physical_capacity_locked(len(content)):
                return False
            try:
                self._atomic_write_bytes(target, content)
            except OSError:
                return False
            return True

    def allocate(
        self,
        session_key: str,
        kind: RuntimeFileKind,
        *,
        suffix: str = ".bin",
    ) -> RuntimeFileRef:
        """Allocate an incomplete file for an owner-managed streaming write."""
        with self._lock:
            ref = self._new_ref(session_key, kind, suffix)
            if not self._reserve_allocation_locked(ref.scope):
                raise OSError("runtime file quota has no capacity for another stream")
            ref.absolute_path.touch(exist_ok=False)
            self._allocations[ref.absolute_path] = ref
            return ref

    def finalize(
        self,
        ref: RuntimeFileRef,
        *,
        source_name: str | None = None,
        source_id: str | None = None,
    ) -> RuntimeFileRef | None:
        """Publish an allocated file after validating size and ownership."""
        with self._lock:
            self._validate_ref(ref)
            self._allocations.pop(ref.absolute_path, None)
            try:
                return self._finalize_locked(
                    ref,
                    source_name=source_name,
                    source_id=source_id,
                )
            except OSError:
                self._discard_locked(ref)
                return None

    def _finalize_locked(
        self,
        ref: RuntimeFileRef,
        *,
        source_name: str | None,
        source_id: str | None,
    ) -> RuntimeFileRef | None:
        if ref.scope in self._blocked_scopes:
            self._discard_locked(ref)
            return None
        size_bytes = ref.absolute_path.stat().st_size
        if size_bytes > self.max_file_bytes:
            self._discard_locked(ref)
            return None

        sha256 = hashlib.sha256()
        with ref.absolute_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                sha256.update(chunk)
        now = datetime.now(timezone.utc).isoformat()
        complete_ref = replace(ref, size_bytes=size_bytes)
        metadata = {
            "scope": ref.scope,
            "kind": ref.kind,
            "path": ref.relative_path,
            "size_bytes": size_bytes,
            "source_name": source_name,
            "source_id": source_id,
            "created_at": now,
            "sha256": sha256.hexdigest(),
        }
        self._atomic_write_json(ref.metadata_path, metadata)
        if not self._enforce_quotas_locked(complete_ref):
            self._discard_locked(complete_ref)
            return None
        return complete_ref

    def discard(self, ref: RuntimeFileRef) -> None:
        """Remove an incomplete or completed runtime file."""
        with self._lock:
            self._validate_ref(ref)
            self._allocations.pop(ref.absolute_path, None)
            self._discard_locked(ref)

    def mark_accessed(self, path: str | Path) -> bool:
        """Refresh LRU state after a generic file tool reads a runtime file."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        candidate = candidate.resolve()
        sessions_root = (self.root / "sessions").resolve()
        if not candidate.is_relative_to(sessions_root):
            return False
        metadata_path = candidate.with_name(f"{candidate.name}.meta.json")
        if not candidate.is_file() or not metadata_path.is_file():
            return False
        try:
            os.utime(candidate, None)
        except OSError:
            return False
        return True

    @staticmethod
    def _discard_locked(ref: RuntimeFileRef) -> None:
        try:
            ref.absolute_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            ref.metadata_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _validate_ref(self, ref: RuntimeFileRef) -> None:
        expected_root = self.root / "sessions" / ref.scope / ref.kind
        if not ref.absolute_path.resolve().is_relative_to(expected_root.resolve()):
            raise ValueError("runtime file reference is outside its scope")
        if ref.metadata_path != ref.absolute_path.with_name(f"{ref.absolute_path.name}.meta.json"):
            raise ValueError("runtime file metadata path does not match its payload")

    def _stored_records_locked(self) -> list[_StoredRecord]:
        records: list[_StoredRecord] = []
        sessions_root = self.root / "sessions"
        if not sessions_root.is_dir():
            return records
        for metadata_path in sessions_root.rglob("rf_*.meta.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                scope = str(metadata["scope"])
                kind = self._validate_kind(str(metadata["kind"]))  # type: ignore[arg-type]
                relative_path = str(metadata["path"])
                absolute_path = (self.workspace / relative_path).resolve()
                expected_root = (self.root / "sessions" / scope / kind).resolve()
                if not absolute_path.is_relative_to(expected_root) or not absolute_path.is_file():
                    continue
                stat = absolute_path.stat()
                ref = RuntimeFileRef(
                    scope=scope,
                    kind=kind,
                    relative_path=relative_path,
                    absolute_path=absolute_path,
                    metadata_path=metadata_path,
                    size_bytes=stat.st_size,
                )
                records.append(
                    _StoredRecord(ref=ref, access_ns=max(stat.st_atime_ns, stat.st_mtime_ns))
                )
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return records

    def _physical_total_bytes_locked(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def _allocation_reserve_bytes_locked(self) -> int:
        reserved = 0
        for path in self._allocations:
            try:
                current = path.stat().st_size
            except OSError:
                current = 0
            reserved += max(0, self.max_file_bytes - current)
        return reserved

    def _ensure_physical_capacity_locked(
        self,
        incoming: int,
        *,
        protected: Path | None = None,
    ) -> bool:
        records = sorted(self._stored_records_locked(), key=lambda item: item.access_ns)
        while (
            self._physical_total_bytes_locked()
            + self._allocation_reserve_bytes_locked()
            + incoming
            > self.max_total_bytes
            and records
        ):
            record = records.pop(0)
            if protected is not None and record.ref.absolute_path == protected:
                continue
            self._discard_locked(record.ref)
        return (
            self._physical_total_bytes_locked()
            + self._allocation_reserve_bytes_locked()
            + incoming
            <= self.max_total_bytes
        )

    def _enforce_quotas_locked(self, protected: RuntimeFileRef) -> bool:
        records = self._stored_records_locked()
        scope_records = sorted(
            (record for record in records if record.ref.scope == protected.scope),
            key=lambda record: record.access_ns,
        )
        scope_size = sum(record.ref.size_bytes for record in scope_records)
        scope_reserved = sum(
            self.max_file_bytes
            for allocation in self._allocations.values()
            if allocation.scope == protected.scope
        )
        for record in scope_records:
            if scope_size + scope_reserved <= self.max_session_bytes:
                break
            if record.ref.absolute_path == protected.absolute_path:
                continue
            scope_size -= record.ref.size_bytes
            self._discard_locked(record.ref)

        total_ok = self._ensure_physical_capacity_locked(
            0,
            protected=protected.absolute_path,
        )
        return (
            protected.absolute_path.is_file()
            and scope_size + scope_reserved <= self.max_session_bytes
            and total_ok
        )

    def _reserve_allocation_locked(self, scope: str) -> bool:
        """Reserve one maximum-size stream while evicting completed LRU files."""
        records = self._stored_records_locked()
        scope_records = sorted(
            (record for record in records if record.ref.scope == scope),
            key=lambda record: record.access_ns,
        )
        scope_size = sum(record.ref.size_bytes for record in scope_records)
        scope_reserved = sum(
            self.max_file_bytes
            for allocation in self._allocations.values()
            if allocation.scope == scope
        )
        for record in scope_records:
            if scope_size + scope_reserved + self.max_file_bytes <= self.max_session_bytes:
                break
            scope_size -= record.ref.size_bytes
            self._discard_locked(record.ref)
        if scope_size + scope_reserved + self.max_file_bytes > self.max_session_bytes:
            return False

        return self._ensure_physical_capacity_locked(self.max_file_bytes)

    def cleanup_scope(self, session_key: str) -> bool:
        """Best-effort removal of all runtime files owned by one session."""
        scope = self.scope_for(session_key)
        scope_dir = self.root / "sessions" / scope
        with self._lock:
            self._blocked_scopes[scope] = None
            self._blocked_scopes.move_to_end(scope)
            while len(self._blocked_scopes) > _MAX_BLOCKED_SCOPES:
                self._blocked_scopes.popitem(last=False)
            for path, allocation in tuple(self._allocations.items()):
                if allocation.scope == scope:
                    self._allocations.pop(path, None)
                    self._discard_locked(allocation)
            try:
                shutil.rmtree(scope_dir)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            return True

    async def close(self) -> None:
        """Reject later writes and discard incomplete owner-managed files."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for ref in tuple(self._allocations.values()):
                self._discard_locked(ref)
            self._allocations.clear()

    def _cleanup_incomplete_files(self) -> None:
        """Remove interrupted atomic writes and allocations from an earlier run."""
        with self._lock:
            for temp_path in self.root.rglob(".rf_*.tmp"):
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            recovery_dir = self.root / "recovery" / "stopped_turns"
            if recovery_dir.is_dir():
                for temp_path in recovery_dir.glob(".stopped_*.json.*.tmp"):
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
            for payload_path in self.root.rglob("rf_*"):
                if not payload_path.is_file() or payload_path.name.endswith(".meta.json"):
                    continue
                metadata_path = payload_path.with_name(f"{payload_path.name}.meta.json")
                if metadata_path.is_file():
                    continue
                try:
                    payload_path.unlink()
                except OSError:
                    pass
