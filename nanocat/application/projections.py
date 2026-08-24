"""Read models for conversation and execution-trajectory clients."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from nanocat.observability.activity import ActivityEvent
from nanocat.observability.redaction import redact_mapping, redact_value
from nanocat.session.manager import Session

_MAX_PROJECTED_CONTENT_CHARS = 64_000
_MAX_TOOL_RESULT_PREVIEW_CHARS = 12_000
_MAX_REASONING_CHARS = 32_000
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_ARTIFACT_KINDS = frozenset({"text", "markdown", "json", "code", "diff", "image", "binary"})


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = "\x1f".join("" if part is None else str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def _timestamp(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        value = parsed
    value = redact_value(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = json.dumps(str(value), ensure_ascii=False)
    if len(encoded) > _MAX_PROJECTED_CONTENT_CHARS:
        return {
            "truncated": True,
            "totalChars": len(encoded),
            "preview": encoded[:_MAX_PROJECTED_CONTENT_CHARS],
        }
    try:
        return json.loads(encoded)
    except json.JSONDecodeError:
        return str(value)


def _public_content(value: Any) -> Any:
    """Remove runtime-only image paths from persisted provider content."""
    safe = _json_safe(value)

    def sanitize(item: Any) -> Any:
        if (
            isinstance(item, str)
            and item.startswith("[image: ")
            and item.endswith("]")
        ):
            return "[image attachment]"
        if isinstance(item, list):
            return [sanitize(child) for child in item]
        if isinstance(item, dict):
            return {key: sanitize(child) for key, child in item.items()}
        return item

    return sanitize(safe)


def _bounded_public_value(value: Any, *, max_chars: int) -> Any:
    """Project one browser-visible value under an encoded character budget."""
    safe = _public_content(value)
    try:
        encoded = json.dumps(safe, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = str(safe)
    if len(encoded) <= max_chars:
        return safe
    return {
        "truncated": True,
        "totalChars": len(encoded),
        "preview": encoded[:max_chars],
    }


def _public_tool_result(message: Mapping[str, Any]) -> dict[str, Any]:
    """Expose a redacted, bounded preview of a persisted tool result."""
    raw_status = str(message.get("status") or "").casefold()
    failed = message.get("is_error") is True or raw_status in {
        "error",
        "failed",
        "failure",
    }
    raw_content = message.get("content")
    safe_content = _json_safe(raw_content)
    try:
        encoded = json.dumps(safe_content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = str(safe_content)
    truncated = len(encoded) > _MAX_TOOL_RESULT_PREVIEW_CHARS
    if truncated:
        safe_content = {
            "truncated": True,
            "totalChars": len(encoded),
            "preview": encoded[:_MAX_TOOL_RESULT_PREVIEW_CHARS],
        }
    result = {
        "summary": "Tool execution failed." if failed else "Tool execution completed.",
        "status": "failed" if failed else "completed",
        "redacted": True,
        "preview": safe_content,
    }
    artifact_refs = _public_artifact_refs(message.get("artifact_refs"))
    if artifact_refs:
        result["artifactRefs"] = artifact_refs
    return result


def _public_reasoning(message: Mapping[str, Any]) -> dict[str, Any]:
    """Project provider reasoning while removing opaque transport credentials."""
    projected: dict[str, Any] = {}
    reasoning = message.get("reasoning_content")
    if reasoning not in (None, "", []):
        projected["reasoningContent"] = _bounded_public_value(
            reasoning,
            max_chars=_MAX_REASONING_CHARS,
        )

    blocks = message.get("thinking_blocks")
    if isinstance(blocks, (list, tuple)):
        safe_blocks: list[Any] = []
        for block in blocks[:64]:
            if isinstance(block, Mapping):
                display = {
                    str(key): value
                    for key, value in block.items()
                    if str(key).casefold()
                    not in {"signature", "encrypted_content", "encryptedcontent"}
                }
                safe_blocks.append(display)
            else:
                safe_blocks.append(block)
        if safe_blocks:
            projected["thinkingBlocks"] = _bounded_public_value(
                safe_blocks,
                max_chars=_MAX_REASONING_CHARS,
            )
    return projected


def _historical_turn_id(session: Session, start: int, end: int) -> str:
    for message in session.messages[start:end]:
        value = str(message.get("turn_id") or "").strip()
        if value:
            return value
    return _stable_id("historical_turn", session.key, start)


def _historical_turn_timing(
    session: Session,
    start: int,
    end: int,
) -> dict[str, Any]:
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    terminal_status: str | None = None
    for message in session.messages[start:end]:
        started_at = started_at or _timestamp(message.get("turn_started_at"))
        candidate_end = _timestamp(message.get("turn_ended_at"))
        if candidate_end:
            ended_at = candidate_end
        candidate_duration = message.get("turn_duration_ms")
        if candidate_duration is not None:
            try:
                duration_ms = max(0, int(candidate_duration))
            except (TypeError, ValueError):
                pass
        candidate_status = str(message.get("turn_status") or "").strip()
        if candidate_status:
            terminal_status = candidate_status

    if started_at is None:
        started_at = _timestamp(session.messages[start].get("timestamp"))
    if ended_at is None:
        ended_at = _timestamp(session.messages[end - 1].get("timestamp"))
    if duration_ms is None and started_at and ended_at:
        try:
            started = datetime.fromisoformat(started_at)
            ended = datetime.fromisoformat(ended_at)
            duration_ms = max(0, int((ended - started).total_seconds() * 1000))
        except ValueError:
            duration_ms = None
    return {
        key: value
        for key, value in {
            "startedAt": started_at,
            "endedAt": ended_at,
            "durationMs": duration_ms,
            "terminalStatus": terminal_status,
        }.items()
        if value not in (None, "")
    }


def _tool_call(call: Mapping[str, Any]) -> dict[str, Any]:
    function = call.get("function")
    function_data = function if isinstance(function, Mapping) else {}
    arguments: Any = function_data.get("arguments", call.get("arguments"))
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            arguments = parsed
        except json.JSONDecodeError:
            arguments = arguments[:_MAX_PROJECTED_CONTENT_CHARS]
    if isinstance(arguments, Mapping):
        arguments = redact_mapping(arguments)
    else:
        arguments = _json_safe(arguments)
    return {
        "id": str(call.get("id") or ""),
        "name": str(function_data.get("name") or call.get("name") or "unknown"),
        "arguments": arguments,
    }


def _public_artifact_refs(value: Any) -> list[dict[str, Any]]:
    """Project untrusted persisted references through the public artifact schema."""
    if not isinstance(value, (list, tuple)):
        return []
    refs: list[dict[str, Any]] = []
    for item in value[:16]:
        if isinstance(item, str):
            if _ARTIFACT_ID_RE.fullmatch(item):
                refs.append({"id": item, "name": "Attachment", "kind": "binary"})
            else:
                refs.append(
                    {
                        "id": f"invalid-{hashlib.sha256(item.encode()).hexdigest()[:16]}",
                        "name": "Unavailable attachment",
                        "kind": "binary",
                        "expired": True,
                    }
                )
            continue
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        artifact_id = str(item["id"])
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            refs.append(
                {
                    "id": f"invalid-{hashlib.sha256(artifact_id.encode()).hexdigest()[:16]}",
                    "name": "Unavailable attachment",
                    "kind": "binary",
                    "expired": True,
                }
            )
            continue
        raw_name = str(item.get("name") or "Attachment")
        safe_name = re.split(r"[/\\]", raw_name)[-1].strip()[:255] or "Attachment"
        kind = str(item.get("kind") or "binary")
        ref = {
            "id": artifact_id,
            "name": safe_name,
            "kind": kind if kind in _ARTIFACT_KINDS else "binary",
        }
        if isinstance(item.get("size"), int) and item["size"] >= 0:
            ref["size"] = item["size"]
        media_type = item.get("mediaType", item.get("media_type"))
        if isinstance(media_type, str):
            normalized_media_type = media_type.split(";", 1)[0].strip().casefold()
            if _MEDIA_TYPE_RE.fullmatch(normalized_media_type):
                ref["mediaType"] = normalized_media_type
        if item.get("expired") is True:
            ref["expired"] = True
        refs.append(ref)
    return refs


@dataclass(frozen=True, slots=True)
class ProjectionItem:
    """One UI-ready item without transport or framework dependencies."""

    id: str
    type: str
    source: str
    sequence: int | None = None
    timestamp: str | None = None
    status: str | None = None
    turn_id: str | None = None
    request_id: str | None = None
    parent_id: str | None = None
    role: str | None = None
    summary: str = ""
    content: Any = None
    detail_available: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable camelCase API representation."""
        value = {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "status": self.status,
            "turnId": self.turn_id,
            "requestId": self.request_id,
            "parentId": self.parent_id,
            "role": self.role,
            "summary": self.summary,
            "content": self.content,
            "detailAvailable": self.detail_available,
            "metadata": dict(self.metadata),
        }
        return {key: item for key, item in value.items() if item not in (None, "", {})}


@dataclass(frozen=True, slots=True)
class ProjectionPage:
    """Offset page used after source-specific activity pagination."""

    items: tuple[ProjectionItem, ...]
    next_offset: int | None
    has_more: bool
    detail_available: bool
    total_count: int
    previous_offset: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "nextOffset": self.next_offset,
            "hasMore": self.has_more,
            "detailAvailable": self.detail_available,
            "totalCount": self.total_count,
            "previousOffset": self.previous_offset,
        }


def _page(
    items: Sequence[ProjectionItem],
    *,
    offset: int,
    limit: int,
    detail_available: bool,
) -> ProjectionPage:
    if offset < 0:
        raise ValueError("projection offset cannot be negative")
    if not 1 <= limit <= 1_000:
        raise ValueError("projection page limit must be between 1 and 1000")
    selected = tuple(items[offset : offset + limit])
    consumed = offset + len(selected)
    has_more = consumed < len(items)
    return ProjectionPage(
        items=selected,
        next_offset=consumed if has_more else None,
        has_more=has_more,
        detail_available=detail_available,
        total_count=len(items),
        previous_offset=max(0, offset - limit) if offset > 0 else None,
    )


def _activity_item(event: ActivityEvent) -> ProjectionItem:
    artifact_refs = event.metadata.get("artifactRefs")
    if not isinstance(artifact_refs, list):
        artifact_refs = list(event.artifact_refs)
    artifact_refs = _public_artifact_refs(artifact_refs)
    details = {
        **dict(event.metadata),
        "input": event.redacted_input,
        "output": event.redacted_output,
        "artifactRefs": artifact_refs,
        "phase": event.phase,
        "startedAt": event.started_at.isoformat() if event.started_at else None,
        "endedAt": event.ended_at.isoformat() if event.ended_at else None,
        "durationMs": event.duration_ms,
        "model": event.model,
        "usage": dict(event.usage),
    }
    details = {key: value for key, value in details.items() if value not in (None, "", [], {})}
    content = event.redacted_output
    if content is None and event.summary:
        content = event.summary
    return ProjectionItem(
        id=event.node_id or event.event_id or _stable_id("activity", event.sequence, event.type),
        type=event.type,
        source=event.source,
        sequence=event.sequence,
        timestamp=event.recorded_at.isoformat(),
        status=event.status,
        turn_id=event.turn_id,
        request_id=event.request_id,
        parent_id=event.parent_id,
        summary=event.summary,
        content=content,
        detail_available=True,
        metadata=details,
    )


def _conversation_visible(event: ActivityEvent) -> bool:
    if event.metadata.get("conversationVisible") is not None:
        return bool(event.metadata["conversationVisible"])
    category = event.type.split(".", 1)[0]
    return category in {
        "approval",
        "assistant",
        "compaction",
        "message",
        "progress",
        "subagent",
        "tool",
        "turn",
        "user",
    }


_TERMINAL_ACTIVITY_TYPES = frozenset(
    {"assistant.final", "turn.completed", "turn.failed", "turn.cancelled"}
)
_TRANSIENT_ACTIVITY_TYPES = frozenset(
    {
        "assistant.progress",
        "assistant.thinking",
        "tool.event",
        "turn.cancelling",
        "turn.queued",
        "turn.steer_queued",
    }
)


def _settled_activity(events: Iterable[ActivityEvent]) -> list[ActivityEvent]:
    """Discard transient records that were delivered after the same turn ended."""
    terminal_turns: set[str] = set()
    settled: list[ActivityEvent] = []
    for event in events:
        turn_id = str(event.turn_id or "")
        if (
            turn_id
            and turn_id in terminal_turns
            and event.type in _TRANSIENT_ACTIVITY_TYPES
        ):
            continue
        settled.append(event)
        if turn_id and event.type in _TERMINAL_ACTIVITY_TYPES:
            terminal_turns.add(turn_id)
    return settled


class ConversationProjection:
    """Project either structured activity or legacy session messages for chat rendering."""

    @classmethod
    def from_activity(
        cls,
        events: Iterable[ActivityEvent],
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> ProjectionPage:
        items = [
            _activity_item(event)
            for event in _settled_activity(events)
            if _conversation_visible(event)
        ]
        return _page(items, offset=offset, limit=limit, detail_available=True)

    @classmethod
    def _message_visible(cls, message: Mapping[str, Any]) -> bool:
        role = str(message.get("role") or "unknown")
        if role in {"user", "tool"}:
            return True
        if role != "assistant":
            return False
        calls = message.get("tool_calls") or ()
        return any(isinstance(call, Mapping) for call in calls) or message.get(
            "content"
        ) not in (None, "", [])

    @classmethod
    def _project_message(
        cls,
        session: Session,
        index: int,
        *,
        turn_start: int,
        turn_end: int,
    ) -> ProjectionItem:
        message = session.messages[index]
        role = str(message.get("role") or "unknown")
        timestamp = _timestamp(message.get("timestamp"))
        turn_id = _historical_turn_id(session, turn_start, turn_end)
        base_id = _stable_id("historical_message", session.key, index, role)
        content = _public_content(message.get("content"))
        turn_timing = _historical_turn_timing(session, turn_start, turn_end)
        metadata: dict[str, Any] = {
            "messageIndex": index,
            **turn_timing,
        }
        event_type = f"message.{role}"
        projected_role: str | None = role
        status: str | None = None
        summary = ""
        if role == "user":
            event_type = "user.message"
            metadata["artifactRefs"] = _public_artifact_refs(
                message.get("artifact_refs")
            )
        elif role == "assistant":
            calls = [
                call
                for call in message.get("tool_calls") or ()
                if isinstance(call, Mapping)
            ]
            if calls:
                event_type = "assistant.work"
                metadata.update(
                    {
                        "toolCalls": [_tool_call(call) for call in calls],
                        **_public_reasoning(message),
                    }
                )
            else:
                event_type = (
                    "assistant.final" if index == turn_end - 1 else "assistant.message"
                )
                metadata.update(_public_reasoning(message))
                terminal_status = str(turn_timing.get("terminalStatus") or "")
                if index == turn_end - 1 and terminal_status in {"failed", "cancelled"}:
                    event_type = f"turn.{terminal_status}"
                    projected_role = None
                    status = terminal_status
                    content = ""
                    if terminal_status == "failed":
                        raw_error = message.get("turn_error")
                        error = (
                            _json_safe(raw_error)
                            if isinstance(raw_error, Mapping)
                            else {
                                "code": "turn_failed",
                                "title": "Turn failed",
                                "message": "The turn ended before it produced a valid result.",
                            }
                        )
                        metadata["output"] = {"error": error}
                        summary = str(
                            error.get("title")
                            if isinstance(error, Mapping)
                            else "Turn failed"
                        )
                    else:
                        raw_control = message.get("turn_control")
                        control = (
                            _json_safe(raw_control)
                            if isinstance(raw_control, Mapping)
                            else {"kind": "interrupted"}
                        )
                        kind = str(
                            control.get("kind")
                            if isinstance(control, Mapping)
                            else "interrupted"
                        )
                        summary = {
                            "stop": "Turn stopped",
                            "runtime_shutdown": "Runtime stopped the turn",
                        }.get(kind, "Turn interrupted")
                        metadata["output"] = {
                            "control": control,
                            "message": _public_content(message.get("content")),
                        }
        elif role == "tool":
            event_type = "tool.result"
            content = _public_tool_result(message)
            metadata.update(
                {
                    "toolCallId": str(message.get("tool_call_id") or ""),
                    "toolName": str(message.get("name") or "unknown"),
                }
            )
        return ProjectionItem(
            id=base_id,
            type=event_type,
            source="session",
            timestamp=timestamp,
            status=status,
            turn_id=turn_id,
            role=projected_role,
            summary=summary,
            content=content,
            detail_available=False,
            metadata=metadata,
        )

    @classmethod
    def _page_from_session(
        cls,
        session: Session,
        *,
        offset: int | None,
        limit: int,
    ) -> ProjectionPage:
        if offset is not None and offset < 0:
            raise ValueError("projection offset cannot be negative")
        if not 1 <= limit <= 1_000:
            raise ValueError("projection page limit must be between 1 and 1000")
        boundaries = session.get_completed_turn_boundaries(start_idx=0)
        total = sum(
            1
            for start, end in boundaries
            for message in session.messages[start:end]
            if cls._message_visible(message)
        )
        page_offset = max(0, total - limit) if offset is None else offset
        page_end = min(total, page_offset + limit)
        items: list[ProjectionItem] = []
        cursor = 0
        for start, end in boundaries:
            for index in range(start, end):
                message = session.messages[index]
                if not cls._message_visible(message):
                    continue
                if page_offset <= cursor < page_end:
                    items.append(
                        cls._project_message(
                            session,
                            index,
                            turn_start=start,
                            turn_end=end,
                        )
                    )
                cursor += 1
                if cursor >= page_end:
                    break
            if cursor >= page_end:
                break
        consumed = page_offset + len(items)
        return ProjectionPage(
            items=tuple(items),
            next_offset=consumed if consumed < total else None,
            has_more=consumed < total,
            detail_available=False,
            total_count=total,
            previous_offset=max(0, page_offset - limit) if page_offset > 0 else None,
        )

    @classmethod
    def from_session(
        cls,
        session: Session,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> ProjectionPage:
        return cls._page_from_session(session, offset=offset, limit=limit)

    @classmethod
    def from_session_recent(cls, session: Session, *, limit: int = 100) -> ProjectionPage:
        """Build only the newest bounded fallback page."""
        return cls._page_from_session(session, offset=None, limit=limit)


class TrajectoryProjection:
    """Project detailed activity or a conservative hierarchy from session messages."""

    @classmethod
    def from_activity(
        cls,
        events: Iterable[ActivityEvent],
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> ProjectionPage:
        return _page(
            [_activity_item(event) for event in _settled_activity(events)],
            offset=offset,
            limit=limit,
            detail_available=True,
        )

    @classmethod
    def _message_item_count(cls, message: Mapping[str, Any], *, turn_start: bool) -> int:
        calls = message.get("tool_calls") or ()
        return (
            1
            + int(turn_start)
            + sum(1 for call in calls if isinstance(call, Mapping))
        )

    @classmethod
    def _page_from_session(
        cls,
        session: Session,
        *,
        offset: int | None,
        limit: int,
    ) -> ProjectionPage:
        if offset is not None and offset < 0:
            raise ValueError("projection offset cannot be negative")
        if not 1 <= limit <= 1_000:
            raise ValueError("projection page limit must be between 1 and 1000")
        boundaries = session.get_completed_turn_boundaries(start_idx=0)
        total = sum(
            cls._message_item_count(
                session.messages[index],
                turn_start=index == start,
            )
            for start, end in boundaries
            for index in range(start, end)
        )
        page_offset = max(0, total - limit) if offset is None else offset
        page_end = min(total, page_offset + limit)
        items: list[ProjectionItem] = []
        cursor = 0
        for start, end in boundaries:
            turn_id = _historical_turn_id(session, start, end)
            turn_timing = _historical_turn_timing(session, start, end)
            call_nodes: dict[str, str] = {}
            for index in range(start, end):
                message = session.messages[index]
                role = str(message.get("role") or "unknown")
                timestamp = _timestamp(message.get("timestamp"))
                calls = [
                    (ordinal, call)
                    for ordinal, call in enumerate(message.get("tool_calls") or ())
                    if isinstance(call, Mapping)
                ]
                message_id = _stable_id(
                    "historical_message", session.key, index, role
                )
                raw_nodes: list[tuple[str, int, Mapping[str, Any] | None]] = []
                if index == start:
                    raw_nodes.append(("turn", -1, None))
                raw_nodes.append(("message", -1, None))
                raw_nodes.extend(("call", ordinal, call) for ordinal, call in calls)
                for kind, ordinal, call in raw_nodes:
                    if page_offset <= cursor < page_end:
                        if kind == "turn":
                            items.append(
                                ProjectionItem(
                                    id=turn_id,
                                    type="turn.historical",
                                    source="session",
                                    timestamp=timestamp,
                                    turn_id=turn_id,
                                    detail_available=False,
                                    metadata={
                                        "messageStart": start,
                                        "messageEnd": end,
                                        "derived": True,
                                        **turn_timing,
                                    },
                                )
                            )
                        elif kind == "message":
                            parent_id = turn_id
                            event_type = f"message.{role}"
                            projected_role: str | None = role
                            projected_status: str | None = None
                            projected_summary = ""
                            projected_content = (
                                _public_tool_result(message)
                                if role == "tool"
                                else _public_content(message.get("content"))
                            )
                            metadata: dict[str, Any] = {
                                "messageIndex": index,
                                **turn_timing,
                            }
                            if role == "user":
                                event_type = "user.message"
                                artifact_refs = _public_artifact_refs(
                                    message.get("artifact_refs")
                                )
                                if artifact_refs:
                                    metadata["artifactRefs"] = artifact_refs
                            elif role == "assistant" and calls:
                                event_type = "provider.step"
                                metadata.update(_public_reasoning(message))
                            elif role == "assistant" and index == end - 1:
                                event_type = "assistant.final"
                                metadata.update(_public_reasoning(message))
                                terminal_status = str(
                                    turn_timing.get("terminalStatus") or ""
                                )
                                if terminal_status in {"failed", "cancelled"}:
                                    event_type = f"turn.{terminal_status}"
                                    projected_role = None
                                    projected_status = terminal_status
                                    projected_content = ""
                                    if terminal_status == "failed":
                                        raw_error = message.get("turn_error")
                                        error = (
                                            _json_safe(raw_error)
                                            if isinstance(raw_error, Mapping)
                                            else {
                                                "code": "turn_failed",
                                                "title": "Turn failed",
                                                "message": "The turn ended before it produced a valid result.",
                                            }
                                        )
                                        metadata["output"] = {"error": error}
                                        projected_summary = str(
                                            error.get("title")
                                            if isinstance(error, Mapping)
                                            else "Turn failed"
                                        )
                                    else:
                                        raw_control = message.get("turn_control")
                                        control = (
                                            _json_safe(raw_control)
                                            if isinstance(raw_control, Mapping)
                                            else {"kind": "interrupted"}
                                        )
                                        control_kind = str(
                                            control.get("kind")
                                            if isinstance(control, Mapping)
                                            else "interrupted"
                                        )
                                        projected_summary = {
                                            "stop": "Turn stopped",
                                            "runtime_shutdown": "Runtime stopped the turn",
                                        }.get(control_kind, "Turn interrupted")
                                        metadata["output"] = {
                                            "control": control,
                                            "message": _public_content(
                                                message.get("content")
                                            ),
                                        }
                            elif role == "tool":
                                event_type = "tool.result"
                                call_id = str(message.get("tool_call_id") or "")
                                parent_id = call_nodes.get(call_id, turn_id)
                                metadata.update(
                                    {
                                        "toolCallId": call_id,
                                        "toolName": str(
                                            message.get("name") or "unknown"
                                        ),
                                    }
                                )
                            items.append(
                                ProjectionItem(
                                    id=message_id,
                                    type=event_type,
                                    source="session",
                                    timestamp=timestamp,
                                    status=projected_status,
                                    turn_id=turn_id,
                                    parent_id=parent_id,
                                    role=projected_role,
                                    summary=projected_summary,
                                    content=projected_content,
                                    detail_available=False,
                                    metadata=metadata,
                                )
                            )
                        else:
                            assert call is not None
                            call_data = _tool_call(call)
                            call_id = call_data["id"]
                            node_id = _stable_id(
                                "historical_tool_call",
                                session.key,
                                index,
                                call_id,
                                ordinal,
                            )
                            items.append(
                                ProjectionItem(
                                    id=node_id,
                                    type="tool.call",
                                    source="session",
                                    timestamp=timestamp,
                                    turn_id=turn_id,
                                    parent_id=message_id,
                                    detail_available=False,
                                    metadata={"messageIndex": index, **call_data},
                                )
                            )
                    cursor += 1
                if role == "assistant":
                    for ordinal, call in calls:
                        call_id = str(call.get("id") or "")
                        if call_id:
                            call_nodes[call_id] = _stable_id(
                                "historical_tool_call",
                                session.key,
                                index,
                                call_id,
                                ordinal,
                            )
                if cursor >= page_end:
                    break
            if cursor >= page_end:
                break
        consumed = page_offset + len(items)
        return ProjectionPage(
            items=tuple(items),
            next_offset=consumed if consumed < total else None,
            has_more=consumed < total,
            detail_available=False,
            total_count=total,
            previous_offset=max(0, page_offset - limit) if page_offset > 0 else None,
        )

    @classmethod
    def from_session(
        cls,
        session: Session,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> ProjectionPage:
        return cls._page_from_session(session, offset=offset, limit=limit)

    @classmethod
    def from_session_recent(cls, session: Session, *, limit: int = 100) -> ProjectionPage:
        """Build only the newest bounded fallback page."""
        return cls._page_from_session(session, offset=None, limit=limit)
