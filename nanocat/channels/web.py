"""Single-user web channel adapter backed by the runtime message bus."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from nanocat.api.events import SseBroker
from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import BusError, MessageBus
from nanocat.channels.base import BaseChannel
from nanocat.config.schema import WebChannelConfig
from nanocat.core.ports import ChannelCapabilities
from nanocat.observability.redaction import redact_value

_MAX_WORKING_VALUE_CHARS = 8_000


def _working_value(value: Any) -> Any:
    """Return a redacted bounded value for browser-visible Working details."""
    safe = redact_value(value)
    try:
        encoded = json.dumps(safe, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = str(safe)
    if len(encoded) <= _MAX_WORKING_VALUE_CHARS:
        return safe
    return {
        "truncated": True,
        "totalChars": len(encoded),
        "preview": encoded[:_MAX_WORKING_VALUE_CHARS],
    }


def _working_thinking(value: Any) -> Any:
    """Remove provider-only proof fields before exposing structured thinking."""
    def strip_opaque(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): strip_opaque(child)
                for key, child in item.items()
                if str(key).casefold()
                not in {"signature", "encrypted_content", "encryptedcontent"}
            }
        if isinstance(item, (list, tuple)):
            return [strip_opaque(child) for child in item]
        return item

    return _working_value(strip_opaque(value))


class WebIngressRejectedError(RuntimeError):
    """Raised when browser ingress cannot be admitted to the runtime bus."""

    def __init__(self, reason: str, *, retry_after: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class WebChannel(BaseChannel):
    """Normalize browser input and project runtime output into SSE events."""

    name = "web"
    display_name = "Web"
    capabilities = ChannelCapabilities(
        progress=True,
        tool_events=True,
        media=True,
        interactive_reply=True,
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebChannelConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WebChannelConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebChannelConfig = config
        self._broker: SseBroker | None = None
        self._control: Any = None
        self._activity_journal: Any = None
        self._artifact_registry: Any = None
        self._stop_event: asyncio.Event | None = None
        self._ingress_lock = asyncio.Lock()

    def bind_broker(self, broker: SseBroker) -> None:
        self._broker = broker

    def bind_control(self, control: Any) -> None:
        self._control = control

    def bind_activity(self, activity_journal: Any) -> None:
        self._activity_journal = activity_journal

    def bind_artifacts(self, artifact_registry: Any) -> None:
        self._artifact_registry = artifact_registry

    async def _artifact_refs(self, paths: list[str]) -> list[dict[str, Any]]:
        if self._artifact_registry is None:
            return []
        refs = []
        for value in paths:
            try:
                entry = await self._artifact_registry.register_path_async(Path(value))
                refs.append(entry.public())
            except Exception as exc:
                logger.warning("Web artifact registration failed ({})", type(exc).__name__)
        return refs

    def _storage_state(self, session_id: str | None) -> tuple[str | None, int | None]:
        if not session_id or self._control is None:
            return None, None
        session = self._control._sessions.get_session("web", session_id)
        if session is None:
            return None, None
        return session.key, len(session.messages)

    def _storage_scope(self, session_id: str | None) -> str | None:
        return self._storage_state(session_id)[0]

    def _turn_timing(self, turn_id: str | None) -> dict[str, Any]:
        if not turn_id or self._control is None:
            return {}
        record = self._control._engine.turns.get(turn_id)
        if record is None:
            return {}
        endpoint = record.ended_at or datetime.now(timezone.utc)
        return {
            "startedAt": record.started_at.isoformat(),
            **({"endedAt": record.ended_at.isoformat()} if record.ended_at else {}),
            "durationMs": max(
                0,
                int((endpoint - record.started_at).total_seconds() * 1000),
            ),
        }

    async def _record_activity(self, storage_scope: str | None, **values: Any) -> None:
        if storage_scope is None or self._activity_journal is None:
            return
        try:
            from nanocat.observability.activity import ActivityEvent

            event = ActivityEvent(**values)
            await asyncio.to_thread(self._activity_journal.append, storage_scope, event)
        except Exception as exc:
            logger.warning("Web activity append failed ({})", type(exc).__name__)

    async def _emit(self, event_type: str, payload: dict[str, Any], **values: Any) -> None:
        if self._broker is None:
            return
        try:
            await self._broker.publish(event_type, payload, **values)
        except Exception as exc:
            logger.warning("Web SSE publish failed ({})", type(exc).__name__)

    async def submit(
        self,
        content: str,
        *,
        chat_id: str = "local",
        media: list[str] | None = None,
        artifact_refs: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        turn_id: str | None = None,
        steer: bool = False,
    ) -> dict[str, str]:
        """Serialize single-user Web admission and reject ambiguous hard interrupts."""
        async with self._ingress_lock:
            return await self._submit_owned(
                content,
                chat_id=chat_id,
                media=media,
                artifact_refs=artifact_refs,
                session_id=session_id,
                request_id=request_id,
                turn_id=turn_id,
                steer=steer,
            )

    async def _submit_owned(
        self,
        content: str,
        *,
        chat_id: str = "local",
        media: list[str] | None = None,
        artifact_refs: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        turn_id: str | None = None,
        steer: bool = False,
    ) -> dict[str, str]:
        request_id = request_id or uuid4().hex
        if MessageBus.is_command_candidate(content):
            raise WebIngressRejectedError("command_input")
        storage_scope, history_message_count = await asyncio.to_thread(
            self._storage_state, session_id
        )
        if storage_scope is None:
            raise WebIngressRejectedError("session_missing")
        if not self.is_allowed("web:local"):
            raise WebIngressRejectedError("access_denied")
        session_key = f"web:{chat_id}"
        turns = self._control._engine.turns if self._control is not None else None
        engine = self._control._engine if self._control is not None else None
        if engine is not None and engine.has_pending_session_durability(session_key):
            engine.request_session_durability_retry(session_key)
            raise WebIngressRejectedError("durability_pending", retry_after=1)
        attachment_count = len(artifact_refs or [])
        attachment_bytes = sum(
            max(int(item.get("size") or 0), 0)
            for item in artifact_refs or []
            if isinstance(item, dict)
        )
        joined_turn = False
        steer_reserved = False
        turn_request_id = request_id
        active = turns.active_for_session(session_key) if turns is not None else None
        if turn_id is None and not steer and active is not None:
            raise WebIngressRejectedError("busy", retry_after=1)
        if turn_id is None and steer and turns is not None:
            if active is not None:
                turn_id = active.turn_id
                turn_request_id = active.request_id or request_id
                joined_turn = True
                if not turns.reserve_join(turn_id):
                    raise WebIngressRejectedError("busy", retry_after=1)
                if engine is None or not engine.reserve_web_steer(
                    session_key,
                    turn_id,
                    len(content),
                    attachment_count,
                    attachment_bytes,
                ):
                    turns.release_join(turn_id)
                    raise WebIngressRejectedError("capacity_timeout", retry_after=1)
                steer_reserved = True
        turn_id = turn_id or uuid4().hex
        if turns is not None and not joined_turn:
            turns.register(
                turn_id,
                session_key,
                "web:local",
                request_id=request_id,
                conversation_id=session_id,
            )
            if engine is not None and not engine.track_web_turn_attachments(
                turn_id,
                attachment_count,
                attachment_bytes,
            ):
                turns.fail(turn_id, "attachment budget exceeded")
                raise WebIngressRejectedError("capacity_timeout", retry_after=1)
        metadata = {
            "request_id": turn_request_id,
            "_web_ingress_request_id": request_id,
            "turn_id": turn_id,
            "_web_session_id": session_id or "",
            "_web_steer": steer,
            "_web_steer_reserved": steer_reserved,
            "_web_joined_turn": joined_turn,
            "_artifact_refs": list(artifact_refs or []),
        }
        try:
            await self._record_activity(
                storage_scope,
                type="user.steer" if steer else "user.message",
                source="web",
                status="queued",
                turn_id=turn_id,
                request_id=request_id,
                summary=f"User message ({len(content)} chars)",
                redacted_input={"contentChars": len(content), "mediaCount": len(media or [])},
                artifact_refs=tuple(
                    str(item.get("id") or "")
                    for item in artifact_refs or []
                    if item.get("id")
                ),
                metadata={
                    "conversationVisible": True,
                    "historyMessageCount": history_message_count,
                    "artifactRefs": list(artifact_refs or []),
                },
            )
            await self._emit(
                "turn.steer_queued" if steer else "turn.queued",
                {
                    "content": content[:240],
                    "contentChars": len(content),
                    "mediaCount": len(media or []),
                    "artifactRefs": list(artifact_refs or []),
                },
                session_id=session_id,
                request_id=request_id,
                turn_id=turn_id,
                source="web",
                status="queued",
            )
        except asyncio.CancelledError:
            if steer_reserved and engine is not None:
                engine.release_web_steer(
                    session_key,
                    len(content),
                    turn_id=turn_id if joined_turn else None,
                    rollback_attachments=(attachment_count, attachment_bytes),
                )
            if turns is not None and not joined_turn:
                turns.fail(turn_id, "ingress request cancelled")
            raise
        try:
            admission = await asyncio.wait_for(
                self._handle_message(
                    sender_id="web:local",
                    chat_id=chat_id,
                    content=content,
                    media=media,
                    metadata=metadata,
                ),
                timeout=2.0,
            )
        except TimeoutError:
            admission = "capacity_timeout"
        except BusError:
            admission = "unavailable"
        except asyncio.CancelledError:
            if steer_reserved and engine is not None:
                engine.release_web_steer(
                    session_key,
                    len(content),
                    turn_id=turn_id if joined_turn else None,
                    rollback_attachments=(attachment_count, attachment_bytes),
                )
            if turns is not None and not joined_turn:
                turns.fail(turn_id, "ingress request cancelled")
            raise
        if admission != "accepted":
            if steer_reserved and engine is not None:
                engine.release_web_steer(
                    session_key,
                    len(content),
                    turn_id=turn_id if joined_turn else None,
                    rollback_attachments=(attachment_count, attachment_bytes),
                )
            if turns is not None and not joined_turn:
                turns.fail(turn_id, f"ingress rejected: {admission}")
            await self._record_activity(
                storage_scope,
                type="turn.rejected",
                source="web",
                status="failed",
                turn_id=turn_id,
                request_id=request_id,
                summary="Web ingress rejected",
                redacted_output={"reason": admission},
                metadata={"conversationVisible": True},
            )
            await self._emit(
                "turn.rejected",
                {"reason": admission},
                session_id=session_id,
                request_id=request_id,
                turn_id=turn_id,
                source="web",
                status="failed",
            )
            raise WebIngressRejectedError(
                admission,
                retry_after=1 if admission == "capacity_timeout" else None,
            )
        return {"requestId": request_id, "turnId": turn_id}

    async def start(self) -> None:
        self._running = True
        self._stop_event = asyncio.Event()
        try:
            await self._stop_event.wait()
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        await self._cancel_owned_tasks()

    async def send(self, msg: OutboundMessage) -> None:
        if self._control is not None:
            async with self._control.routing_guard(f"web:{msg.chat_id}"):
                await self._send_locked(msg)
            return
        await self._send_locked(msg)

    async def _send_locked(self, msg: OutboundMessage) -> None:
        """Deliver one outbound event while destructive route changes are excluded."""
        broker = self._broker
        if broker is None:
            return
        metadata = dict(msg.metadata or {})
        explicit_session_id = str(metadata.pop("_web_session_id", "") or "") or None
        session_id = explicit_session_id
        if session_id is not None:
            if self._control is None:
                logger.warning("Dropping Web outbound before control binding")
                return
            target = await asyncio.to_thread(
                self._control._sessions.get_session, "web", session_id
            )
            if target is None or str(target.chat_id) != str(msg.chat_id):
                logger.info("Dropping outbound for deleted Web session {}", session_id)
                return
        if session_id is None and self._control is not None:
            sessions = await asyncio.to_thread(
                self._control._sessions.list_sessions,
                "web",
                0,
                1000,
            )
            match = next(
                (
                    item
                    for item in sessions
                    if str(item.get("chat_id") or "") == str(msg.chat_id)
                ),
                None,
            )
            if match is not None:
                session_id = str(match.get("id") or "") or None
        if session_id is None:
            logger.warning("Dropping Web outbound without a live session mapping")
            return
        request_id = msg.request_id or str(metadata.get("request_id") or "") or None
        turn_id = msg.turn_id or str(metadata.get("turn_id") or "") or None
        if metadata.get("_turn_failed"):
            event_type = "turn.failed"
        elif metadata.get("_intervention"):
            action_names = {
                "approve_once": "once",
                "approve_turn": "turn",
                "approve_forever": "forever",
                "reject": "deny",
            }
            metadata["allowed_actions"] = [
                action_names.get(str(action), str(action))
                for action in metadata.get("allowed_actions") or []
            ]
            event_type = (
                "approval.updated" if metadata.get("_intervention_update") else "approval.pending"
            )
        elif metadata.get("_tool_event"):
            event_type = "tool.event"
        elif metadata.get("_thinking"):
            event_type = "assistant.thinking"
        elif metadata.get("_progress"):
            event_type = "assistant.progress"
        else:
            event_type = "assistant.final"
        if event_type in {"assistant.final", "turn.failed"} and metadata.get(
            "_terminal_deferred"
        ) is True:
            event_type = "assistant.progress"
        tool_event = metadata.get("_tool_event")
        safe_tool_event: dict[str, Any] | None = None
        if isinstance(tool_event, dict):
            safe_calls = []
            for call in tool_event.get("calls") or []:
                if not isinstance(call, dict):
                    continue
                safe_calls.append(
                    {
                        **{
                            key: call[key]
                            for key in ("id", "name", "status", "resultChars", "durationMs")
                            if call.get(key) not in (None, "")
                        },
                        **(
                            {"arguments": _working_value(call["args"])}
                            if call.get("args") is not None
                            else {}
                        ),
                        **(
                            {"resultPreview": _working_value(call["resultPreview"])}
                            if call.get("resultPreview") is not None
                            else {}
                        ),
                    }
                )
            safe_tool_event = {
                "phase": tool_event.get("phase"),
                "calls": safe_calls,
            }
        tool_summary = None
        if safe_tool_event is not None and safe_tool_event["calls"]:
            labels = [
                f"{call.get('name', 'tool')} ({call.get('status', 'updated')})"
                for call in safe_tool_event["calls"]
            ]
            tool_summary = f"Tools: {', '.join(labels)}"
        public_metadata: dict[str, Any] = {}
        if safe_tool_event is not None:
            public_metadata["_tool_event"] = safe_tool_event
        redacted_input = None
        redacted_output = None
        if safe_tool_event is not None:
            input_calls = [
                {
                    key: call[key]
                    for key in ("id", "name", "status", "arguments")
                    if call.get(key) not in (None, "")
                }
                for call in safe_tool_event["calls"]
            ]
            output_calls = [
                {
                    key: call[key]
                    for key in ("id", "name", "status", "resultPreview", "resultChars", "durationMs")
                    if call.get(key) not in (None, "")
                }
                for call in safe_tool_event["calls"]
                if call.get("resultPreview") is not None
                or call.get("resultChars") is not None
                or str(call.get("status") or "").casefold()
                in {"ok", "completed", "failed", "error", "cancelled"}
            ]
            if input_calls:
                redacted_input = {"calls": input_calls}
            if output_calls:
                redacted_output = {"calls": output_calls}
        if event_type == "assistant.thinking" and metadata.get(
            "_thinking_payload"
        ) is not None:
            public_metadata["thinking"] = _working_thinking(
                metadata["_thinking_payload"]
            )
        artifact_refs = await self._artifact_refs(list(msg.media or []))
        timing = self._turn_timing(turn_id)
        payload = {
            "content": (
                ""
                if event_type == "tool.event"
                else _working_value(msg.content)
                if event_type == "assistant.thinking"
                else msg.content
            ),
            "artifactRefs": artifact_refs,
            "replyTo": msg.reply_to,
            "metadata": public_metadata,
            **({"redactedInput": redacted_input} if redacted_input is not None else {}),
            **({"redactedOutput": redacted_output} if redacted_output is not None else {}),
            **timing,
            **({"summary": tool_summary} if tool_summary else {}),
        }
        await broker.publish(
            event_type,
            payload,
            session_id=session_id,
            request_id=request_id,
            turn_id=turn_id,
            source="runtime",
            status=(
                "failed"
                if event_type == "turn.failed"
                else "completed"
                if event_type == "assistant.final"
                else "waiting_approval"
                if event_type == "approval.pending"
                else "running"
            ),
            node_id=str(metadata.get("node_id") or "") or None,
            parent_id=str(metadata.get("parent_id") or "") or None,
            phase=(
                str(safe_tool_event.get("phase") or "") or None
                if safe_tool_event is not None
                else str(metadata.get("phase") or "") or None
            ),
        )
        activity_output: dict[str, Any] = {
            "contentChars": len(msg.content),
            "artifactCount": len(artifact_refs),
            "toolEvent": safe_tool_event,
        }
        if event_type == "assistant.thinking" and msg.content:
            activity_output["content"] = _working_value(msg.content)
        await self._record_activity(
            self._storage_scope(session_id),
            type=event_type,
            source="runtime",
            status=(
                "failed"
                if event_type == "turn.failed"
                else "completed"
                if event_type == "assistant.final"
                else "waiting_approval"
                if event_type == "approval.pending"
                else "running"
            ),
            turn_id=turn_id,
            request_id=request_id,
            summary=(
                f"Assistant response ({len(msg.content)} chars)"
                if event_type == "assistant.final"
                else f"Turn failed ({len(msg.content)} chars)"
                if event_type == "turn.failed"
                else tool_summary
                if tool_summary
                else event_type.replace(".", " ").title()
            ),
            redacted_output=activity_output,
            artifact_refs=tuple(
                str(item["id"])
                for item in artifact_refs
                if isinstance(item, dict) and item.get("id")
            ),
            started_at=(
                datetime.fromisoformat(timing["startedAt"])
                if timing.get("startedAt")
                else None
            ),
            ended_at=(
                datetime.fromisoformat(timing["endedAt"])
                if timing.get("endedAt")
                else None
            ),
            duration_ms=timing.get("durationMs"),
            phase=(
                str(safe_tool_event.get("phase") or "") or None
                if safe_tool_event is not None
                else str(metadata.get("phase") or "") or None
            ),
            metadata={
                "conversationVisible": True,
                **({"phase": safe_tool_event.get("phase")} if safe_tool_event else {}),
            },
        )
