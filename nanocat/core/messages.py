"""Immutable message and event contracts for the runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Generic, Mapping, TypeVar
from uuid import uuid4

PayloadT = TypeVar("PayloadT")


def new_event_id() -> str:
    """Return a process-independent identifier for a runtime event."""
    return str(uuid4())


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Copy a metadata mapping so event contracts cannot mutate caller state."""
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class Principal:
    """Normalized identity used for ACL and command/intervention checks."""

    id: str
    display_name: str | None = None
    roles: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "attributes", _freeze_mapping(self.attributes))


@dataclass(frozen=True, slots=True)
class ConversationRef:
    """Stable channel/chat/session identity shared by all application events."""

    channel: str
    chat_id: str
    session_key: str = ""

    def __post_init__(self) -> None:
        if not self.session_key:
            object.__setattr__(self, "session_key", f"{self.channel}:{self.chat_id}")


@dataclass(frozen=True, slots=True)
class TurnRef:
    """Correlation identity for one application turn."""

    turn_id: str
    conversation: ConversationRef
    mode: str
    parent_turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """Normalized inbound event produced by a channel adapter."""

    event_id: str
    received_at: datetime
    sender_id: str
    conversation: ConversationRef
    content: str
    media: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    principal: Principal | None = None
    source: str = "unknown"
    correlation_id: str | None = None
    deadline_at: datetime | None = None
    priority: int = 100
    request_id: str | None = None
    turn_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "media", tuple(self.media))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class OutboundEvent:
    """Normalized outbound event consumed by the channel dispatcher."""

    event_id: str
    conversation: ConversationRef
    content: str
    reply_to: str | None = None
    media: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    deadline_at: datetime | None = None
    priority: int = 100
    request_id: str | None = None
    turn_id: str | None = None
    principal_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "media", tuple(self.media))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class EventEnvelope(Generic[PayloadT]):
    """Transport envelope carrying ordering and retry metadata."""

    event_id: str
    kind: str
    payload: PayloadT
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str | None = None
    priority: int = 100
    attempt: int = 0
    deadline_at: datetime | None = None
