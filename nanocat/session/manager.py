"""Session management — multi-session per channel with UUID-based IDs."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import uuid
import weakref
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanocat.utils.helpers import ensure_dir

# Callback: (session, channel) -> name string or None on failure
NameGenerator = Callable[["Session", str], Awaitable[str | None]]
_MAX_DELETED_TOMBSTONES = 65_536


class SessionRevisionConflictError(RuntimeError):
    """Raised when a save no longer targets the persisted session revision."""


class SessionDeletedError(RuntimeError):
    """Raised when late background work tries to recreate a deleted session."""


class SessionStorageError(RuntimeError):
    """Raised when existing session storage cannot be read safely."""


@dataclass
class Session:
    """A conversation session identified by a 6-char UUID."""

    id: str  # 6-char UUID
    channel: str
    chat_id: str
    name: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    compacted_memory: str = ""
    last_compacted: int = 0
    compaction_checkpoint: dict[str, Any] = field(default_factory=dict)
    revision: int = 0

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.chat_id}:{self.id}"

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        self.messages.append(msg)
        self.revision += 1
        self.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _scan_completed_turns(
        messages: list[dict[str, Any]],
        start: int = 0,
        end: int | None = None,
    ) -> list[tuple[int, int]]:
        """Return only user-rooted turns with a complete tool protocol and terminal reply."""
        stop = len(messages) if end is None else min(len(messages), end)
        turns: list[tuple[int, int]] = []
        turn_start: int | None = None
        declared: set[str] = set()
        pending: set[str] = set()

        def reset() -> None:
            nonlocal turn_start
            turn_start = None
            declared.clear()
            pending.clear()

        for index in range(max(0, start), stop):
            message = messages[index]
            role = message.get("role")

            if role == "user":
                if pending:
                    reset()
                if turn_start is None:
                    turn_start = index
                continue

            if turn_start is None:
                continue

            if role == "assistant":
                if pending:
                    reset()
                    continue
                calls = message.get("tool_calls") or []
                if calls:
                    call_ids = [
                        str(call["id"])
                        for call in calls
                        if isinstance(call, dict) and call.get("id")
                    ]
                    if (
                        len(call_ids) != len(calls)
                        or len(set(call_ids)) != len(call_ids)
                        or any(call_id in declared for call_id in call_ids)
                    ):
                        reset()
                        continue
                    declared.update(call_ids)
                    pending.update(call_ids)
                    continue
                if not message.get("content"):
                    reset()
                    continue
                turns.append((turn_start, index + 1))
                reset()
                continue

            if role == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if call_id not in pending:
                    reset()
                    continue
                pending.remove(call_id)
                continue

            reset()

        return turns

    @classmethod
    def validate_turn_entries(cls, entries: list[dict[str, Any]]) -> None:
        """Require one or more contiguous complete user-rooted turns."""
        if not entries:
            raise ValueError("persisted history cannot be empty")
        boundaries = cls._scan_completed_turns(entries)
        cursor = 0
        for start, end in boundaries:
            if start != cursor:
                raise ValueError("persisted history contains an invalid turn fragment")
            cursor = end
        if cursor != len(entries):
            raise ValueError("persisted history must end at a complete turn boundary")

    @classmethod
    def _build_history_view(
        cls,
        messages: list[dict[str, Any]],
        max_messages: int = 500,
    ) -> list[dict[str, Any]]:
        sliced = messages if max_messages == 0 else messages[-max_messages:]
        boundaries = cls._scan_completed_turns(sliced)
        sliced = [message for start, end in boundaries for message in sliced[start:end]]
        out: list[dict[str, Any]] = []
        for message in sliced:
            entry: dict[str, Any] = {
                "role": message["role"],
                "content": message.get("content", ""),
            }
            for key in (
                "tool_calls",
                "tool_call_id",
                "name",
                "reasoning_content",
                "thinking_blocks",
            ):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)
        return out

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        uncompacted = self.messages[self.last_compacted :]
        return self._build_history_view(uncompacted, max_messages=max_messages)

    def get_history_from(self, start_idx: int, max_messages: int = 500) -> list[dict[str, Any]]:
        start = max(0, start_idx)
        return self._build_history_view(self.messages[start:], max_messages=max_messages)

    def get_completed_turn_boundaries(
        self,
        start_idx: int | None = None,
        end_idx: int | None = None,
    ) -> list[tuple[int, int]]:
        start = self.last_compacted if start_idx is None else max(0, start_idx)
        end = len(self.messages) if end_idx is None else min(len(self.messages), end_idx)
        return self._scan_completed_turns(self.messages, start, end)

    def clear(self) -> None:
        self.messages = []
        self.compacted_memory = ""
        self.last_compacted = 0
        self.compaction_checkpoint = {}
        self.revision = 0
        self.updated_at = datetime.now(timezone.utc)


class SessionManager:
    """Manages per-channel multi-session storage under {workspace}/sessions/."""

    _MAX_NAMING_TASKS = 128

    def __init__(self, sessions_root: Path):
        self.sessions_dir = ensure_dir(sessions_root)
        self._system_dir = ensure_dir(self.sessions_dir / "_system")
        self._cache: weakref.WeakValueDictionary[str, Session] = (
            weakref.WeakValueDictionary()
        )
        self._cache_guard = threading.RLock()
        self._name_generator: NameGenerator | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._naming_tasks: dict[str, asyncio.Task[Any]] = {}
        self._write_locks: weakref.WeakValueDictionary[str, threading.RLock] = (
            weakref.WeakValueDictionary()
        )
        self._write_locks_guard = threading.Lock()
        self._channel_locks: dict[str, threading.RLock] = {}
        self._channel_locks_guard = threading.Lock()
        self._deleted_keys: OrderedDict[str, None] = OrderedDict()
        self._deleted_keys_guard = threading.RLock()
        self._recover_delete_tombstones()

    def _channel_lock(self, channel: str) -> threading.RLock:
        with self._channel_locks_guard:
            return self._channel_locks.setdefault(channel, threading.RLock())

    def _cache_get(self, key: str) -> Session | None:
        with self._cache_guard:
            return self._cache.get(key)

    def _cache_set(self, key: str, session: Session) -> None:
        with self._cache_guard:
            self._cache[key] = session

    def _cache_pop(self, key: str) -> None:
        with self._cache_guard:
            self._cache.pop(key, None)

    def _cache_snapshot(self) -> tuple[tuple[str, Session], ...]:
        with self._cache_guard:
            return tuple(self._cache.items())

    def _write_lock(self, session_key: str) -> threading.RLock:
        with self._write_locks_guard:
            lock = self._write_locks.get(session_key)
            if lock is None:
                lock = threading.RLock()
                self._write_locks[session_key] = lock
            return lock

    def _mark_deleted(self, session_key: str) -> None:
        """Retain a bounded late-writer guard and release the indexed lock."""
        with self._deleted_keys_guard:
            self._deleted_keys[session_key] = None
            self._deleted_keys.move_to_end(session_key)
            while len(self._deleted_keys) > _MAX_DELETED_TOMBSTONES:
                self._deleted_keys.popitem(last=False)
        with self._write_locks_guard:
            self._write_locks.pop(session_key, None)

    def _is_deleted(self, session_key: str) -> bool:
        with self._deleted_keys_guard:
            return session_key in self._deleted_keys

    def _deleted_snapshot(self) -> tuple[str, ...]:
        with self._deleted_keys_guard:
            return tuple(self._deleted_keys)

    # -- public configuration ------------------------------------------------

    def set_name_generator(self, callback: NameGenerator) -> None:
        self._name_generator = callback

    # -- normal session (channel + metadata) ---------------------------------

    def get_or_create(self, channel: str, chat_id: str) -> Session:
        """Return the active session for (channel, chat_id), creating one if needed."""
        with self._channel_lock(channel):
            cache_key = f"{channel}:{chat_id}"
            meta = self._read_metadata(channel)
            chat_entry = meta.get("chats", {}).get(chat_id)
            active_id = chat_entry.get("active") if isinstance(chat_entry, dict) else None
            if chat_entry is not None and not active_id:
                raise SessionStorageError(f"session scope {channel}:{chat_id} has no active session")

            cached = self._cache_get(cache_key)
            if active_id:
                info = meta.get("sessions", {}).get(active_id)
                if info is None or str(info.get("chat_id") or "") != chat_id:
                    raise SessionStorageError(
                        f"active session {channel}:{chat_id}:{active_id} has invalid ownership"
                    )
                if cached is not None and cached.id == active_id:
                    if cached.chat_id != chat_id:
                        raise SessionStorageError(
                            f"cached session {channel}:{active_id} has invalid ownership"
                        )
                    return cached
                session = self._load(channel, active_id)
                if session is None:
                    raise SessionStorageError(
                        f"active session {channel}:{chat_id}:{active_id} is missing"
                    )
                if session.chat_id != chat_id:
                    raise SessionStorageError(
                        f"session {channel}:{active_id} payload has invalid ownership"
                    )
                self._cache_set(cache_key, session)
                return session

            session = self._new_session(channel, chat_id)
            self._cache_set(cache_key, session)
            return session

    def _new_session(
        self,
        channel: str,
        chat_id: str,
        *,
        name: str | None = None,
    ) -> Session:
        """Create a durable session and publish it in metadata as one operation."""
        with self._channel_lock(channel):
            session_id = self._generate_id(channel)
            now = datetime.now(timezone.utc)
            session = Session(
                id=session_id,
                channel=channel,
                chat_id=chat_id,
                created_at=now,
                updated_at=now,
                name=name,
            )
            meta = self._read_metadata(channel)
            meta.setdefault("chats", {})[chat_id] = {
                "active": session_id,
                "last_active": now.isoformat(),
            }
            meta.setdefault("sessions", {})[session_id] = {
                "chat_id": chat_id,
                "name": name,
                "created_at": now.isoformat(),
                "last_active": now.isoformat(),
                "message_count": 0,
            }
            path = self._session_path(channel, session_id)
            self._write_session_file(path, session)
            try:
                self._write_metadata(channel, meta)
            except BaseException:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("Failed to remove unindexed new session {}", path)
                raise
            self._cache_set(f"{channel}:{chat_id}", session)
            return session

    def _load(self, channel: str, session_id: str) -> Session | None:
        with self._channel_lock(channel):
            return self._load_unlocked(channel, session_id)

    def _load_unlocked(self, channel: str, session_id: str) -> Session | None:
        path = self._session_path(channel, session_id)
        if not path.exists():
            return None
        try:
            messages = []
            metadata = {}
            created_at = None
            last_compacted = 0
            compacted_memory = ""
            compaction_checkpoint: dict[str, Any] = {}
            revision = 0
            payload_key: str | None = None
            metadata_seen = False
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        raise ValueError("session record is not a JSON object")
                    if data.get("_type") == "metadata":
                        if metadata_seen:
                            raise ValueError("session file has multiple metadata records")
                        metadata_seen = True
                        raw_key = data.get("key")
                        if not isinstance(raw_key, str) or not raw_key:
                            raise ValueError("session metadata record has no owner key")
                        payload_key = raw_key
                        metadata = data.get("metadata", {})
                        created_at = (
                            datetime.fromisoformat(data["created_at"])
                            if data.get("created_at")
                            else None
                        )
                        compacted_memory = data.get("compacted_memory", "")
                        last_compacted = data.get("last_compacted", 0)
                        compaction_checkpoint = data.get("compaction_checkpoint", {}) or {}
                        revision = int(data.get("revision", 0) or 0)
                    else:
                        messages.append(data)

            meta = self._read_metadata(channel)
            info = meta.get("sessions", {}).get(session_id)
            if info is None:
                raise ValueError("session is missing from metadata index")
            chat_id = str(info.get("chat_id") or "")
            if not chat_id:
                raise ValueError("session metadata index has no owner chat")
            if not metadata_seen:
                raise ValueError("session file has no metadata record")
            expected_key = f"{channel}:{chat_id}:{session_id}"
            if payload_key != expected_key:
                raise ValueError("session payload owner does not match metadata index")
            name = info.get("name")
            last_compacted = min(max(int(last_compacted or 0), 0), len(messages))
            return Session(
                id=session_id,
                channel=channel,
                chat_id=chat_id,
                name=name,
                messages=messages,
                created_at=created_at or datetime.now(timezone.utc),
                metadata=metadata,
                compacted_memory=compacted_memory,
                last_compacted=last_compacted,
                compaction_checkpoint=compaction_checkpoint,
                revision=max(int(revision or 0), len(messages)),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SessionStorageError(
                f"cannot read existing session {channel}/{session_id}: {exc}"
            ) from exc

    def save(self, session: Session, *, expected_revision: int | None = None) -> None:
        """Persist a session, optionally requiring an exact on-disk revision."""
        if self._is_deleted(session.key):
            raise SessionDeletedError(f"session {session.key} was deleted")
        if session.channel == "_system":
            self._save_system(session, expected_revision=expected_revision)
            return
        self._save_normal(session, expected_revision=expected_revision)

    def _save_normal(
        self,
        session: Session,
        *,
        expected_revision: int | None = None,
    ) -> None:
        path = self._session_path(session.channel, session.id)
        lock = self._write_lock(session.key)
        with lock:
            if self._is_deleted(session.key):
                raise SessionDeletedError(f"session {session.key} was deleted")
            disk_revision = self._read_session_revision(path)
            self._validate_save_revision(
                session,
                disk_revision=disk_revision,
                expected_revision=expected_revision,
            )
            self._write_session_file(path, session)

            # Update metadata only after the session file is durable.
            try:
                with self._channel_lock(session.channel):
                    cache_key = f"{session.channel}:{session.chat_id}"
                    self._cache_set(cache_key, session)
                    meta = self._read_metadata(session.channel)
                    info = meta.setdefault("sessions", {}).setdefault(session.id, {})
                    info["last_active"] = session.updated_at.isoformat()
                    info["message_count"] = len(session.messages)
                    info["chat_id"] = session.chat_id
                    if not session.name and info.get("name"):
                        session.name = str(info["name"])
                    meta.setdefault("chats", {}).setdefault(session.chat_id, {})[
                        "last_active"
                    ] = session.updated_at.isoformat()
                    self._write_metadata(session.channel, meta)
            except Exception as exc:
                logger.error(
                    "Session {} is durable but its metadata index update failed: {}",
                    session.key,
                    exc,
                )

        # Trigger background naming if needed
        turn_count = self._count_turns(session)
        if (
            turn_count >= 3
            and not session.name
            and self._name_generator
            and session.key not in self._naming_tasks
            and len(self._naming_tasks) < self._MAX_NAMING_TASKS
        ):
            task = asyncio.create_task(self._auto_name_session(session))
            self._background_tasks.add(task)
            self._naming_tasks[session.key] = task

            def _forget(completed: asyncio.Task[Any], key: str = session.key) -> None:
                self._background_tasks.discard(completed)
                if self._naming_tasks.get(key) is completed:
                    self._naming_tasks.pop(key, None)

            task.add_done_callback(_forget)

    def _save_system(
        self,
        session: Session,
        *,
        expected_revision: int | None = None,
    ) -> None:
        """Persist a system session (no metadata)."""
        filename = session.id.replace(":", "_")
        for ch in r'<>:"/\|?*':
            filename = filename.replace(ch, "_")
        path = self._system_dir / f"{filename}.jsonl"
        lock = self._write_lock(session.key)
        with lock:
            if self._is_deleted(session.key):
                raise SessionDeletedError(f"session {session.key} was deleted")
            self._validate_save_revision(
                session,
                disk_revision=self._read_session_revision(path),
                expected_revision=expected_revision,
            )
            self._write_session_file(path, session)
            self._cache_set(session.id, session)

    @staticmethod
    def _validate_save_revision(
        session: Session,
        *,
        disk_revision: int | None,
        expected_revision: int | None,
    ) -> None:
        actual_revision = disk_revision if disk_revision is not None else 0
        if expected_revision is not None and actual_revision != expected_revision:
            raise SessionRevisionConflictError(
                f"session {session.key} revision changed: "
                f"expected {expected_revision}, found {actual_revision}"
            )
        if expected_revision is None and disk_revision is not None:
            if disk_revision >= session.revision:
                raise SessionRevisionConflictError(
                    f"session {session.key} is stale: memory revision "
                    f"{session.revision}, disk revision {disk_revision}"
                )

    def set_active(self, channel: str, chat_id: str, session_id: str) -> bool:
        """Switch the active session for (channel, chat_id). Returns True on success."""
        with self._channel_lock(channel):
            return self._set_active_unlocked(channel, chat_id, session_id)

    def _set_active_unlocked(self, channel: str, chat_id: str, session_id: str) -> bool:
        meta = self._read_metadata(channel)
        info = meta.get("sessions", {}).get(session_id)
        if info is None or str(info.get("chat_id") or "") != chat_id:
            return False
        session = self._load(channel, session_id)
        if session is None or session.chat_id != chat_id:
            return False
        meta.setdefault("chats", {})[chat_id] = {
            "active": session_id,
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        self._write_metadata(channel, meta)
        self._cache_pop(f"{channel}:{chat_id}")
        return True

    def set_name(self, channel: str, session_id: str, name: str | None) -> None:
        """Update the display name of a session in metadata."""
        self._set_name(channel, session_id, name, cancel_pending=True)

    def _set_name(
        self,
        channel: str,
        session_id: str,
        name: str | None,
        *,
        cancel_pending: bool,
    ) -> None:
        """Persist a name and optionally cancel an older automatic naming task."""
        with self._channel_lock(channel):
            self._set_name_unlocked(
                channel,
                session_id,
                name,
                cancel_pending=cancel_pending,
            )

    def _set_name_unlocked(
        self,
        channel: str,
        session_id: str,
        name: str | None,
        *,
        cancel_pending: bool,
    ) -> None:
        meta = self._read_metadata(channel)
        info = meta.get("sessions", {}).get(session_id)
        if info is None:
            return
        info["name"] = name
        self._write_metadata(channel, meta)

        session_key = f"{channel}:{info.get('chat_id', '')}:{session_id}"
        if cancel_pending and (task := self._naming_tasks.get(session_key)) is not None:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        for _, cached in self._cache_snapshot():
            if cached.channel == channel and cached.id == session_id:
                cached.name = name

    def delete_session(
        self,
        channel: str,
        session_id: str,
        *,
        allow_active: bool = False,
        expected_revision: int | None = None,
    ) -> bool:
        """Atomically remove one session from the index and durable storage.

        The payload is first moved to a same-directory tombstone. A failed
        index commit restores that payload, while a successful commit blocks
        every later save for the same session key before the tombstone is sent
        to the OS trash.
        """
        initial_meta = self._read_metadata(channel)
        info = initial_meta.get("sessions", {}).get(session_id)
        if info is None:
            return False
        chat_id = info.get("chat_id", "")
        session_key = f"{channel}:{chat_id}:{session_id}"
        lock = self._write_lock(session_key)
        path = self._session_path(channel, session_id)
        tombstone = path.with_name(f".{path.name}.{uuid.uuid4().hex}.deleted")
        with lock, self._channel_lock(channel):
            meta = self._read_metadata(channel)
            current_info = meta.get("sessions", {}).get(session_id)
            if current_info is None:
                return False
            current_chat_id = current_info.get("chat_id", "")
            if current_chat_id != chat_id:
                raise SessionStorageError(
                    f"session {channel}:{session_id} changed ownership during deletion"
                )
            disk_revision = self._read_session_revision(path)
            if expected_revision is not None and disk_revision != expected_revision:
                raise SessionRevisionConflictError(
                    f"session {channel}:{session_id} revision conflict: "
                    f"expected {expected_revision}, found {disk_revision}"
                )
            chat_meta = meta.get("chats", {}).get(chat_id) or {}
            if chat_meta.get("active") == session_id and not allow_active:
                return False
            try:
                if path.exists():
                    os.replace(path, tombstone)
                meta["sessions"].pop(session_id, None)
                if chat_meta.get("active") == session_id:
                    meta.get("chats", {}).pop(chat_id, None)
                self._write_metadata(channel, meta)
            except Exception as exc:
                if tombstone.exists() and not path.exists():
                    try:
                        os.replace(tombstone, path)
                    except OSError:
                        logger.exception(
                            "Failed to restore session after delete rollback: {}", path
                        )
                logger.error("Failed to delete session {}: {}", path, exc)
                return False
            self._mark_deleted(session_key)
            for key, cached in self._cache_snapshot():
                if cached.channel == channel and cached.id == session_id:
                    self._cache_pop(key)
            naming_task = self._naming_tasks.pop(session_key, None)
            if naming_task is not None and not naming_task.done():
                naming_task.cancel()
        if not tombstone.exists():
            return True
        try:
            from send2trash import send2trash

            send2trash(str(tombstone))
        except Exception as exc:
            logger.warning("Session tombstone retained at {}: {}", tombstone, exc)
        return True

    def delete_active_and_replace(
        self,
        channel: str,
        session_id: str,
        *,
        expected_revision: int | None = None,
    ) -> Session | None:
        """Atomically replace an active session while retiring its payload."""
        initial_meta = self._read_metadata(channel)
        info = initial_meta.get("sessions", {}).get(session_id)
        if info is None:
            return None
        chat_id = str(info.get("chat_id") or "")
        session_key = f"{channel}:{chat_id}:{session_id}"
        lock = self._write_lock(session_key)
        old_path = self._session_path(channel, session_id)
        tombstone = old_path.with_name(f".{old_path.name}.{uuid.uuid4().hex}.deleted")
        replacement: Session | None = None
        replacement_path: Path | None = None
        with lock, self._channel_lock(channel):
            meta = self._read_metadata(channel)
            current_info = meta.get("sessions", {}).get(session_id)
            chat_meta = meta.get("chats", {}).get(chat_id) or {}
            if current_info is None or chat_meta.get("active") != session_id:
                return None
            disk_revision = self._read_session_revision(old_path)
            if expected_revision is not None and disk_revision != expected_revision:
                raise SessionRevisionConflictError(
                    f"session {channel}:{session_id} revision conflict: "
                    f"expected {expected_revision}, found {disk_revision}"
                )

            replacement_id = self._generate_id(channel)
            now = datetime.now(timezone.utc)
            replacement = Session(
                id=replacement_id,
                channel=channel,
                chat_id=chat_id,
                created_at=now,
                updated_at=now,
            )
            replacement_path = self._session_path(channel, replacement_id)
            self._write_session_file(replacement_path, replacement)
            candidate = deepcopy(meta)
            candidate.setdefault("sessions", {}).pop(session_id, None)
            candidate["sessions"][replacement_id] = {
                "chat_id": chat_id,
                "name": None,
                "created_at": now.isoformat(),
                "last_active": now.isoformat(),
                "message_count": 0,
            }
            candidate.setdefault("chats", {})[chat_id] = {
                "active": replacement_id,
                "last_active": now.isoformat(),
            }
            try:
                if old_path.exists():
                    os.replace(old_path, tombstone)
                self._write_metadata(channel, candidate)
            except BaseException:
                if tombstone.exists() and not old_path.exists():
                    try:
                        os.replace(tombstone, old_path)
                    except OSError:
                        logger.exception(
                            "Failed to restore active session after replace rollback: {}",
                            old_path,
                        )
                try:
                    replacement_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "Failed to remove replacement session after rollback: {}",
                        replacement_path,
                    )
                raise

            self._mark_deleted(session_key)
            self._cache_set(f"{channel}:{chat_id}", replacement)
            naming_task = self._naming_tasks.pop(session_key, None)
            if naming_task is not None and not naming_task.done():
                naming_task.cancel()

        if tombstone.exists():
            try:
                from send2trash import send2trash

                send2trash(str(tombstone))
            except Exception as exc:
                logger.warning("Session tombstone retained at {}: {}", tombstone, exc)
        return replacement

    def get_session(
        self,
        channel: str,
        session_id: str,
        *,
        chat_id: str | None = None,
    ) -> Session | None:
        """Load a session by channel and ID, optionally enforcing its owner chat."""
        session = self._load(channel, session_id)
        if session is not None and chat_id is not None and session.chat_id != chat_id:
            return None
        return session

    def list_sessions(
        self,
        channel: str,
        min_turns: int = 3,
        limit: int = 10,
        *,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return sessions for *channel* with turn_count >= min_turns, sorted by last_active desc."""
        with self._channel_lock(channel):
            return self._list_sessions_unlocked(
                channel,
                min_turns=min_turns,
                limit=limit,
                chat_id=chat_id,
            )

    def _list_sessions_unlocked(
        self,
        channel: str,
        min_turns: int = 3,
        limit: int = 10,
        *,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        meta = self._read_metadata(channel)
        sessions_meta = meta.get("sessions", {})
        results = []
        for sid, info in sessions_meta.items():
            if chat_id is not None and str(info.get("chat_id") or "") != chat_id:
                continue
            path = self._session_path(channel, sid)
            if not path.exists():
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if not first_line:
                        continue
                    data = json.loads(first_line)
                    msg_count = info.get("message_count", 0)
                    last_compacted = data.get("last_compacted", 0)
                    # Quick turn-count estimate from raw message count;
                    # accurate count requires full session load
                    results.append(
                        {
                            "id": sid,
                            "name": info.get("name"),
                            "chat_id": info.get("chat_id", ""),
                            "last_active": info.get("last_active", ""),
                            "created_at": info.get("created_at", ""),
                            "message_count": msg_count,
                            "last_compacted": last_compacted,
                        }
                    )
            except Exception:
                continue

        results.sort(key=lambda x: x.get("last_active", ""), reverse=True)
        # Re-filter with accurate turn counts for sessions near the threshold
        filtered = []
        for r in results[: limit * 2]:  # generous over-fetch for turn counting
            if len(filtered) >= limit:
                break
            s = self._load(channel, r["id"])
            if s is None:
                continue
            turns = self._count_turns(s)
            if turns >= min_turns:
                r["turn_count"] = turns
                filtered.append(r)

        return filtered[:limit]

    # -- system session (cron / heartbeat / transient) ------------------------

    def get_system_session(self, key: str) -> Session:
        """Return a system session keyed by an arbitrary string (no metadata)."""
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        # Try loading from disk
        filename = key.replace(":", "_")
        # Sanitize filename
        for ch in r'<>:"/\|?*':
            filename = filename.replace(ch, "_")
        path = self._system_dir / f"{filename}.jsonl"
        if path.exists():
            try:
                s = self._load_raw(path, id=key, channel="_system", chat_id=key)
                if s:
                    self._cache_set(key, s)
                    return s
            except Exception:
                pass

        s = Session(
            id=key,
            channel="_system",
            chat_id=key,
            created_at=datetime.now(timezone.utc),
        )
        self._cache_set(key, s)
        return s

    # -- internal helpers ----------------------------------------------------

    def _recover_delete_tombstones(self) -> None:
        """Finish or roll back session deletion interrupted by process exit."""
        for channel_dir in self.sessions_dir.iterdir():
            if not channel_dir.is_dir() or channel_dir.name == "_system":
                continue
            try:
                metadata = self._read_metadata(channel_dir.name)
            except SessionStorageError as exc:
                logger.error(
                    "Session metadata is unreadable; retaining delete tombstones in {}: {}",
                    channel_dir,
                    exc,
                )
                continue
            known_sessions = metadata.get("sessions", {})
            for tombstone in channel_dir.glob(".*.jsonl.*.deleted"):
                parts = tombstone.name[1:].rsplit(".", 2)
                if len(parts) != 3 or parts[2] != "deleted":
                    continue
                original = channel_dir / parts[0]
                session_id = original.stem
                if session_id in known_sessions and not original.exists():
                    try:
                        os.replace(tombstone, original)
                        logger.warning("Restored interrupted session deletion: {}", original)
                    except OSError:
                        logger.exception("Failed to restore interrupted session deletion: {}", original)
                    continue
                try:
                    from send2trash import send2trash

                    send2trash(str(tombstone))
                except Exception as exc:
                    logger.warning("Session tombstone retained at {}: {}", tombstone, exc)

    def _channel_dir(self, channel: str) -> Path:
        return ensure_dir(self.sessions_dir / channel)

    def _metadata_path(self, channel: str) -> Path:
        return self._channel_dir(channel) / "metadata.json"

    def _session_path(self, channel: str, session_id: str) -> Path:
        return self._channel_dir(channel) / f"{session_id}.jsonl"

    def _generate_id(self, channel: str) -> str:
        metadata_ids = set(self._read_metadata(channel).get("sessions", {}))
        for _ in range(10):
            candidate = uuid.uuid4().hex[:12]
            if (
                candidate not in metadata_ids
                and not self._session_path(channel, candidate).exists()
                and not any(
                    key.endswith(f":{candidate}") for key in self._deleted_snapshot()
                )
            ):
                return candidate
        return uuid.uuid4().hex

    def _count_turns(self, session: Session) -> int:
        return len(session.get_completed_turn_boundaries())

    def _read_metadata(self, channel: str) -> dict[str, Any]:
        with self._channel_lock(channel):
            path = self._metadata_path(channel)
            if not path.exists():
                return {}
            try:
                with open(path, encoding="utf-8") as f:
                    value = json.load(f)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SessionStorageError(
                    f"cannot read existing metadata {path}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise SessionStorageError(f"existing metadata {path} is not a JSON object")
            for key in ("sessions", "chats"):
                section = value.get(key)
                if section is not None and not isinstance(section, dict):
                    raise SessionStorageError(
                        f"existing metadata {path} has an invalid {key} section"
                    )
            return value

    @staticmethod
    def _read_session_revision(path: Path) -> int | None:
        """Read the effective persisted revision, including legacy files."""
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                first_line = handle.readline().strip()
                if not first_line:
                    raise ValueError("session file is empty")
                first_record = json.loads(first_line)
                if not isinstance(first_record, dict):
                    raise ValueError("session record is not a JSON object")
                has_metadata = first_record.get("_type") == "metadata"
                revision = int(first_record.get("revision", 0) or 0) if has_metadata else 0
                message_count = 0 if has_metadata else 1
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("session record is not a JSON object")
                    message_count += 1
                return max(revision, message_count)
        except (json.JSONDecodeError, OSError, UnicodeError, TypeError, ValueError) as exc:
            raise SessionStorageError(f"cannot inspect existing session {path}: {exc}") from exc

    def _write_metadata(self, channel: str, data: dict[str, Any]) -> None:
        with self._channel_lock(channel):
            path = self._metadata_path(channel)
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            self._atomic_write(path, payload + "\n")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write beside *path* and replace it only after the full write succeeds."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    @classmethod
    def _write_session_file(cls, path: Path, session: Session) -> None:
        metadata_line = {
            "_type": "metadata",
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "compacted_memory": session.compacted_memory,
            "last_compacted": session.last_compacted,
            "compaction_checkpoint": session.compaction_checkpoint,
            "revision": session.revision,
        }
        lines = [json.dumps(metadata_line, ensure_ascii=False)]
        lines.extend(json.dumps(msg, ensure_ascii=False) for msg in session.messages)
        cls._atomic_write(path, "\n".join(lines) + "\n")

    def _load_raw(self, path: Path, id: str, channel: str, chat_id: str) -> Session | None:
        """Load a session from an arbitrary JSONL path."""
        if not path.exists():
            return None
        messages = []
        metadata = {}
        created_at = None
        last_compacted = 0
        compacted_memory = ""
        compaction_checkpoint: dict[str, Any] = {}
        revision = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("_type") == "metadata":
                    metadata = data.get("metadata", {})
                    created_at = (
                        datetime.fromisoformat(data["created_at"])
                        if data.get("created_at")
                        else None
                    )
                    compacted_memory = data.get("compacted_memory", "")
                    last_compacted = data.get("last_compacted", 0)
                    compaction_checkpoint = data.get("compaction_checkpoint", {}) or {}
                    revision = int(data.get("revision", 0) or 0)
                else:
                    messages.append(data)
        last_compacted = min(max(int(last_compacted or 0), 0), len(messages))
        return Session(
            id=id,
            channel=channel,
            chat_id=chat_id,
            messages=messages,
            created_at=created_at or datetime.now(timezone.utc),
            metadata=metadata,
            compacted_memory=compacted_memory,
            last_compacted=last_compacted,
            compaction_checkpoint=compaction_checkpoint,
            revision=max(int(revision or 0), len(messages)),
        )

    async def _auto_name_session(self, session: Session) -> None:
        """Background task: generate and store a session name."""
        if not self._name_generator:
            return
        try:
            name = await self._name_generator(session, session.channel)
            if name:
                with self._channel_lock(session.channel):
                    meta = self._read_metadata(session.channel)
                    info = meta.get("sessions", {}).get(session.id)
                    if info is None:
                        return
                    if current_name := info.get("name"):
                        session.name = str(current_name)
                        return
                    self._set_name(
                        session.channel,
                        session.id,
                        name,
                        cancel_pending=False,
                    )
                    session.name = name
                logger.debug("Auto-named session {}/{} -> {}", session.channel, session.id, name)
        except Exception as e:
            logger.warning(
                "Auto-naming failed for session {}/{}: {}", session.channel, session.id, e
            )

    async def close(self) -> None:
        """Cancel and drain runtime-owned background naming tasks."""
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._naming_tasks.clear()
