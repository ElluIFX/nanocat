"""Application boundary for scheduler-owned, non-channel agent turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from nanocat.application.turns import TurnRequest
from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.core.messages import ConversationRef


@dataclass(frozen=True, slots=True)
class SystemTurnRequest:
    """Describe a turn submitted by a runtime service such as cron or heartbeat."""

    source: str
    content: str
    conversation: ConversationRef
    principal_id: str = "user"
    transient: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    deadline_at: datetime | None = None


class SystemTurnGateway:
    """Submit system turns through one application-owned agent boundary.

    Scheduler services do not need to know the legacy engine's direct-call
    signature or construct bus messages themselves.  This boundary is also the
    extension point for future system actors such as webhooks and maintenance
    jobs.
    """

    def __init__(self, agent: Any, bus: MessageBus):
        self._agent = agent
        self._bus = bus

    async def submit(
        self,
        request: SystemTurnRequest,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Execute one system turn with the same engine and session semantics."""
        turn = TurnRequest(
            content=request.content,
            conversation=request.conversation,
            principal_id=request.principal_id,
            source=request.source,
            transient=request.transient,
            metadata=request.metadata,
            deadline_at=request.deadline_at,
        )
        submit = getattr(self._agent, "submit", None)
        if submit is not None:
            return await submit(turn, on_progress=on_progress)
        return await self._agent.process_direct(
            request.content,
            session_key=request.conversation.session_key,
            channel=request.conversation.channel,
            chat_id=request.conversation.chat_id,
            on_progress=on_progress,
            transient=request.transient,
            principal_id=request.principal_id,
            metadata=dict(request.metadata),
            deadline_at=request.deadline_at,
        )

    async def deliver(
        self,
        request: SystemTurnRequest,
        content: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Publish a system-generated response through the outbound port."""
        if not content:
            return
        event_metadata = dict(request.metadata)
        event_metadata.update(metadata or {})
        event_metadata.setdefault("_system_turn", request.source)
        await self._bus.publish_outbound(
            OutboundMessage(
                channel=request.conversation.channel,
                chat_id=request.conversation.chat_id,
                content=content,
                metadata=event_metadata,
            )
        )
