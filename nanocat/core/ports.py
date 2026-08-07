"""Application ports expressed without concrete SDK or infrastructure types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from nanocat.core.intervention import (
    InterventionAction,
    InterventionRequest,
    InterventionResult,
)
from nanocat.core.messages import ConversationRef, InboundEvent, OutboundEvent


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """SDK-neutral rendering and interaction capabilities of one channel."""

    progress: bool = False
    tool_events: bool = False
    media: bool = True
    reply_threads: bool = False
    interactive_reply: bool = False


def resolve_channel_capabilities(channel: Any) -> ChannelCapabilities:
    """Normalize current and legacy channel capability declarations."""
    raw = getattr(channel, "capabilities", None)

    def read(name: str, default: bool) -> bool:
        if isinstance(raw, Mapping):
            value = raw.get(name, default)
        else:
            value = getattr(raw, name, default) if raw is not None else default
        return bool(value)

    return ChannelCapabilities(
        progress=read("progress", bool(getattr(channel, "supports_progress", False))),
        tool_events=read("tool_events", bool(getattr(channel, "wants_tool_events", False))),
        media=read("media", True),
        reply_threads=read("reply_threads", bool(getattr(channel, "supports_threads", False))),
        interactive_reply=read(
            "interactive_reply",
            bool(getattr(channel, "supports_interactive_reply", False)),
        ),
    )


class MessagePort(Protocol):
    """Ingress and egress port used by application services."""

    async def publish_inbound(self, event: InboundEvent) -> None: ...

    async def publish_outbound(self, event: OutboundEvent) -> None: ...

    async def receive_inbound(self) -> InboundEvent: ...

    async def receive_outbound(self) -> OutboundEvent: ...


class UserInteractionPort(Protocol):
    """Scheduler-owned channel-neutral user interaction port."""

    async def request_user(self, request: InterventionRequest) -> Any: ...


class InterventionBrokerPort(Protocol):
    """Port for suspending and resolving runtime-scoped interventions."""

    async def suspend(self, request: InterventionRequest) -> InterventionResult: ...

    async def resolve(
        self,
        principal_id: str,
        conversation: ConversationRef,
        action: InterventionAction,
    ) -> InterventionResult | None: ...

    async def cancel_turn(self, turn_id: str) -> None: ...

    async def cancel_session(self, session_key: str) -> None: ...

    def has_turn_grant(
        self,
        conversation: ConversationRef,
        principal_id: str,
        turn_id: str,
        capability: str,
    ) -> bool: ...

    def has_session_grant(
        self,
        conversation: ConversationRef,
        principal_id: str,
        capability: str,
        tool_name: str | None = None,
    ) -> bool: ...

    async def finish_turn(self, turn_id: str) -> None: ...

    async def close(self) -> None: ...


class SessionStore(Protocol):
    """Session persistence port; the concrete model remains outside core."""

    async def get_or_create(self, ref: ConversationRef) -> Any: ...

    async def save(self, session: Any) -> None: ...

    async def list(self, channel: str, limit: int = 10) -> list[Any]: ...

    async def delete(self, ref: ConversationRef) -> None: ...


class ProviderResolver(Protocol):
    """Resolve a model into a runtime-scoped provider client."""

    def resolve(self, model: str | None = None) -> Any: ...


class ToolCatalog(Protocol):
    """Describe and resolve tools without exposing their implementation owner."""

    def definitions(self) -> Sequence[Mapping[str, Any]]: ...

    def descriptors(self) -> Sequence[Any]: ...

    def get(self, name: str) -> Any | None: ...
