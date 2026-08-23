"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nanocat.core.messages import (
    ConversationRef,
    InboundEvent,
    OutboundEvent,
    Principal,
    new_event_id,
)


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions
    event_id: str | None = None
    correlation_id: str | None = None
    priority: int | None = None
    deadline_at: datetime | None = None
    request_id: str | None = None
    turn_id: str | None = None
    principal_id: str | None = None
    ingress_ordinal: int = 0

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None
    correlation_id: str | None = None
    priority: int | None = None
    deadline_at: datetime | None = None
    request_id: str | None = None
    turn_id: str | None = None
    principal_id: str | None = None


def inbound_to_event(message: InboundMessage) -> InboundEvent:
    """Convert the legacy mutable inbound message at the adapter boundary."""
    conversation = ConversationRef(
        channel=message.channel,
        chat_id=message.chat_id,
        session_key=message.session_key,
    )
    return InboundEvent(
        event_id=message.event_id or new_event_id(),
        received_at=message.timestamp,
        sender_id=message.sender_id,
        conversation=conversation,
        content=message.content,
        media=tuple(message.media),
        metadata=message.metadata,
        principal=Principal(id=message.principal_id or message.sender_id),
        source=message.channel,
        correlation_id=message.correlation_id,
        deadline_at=message.deadline_at,
        priority=message.priority if message.priority is not None else 100,
        request_id=message.request_id,
        turn_id=message.turn_id,
    )


def event_to_inbound(event: InboundEvent) -> InboundMessage:
    """Convert a normalized event for legacy channel/agent callers."""
    return InboundMessage(
        channel=event.conversation.channel,
        sender_id=event.sender_id,
        chat_id=event.conversation.chat_id,
        content=event.content,
        timestamp=event.received_at,
        media=list(event.media),
        metadata=dict(event.metadata),
        session_key_override=event.conversation.session_key
        if event.conversation.session_key
        != f"{event.conversation.channel}:{event.conversation.chat_id}"
        else None,
        event_id=event.event_id,
        correlation_id=event.correlation_id,
        priority=event.priority,
        deadline_at=event.deadline_at,
        request_id=event.request_id,
        turn_id=event.turn_id,
        principal_id=event.principal.id if event.principal is not None else event.sender_id,
    )


def event_to_outbound(event: OutboundEvent) -> OutboundMessage:
    """Convert a normalized outbound event for legacy channel callers."""
    return OutboundMessage(
        channel=event.conversation.channel,
        chat_id=event.conversation.chat_id,
        content=event.content,
        reply_to=event.reply_to,
        media=list(event.media),
        metadata=dict(event.metadata),
        event_id=event.event_id,
        correlation_id=event.correlation_id,
        priority=event.priority,
        deadline_at=event.deadline_at,
        request_id=event.request_id,
        turn_id=event.turn_id,
        principal_id=event.principal_id,
    )
