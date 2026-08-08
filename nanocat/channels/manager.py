"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loguru import logger

from nanocat.application.channel_dispatcher import OutboundDispatcher
from nanocat.application.text_catalog import USER_TEXT
from nanocat.bus.queue import MessageBus
from nanocat.channels.base import BaseChannel
from nanocat.config.schema import Config
from nanocat.core.intervention import DeliveryResult
from nanocat.core.ports import resolve_channel_capabilities


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, Discord, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        force_channel: str | None = None,
        delivery_sink: Callable[[str, DeliveryResult], None] | None = None,
        delivery_guard: Callable[[str], bool] | None = None,
    ):
        self.config = config
        self.bus = bus
        self.force_channel = force_channel
        self.channels: dict[str, BaseChannel] = {}
        self.descriptors: dict[str, Any] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._channel_tasks: list[asyncio.Task[Any]] = []
        self._stopping = False
        self._channel_errors: dict[str, str] = {}
        self._ready_event: asyncio.Event | None = None
        self._startup_timeout_s = 5.0

        self._init_channels()
        self._dispatcher = OutboundDispatcher(
            self.config,
            self.bus,
            self.channels,
            delivery_sink=delivery_sink,
            delivery_guard=delivery_guard,
        )

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        from nanocat.channels.registry import discover_descriptors
        from nanocat.providers.transcription import WhisperTranscriptionProvider

        transcription_cfg = self.config.transcription
        transcription_provider = (
            WhisperTranscriptionProvider(
                api_key=transcription_cfg.whisper.api_key,
                api_url=transcription_cfg.whisper.api_url,
                model=transcription_cfg.whisper.model,
            )
            if transcription_cfg.enabled
            else None
        )

        discovered = discover_descriptors()
        self.descriptors = discovered

        # Local mode: force exactly one channel (e.g. the TUI), ignore config so
        # every network channel stays disabled. Synthesized from default_config.
        if self.force_channel:
            descriptor = discovered.get(self.force_channel)
            if descriptor is None:
                raise SystemExit(f"Error: unknown channel '{self.force_channel}'")
            try:
                channel = descriptor.factory(descriptor.config_schema, self.bus)
                channel._transcription_provider = transcription_provider
                channel._help_text = USER_TEXT.help
                self.channels[self.force_channel] = channel
                logger.info(
                    "{} channel enabled (local mode)",
                    getattr(descriptor.factory, "display_name", self.force_channel),
                )
            except Exception as e:
                raise SystemExit(f"Error: failed to start '{self.force_channel}': {e}")
            return

        for name, descriptor in discovered.items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if not enabled:
                continue
            try:
                channel = descriptor.factory(section, self.bus)
                channel._transcription_provider = transcription_provider
                channel._help_text = USER_TEXT.help
                self.channels[name] = channel
                logger.info(
                    "{} channel enabled",
                    getattr(descriptor.factory, "display_name", name),
                )
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
            if not self._stopping and not channel.is_running:
                self._channel_errors[name] = "channel stopped before becoming ready"
                logger.warning("{} channel stopped unexpectedly", name)
        except Exception as e:
            self._channel_errors[name] = str(e)
            logger.error("Failed to start channel {}: {}", name, e)

    async def _wait_for_channel_readiness(self, tasks: list[asyncio.Task[Any]]) -> None:
        """Wait for enabled channels to enter their adapter-defined running state."""
        deadline = asyncio.get_running_loop().time() + self._startup_timeout_s
        while not self._stopping:
            if all(channel.is_running for channel in self.channels.values()):
                return
            if any(task.done() and not channel.is_running for task, channel in zip(tasks, self.channels.values())):
                return
            if asyncio.get_running_loop().time() >= deadline:
                for name, channel in self.channels.items():
                    if not channel.is_running:
                        self._channel_errors.setdefault(
                            name, "channel did not become ready before startup timeout"
                        )
                return
            await asyncio.sleep(0.05)

    async def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait for the startup handshake and return whether all channels are running."""
        event = self._ready_event
        if event is None:
            await asyncio.sleep(0)
            event = self._ready_event
        if event is None:
            return False
        try:
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(asyncio.shield(event.wait()), timeout)
        except asyncio.TimeoutError:
            return False
        return bool(self.channels) and all(channel.is_running for channel in self.channels.values())

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if self._channel_tasks or (
            self._dispatch_task is not None and not self._dispatch_task.done()
        ):
            return
        if not self.channels:
            logger.warning("No channels enabled")
            self._ready_event = asyncio.Event()
            self._ready_event.set()
            return

        self._stopping = False
        self._channel_errors.clear()
        self._ready_event = asyncio.Event()
        # Start outbound dispatcher
        self._dispatch_task = await self._dispatcher.start()

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))
        self._channel_tasks = tasks

        await self._wait_for_channel_readiness(tasks)
        self._ready_event.set()

        if self.channels and not any(channel.is_running for channel in self.channels.values()):
            raise RuntimeError("all enabled channels failed to become ready")

        # Wait for all to complete (they should run forever)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if self._channel_tasks == tasks:
                self._channel_tasks = []

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        if self._stopping:
            return
        self._stopping = True
        logger.info("Stopping all channels...")

        # Stop dispatcher
        await self._dispatcher.stop()
        self._dispatch_task = None

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

        tasks = tuple(self._channel_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._channel_tasks = []
        if self._ready_event is not None and not self._ready_event.is_set():
            self._ready_event.set()

    async def close(self) -> None:
        """Lifecycle alias used by the runtime owner registry."""
        await self.stop_all()

    async def _dispatch_outbound(self) -> None:
        """Compatibility facade for the application-owned dispatcher."""
        await self._dispatcher.run()

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        status = {}
        for name, channel in self.channels.items():
            capabilities = resolve_channel_capabilities(channel)
            descriptor = self.descriptors.get(name)
            status[name] = {
                "enabled": True,
                "running": channel.is_running,
                "source": descriptor.source if descriptor else "legacy",
                "version": descriptor.version if descriptor else "0",
                "capabilities": {
                    "progress": capabilities.progress,
                    "tool_events": capabilities.tool_events,
                    "media": capabilities.media,
                    "reply_threads": capabilities.reply_threads,
                    "interactive_reply": capabilities.interactive_reply,
                },
                **({"error": self._channel_errors[name]} if name in self._channel_errors else {}),
            }
        return status

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
