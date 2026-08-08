"""Implementation-neutral health, correlation and metric contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from nanocat.core.runtime import HealthState


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """Explicit correlation data passed through one operation tree."""

    event_id: str | None = None
    turn_id: str | None = None
    request_id: str | None = None
    parent_id: str | None = None
    session_key: str | None = None
    principal_id: str | None = None

    def child(self, **updates: str | None) -> "CorrelationContext":
        """Return a detached child context without hidden global state."""
        values = {
            "event_id": self.event_id,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "parent_id": self.parent_id,
            "session_key": self.session_key,
            "principal_id": self.principal_id,
        }
        values.update(updates)
        return CorrelationContext(**values)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Component health with recoverability and correlation information."""

    component: str
    state: HealthState
    reason: str = ""
    correlation_id: str | None = None
    recoverable: bool = True
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    """Serializable resource ownership and capacity observation."""

    resource_id: str
    kind: str
    owner_id: str
    limit: int
    in_use: int = 0
    queued: int = 0
    deadline_at: datetime | None = None
    close_path: str = ""
    state: str = "active"


@dataclass(frozen=True, slots=True)
class MetricSample:
    """Single metric observation; transport/export is intentionally out of scope."""

    name: str
    value: float
    unit: str = "count"
    dimensions: Mapping[str, str] = field(default_factory=dict)
    correlation_id: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", MappingProxyType(dict(self.dimensions)))
