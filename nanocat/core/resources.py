"""Capacity and execution-budget contracts for runtime resource isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from nanocat.core.runtime import CancellationToken, DropPolicy


class ResourceKind(StrEnum):
    """Resource dimensions which require an owner and explicit capacity."""

    RUNTIME = "runtime"
    SESSION = "session"
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    TOOL = "tool"
    SUBAGENT = "subagent"
    PROVIDER = "provider"
    MCP = "mcp"
    CHANNEL = "channel"
    CRON = "cron"
    HEARTBEAT = "heartbeat"
    PROCESS = "process"
    HTTP = "http"
    MEDIA = "media"
    TEMP_FILE = "temp_file"
    STREAM = "stream"
    CACHE = "cache"


class PriorityClass(IntEnum):
    """Lower values are reserved for control-plane work."""

    CONTROL = 0
    INTERVENTION = 10
    INTERACTIVE = 20
    BACKGROUND = 50
    PROGRESS = 100


class BatchFailurePolicy(StrEnum):
    """Explicit failure policy for a batch of potentially side-effecting work."""

    FAIL_FAST = "fail_fast"
    CONTINUE = "continue"
    CANCEL_SIBLINGS = "cancel_siblings"
    PARTIAL_COMMIT = "partial_commit"


@dataclass(frozen=True, slots=True)
class Deadline:
    """Absolute deadline independent of the event-loop implementation."""

    at: datetime | None = None

    @classmethod
    def after_seconds(cls, seconds: float, *, now: datetime | None = None) -> "Deadline":
        """Create a deadline from a duration using an aware UTC timestamp."""
        if seconds < 0:
            raise ValueError("deadline duration must not be negative")
        base = now or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        return cls(at=base + timedelta(seconds=seconds))

    @property
    def expired(self) -> bool:
        """Return whether the deadline has passed."""
        return self.remaining_seconds <= 0 if self.at is not None else False

    @property
    def remaining_seconds(self) -> float:
        """Return remaining seconds, or infinity when no deadline is configured."""
        if self.at is None:
            return float("inf")
        at = self.at if self.at.tzinfo else self.at.replace(tzinfo=timezone.utc)
        return (at - datetime.now(timezone.utc)).total_seconds()


@dataclass(frozen=True, slots=True)
class ResourceQuota:
    """Capacity policy for one resource dimension."""

    name: str
    limit: int
    reserved: int = 0
    on_exhausted: DropPolicy = DropPolicy.REJECT

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("resource quota limit must be positive")
        if self.reserved < 0 or self.reserved > self.limit:
            raise ValueError("reserved capacity must be within the quota limit")


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """Runtime-scoped quota catalog; enforcement belongs to the supervisor."""

    quotas: Mapping[ResourceKind, ResourceQuota]

    def __post_init__(self) -> None:
        normalized = {ResourceKind(kind): quota for kind, quota in self.quotas.items()}
        object.__setattr__(self, "quotas", MappingProxyType(normalized))

    def quota_for(self, resource: ResourceKind) -> ResourceQuota:
        """Return the explicit quota for a resource dimension."""
        return self.quotas[resource]


@dataclass(frozen=True, slots=True)
class ResourceDescriptor:
    """Owner and cleanup metadata for a created runtime resource."""

    resource_id: str
    kind: ResourceKind
    owner_id: str
    quota: ResourceQuota
    close_path: str
    max_lifetime_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_lifetime_seconds is not None and self.max_lifetime_seconds <= 0:
            raise ValueError("max_lifetime_seconds must be positive")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class OperationBudget:
    """Deadline, cancellation and queue policy carried by one operation."""

    operation_id: str
    owner_id: str
    resource: ResourceKind
    deadline: Deadline
    cancellation: CancellationToken
    priority: PriorityClass = PriorityClass.INTERACTIVE
    queue_limit: int = 1
    batch_failure_policy: BatchFailurePolicy = BatchFailurePolicy.FAIL_FAST

    def __post_init__(self) -> None:
        if self.queue_limit <= 0:
            raise ValueError("queue_limit must be positive")


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Read-only capacity snapshot for status and metrics surfaces."""

    resource: ResourceKind
    owner_id: str
    limit: int
    in_use: int = 0
    queued: int = 0
    rejected: int = 0
