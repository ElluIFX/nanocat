"""Runtime-independent lifecycle, error and resource contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable


class ErrorCategory(StrEnum):
    """Stable error categories used by application and infrastructure layers."""

    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    DEPENDENCY = "dependency"
    POLICY = "policy"
    VALIDATION = "validation"
    STATE = "state"
    INTERNAL = "internal"


class HealthState(StrEnum):
    """Lifecycle state exposed by runtime components."""

    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"
    FAILED = "failed"
    STOPPED = "stopped"


class DropPolicy(StrEnum):
    """Queue behavior when a bounded transport reaches capacity."""

    REJECT = "reject"
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    BLOCK = "block"


class CancellationRequestedError(Exception):
    """Raised when a cooperative cancellation boundary is reached."""


@dataclass(slots=True)
class CancellationToken:
    """Synchronous cancellation token that can be bridged by async adapters."""

    parent: "CancellationToken | None" = None
    name: str = "operation"
    _cancelled: bool = field(default=False, init=False, repr=False)
    _reason: str | None = field(default=None, init=False, repr=False)

    def cancel(self, reason: str = "cancelled") -> bool:
        """Cancel the token once and return whether this call changed its state."""
        if self._cancelled:
            return False
        self._cancelled = True
        self._reason = reason
        return True

    @property
    def cancelled(self) -> bool:
        """Return whether this token or one of its parents has been cancelled."""
        return self._cancelled or bool(self.parent and self.parent.cancelled)

    @property
    def reason(self) -> str | None:
        """Return the nearest cancellation reason, if available."""
        if self._reason is not None:
            return self._reason
        return self.parent.reason if self.parent else None

    def child(self, name: str) -> "CancellationToken":
        """Create a child token which follows this token's cancellation state."""
        return CancellationToken(parent=self, name=name)

    def raise_if_cancelled(self) -> None:
        """Enforce a cooperative cancellation boundary."""
        if self.cancelled:
            raise CancellationRequestedError(self.reason or "cancelled")


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Describes an extension's capabilities without importing its SDK."""

    name: str
    version: str = "1"
    capabilities: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ShutdownReason:
    """Reason recorded when a runtime begins coordinated shutdown."""

    kind: str
    detail: str = ""
    at: datetime | None = None


@runtime_checkable
class RuntimeComponent(Protocol):
    """Minimal lifecycle contract used by the future supervisor."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class ResourceOwner(Protocol):
    """Contract for resources which must have one runtime owner and close path."""

    @property
    def owner_id(self) -> str: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Ack:
    """Acknowledgement for a consumed event."""

    event_id: str
    consumer: str
    accepted: bool = True
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Close:
    """Control message requesting an orderly transport close."""

    reason: str = "shutdown"


@dataclass(frozen=True, slots=True)
class Backpressure:
    """Capacity signal emitted when a bounded queue cannot accept an event."""

    queue_name: str
    limit: int
    policy: DropPolicy
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConsumerFailure:
    """Structured consumer failure for retry/dead-letter decisions."""

    event_id: str
    consumer: str
    category: ErrorCategory
    detail: str
    retryable: bool = False
