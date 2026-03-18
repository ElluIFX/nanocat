"""Session management for conversation history."""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir
from nanobot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    Consolidation only advances an offset and updates the session-level
    consolidated memory block; raw messages remain on disk.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    consolidated_memory: str = ""
    skip_next_nowledge_extraction: bool = False
    last_consolidated: int = (
        0  # Exclusive raw-message offset already folded into consolidated_memory
    )

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {"role": role, "content": content, "timestamp": datetime.now().isoformat(), **kwargs}
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @staticmethod
    def _find_legal_start(messages: list[dict[str, Any]]) -> int:
        """Find first index where every tool result has a matching assistant tool_call."""
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
        """Convert a raw message slice into an LLM-safe history view."""
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
            entry: dict[str, Any] = {"role": message["role"], "content": message.get("content", "")}
            for key in ("tool_calls", "tool_call_id", "name"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)
        return out

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a legal tool-call boundary."""
        unconsolidated = self.messages[self.last_consolidated :]
        return self._build_history_view(unconsolidated, max_messages=max_messages)

    def get_history_from(self, start_idx: int, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return an LLM-safe history view from an arbitrary raw-message offset."""
        start = max(0, start_idx)
        return self._build_history_view(self.messages[start:], max_messages=max_messages)

    def get_completed_turn_boundaries(
        self,
        start_idx: int | None = None,
        end_idx: int | None = None,
    ) -> list[tuple[int, int]]:
        """Return completed user/assistant turn boundaries in raw message indices.

        A turn starts at a user message and ends at the final assistant reply for
        that user request. Intermediate assistant tool-calls and tool results are
        included in the same turn and do not count as separate turns.
        """
        start = self.last_consolidated if start_idx is None else max(0, start_idx)
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
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.consolidated_memory = ""
        self.skip_next_nowledge_extraction = False
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanobot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0
            consolidated_memory = ""
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
                        consolidated_memory = data.get("consolidated_memory", "")
                        skip_next_nowledge_extraction = data.get(
                            "skip_next_nowledge_extraction",
                            False,
                        )
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                consolidated_memory=consolidated_memory,
                skip_next_nowledge_extraction=skip_next_nowledge_extraction,
                last_consolidated=last_consolidated,
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "consolidated_memory": session.consolidated_memory,
                "skip_next_nowledge_extraction": session.skip_next_nowledge_extraction,
                "last_consolidated": session.last_consolidated,
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def _get_saved_dir(self, key: str) -> Path:
        """Return the directory that holds named saves for a session key."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / safe_key

    def save_named(self, session: Session, name: str) -> Path:
        """Write a named snapshot of *session* to disk, silently overwriting if it exists."""
        save_dir = ensure_dir(self._get_saved_dir(session.key))
        path = save_dir / f"{name}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "consolidated_memory": session.consolidated_memory,
                "skip_next_nowledge_extraction": session.skip_next_nowledge_extraction,
                "last_consolidated": session.last_consolidated,
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        logger.debug("Named session saved: {} -> {}", session.key, path)
        return path

    def load_named(self, key: str, name: str) -> Session | None:
        """Replace the active session with a named snapshot. Returns the loaded Session or None."""
        path = self._get_saved_dir(key) / f"{name}.jsonl"
        if not path.exists():
            return None
        # Overwrite the active session file so the state is durable immediately.
        active_path = self._get_session_path(key)
        shutil.copy2(str(path), str(active_path))
        self.invalidate(key)
        session = self.get_or_create(key)
        # The saved file carries the original key; keep it consistent.
        session.key = key
        self._cache[key] = session
        logger.debug("Named session loaded: {} <- {}", key, path)
        return session

    def list_named(self, key: str) -> list[str]:
        """Return sorted names of all saved snapshots for *key* (without .jsonl suffix)."""
        save_dir = self._get_saved_dir(key)
        if not save_dir.exists():
            return []
        return sorted(p.stem for p in save_dir.glob("*.jsonl"))

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append(
                                {
                                    "key": key,
                                    "created_at": data.get("created_at"),
                                    "updated_at": data.get("updated_at"),
                                    "path": str(path),
                                }
                            )
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
