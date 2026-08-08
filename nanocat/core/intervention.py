"""Runtime-scoped human intervention contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from nanocat.core.messages import ConversationRef


class InterventionKind(StrEnum):
    """Kinds of scheduler-owned user interaction."""

    APPROVAL = "approval"
    CONFIRMATION = "confirmation"
    CLARIFICATION = "clarification"
    SELECTION = "selection"


class InterventionFlow(StrEnum):
    """Presentation and action policy for one intervention request."""

    MANUAL = "manual"
    AUTO_REVIEW = "auto_review"


class InterventionAction(StrEnum):
    """Actions accepted by the intervention broker."""

    APPROVE_ONCE = "approve_once"
    APPROVE_TURN = "approve_turn"
    APPROVE_FOREVER = "approve_forever"
    REJECT = "reject"
    CANCEL = "cancel"
    REVOKE_SESSION = "revoke_session"


class ResumeMode(StrEnum):
    """How the owning turn resumes after user input."""

    RETRY_CALL = "retry_call"
    RESUME_WITH_VALUE = "resume_with_value"
    ABORT_TURN = "abort_turn"


class InterventionState(StrEnum):
    """Monotonic broker state for one intervention request."""

    PENDING = "pending"
    APPROVED_ONCE = "approved_once"
    APPROVED_TURN = "approved_turn"
    APPROVED_FOREVER = "approved_forever"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    DELIVERY_FAILED = "delivery_failed"
    CONSUMED = "consumed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Result of sending an intervention prompt through a channel-neutral port."""

    delivered: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class InterventionRequest:
    """A scheduler-owned request which never needs to be explained by an LLM."""

    request_id: str
    kind: InterventionKind
    turn_id: str
    session_key: str
    conversation: ConversationRef
    principal_id: str
    capability: str
    summary: str
    call_fingerprint: str
    allowed_actions: tuple[InterventionAction, ...]
    resume_mode: ResumeMode
    expires_at: datetime
    tool_name: str = ""
    tool_params: str = "{}"
    flow: InterventionFlow = InterventionFlow.MANUAL
    review_decision: str | None = None
    review_reason: str | None = None
    tool_call_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_actions", tuple(self.allowed_actions))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class InterventionResult:
    """Result returned to the owning turn after a broker resolution."""

    request_id: str
    action: InterventionAction
    scope: str | None = None
    resume_value: Any = None
    state: InterventionState = InterventionState.CONSUMED
    review_reason: str | None = None
