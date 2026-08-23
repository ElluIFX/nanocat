"""Bounded runtime event stream used by HTTP SSE consumers."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_DEFAULT_MAX_EVENT_BYTES = 64 * 1024


class SseCapacityError(RuntimeError):
    """Raised before response start when the subscriber budget is exhausted."""


class SseSubscription:
    """Explicit subscriber owner whose close works before first iteration."""

    def __init__(
        self,
        stream: AsyncIterator[bytes],
        unsubscribe: Callable[[], Awaitable[None]],
    ) -> None:
        self._stream = stream
        self._unsubscribe = unsubscribe
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def __aiter__(self) -> "SseSubscription":
        return self

    async def __anext__(self) -> bytes:
        return await self._stream.__anext__()

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
            self._close_task.add_done_callback(self._consume_close_error)
        await asyncio.shield(self._close_task)

    async def _close_owned(self) -> None:
        error: BaseException | None = None
        try:
            await self._unsubscribe()
        except BaseException as exc:
            error = exc
        try:
            closer = getattr(self._stream, "aclose", None)
            if closer is not None:
                await closer()
        except BaseException as exc:
            if error is None:
                error = exc
        finally:
            self._closed = True
        if error is not None:
            raise error

    @staticmethod
    def _consume_close_error(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One immutable SSE event with global replay and scoped ordering IDs."""

    event_id: int
    sequence: int
    timestamp: str
    type: str
    payload: dict[str, Any]
    source: str | None = None
    status: str | None = None
    node_id: str | None = None
    parent_id: str | None = None
    phase: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    request_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "eventId": str(self.event_id),
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": self.type,
            "source": self.source,
            "status": self.status,
            "nodeId": self.node_id,
            "parentId": self.parent_id,
            "phase": self.phase,
            "sessionId": self.session_id,
            "turnId": self.turn_id,
            "requestId": self.request_id,
            "payload": self.payload,
        }

    def encode(self) -> bytes:
        data = json.dumps(
            self.as_dict(), ensure_ascii=False, separators=(",", ":"), default=str
        )
        return f"id: {self.event_id}\nevent: {self.type}\ndata: {data}\n\n".encode()


@dataclass(slots=True, eq=False)
class _Subscriber:
    queue: asyncio.Queue[StreamEvent | None]
    session_id: str | None


class SseBroker:
    """Fan out events with bounded history and bounded subscriber queues."""

    def __init__(
        self,
        *,
        history_limit: int = 2048,
        subscriber_limit: int = 256,
        max_subscribers: int = 128,
        sequence_scope_limit: int = 4096,
        max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
    ) -> None:
        if (
            history_limit <= 0
            or subscriber_limit <= 1
            or max_subscribers <= 0
            or sequence_scope_limit <= 0
            or max_event_bytes < 1024
        ):
            raise ValueError("SSE limits must be positive")
        self._history: deque[StreamEvent] = deque(maxlen=history_limit)
        self._subscriber_limit = subscriber_limit
        self._max_subscribers = max_subscribers
        self._sequence_scope_limit = sequence_scope_limit
        self._max_event_bytes = max_event_bytes
        self._subscribers: set[_Subscriber] = set()
        self._sequences: OrderedDict[str, int] = OrderedDict()
        self._event_id = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _bounded_payload(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        value = dict(payload or {})
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(encoded) <= self._max_event_bytes:
            return value
        return {
            "truncated": True,
            "totalBytes": len(encoded),
        }

    @property
    def closed(self) -> bool:
        return self._closed

    async def publish(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
        source: str | None = None,
        status: str | None = None,
        node_id: str | None = None,
        parent_id: str | None = None,
        phase: str | None = None,
    ) -> StreamEvent:
        scope = session_id or "_runtime"
        async with self._lock:
            if self._closed:
                raise RuntimeError("SSE broker is closed")
            self._event_id += 1
            self._sequences[scope] = self._sequences.get(scope, 0) + 1
            self._sequences.move_to_end(scope)
            while len(self._sequences) > self._sequence_scope_limit:
                self._sequences.popitem(last=False)
            event = StreamEvent(
                event_id=self._event_id,
                sequence=self._sequences[scope],
                timestamp=datetime.now(timezone.utc).isoformat(),
                type=event_type,
                payload=self._bounded_payload(payload),
                source=source,
                status=status,
                node_id=node_id,
                parent_id=parent_id,
                phase=phase,
                session_id=session_id,
                turn_id=turn_id,
                request_id=request_id,
            )
            self._history.append(event)
            subscribers = tuple(self._subscribers)

        for subscriber in subscribers:
            if subscriber.session_id is not None and subscriber.session_id != session_id:
                continue
            self._offer(subscriber, event)
        return event

    def schedule_publish(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> None:
        """Publish from synchronous lifecycle finalizers under broker ownership."""
        if self._closed:
            return

        async def publish_owned() -> None:
            try:
                await self.publish(event_type, payload, **values)
            except RuntimeError:
                if not self._closed:
                    raise

        task = asyncio.create_task(
            publish_owned(),
            name=f"nanocat.sse.publish.{event_type}",
        )
        self._background_tasks.add(task)

        def forget(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(forget)

    def _offer(self, subscriber: _Subscriber, event: StreamEvent) -> None:
        try:
            subscriber.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        while not subscriber.queue.empty():
            try:
                subscriber.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        reset = StreamEvent(
            event_id=event.event_id,
            sequence=event.sequence,
            timestamp=event.timestamp,
            type="stream.reset",
            payload={"reason": "subscriber_overflow"},
            session_id=subscriber.session_id,
        )
        subscriber.queue.put_nowait(reset)

    async def open_subscription(
        self,
        *,
        last_event_id: str | None = None,
        session_id: str | None = None,
    ) -> SseSubscription:
        """Reserve subscriber capacity before an HTTP response is started."""
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue(
            maxsize=self._subscriber_limit
        )
        subscriber = _Subscriber(queue=queue, session_id=session_id)
        try:
            cursor = int(last_event_id) if last_event_id else 0
        except ValueError:
            cursor = -1

        async with self._lock:
            if self._closed:
                raise RuntimeError("SSE broker is closed")
            history = tuple(self._history)
            oldest = history[0].event_id if history else self._event_id + 1
            newest = self._event_id
            if len(self._subscribers) >= self._max_subscribers:
                raise SseCapacityError("SSE subscriber capacity is exhausted")
            self._subscribers.add(subscriber)

        initial: list[StreamEvent] = []
        if cursor < 0 or cursor > newest or (cursor and cursor < oldest - 1):
            initial.append(
                StreamEvent(
                    event_id=newest,
                    sequence=0,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    type="stream.reset",
                    payload={"reason": "replay_window_expired"},
                    session_id=session_id,
                )
            )
        else:
            for event in history:
                if event.event_id <= cursor:
                    continue
                if session_id is not None and event.session_id != session_id:
                    continue
                initial.append(event)

        async def unsubscribe() -> None:
            async with self._lock:
                self._subscribers.discard(subscriber)

        async def stream() -> AsyncIterator[bytes]:
            try:
                yield b": connected\n\n"
                for event in initial:
                    yield event.encode()
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    if event is None:
                        break
                    yield event.encode()
            finally:
                await unsubscribe()

        return SseSubscription(stream(), unsubscribe)

    async def subscribe(
        self,
        *,
        last_event_id: str | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Compatibility wrapper for non-HTTP consumers."""
        stream = await self.open_subscription(
            last_event_id=last_event_id,
            session_id=session_id,
        )
        try:
            async for chunk in stream:
                yield chunk
        finally:
            await stream.aclose()

    async def turn_snapshot(self, turn_id: str) -> dict[str, Any] | None:
        """Return the current public state derived from retained stream events."""
        async with self._lock:
            events = tuple(event for event in self._history if event.turn_id == turn_id)
        if not events:
            return None
        latest = events[-1]
        state = "received"
        for event in events:
            if event.type == "assistant.final":
                state = "completed"
            elif event.type == "turn.cancelled":
                state = "cancelled"
            elif event.type == "turn.failed":
                state = "failed"
            elif event.type == "turn.cancelling":
                state = "cancelling"
            elif event.type == "approval.pending":
                state = "waitingForUser"
            elif event.type in {"assistant.progress", "tool.event"}:
                state = "running"
        return {
            "turnId": turn_id,
            "requestId": latest.request_id,
            "sessionId": latest.session_id,
            "state": state,
            "updatedAt": latest.timestamp,
            "latestEventId": str(latest.event_id),
        }

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            while not subscriber.queue.empty():
                try:
                    subscriber.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            subscriber.queue.put_nowait(None)
        tasks = tuple(self._background_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
