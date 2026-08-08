"""Runtime-local turn execution state and transition guard."""

from __future__ import annotations

from collections import deque
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
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(slots=True)
class TurnRecord:
    """In-memory state for a single turn; never persisted to session history."""

    turn_id: str
    session_key: str
    principal_id: str
    state: TurnState = TurnState.RECEIVED
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail: str = ""


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
        self._terminal_retention = terminal_retention

    def start(self, turn_id: str, session_key: str, principal_id: str) -> TurnRecord:
        if turn_id in self._turns:
            raise ValueError(f"duplicate turn id: {turn_id}")
        now = datetime.now(timezone.utc)
        record = TurnRecord(
            turn_id=turn_id,
            session_key=session_key,
            principal_id=principal_id,
            started_at=now,
            updated_at=now,
        )
        self._turns[turn_id] = record
        self.transition(turn_id, TurnState.RUNNING)
        return record

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
        if state is TurnState.RECEIVED:
            raise ValueError("a turn cannot transition back to received")
        record.state = state
        record.detail = detail
        record.updated_at = datetime.now(timezone.utc)
        if state in self._TERMINAL:
            self._terminal_ids.append(turn_id)
            while len(self._terminal_ids) > self._terminal_retention:
                self._turns.pop(self._terminal_ids.popleft(), None)

    def waiting_for_user(self, turn_id: str) -> None:
        self.transition(turn_id, TurnState.WAITING_FOR_USER)

    def resumed(self, turn_id: str) -> None:
        self.transition(turn_id, TurnState.RUNNING)

    def complete(self, turn_id: str) -> None:
        self.transition(turn_id, TurnState.COMPLETED)

    def cancel(self, turn_id: str, detail: str = "") -> None:
        self.transition(turn_id, TurnState.CANCELLED, detail)

    def fail(self, turn_id: str, detail: str = "") -> None:
        self.transition(turn_id, TurnState.FAILED, detail)

    def cancel_session(self, session_key: str, detail: str = "") -> None:
        for record in self._turns.values():
            if record.session_key == session_key and record.state not in self._TERMINAL:
                self.cancel(record.turn_id, detail)

    def remove_terminal(self, turn_id: str) -> TurnRecord | None:
        record = self._turns.get(turn_id)
        if record is not None and record.state in self._TERMINAL:
            return self._turns.pop(turn_id)
        return None
