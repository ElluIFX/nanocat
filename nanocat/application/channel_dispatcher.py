"""Runtime-owned outbound dispatch service for channel adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from loguru import logger

from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import BusClosedError, MessageBus
from nanocat.core.intervention import DeliveryResult
from nanocat.core.ports import resolve_channel_capabilities

DeliverySink = Callable[[str, DeliveryResult], None]
DeliveryGuard = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """Bounded retry policy for adapter sends."""

    max_attempts: int = 3
    retry_delay_s: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("outbound max_attempts must be positive")
        if self.retry_delay_s < 0:
            raise ValueError("outbound retry delay cannot be negative")


class OutboundDispatcher:
    """Consume outbound events and route them without owning channel SDKs."""

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        channels: Mapping[str, Any],
        policy: DeliveryPolicy | None = None,
        delivery_sink: DeliverySink | None = None,
        delivery_guard: DeliveryGuard | None = None,
    ):
        self._config = config
        self._bus = bus
        self._channels = channels
        self._delivery_sink = delivery_sink
        self._delivery_guard = delivery_guard
        self._policy = policy or DeliveryPolicy(
            max_attempts=max(1, int(getattr(config.channels, "outbound_max_attempts", 3))),
            retry_delay_s=max(0.0, float(getattr(config.channels, "outbound_retry_delay_s", 0.25))),
        )
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> asyncio.Task[Any]:
        """Start one dispatcher task and return its compatibility task handle."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="nanocat.outbound-dispatcher")
        return self._task

    async def stop(self) -> None:
        """Cancel and drain the dispatcher task exactly once."""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        """Route outbound messages until cancellation or bus closure."""
        logger.info("Outbound dispatcher started")
        while True:
            try:
                msg = await self._bus.consume_outbound()
            except asyncio.CancelledError:
                raise
            except BusClosedError:
                logger.info("Outbound dispatcher stopped: message bus closed")
                return
            except Exception as exc:
                logger.error("Outbound dispatcher failed to consume message: {}", exc)
                continue
            try:
                await self._dispatch_one(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Outbound dispatcher failed to deliver message: {}", exc)

    async def _dispatch_one(self, msg: OutboundMessage) -> None:
        """Apply delivery policy and send one already-normalized message."""
        if msg.metadata.get("_tool_event"):
            channel = self._channels.get(msg.channel)
            supports_tool_events = (
                resolve_channel_capabilities(channel).tool_events if channel else False
            )
            if channel and supports_tool_events:
                await self._send_with_retry(channel, msg)
            return

        # Empty text without media is not a deliverable message.  This can be
        # produced when a provider ends a tool-calling turn with an empty
        # assistant content; drop it centrally so channel adapters cannot turn
        # it into a visible blank message.
        if not (msg.content or "").strip() and not msg.media:
            logger.debug("Skipping empty outbound message for {}:{}", msg.channel, msg.chat_id)
            self._report_delivery(
                msg,
                DeliveryResult(delivered=False, detail="empty outbound message"),
            )
            return

        if msg.metadata.get("_progress"):
            if msg.metadata.get("_tool_hint") and not self._config.channels.send_tool_hints:
                return
            if not msg.metadata.get("_tool_hint") and not self._config.channels.send_progress:
                return

        channel = self._channels.get(msg.channel)
        if channel is None:
            logger.warning("Unknown channel: {}", msg.channel)
            self._report_delivery(msg, DeliveryResult(delivered=False, detail="unknown channel"))
            return
        if msg.request_id and self._delivery_guard and not self._delivery_guard(msg.request_id):
            self._report_delivery(
                msg,
                DeliveryResult(delivered=False, detail="delivery request is no longer active"),
            )
            return
        await self._send_with_retry(channel, msg)

    def _report_delivery(self, msg: OutboundMessage, result: DeliveryResult) -> None:
        """Report only request-scoped delivery outcomes to the application port."""
        if msg.request_id is None or self._delivery_sink is None:
            return
        self._delivery_sink(msg.request_id, result)

    async def _send_with_retry(self, channel: Any, msg: OutboundMessage) -> bool:
        """Send with bounded exponential backoff and a single terminal failure."""
        for attempt in range(1, self._policy.max_attempts + 1):
            if msg.request_id and self._delivery_guard and not self._delivery_guard(msg.request_id):
                self._report_delivery(
                    msg,
                    DeliveryResult(delivered=False, detail="delivery request is no longer active"),
                )
                return False
            if not getattr(channel, "is_running", True):
                self._report_delivery(
                    msg,
                    DeliveryResult(delivered=False, detail="channel is not running"),
                )
                return False
            try:
                result = await channel.send(msg)
                if isinstance(result, DeliveryResult) and not result.delivered:
                    raise RuntimeError(result.detail or "channel rejected delivery")
                if isinstance(result, DeliveryResult):
                    self._report_delivery(msg, result)
                    return True
                self._report_delivery(msg, DeliveryResult(delivered=True))
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt == self._policy.max_attempts:
                    logger.error(
                        "Delivery failed permanently for {} after {} attempt(s): {}",
                        msg.channel,
                        attempt,
                        exc,
                    )
                    self._report_delivery(
                        msg,
                        DeliveryResult(delivered=False, detail=str(exc)),
                    )
                    return False
                delay = self._policy.retry_delay_s * (2 ** (attempt - 1))
                logger.warning(
                    "Delivery attempt {} failed for {}; retrying in {:.2f}s: {}",
                    attempt,
                    msg.channel,
                    delay,
                    exc,
                )
                if delay:
                    await asyncio.sleep(delay)
        return False
