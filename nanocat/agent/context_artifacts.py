"""Durable storage and bounded retrieval for large tool observations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanocat.agent.tools.base import Tool

_DEFAULT_INLINE_CHARS = 6000
_DEFAULT_LOOKUP_CHARS = 6000
_PREVIEW_CHARS = 1200
_MAX_SEARCH_RESULTS = 8


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    tool_name: str
    tool_call_id: str | None
    total_chars: int
    total_lines: int


class ContextArtifactStore:
    """Persist complete tool observations while exposing bounded model views."""

    def __init__(self, sessions_root: Path, inline_chars: int = _DEFAULT_INLINE_CHARS):
        self.root = sessions_root / "_artifacts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.inline_chars = max(512, inline_chars)

    @staticmethod
    def _session_dir_name(session_key: str) -> str:
        return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]

    def _session_dir(self, session_key: str) -> Path:
        path = self.root / self._session_dir_name(session_key)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
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

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _strip_success_markers(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ContextArtifactStore._strip_success_markers(item)
                for key, item in value.items()
                if not (key == "ok" and item is True)
            }
        if isinstance(value, list):
            return [ContextArtifactStore._strip_success_markers(item) for item in value]
        return value

    @classmethod
    def _sanitized_text(cls, value: Any) -> str:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return value
            cleaned = cls._strip_success_markers(parsed)
            if cleaned == parsed:
                return value
            return json.dumps(cleaned, ensure_ascii=False)
        return cls._text(cls._strip_success_markers(value))

    @staticmethod
    def _preview(text: str) -> str:
        if len(text) <= _PREVIEW_CHARS * 2:
            return text
        return (
            text[:_PREVIEW_CHARS]
            + f"\n...[{len(text) - _PREVIEW_CHARS * 2} chars omitted; use context_lookup]...\n"
            + text[-_PREVIEW_CHARS:]
        )

    def capture(
        self,
        session_key: str,
        tool_name: str,
        tool_call_id: str | None,
        result: Any,
    ) -> str:
        """Return a bounded LLM view and archive large observations locally."""
        text = self._sanitized_text(result)
        if len(text) <= self.inline_chars:
            return text

        artifact_id = f"art_{uuid.uuid4().hex[:16]}"
        session_dir = self._session_dir(session_key)
        payload_path = session_dir / f"{artifact_id}.txt"
        metadata_path = session_dir / f"{artifact_id}.json"
        metadata = {
            "artifact_id": artifact_id,
            "session_hash": self._session_dir_name(session_key),
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "total_chars": len(text),
            "total_lines": text.count("\n") + 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        self._atomic_write(payload_path, text)
        self._atomic_write(metadata_path, json.dumps(metadata, ensure_ascii=False))
        ref = ArtifactRef(
            artifact_id=artifact_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            total_chars=len(text),
            total_lines=text.count("\n") + 1,
        )
        return json.dumps(
            {
                "status": "stored",
                "tool": ref.tool_name,
                "artifact_id": ref.artifact_id,
                "total_chars": ref.total_chars,
                "total_lines": ref.total_lines,
                "preview": self._preview(text),
                "hint": "Use context_lookup with artifact_id or query to retrieve more.",
            },
            ensure_ascii=False,
        )

    def _metadata_files(self, session_key: str) -> list[Path]:
        return list(self._session_dir(session_key).glob("art_*.json"))

    def _load_record(self, session_key: str, artifact_id: str) -> tuple[dict[str, Any], str] | None:
        if not re.fullmatch(r"art_[0-9a-f]{16}", artifact_id):
            return None
        session_dir = self._session_dir(session_key)
        metadata_path = session_dir / f"{artifact_id}.json"
        payload_path = session_dir / f"{artifact_id}.txt"
        if not metadata_path.is_file() or not payload_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("artifact_id") != artifact_id:
                return None
            if metadata.get("session_hash") not in (None, self._session_dir_name(session_key)):
                return None
            return metadata, payload_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return None

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 80)] + "\n...[lookup output clipped]..."

    def lookup(
        self,
        session_key: str,
        *,
        query: str | None = None,
        artifact_id: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        pattern: str | None = None,
        max_chars: int = _DEFAULT_LOOKUP_CHARS,
    ) -> str:
        """Search or read an artifact without returning unbounded content."""
        max_chars = max(512, max_chars)
        if artifact_id:
            loaded = self._load_record(session_key, artifact_id)
            if loaded is None:
                return json.dumps({"error": "artifact not found", "artifact_id": artifact_id})
            metadata, text = loaded
            if pattern:
                try:
                    matcher = re.compile(pattern, re.IGNORECASE)
                except re.error as exc:
                    return json.dumps({"error": f"invalid pattern: {exc}"}, ensure_ascii=False)
                selected = [
                    f"{index}: {line}"
                    for index, line in enumerate(text.splitlines(), start=1)
                    if matcher.search(line)
                ]
                content = "\n".join(selected)
            else:
                lines = text.splitlines()
                start = max(1, start_line or 1)
                end = min(len(lines), end_line or len(lines))
                content = "\n".join(
                    f"{index}: {lines[index - 1]}" for index in range(start, end + 1)
                )
            return json.dumps(
                {
                    "artifact_id": artifact_id,
                    "tool": metadata.get("tool_name"),
                    "range": {"start_line": start_line, "end_line": end_line},
                    "content": self._clip(content, max_chars),
                },
                ensure_ascii=False,
            )

        if not query:
            return json.dumps({"error": "provide query or artifact_id"}, ensure_ascii=False)

        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for metadata_path in self._metadata_files(session_key):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                artifact_id = metadata["artifact_id"]
                loaded = self._load_record(session_key, artifact_id)
                if loaded is None:
                    continue
                _, text = loaded
            except (OSError, UnicodeError, ValueError, KeyError):
                continue
            haystack = " ".join(
                str(metadata.get(key, "")) for key in ("tool_name", "tool_call_id", "artifact_id")
            )
            if needle not in haystack.casefold() and needle not in text.casefold():
                continue
            matches.append(
                {
                    "artifact_id": artifact_id,
                    "tool": metadata.get("tool_name"),
                    "total_chars": metadata.get("total_chars"),
                    "preview": self._preview(text),
                }
            )
            if len(matches) >= _MAX_SEARCH_RESULTS:
                break
        return json.dumps(
            {"query": query, "matches": matches},
            ensure_ascii=False,
        )


class ContextLookupTool(Tool):
    def __init__(self, store: ContextArtifactStore):
        self._store = store
        self._session_key: ContextVar[str | None] = ContextVar(
            "context_lookup_session_key", default=None
        )

    @property
    def name(self) -> str:
        return "context_lookup"

    @property
    def description(self) -> str:
        return (
            "Search or read large previous tool outputs saved outside the active context. "
            "Use artifact_id for exact retrieval, or query to find relevant archived output."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search archived tool output."},
                "artifact_id": {"type": "string", "description": "Artifact reference to read."},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "pattern": {"type": "string", "description": "Case-insensitive regex filter."},
            },
            "additionalProperties": False,
        }

    def set_session_key(self, session_key: str) -> None:
        self._session_key.set(session_key)

    async def execute(
        self,
        query: str | None = None,
        artifact_id: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        pattern: str | None = None,
        **_: Any,
    ) -> str:
        session_key = self._session_key.get()
        if not session_key:
            return json.dumps({"error": "context lookup has no session"}, ensure_ascii=False)
        return self._store.lookup(
            session_key,
            query=query,
            artifact_id=artifact_id,
            start_line=start_line,
            end_line=end_line,
            pattern=pattern,
        )

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not params.get("query") and not params.get("artifact_id"):
            errors.append("one of query or artifact_id is required")
        return errors
