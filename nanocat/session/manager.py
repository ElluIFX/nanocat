"""Session management — multi-session per channel with UUID-based IDs."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanocat.utils.helpers import ensure_dir

# Callback: (session, channel) -> name string or None on failure
NameGenerator = Callable[["Session", str], Awaitable[str | None]]


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
    skip_next_nowledge_extraction: bool = False
    last_compacted: int = 0

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
        self.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _find_legal_start(messages: list[dict[str, Any]]) -> int:
        declared: set[str] = set()
        start = 0
        for i, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    start = i + 1
                    declared.clear()
                    for prev in messages[start : i + 1]:
                        if prev.get("role") == "assistant":
                            for tc in prev.get("tool_calls") or []:
                                if isinstance(tc, dict) and tc.get("id"):
                                    declared.add(str(tc["id"]))
        return start

    @classmethod
    def _build_history_view(
        cls,
        messages: list[dict[str, Any]],
        max_messages: int = 500,
    ) -> list[dict[str, Any]]:
        sliced = messages if max_messages == 0 else messages[-max_messages:]
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                sliced = sliced[i:]
                break
        start = cls._find_legal_start(sliced)
        if start:
            sliced = sliced[start:]
        out: list[dict[str, Any]] = []
        for message in sliced:
            entry: dict[str, Any] = {
                "role": message["role"],
                "content": message.get("content", ""),
            }
            for key in ("tool_calls", "tool_call_id", "name", "reasoning_content"):
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
        turns: list[tuple[int, int]] = []
        current_start: int | None = None
        for idx in range(start, end):
            message = self.messages[idx]
            role = message.get("role")
            if role == "user":
                if current_start is None:
                    current_start = idx
                continue
            if role == "tool" and current_start is not None and message.get("name") == "message":
                turns.append((current_start, idx + 1))
                current_start = None
                continue
            if role != "assistant" or current_start is None:
                continue
            if message.get("tool_calls"):
                continue
            turns.append((current_start, idx + 1))
            current_start = None
        return turns

    def clear(self) -> None:
        self.messages = []
        self.compacted_memory = ""
        self.skip_next_nowledge_extraction = False
        self.last_compacted = 0
        self.updated_at = datetime.now(timezone.utc)


class SessionManager:
    """Manages per-channel multi-session storage under {workspace}/sessions/."""

    def __init__(self, sessions_root: Path):
        self.sessions_dir = ensure_dir(sessions_root)
        self._system_dir = ensure_dir(self.sessions_dir / "_system")
        self._cache: dict[str, Session] = {}
        self._name_generator: NameGenerator | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # -- public configuration ------------------------------------------------

    def set_name_generator(self, callback: NameGenerator) -> None:
        self._name_generator = callback

    # -- normal session (channel + metadata) ---------------------------------

    def get_or_create(self, channel: str, chat_id: str) -> Session:
        """Return the active session for (channel, chat_id), creating one if needed."""
        cache_key = f"{channel}:{chat_id}"
        meta = self._read_metadata(channel)
        active_id = meta.get("chats", {}).get(chat_id, {}).get("active")

        if active_id and (s := self._load(channel, active_id)):
            self._cache[cache_key] = s
            return s

        session = self._new_session(channel, chat_id)
        self._cache[cache_key] = session
        return session

    def _new_session(self, channel: str, chat_id: str) -> Session:
        session_id = self._generate_id(channel)
        now = datetime.now(timezone.utc)
        session = Session(
            id=session_id,
            channel=channel,
            chat_id=chat_id,
            created_at=now,
            updated_at=now,
        )
        meta = self._read_metadata(channel)
        meta.setdefault("chats", {})[chat_id] = {
            "active": session_id,
            "last_active": now.isoformat(),
        }
        meta.setdefault("sessions", {})[session_id] = {
            "chat_id": chat_id,
            "name": None,
            "created_at": now.isoformat(),
            "last_active": now.isoformat(),
            "message_count": 0,
        }
        self._write_metadata(channel, meta)
        return session

    def _load(self, channel: str, session_id: str) -> Session | None:
        path = self._session_path(channel, session_id)
        if not path.exists():
            return None
        try:
            messages = []
            metadata = {}
            created_at = None
            last_compacted = 0
            compacted_memory = ""
            skip_next_nowledge_extraction = False
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
                        skip_next_nowledge_extraction = data.get(
                            "skip_next_nowledge_extraction", False
                        )
                        last_compacted = data.get("last_compacted", 0)
                    else:
                        messages.append(data)

            meta = self._read_metadata(channel)
            info = meta.get("sessions", {}).get(session_id, {})
            name = info.get("name")
            return Session(
                id=session_id,
                channel=channel,
                chat_id=info.get("chat_id", ""),
                name=name,
                messages=messages,
                created_at=created_at or datetime.now(timezone.utc),
                metadata=metadata,
                compacted_memory=compacted_memory,
                skip_next_nowledge_extraction=skip_next_nowledge_extraction,
                last_compacted=last_compacted,
            )
        except Exception as e:
            logger.warning("Failed to load session {}/{}: {}", channel, session_id, e)
            return None

    def save(self, session: Session) -> None:
        """Persist session to disk and update metadata."""
        if session.channel == "_system":
            self._save_system(session)
            return
        self._save_normal(session)

    def _save_normal(self, session: Session) -> None:
        path = self._session_path(session.channel, session.id)
        self._write_session_file(path, session)

        cache_key = f"{session.channel}:{session.chat_id}"
        self._cache[cache_key] = session

        # Update metadata
        meta = self._read_metadata(session.channel)
        info = meta.setdefault("sessions", {}).setdefault(session.id, {})
        info["last_active"] = session.updated_at.isoformat()
        info["message_count"] = len(session.messages)
        info["chat_id"] = session.chat_id
        meta.setdefault("chats", {}).setdefault(session.chat_id, {})["last_active"] = (
            session.updated_at.isoformat()
        )
        self._write_metadata(session.channel, meta)

        # Trigger background naming if needed
        turn_count = self._count_turns(session)
        if turn_count >= 3 and not session.name and self._name_generator:
            task = asyncio.create_task(self._auto_name_session(session))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    def _save_system(self, session: Session) -> None:
        """Persist a system session (no metadata)."""
        filename = session.id.replace(":", "_")
        for ch in r'<>:"/\|?*':
            filename = filename.replace(ch, "_")
        path = self._system_dir / f"{filename}.jsonl"
        self._write_session_file(path, session)
        self._cache[session.id] = session

    def set_active(self, channel: str, chat_id: str, session_id: str) -> bool:
        """Switch the active session for (channel, chat_id). Returns True on success."""
        meta = self._read_metadata(channel)
        if session_id not in meta.get("sessions", {}):
            return False
        meta.setdefault("chats", {})[chat_id] = {
            "active": session_id,
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        self._write_metadata(channel, meta)
        self._cache.pop(f"{channel}:{chat_id}", None)
        return True

    def set_name(self, channel: str, session_id: str, name: str | None) -> None:
        """Update the display name of a session in metadata."""
        meta = self._read_metadata(channel)
        if session_id in meta.get("sessions", {}):
            meta["sessions"][session_id]["name"] = name
            self._write_metadata(channel, meta)

    def get_session(self, channel: str, session_id: str) -> Session | None:
        """Load a session by channel and session ID."""
        return self._load(channel, session_id)

    def list_sessions(
        self, channel: str, min_turns: int = 3, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return sessions for *channel* with turn_count >= min_turns, sorted by last_active desc."""
        meta = self._read_metadata(channel)
        sessions_meta = meta.get("sessions", {})
        results = []
        for sid, info in sessions_meta.items():
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
        if key in self._cache:
            return self._cache[key]

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
                    self._cache[key] = s
                    return s
            except Exception:
                pass

        s = Session(
            id=key,
            channel="_system",
            chat_id=key,
            created_at=datetime.now(timezone.utc),
        )
        self._cache[key] = s
        return s

    # -- internal helpers ----------------------------------------------------

    def _channel_dir(self, channel: str) -> Path:
        return ensure_dir(self.sessions_dir / channel)

    def _metadata_path(self, channel: str) -> Path:
        return self._channel_dir(channel) / "metadata.json"

    def _session_path(self, channel: str, session_id: str) -> Path:
        return self._channel_dir(channel) / f"{session_id}.jsonl"

    def _generate_id(self, channel: str) -> str:
        for _ in range(10):
            candidate = uuid.uuid4().hex[:6]
            if not self._session_path(channel, candidate).exists():
                return candidate
        return uuid.uuid4().hex[:6]

    def _count_turns(self, session: Session) -> int:
        return len(session.get_completed_turn_boundaries())

    def _read_metadata(self, channel: str) -> dict[str, Any]:
        path = self._metadata_path(channel)
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_metadata(self, channel: str, data: dict[str, Any]) -> None:
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
            "skip_next_nowledge_extraction": session.skip_next_nowledge_extraction,
            "last_compacted": session.last_compacted,
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
        skip_next_nowledge_extraction = False
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
                    skip_next_nowledge_extraction = data.get("skip_next_nowledge_extraction", False)
                    last_compacted = data.get("last_compacted", 0)
                else:
                    messages.append(data)
        return Session(
            id=id,
            channel=channel,
            chat_id=chat_id,
            messages=messages,
            created_at=created_at or datetime.now(timezone.utc),
            metadata=metadata,
            compacted_memory=compacted_memory,
            skip_next_nowledge_extraction=skip_next_nowledge_extraction,
            last_compacted=last_compacted,
        )

    async def _auto_name_session(self, session: Session) -> None:
        """Background task: generate and store a session name."""
        if not self._name_generator:
            return
        try:
            name = await self._name_generator(session, session.channel)
            if name:
                self.set_name(session.channel, session.id, name)
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
