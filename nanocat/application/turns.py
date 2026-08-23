"""Runtime-local turn execution state and transition guard."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from nanocat.core.messages import ConversationRef


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Normalized request shared by interactive and scheduler turn callers."""

    content: str
    conversation: ConversationRef
    principal_id: str = "user"
    source: str = "direct"
    transient: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    deadline_at: datetime | None = None


class TurnState(StrEnum):
    """Observable application state for one user turn."""

    RECEIVED = "received"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(slots=True)
class TurnRecord:
    """In-memory state for a single turn; never persisted to session history."""

    turn_id: str
    session_key: str
    principal_id: str
    request_id: str | None = None
    conversation_id: str | None = None
    state: TurnState = TurnState.RECEIVED
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    detail: str = ""

    @property
    def duration_ms(self) -> int:
        """Return monotonic-by-state wall duration for public runtime views."""
        endpoint = self.ended_at or self.updated_at
        return max(0, int((endpoint - self.started_at).total_seconds() * 1000))


class TurnCoordinator:
    """Own turn state transitions independently from AgentLoop implementation."""

    _TERMINAL = frozenset(
        {TurnState.COMPLETED, TurnState.CANCELLED, TurnState.FAILED}
    )

    def __init__(self, terminal_retention: int = 256) -> None:
        if terminal_retention <= 0:
            raise ValueError("terminal turn retention must be positive")
        self._turns: dict[str, TurnRecord] = {}
        self._terminal_ids: deque[str] = deque()
        self._terminal_finalizers: dict[str, list[Callable[[], None]]] = {}
        self._join_counts: dict[str, int] = {}
        self._deferred_terminal: dict[str, tuple[TurnState, str]] = {}
        self._terminal_retention = terminal_retention

    def register(
        self,
        turn_id: str,
        session_key: str,
        principal_id: str,
        *,
        request_id: str | None = None,
        conversation_id: str | None = None,
    ) -> TurnRecord:
        existing = self._turns.get(turn_id)
        if existing is not None:
            if (
                existing.session_key != session_key
                or existing.principal_id != principal_id
                or existing.request_id != request_id
                or existing.conversation_id != conversation_id
            ):
                raise ValueError(f"turn id ownership mismatch: {turn_id}")
            return existing
        now = datetime.now(timezone.utc)
        record = TurnRecord(
            turn_id=turn_id,
            session_key=session_key,
            principal_id=principal_id,
            request_id=request_id,
            conversation_id=conversation_id,
            started_at=now,
            updated_at=now,
        )
        self._turns[turn_id] = record
        return record

    def start(
        self,
        turn_id: str,
        session_key: str,
        principal_id: str,
        *,
        request_id: str | None = None,
        conversation_id: str | None = None,
    ) -> TurnRecord:
        """Register a turn if needed and transition admitted work to running."""
        record = self.register(
            turn_id,
            session_key,
            principal_id,
            request_id=request_id,
            conversation_id=conversation_id,
        )
        if record.state is TurnState.RECEIVED:
            self.transition(turn_id, TurnState.RUNNING)
        elif record.state is not TurnState.RUNNING:
            raise ValueError(f"turn {turn_id} cannot start from {record.state.value}")
        return record

    def active_for_session(self, session_key: str) -> TurnRecord | None:
        """Return the newest turn that is actively executing or awaiting input."""
        candidates = [
            record
            for record in self._turns.values()
            if record.session_key == session_key and record.state not in self._TERMINAL
        ]
        running = [
            record
            for record in candidates
            if record.state in {TurnState.RUNNING, TurnState.WAITING_FOR_USER}
        ]
        if running:
            return max(running, key=lambda record: record.started_at)
        received = [record for record in candidates if record.state is TurnState.RECEIVED]
        return min(received, key=lambda record: record.started_at) if received else None

    def get(self, turn_id: str) -> TurnRecord | None:
        return self._turns.get(turn_id)

    def snapshot(self) -> tuple[TurnRecord, ...]:
        return tuple(self._turns.values())

    def transition(self, turn_id: str, state: TurnState, detail: str = "") -> None:
        record = self._turns.get(turn_id)
        if record is None:
            raise KeyError(f"unknown turn id: {turn_id}")
        if record.state in self._TERMINAL:
            return
        if record.state is TurnState.CANCELLING:
            return
        if state is TurnState.RECEIVED:
            raise ValueError("a turn cannot transition back to received")
        if state in self._TERMINAL and self._join_counts.get(turn_id, 0) > 0:
            self._deferred_terminal[turn_id] = (state, detail)
            record.detail = detail
            record.updated_at = datetime.now(timezone.utc)
            return
        record.state = state
        record.detail = detail
        record.updated_at = datetime.now(timezone.utc)
        record.ended_at = record.updated_at if state in self._TERMINAL else None
        if state in self._TERMINAL:
            for finalizer in self._terminal_finalizers.pop(turn_id, []):
                try:
                    finalizer()
                except Exception:
                    # Terminal state must remain authoritative even when cleanup fails.
                    pass
            self._terminal_ids.append(turn_id)
            while len(self._terminal_ids) > self._terminal_retention:
                expired_id = self._terminal_ids.popleft()
                self._turns.pop(expired_id, None)
                self._join_counts.pop(expired_id, None)
                self._deferred_terminal.pop(expired_id, None)

    def reserve_join(self, turn_id: str) -> bool:
        """Keep a live turn open while an admitted steer is awaiting consumption."""
        record = self._turns.get(turn_id)
        if record is None or record.state in self._TERMINAL | {TurnState.CANCELLING}:
            return False
        self._join_counts[turn_id] = self._join_counts.get(turn_id, 0) + 1
        return True

    def release_join(self, turn_id: str) -> None:
        """Release one queued steer and apply any deferred terminal transition."""
        count = self._join_counts.get(turn_id, 0)
        if count <= 1:
            self._join_counts.pop(turn_id, None)
            terminal = self._deferred_terminal.pop(turn_id, None)
            if terminal is not None:
                self.transition(turn_id, *terminal)
            return
        self._join_counts[turn_id] = count - 1

    def activate_join(self, turn_id: str) -> bool:
        """Transfer a queued steer into a new execution of the same logical turn."""
        record = self._turns.get(turn_id)
        if record is None or record.state in self._TERMINAL | {TurnState.CANCELLING}:
            return False
        count = self._join_counts.get(turn_id, 0)
        if count <= 0:
            return False
        if count == 1:
            self._join_counts.pop(turn_id, None)
        else:
            self._join_counts[turn_id] = count - 1
        self._deferred_terminal.pop(turn_id, None)
        return True

    def terminal_deferred(self, turn_id: str) -> bool:
        """Return whether a queued steer is holding a terminal transition open."""
        return turn_id in self._deferred_terminal

    def add_terminal_finalizer(
        self, turn_id: str, finalizer: Callable[[], None]
    ) -> None:
        """Run cleanup once when a retained turn reaches a terminal state."""
        record = self._turns.get(turn_id)
        if record is None:
            raise KeyError(f"unknown turn id: {turn_id}")
        if record.state in self._TERMINAL:
            finalizer()
            return
        self._terminal_finalizers.setdefault(turn_id, []).append(finalizer)

    def waiting_for_user(self, turn_id: str) -> None:
        self.transition(turn_id, TurnState.WAITING_FOR_USER)

    def resumed(self, turn_id: str) -> None:
        self.transition(turn_id, TurnState.RUNNING)

    def complete(self, turn_id: str) -> None:
        self.transition(turn_id, TurnState.COMPLETED)

    def cancel(self, turn_id: str, detail: str = "") -> None:
        self.transition(turn_id, TurnState.CANCELLED, detail)

    def begin_cancel(self, turn_id: str, detail: str = "") -> None:
        self.transition(turn_id, TurnState.CANCELLING, detail)

    def finalize_cancel(self, turn_id: str, detail: str = "") -> None:
        record = self._turns.get(turn_id)
        if record is None:
            raise KeyError(f"unknown turn id: {turn_id}")
        if record.state is not TurnState.CANCELLING:
            self.cancel(turn_id, detail)
            return
        record.state = TurnState.RUNNING
        self.transition(turn_id, TurnState.CANCELLED, detail)

    def fail(self, turn_id: str, detail: str = "") -> None:
        self.transition(turn_id, TurnState.FAILED, detail)

    def finalize_fail(self, turn_id: str, detail: str = "") -> None:
        """Finish cancellation cleanup with a durable failure terminal state."""
        record = self._turns.get(turn_id)
        if record is None:
            raise KeyError(f"unknown turn id: {turn_id}")
        if record.state is not TurnState.CANCELLING:
            self.fail(turn_id, detail)
            return
        record.state = TurnState.RUNNING
        self.transition(turn_id, TurnState.FAILED, detail)

    def cancel_session(self, session_key: str, detail: str = "") -> None:
        for record in self._turns.values():
            if record.session_key == session_key and record.state not in self._TERMINAL:
                self.cancel(record.turn_id, detail)

    def remove_terminal(self, turn_id: str) -> TurnRecord | None:
        record = self._turns.get(turn_id)
        if record is not None and record.state in self._TERMINAL:
            self._terminal_finalizers.pop(turn_id, None)
            self._join_counts.pop(turn_id, None)
            self._deferred_terminal.pop(turn_id, None)
            return self._turns.pop(turn_id)
        return None
