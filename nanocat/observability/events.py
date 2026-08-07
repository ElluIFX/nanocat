"""Correlation and audit records with detached, redacted details."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from nanocat.core.runtime import ErrorCategory
from nanocat.observability.contracts import CorrelationContext
from nanocat.observability.redaction import redact_mapping

CorrelationIds = CorrelationContext


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Structured audit record whose details are redacted at construction time."""

    action: str
    outcome: str
    category: ErrorCategory | None = None
    correlation: CorrelationContext = field(default_factory=CorrelationContext)
    details: Mapping[str, Any] = field(default_factory=dict)
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", redact_mapping(self.details))
