"""Runtime-scoped scheduler-owned human intervention broker."""

from __future__ import annotations

import asyncio
import shlex
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from nanocat.application.text_catalog import USER_TEXT
from nanocat.bus.events import OutboundMessage
from nanocat.core.intervention import (
    DeliveryResult,
    InterventionAction,
    InterventionFlow,
    InterventionRequest,
    InterventionResult,
    InterventionState,
)
from nanocat.core.messages import ConversationRef


class InterventionError(RuntimeError):
    """Base error for scheduler-owned intervention failures."""


class InterventionDeliveryError(InterventionError):
    """Raised when the user cannot receive a sensitive-operation prompt."""


@dataclass(frozen=True, slots=True)
class ParsedInterventionAction:
    """Normalized text/button equivalent for an intervention response."""

    action: InterventionAction | None
    error: str | None = None


@dataclass(slots=True)
class _PendingIntervention:
    request: InterventionRequest
    future: asyncio.Future[InterventionResult]
    deferred: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class _DeferredDelivery:
    message: Any
    completion: asyncio.Future[None]
    released: bool = False
    claimed: bool = False
    force_failure: bool = False
    phase: str = "queued"
    operation: asyncio.Task[None] | None = None


RequestUser = Callable[[InterventionRequest], Awaitable[DeliveryResult | bool | None]]
DeferredSink = Callable[[Any], Awaitable[None]]
DeferredFailure = Callable[[Any], Awaitable[None]]


class DeliveryTracker:
    """Resolve adapter delivery results for request-scoped control messages."""

    def __init__(self, confirmation_timeout_seconds: float = 10.0) -> None:
        if confirmation_timeout_seconds <= 0:
            raise ValueError("delivery confirmation timeout must be positive")
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self._waiters: dict[str, asyncio.Future[DeliveryResult]] = {}
        self._closed = False

    def register(self, request_id: str) -> None:
        """Create a delivery waiter before publishing an outbound message."""
        if self._closed:
            raise InterventionDeliveryError("delivery tracker is closed")
        if request_id in self._waiters:
            raise InterventionError(f"duplicate delivery request: {request_id}")
        self._waiters[request_id] = asyncio.get_running_loop().create_future()

    async def wait(self, request_id: str, timeout: float) -> DeliveryResult:
        """Wait for the dispatcher to report the adapter's terminal result."""
        future = self._waiters.get(request_id)
        if future is None:
            return DeliveryResult(delivered=False, detail="delivery request is unknown")
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            return DeliveryResult(delivered=False, detail="delivery confirmation timed out")
        finally:
            self._waiters.pop(request_id, None)

    def resolve(self, request_id: str, result: DeliveryResult) -> None:
        """Complete one dispatcher delivery result without awaiting the broker."""
        future = self._waiters.get(request_id)
        if future is not None and not future.done():
            future.set_result(result)

    def is_pending(self, request_id: str) -> bool:
        """Return whether a request still has an active delivery waiter."""
        future = self._waiters.get(request_id)
        return future is not None and not future.done()

    def discard(self, request_id: str) -> None:
        """Forget a request which failed before entering the outbound queue."""
        self._waiters.pop(request_id, None)

    def close(self) -> None:
        """Wake all waiters with a terminal failure during runtime shutdown."""
        self._closed = True
        for future in self._waiters.values():
            if not future.done():
                future.set_result(DeliveryResult(delivered=False, detail="runtime is closing"))


def new_intervention_request(
    *,
    kind: Any,
    turn_id: str,
    conversation: ConversationRef,
    principal_id: str,
    capability: str,
    summary: str,
    call_fingerprint: str,
    expires_at: datetime,
    resume_mode: Any,
    tool_name: str = "",
    tool_params: str = "{}",
    flow: InterventionFlow = InterventionFlow.MANUAL,
    review_decision: str | None = None,
    review_reason: str | None = None,
    allowed_actions: tuple[InterventionAction, ...] | None = None,
    tool_call_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> InterventionRequest:
    """Create a request with scheduler-generated identifiers."""
    return InterventionRequest(
        request_id=str(uuid4()),
        kind=kind,
        turn_id=turn_id,
        session_key=conversation.session_key,
        conversation=conversation,
        principal_id=principal_id,
        capability=capability,
        summary=summary,
        call_fingerprint=call_fingerprint,
        allowed_actions=allowed_actions
        or (
            InterventionAction.APPROVE_ONCE,
            InterventionAction.APPROVE_TURN,
            InterventionAction.APPROVE_FOREVER,
            InterventionAction.REJECT,
        ),
        resume_mode=resume_mode,
        expires_at=expires_at,
        tool_name=tool_name,
        tool_params=tool_params,
        flow=flow,
        review_decision=review_decision,
        review_reason=review_reason,
        tool_call_id=tool_call_id,
        metadata=metadata or {},
    )


def parse_intervention_action(text: str) -> ParsedInterventionAction | None:
    """Parse only explicit approval, rejection, or session-revoke syntax."""
    stripped = text.strip()
    command_hint = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    if command_hint not in {"/approve", "/deny", "/reject"}:
        return None
    try:
        parts = shlex.split(stripped)
    except ValueError as exc:
        return ParsedInterventionAction(None, str(exc))

    command = parts[0].lower()
    if command == "/approve":
        if len(parts) == 1:
            action = InterventionAction.APPROVE_ONCE
        elif len(parts) == 2 and parts[1].lower() in {"once", "turn", "forever", "cancel"}:
            action = {
                "once": InterventionAction.APPROVE_ONCE,
                "turn": InterventionAction.APPROVE_TURN,
                "forever": InterventionAction.APPROVE_FOREVER,
                "cancel": InterventionAction.REVOKE_SESSION,
            }[parts[1].lower()]
        else:
            return ParsedInterventionAction(
                None,
                "Usage: /approve [once|turn|forever|cancel]",
            )
    elif command in {"/deny", "/reject"}:
        if len(parts) != 1:
            return ParsedInterventionAction(None, "Usage: /deny")
        action = InterventionAction.REJECT
    return ParsedInterventionAction(action)


def intervention_prompt(request: InterventionRequest) -> str:
    """Build a compact, scheduler-owned prompt for any text channel."""
    tool_name = str(request.tool_name or request.metadata.get("tool_name") or "unknown")
    tool_params = str(request.tool_params or request.metadata.get("tool_params") or "{}")
    tool = f"{tool_name}({tool_params})".replace("`", "'").replace("\n", " ")
    reason = (request.review_reason or request.summary or "Sensitive operation")[:1_000]
    if request.flow is InterventionFlow.AUTO_REVIEW:
        return USER_TEXT.intervention_auto_review.format(tool=tool, reason=reason)

    commands: list[str] = []
    if InterventionAction.APPROVE_ONCE in request.allowed_actions:
        commands.append("`/approve`")
    if InterventionAction.APPROVE_TURN in request.allowed_actions:
        commands.append("`/approve turn`")
    if InterventionAction.APPROVE_FOREVER in request.allowed_actions:
        commands.append("`/approve forever`")
    if InterventionAction.REJECT in request.allowed_actions:
        commands.append("`/deny`")
    return USER_TEXT.intervention_manual.format(
        tool=tool,
        reason=reason,
        actions="、".join(commands),
    )


def make_bus_presenter(
    bus: Any,
    delivery_tracker: DeliveryTracker | None = None,
) -> RequestUser:
    """Create a channel-neutral presenter backed by the runtime outbound port."""

    async def present(request: InterventionRequest) -> DeliveryResult:
        if delivery_tracker is not None:
            delivery_tracker.register(request.request_id)
        try:
            await bus.publish_outbound(
                OutboundMessage(
                    channel=request.conversation.channel,
                    chat_id=request.conversation.chat_id,
                    content=intervention_prompt(request),
                    metadata={
                        "_control": True,
                        "_intervention": True,
                        "request_id": request.request_id,
                        "capability": request.capability,
                        "operation": request.summary,
                        "tool_name": request.tool_name,
                        "tool_params": request.tool_params,
                        "approval_flow": request.flow.value,
                        "review_decision": request.review_decision,
                        "review_reason": request.review_reason,
                        "allowed_actions": [action.value for action in request.allowed_actions],
                        "expires_at": request.expires_at.isoformat(),
                    },
                    request_id=request.request_id,
                    turn_id=request.turn_id,
                    principal_id=request.principal_id,
                )
            )
        except Exception:
            if delivery_tracker is not None:
                delivery_tracker.discard(request.request_id)
            raise
        if delivery_tracker is not None:
            remaining = max(
                0.1,
                (request.expires_at - datetime.now(timezone.utc)).total_seconds(),
            )
            return await delivery_tracker.wait(
                request.request_id,
                min(remaining, delivery_tracker.confirmation_timeout_seconds),
            )
        return DeliveryResult(delivered=True)

    return present


class InterventionBroker:
    """Own pending intervention state for exactly one runtime instance."""

    _MAX_DEFERRED_MESSAGES = 4096

    def __init__(
        self,
        request_user: RequestUser,
        *,
        deferred_sink: DeferredSink | None = None,
        delivery_tracker: DeliveryTracker | None = None,
        default_timeout_seconds: float = 300.0,
    ):
        if default_timeout_seconds <= 0:
            raise ValueError("default intervention timeout must be positive")
        self._request_user = request_user
        self._deferred_sink = deferred_sink
        self._deferred_release: Callable[[Any], None] | None = None
        self._deferred_failure: DeferredFailure | None = None
        self._delivery_tracker = delivery_tracker
        self._default_timeout_seconds = default_timeout_seconds
        self._pending: dict[str, _PendingIntervention] = {}
        self._turn_grants: set[tuple[ConversationRef, str, str, str]] = set()
        self._session_grants: set[tuple[ConversationRef, str, str]] = set()
        self._defer_scopes: dict[ConversationRef, int] = {}
        self._deferred_hold: dict[ConversationRef, list[Any]] = {}
        self._deferred_delivery_backlog: deque[_DeferredDelivery] = deque()
        self._deferred_delivery_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether the broker has been closed."""
        return self._closed

    def set_deferred_release(self, callback: Callable[[Any], None]) -> None:
        """Bind the runtime admission release used by deferred messages."""
        self._deferred_release = callback

    def set_deferred_failure(self, callback: DeferredFailure) -> None:
        """Bind terminal handling for a deferred message that cannot be replayed."""
        self._deferred_failure = callback

    @property
    def default_timeout_seconds(self) -> float:
        """Return the runtime-wide intervention timeout."""
        return self._default_timeout_seconds

    def pending_requests(self) -> tuple[InterventionRequest, ...]:
        """Return a detached snapshot for status and diagnostics."""
        return tuple(item.request for item in self._pending.values())

    def current_pending(
        self,
        conversation: ConversationRef,
        principal_id: str,
    ) -> InterventionRequest | None:
        """Return the first active request for command feedback."""
        now = datetime.now(timezone.utc)
        return next(
            (
                item.request
                for item in self._pending.values()
                if item.request.conversation == conversation
                and item.request.principal_id == principal_id
                and not item.future.done()
                and item.request.expires_at > now
            ),
            None,
        )

    def has_pending(self, conversation: ConversationRef) -> bool:
        """Return whether a conversation is waiting for a user decision."""
        now = datetime.now(timezone.utc)
        return any(
            item.request.conversation == conversation and item.request.expires_at > now
            for item in self._pending.values()
        )

    def has_turn_grant(
        self,
        conversation: ConversationRef,
        principal_id: str,
        turn_id: str,
        capability: str,
    ) -> bool:
        """Return whether this turn already approved the same capability."""
        return (conversation, principal_id, turn_id, capability) in self._turn_grants

    def has_session_grant(
        self,
        conversation: ConversationRef,
        principal_id: str,
        capability: str,
        tool_name: str | None = None,
    ) -> bool:
        """Return whether this session approved a capability or tool forever."""
        if (conversation, principal_id, capability) in self._session_grants:
            return True
        if tool_name is None:
            return False
        return any(
            grant[:2] == (conversation, principal_id)
            and grant[2] in {f"tool:{tool_name}", "tool:*"}
            for grant in self._session_grants
        )

    async def suspend(self, request: InterventionRequest) -> InterventionResult:
        """Publish a prompt and suspend until a validated action, timeout or close."""
        loop = asyncio.get_running_loop()
        pending = _PendingIntervention(request=request, future=loop.create_future())
        async with self._lock:
            if self._closed:
                return InterventionResult(
                    request_id=request.request_id,
                    action=InterventionAction.CANCEL,
                    state=InterventionState.CANCELLED,
                )
            if request.request_id in self._pending:
                raise InterventionError(f"duplicate intervention request: {request.request_id}")
            self._pending[request.request_id] = pending

        try:
            try:
                delivery = await self._request_user(request)
                if isinstance(delivery, DeliveryResult) and not delivery.delivered:
                    raise InterventionDeliveryError(delivery.detail or "prompt delivery failed")
                if delivery is False:
                    raise InterventionDeliveryError("prompt delivery failed")
            except Exception:
                return InterventionResult(
                    request_id=request.request_id,
                    action=InterventionAction.CANCEL,
                    state=InterventionState.DELIVERY_FAILED,
                )

            remaining = max(
                0.0,
                (request.expires_at - datetime.now(timezone.utc)).total_seconds(),
            )
            if remaining == 0:
                return InterventionResult(
                    request_id=request.request_id,
                    action=InterventionAction.CANCEL,
                    state=InterventionState.EXPIRED,
                )
            try:
                return await asyncio.wait_for(asyncio.shield(pending.future), remaining)
            except asyncio.TimeoutError:
                async with self._lock:
                    if not pending.future.done():
                        pending.future.set_result(
                            InterventionResult(
                                request_id=request.request_id,
                                action=InterventionAction.CANCEL,
                                state=InterventionState.EXPIRED,
                            )
                        )
                    return pending.future.result()
        finally:
            async with self._lock:
                self._pending.pop(request.request_id, None)
                deferred = list(pending.deferred)
                pending.deferred.clear()
                if self._defer_scopes.get(request.conversation, 0):
                    self._deferred_hold.setdefault(request.conversation, []).extend(deferred)
                    deferred = []
                self._queue_deferred_locked(deferred)

    async def resolve(
        self,
        principal_id: str,
        conversation: ConversationRef,
        action: InterventionAction,
    ) -> InterventionResult | None:
        """Resolve the current session's pending request without user tokens."""
        async with self._lock:
            if self._closed:
                return None
            if action is InterventionAction.REVOKE_SESSION:
                return self._revoke_session_locked(conversation, principal_id)

            pending = next(
                (
                    item
                    for item in self._pending.values()
                    if item.request.conversation == conversation
                    and item.request.principal_id == principal_id
                    and not item.future.done()
                ),
                None,
            )
            if pending is None:
                # ``/deny`` and ``/reject`` are intentionally hidden aliases
                # for ``/approve cancel`` while the session is idle.
                if action is InterventionAction.REJECT:
                    return self._revoke_session_locked(conversation, principal_id)
                if action is not InterventionAction.APPROVE_FOREVER:
                    return None
                self._session_grants.add((conversation, principal_id, "tool:*"))
                return InterventionResult(
                    request_id="",
                    action=action,
                    scope="session",
                    state=InterventionState.APPROVED_FOREVER,
                )
            request = pending.request
            if action not in request.allowed_actions:
                return None
            if request.expires_at <= datetime.now(timezone.utc):
                pending.future.set_result(
                    InterventionResult(
                        request_id=request.request_id,
                        action=InterventionAction.CANCEL,
                        state=InterventionState.EXPIRED,
                    )
                )
                return None
            state = {
                InterventionAction.APPROVE_ONCE: InterventionState.APPROVED_ONCE,
                InterventionAction.APPROVE_TURN: InterventionState.APPROVED_TURN,
                InterventionAction.APPROVE_FOREVER: InterventionState.APPROVED_FOREVER,
                InterventionAction.REJECT: InterventionState.REJECTED,
                InterventionAction.CANCEL: InterventionState.CANCELLED,
            }[action]
            scope = {
                InterventionAction.APPROVE_ONCE: "once",
                InterventionAction.APPROVE_TURN: "turn",
                InterventionAction.APPROVE_FOREVER: "session",
            }.get(action)
            if action is InterventionAction.APPROVE_TURN:
                self._turn_grants.add(
                    (request.conversation, request.principal_id, request.turn_id, request.capability)
                )
            elif action is InterventionAction.APPROVE_FOREVER:
                self._session_grants.add(
                    (request.conversation, request.principal_id, "tool:*")
                )
            pending.future.set_result(
                InterventionResult(
                    request_id=request.request_id,
                    action=action,
                    scope=scope,
                    state=state,
                    review_reason=request.review_reason,
                )
            )
            return pending.future.result()

    def _revoke_session_locked(
        self,
        conversation: ConversationRef,
        principal_id: str,
    ) -> InterventionResult:
        pending_request_id = ""
        self._session_grants = {
            grant
            for grant in self._session_grants
            if not (grant[0] == conversation and grant[1] == principal_id)
        }
        self._turn_grants = {
            grant
            for grant in self._turn_grants
            if not (grant[0] == conversation and grant[1] == principal_id)
        }
        for pending in self._pending.values():
            if (
                pending.request.conversation == conversation
                and pending.request.principal_id == principal_id
                and not pending.future.done()
            ):
                pending_request_id = pending_request_id or pending.request.request_id
                pending.future.set_result(
                    InterventionResult(
                        request_id=pending.request.request_id,
                        action=InterventionAction.CANCEL,
                        scope="session",
                        state=InterventionState.CANCELLED,
                    )
                )
        return InterventionResult(
            request_id=pending_request_id,
            action=InterventionAction.REVOKE_SESSION,
            scope="session",
            state=InterventionState.REVOKED,
        )

    def defer(self, conversation: ConversationRef, message: Any) -> bool:
        """Defer ordinary input while intervention or a grouped approval is active."""
        deferred_count = len(self._deferred_delivery_backlog) + sum(
            len(pending.deferred) for pending in self._pending.values()
        ) + sum(len(messages) for messages in self._deferred_hold.values())
        if deferred_count >= self._MAX_DEFERRED_MESSAGES:
            return False
        for pending in self._pending.values():
            if pending.request.conversation == conversation:
                pending.deferred.append(message)
                return True
        if self._defer_scopes.get(conversation, 0):
            self._deferred_hold.setdefault(conversation, []).append(message)
            return True
        return False

    def extract_deferred_turn(self, turn_id: str) -> list[Any]:
        """Remove deferred messages owned by one exact turn."""
        extracted: list[Any] = []

        def matches(message: Any) -> bool:
            value = getattr(message, "turn_id", None)
            if value:
                return str(value) == turn_id
            metadata = getattr(message, "metadata", None)
            return isinstance(metadata, dict) and str(metadata.get("turn_id") or "") == turn_id

        for pending in self._pending.values():
            retained = []
            for message in pending.deferred:
                (extracted if matches(message) else retained).append(message)
            pending.deferred = retained
        for conversation, messages in tuple(self._deferred_hold.items()):
            retained = []
            for message in messages:
                (extracted if matches(message) else retained).append(message)
            if retained:
                self._deferred_hold[conversation] = retained
            else:
                self._deferred_hold.pop(conversation, None)
        extracted.extend(self._extract_deferred_delivery(matches))
        return extracted

    def extract_deferred_session(self, session_key: str) -> list[Any]:
        """Remove every deferred message owned by one session."""
        extracted: list[Any] = []
        for pending in self._pending.values():
            if pending.request.session_key == session_key:
                extracted.extend(pending.deferred)
                pending.deferred.clear()
        for conversation, messages in tuple(self._deferred_hold.items()):
            if conversation.session_key == session_key:
                extracted.extend(messages)
                self._deferred_hold.pop(conversation, None)
        extracted.extend(
            self._extract_deferred_delivery(
                lambda message: getattr(message, "session_key", None) == session_key
            )
        )
        return extracted

    async def begin_defer_scope(self, conversation: ConversationRef) -> None:
        """Keep ordinary input deferred across a multi-call approval batch."""
        async with self._lock:
            self._defer_scopes[conversation] = self._defer_scopes.get(conversation, 0) + 1

    async def end_defer_scope(self, conversation: ConversationRef) -> None:
        """Release messages held by a completed approval batch."""
        async with self._lock:
            depth = self._defer_scopes.get(conversation, 0)
            if depth <= 1:
                self._defer_scopes.pop(conversation, None)
                deferred = self._deferred_hold.pop(conversation, [])
            else:
                self._defer_scopes[conversation] = depth - 1
                deferred = []
            self._queue_deferred_locked(deferred)

    async def _deliver_deferred(self, messages: list[Any]) -> None:
        """Transfer deferred messages to the broker-owned delivery worker."""
        self._queue_deferred_locked(messages)

    async def settle_deferred_turn(self, turn_id: str) -> list[Any]:
        """Settle active sink handoffs before exact turn cancellation drains ingress."""

        def matches(message: Any) -> bool:
            value = getattr(message, "turn_id", None)
            if value:
                return str(value) == turn_id
            metadata = getattr(message, "metadata", None)
            return isinstance(metadata, dict) and str(metadata.get("turn_id") or "") == turn_id

        return await self._settle_deferred_delivery(matches)

    async def settle_deferred_session(self, session_key: str) -> list[Any]:
        """Settle active sink handoffs before session cancellation drains ingress."""
        return await self._settle_deferred_delivery(
            lambda message: getattr(message, "session_key", None) == session_key
        )

    async def _settle_deferred_delivery(
        self,
        predicate: Callable[[Any], bool],
    ) -> list[Any]:
        """Claim unsent messages while allowing committed sink operations to settle."""
        extracted: list[Any] = []
        while True:
            extracted.extend(self._extract_deferred_delivery(predicate))
            operations = tuple(
                delivery.operation
                for delivery in self._deferred_delivery_backlog
                if predicate(delivery.message)
                and delivery.phase == "sink"
                and delivery.operation is not None
                and not delivery.operation.done()
            )
            failure_completions = tuple(
                delivery.completion
                for delivery in self._deferred_delivery_backlog
                if predicate(delivery.message) and delivery.phase == "failure"
            )
            if not operations and not failure_completions:
                extracted.extend(self._extract_deferred_delivery(predicate))
                return extracted
            await asyncio.gather(
                *(asyncio.shield(operation) for operation in operations),
                *(asyncio.shield(completion) for completion in failure_completions),
                return_exceptions=True,
            )

    def _queue_deferred_locked(self, messages: list[Any]) -> tuple[_DeferredDelivery, ...]:
        if not messages:
            return ()
        if len(self._deferred_delivery_backlog) + len(messages) > self._MAX_DEFERRED_MESSAGES:
            raise InterventionError("deferred delivery capacity exceeded")
        loop = asyncio.get_running_loop()
        deliveries = tuple(
            _DeferredDelivery(message=message, completion=loop.create_future())
            for message in messages
        )
        self._deferred_delivery_backlog.extend(deliveries)
        self._ensure_deferred_delivery_worker()
        return deliveries

    async def _wait_deferred(
        self,
        deliveries: tuple[_DeferredDelivery, ...],
    ) -> None:
        if deliveries:
            await asyncio.gather(
                *(asyncio.shield(delivery.completion) for delivery in deliveries)
            )

    def _ensure_deferred_delivery_worker(self) -> None:
        current = self._deferred_delivery_task
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_deferred_delivery(),
            name="nanocat.intervention-deferred-delivery",
        )
        self._deferred_delivery_task = task

        def settled(completed: asyncio.Task[None]) -> None:
            if self._deferred_delivery_task is completed:
                self._deferred_delivery_task = None
            try:
                completed.result()
            except BaseException:
                pass
            if self._deferred_delivery_backlog:
                self._ensure_deferred_delivery_worker()

        task.add_done_callback(settled)

    def _ack_deferred_delivery(self, delivery: _DeferredDelivery) -> None:
        try:
            self._deferred_delivery_backlog.remove(delivery)
        except ValueError:
            pass
        if not delivery.completion.done():
            delivery.completion.set_result(None)

    async def _run_deferred_delivery(self) -> None:
        retry_delay = 0.05
        while self._deferred_delivery_backlog:
            delivery = self._deferred_delivery_backlog[0]
            if delivery.claimed:
                self._ack_deferred_delivery(delivery)
                continue
            if not delivery.released and self._deferred_release is not None:
                self._deferred_release(delivery.message)
                delivery.released = True
            callback = (
                self._deferred_failure
                if self._closed or delivery.force_failure or self._deferred_sink is None
                else self._deferred_sink
            )
            if callback is None:
                self._ack_deferred_delivery(delivery)
                continue
            delivery.phase = "failure" if callback is self._deferred_failure else "sink"
            operation = asyncio.create_task(callback(delivery.message))
            delivery.operation = operation
            try:
                await operation
            except asyncio.CancelledError:
                if delivery.claimed:
                    self._ack_deferred_delivery(delivery)
                    continue
                if asyncio.current_task().cancelling():
                    raise
                delivery.force_failure = True
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 1.0)
                continue
            except Exception:
                if delivery.phase == "sink":
                    delivery.force_failure = True
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 1.0)
                continue
            finally:
                delivery.operation = None
            retry_delay = 0.05
            self._ack_deferred_delivery(delivery)

    def _extract_deferred_delivery(
        self,
        predicate: Callable[[Any], bool],
    ) -> list[Any]:
        extracted: list[Any] = []
        for delivery in tuple(self._deferred_delivery_backlog):
            if not predicate(delivery.message) or delivery.phase == "failure":
                continue
            operation = delivery.operation
            if operation is not None and not operation.done():
                continue
            if (
                delivery.phase == "sink"
                and operation is not None
                and operation.done()
                and not operation.cancelled()
                and operation.exception() is None
            ):
                continue
            delivery.claimed = True
            self._ack_deferred_delivery(delivery)
            extracted.append(delivery.message)
        return extracted

    async def cancel_turn(self, turn_id: str) -> None:
        self._turn_grants = {
            grant for grant in self._turn_grants if grant[2] != turn_id
        }
        await self._resolve_cancel(lambda request: request.turn_id == turn_id)

    async def cancel_session(self, session_key: str) -> None:
        self._turn_grants = {
            grant for grant in self._turn_grants if grant[0].session_key != session_key
        }
        self._session_grants = {
            grant for grant in self._session_grants if grant[0].session_key != session_key
        }
        await self._resolve_cancel(lambda request: request.session_key == session_key)

    async def finish_turn(self, turn_id: str) -> None:
        """Expire all grants owned by a completed turn."""
        await self.cancel_turn(turn_id)

    async def _resolve_cancel(self, predicate: Callable[[InterventionRequest], bool]) -> None:
        async with self._lock:
            for pending in self._pending.values():
                if predicate(pending.request) and not pending.future.done():
                    pending.future.set_result(
                        InterventionResult(
                            request_id=pending.request.request_id,
                            action=InterventionAction.CANCEL,
                            state=InterventionState.CANCELLED,
                        )
                    )

    async def close(self) -> None:
        """Cancel all pending requests and prevent new intervention waits."""
        async with self._lock:
            self._closed = True
            if self._delivery_tracker is not None:
                self._delivery_tracker.close()
            self._turn_grants.clear()
            self._session_grants.clear()
            self._defer_scopes.clear()
            for delivery in self._deferred_delivery_backlog:
                if delivery.phase != "sink":
                    continue
                delivery.force_failure = True
                if delivery.operation is not None and not delivery.operation.done():
                    delivery.operation.cancel()
            deferred = [
                message for messages in self._deferred_hold.values() for message in messages
            ]
            self._deferred_hold.clear()
            for pending in self._pending.values():
                deferred.extend(pending.deferred)
                pending.deferred.clear()
                if not pending.future.done():
                    pending.future.set_result(
                        InterventionResult(
                            request_id=pending.request.request_id,
                            action=InterventionAction.CANCEL,
                            state=InterventionState.CANCELLED,
                        )
                    )
            deliveries = self._queue_deferred_locked(deferred)
        await self._wait_deferred(deliveries)
        worker = self._deferred_delivery_task
        if worker is not None:
            await asyncio.shield(worker)
