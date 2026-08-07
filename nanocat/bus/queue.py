"""Bounded, priority-aware async message bus with explicit close semantics."""

from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass
from typing import Generic, TypeVar

from nanocat.bus.events import InboundMessage, OutboundMessage

MessageT = TypeVar("MessageT")


class BusError(RuntimeError):
    """Base error raised by a closed or capacity-exhausted message bus."""


class BusClosedError(BusError):
    """Raised when publishing or consuming after the bus has closed."""


class BusFullError(BusError):
    """Raised when a control event cannot be admitted without silent loss."""


@dataclass(frozen=True, slots=True)
class _QueuedMessage(Generic[MessageT]):
    priority: int
    sequence: int
    message: MessageT

    def as_heap_item(self) -> tuple[int, int, MessageT]:
        return self.priority, self.sequence, self.message


class _PriorityQueue(Generic[MessageT]):
    """Small adapter preserving queue sizing while adding priority ordering."""

    def __init__(self, maxsize: int):
        self._queue: asyncio.PriorityQueue[tuple[int, int, MessageT]] = asyncio.PriorityQueue(
            maxsize=maxsize
        )
        self._sequence = 0

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def _item(self, priority: int, message: MessageT) -> tuple[int, int, MessageT]:
        item = _QueuedMessage(priority, self._sequence, message)
        self._sequence += 1
        return item.as_heap_item()

    async def put(self, priority: int, message: MessageT) -> None:
        await self._queue.put(self._item(priority, message))

    def put_nowait(self, priority: int, message: MessageT) -> None:
        self._queue.put_nowait(self._item(priority, message))

    async def get(self) -> MessageT:
        _, _, message = await self._queue.get()
        return message

    def get_nowait(self) -> MessageT:
        _, _, message = self._queue.get_nowait()
        return message

    def task_done(self) -> None:
        """Mark one dequeued item as processed."""
        self._queue.task_done()

    def drop_worst(self, incoming_priority: int) -> bool:
        """Drop one lower-priority item to admit a control event, if possible."""
        items = self._queue._queue  # noqa: SLF001 - bounded local heap owner
        if not items:
            return False
        worst_index = max(range(len(items)), key=lambda index: (items[index][0], items[index][1]))
        worst_priority = items[worst_index][0]
        if worst_priority <= incoming_priority:
            return False
        items[worst_index] = items[-1]
        items.pop()
        heapq.heapify(items)
        self._queue.task_done()
        return True

    def drain(self) -> int:
        count = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return count
            self._queue.task_done()
            count += 1


class MessageBus:
    """Single-process bounded bus retaining the legacy message API."""

    _CONTROL_PRIORITY = 10
    _DEFAULT_PRIORITY = 20
    _PROGRESS_PRIORITY = 100

    def __init__(self, *, maxsize: int = 256):
        if maxsize <= 0:
            raise ValueError("message bus maxsize must be positive")
        self.inbound = _PriorityQueue[InboundMessage](maxsize)
        self.outbound = _PriorityQueue[OutboundMessage](maxsize)
        self._closed = asyncio.Event()
        self._inbound_empty = asyncio.Event()
        self._outbound_empty = asyncio.Event()
        self._inbound_empty.set()
        self._outbound_empty.set()

    @property
    def closed(self) -> bool:
        """Return whether this bus has begun closing."""
        return self._closed.is_set()

    @staticmethod
    def _priority(message: InboundMessage | OutboundMessage) -> int:
        metadata = message.metadata
        content = message.content.strip().lower()
        if metadata.get("_control") or content.startswith("/"):
            return MessageBus._CONTROL_PRIORITY
        if metadata.get("_intervention"):
            return MessageBus._CONTROL_PRIORITY
        if metadata.get("_progress"):
            return MessageBus._PROGRESS_PRIORITY
        return message.priority if message.priority is not None else MessageBus._DEFAULT_PRIORITY

    async def _publish(
        self,
        queue: _PriorityQueue[MessageT],
        message: MessageT,
        empty_event: asyncio.Event,
    ) -> None:
        if self.closed:
            raise BusClosedError("message bus is closed")

        priority = self._priority(message)  # type: ignore[arg-type]
        if priority <= self._CONTROL_PRIORITY:
            try:
                queue.put_nowait(priority, message)
            except asyncio.QueueFull:
                if not queue.drop_worst(priority):
                    raise BusFullError("control event cannot be admitted") from None
                queue.put_nowait(priority, message)
            empty_event.clear()
            return

        put_task = asyncio.create_task(queue.put(priority, message))
        close_task = asyncio.create_task(self._closed.wait())
        done, _ = await asyncio.wait(
            (put_task, close_task), return_when=asyncio.FIRST_COMPLETED
        )
        if put_task in done:
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
            empty_event.clear()
            return
        put_task.cancel()
        await asyncio.gather(put_task, return_exceptions=True)
        raise BusClosedError("message bus closed while publishing")

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish an inbound message with bounded backpressure."""
        await self._publish(self.inbound, msg, self._inbound_empty)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message or receive BusClosedError on drain."""
        return await self._consume(self.inbound, self._inbound_empty)

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish an outbound message with bounded backpressure."""
        await self._publish(self.outbound, msg, self._outbound_empty)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message or receive BusClosedError on drain."""
        return await self._consume(self.outbound, self._outbound_empty)

    async def _consume(
        self,
        queue: _PriorityQueue[MessageT],
        empty_event: asyncio.Event,
    ) -> MessageT:
        if not queue.empty():
            message = queue.get_nowait()
            queue.task_done()
            if queue.empty():
                empty_event.set()
            return message
        if self.closed:
            raise BusClosedError("message bus is closed")

        get_task = asyncio.create_task(queue.get())
        close_task = asyncio.create_task(self._closed.wait())
        done, _ = await asyncio.wait(
            (get_task, close_task), return_when=asyncio.FIRST_COMPLETED
        )
        if get_task in done:
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
            message = get_task.result()
            queue.task_done()
            if queue.empty():
                empty_event.set()
            return message
        get_task.cancel()
        await asyncio.gather(get_task, return_exceptions=True)
        if not queue.empty():
            message = queue.get_nowait()
            queue.task_done()
            if queue.empty():
                empty_event.set()
            return message
        raise BusClosedError("message bus closed while consuming")

    async def receive_inbound(self) -> InboundMessage:
        """Compatibility name for the future MessagePort contract."""
        return await self.consume_inbound()

    async def receive_outbound(self) -> OutboundMessage:
        """Compatibility name for the future MessagePort contract."""
        return await self.consume_outbound()

    async def join(self) -> None:
        """Wait until both transport queues are empty."""
        await asyncio.gather(self._inbound_empty.wait(), self._outbound_empty.wait())

    async def close(self) -> None:
        """Close once and wake consumers/publishers waiting on this bus."""
        self._closed.set()

    def drain(self) -> int:
        """Drop queued messages during shutdown and return the number removed."""
        removed = self.inbound.drain() + self.outbound.drain()
        self._inbound_empty.set()
        self._outbound_empty.set()
        return removed
