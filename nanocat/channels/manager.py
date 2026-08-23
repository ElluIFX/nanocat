"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
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
        delivery_sink: Callable[[str, DeliveryResult], None] | None = None,
        delivery_guard: Callable[[str], bool] | None = None,
        descriptors: Mapping[str, Any] | None = None,
    ):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self.descriptors: dict[str, Any] = dict(descriptors or {})
        self._descriptors_provided = descriptors is not None
        self._dispatch_task: asyncio.Task | None = None
        self._channel_tasks: list[asyncio.Task[Any]] = []
        self._channel_tasks_by_name: dict[str, asyncio.Task[Any]] = {}
        self._stopping = False
        self._channel_errors: dict[str, str] = {}
        self._ready_event: asyncio.Event | None = None
        self._startup_timeout_s = 5.0
        self._run_stop: asyncio.Event | None = None
        self._reconfigure_lock = asyncio.Lock()

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

        discovered = self.descriptors if self._descriptors_provided else discover_descriptors()
        self.descriptors = discovered

        for name, descriptor in discovered.items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if name == "web" and self.config.api.enabled:
                enabled = True
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
        self._run_stop = asyncio.Event()
        for channel in self.channels.values():
            channel.resume_ingress()
        self._channel_errors.clear()
        self._ready_event = asyncio.Event()
        # Start outbound dispatcher
        self._dispatch_task = await self._dispatcher.start()

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            task = asyncio.create_task(self._start_channel(name, channel))
            tasks.append(task)
            self._channel_tasks_by_name[name] = task
        self._channel_tasks = tasks

        await self._wait_for_channel_readiness(tasks)
        self._ready_event.set()

        if self.channels and not any(channel.is_running for channel in self.channels.values()):
            raise RuntimeError("all enabled channels failed to become ready")

        # Keep the manager task alive while adapters are replaced independently.
        try:
            await self._run_stop.wait()
        finally:
            if self._channel_tasks == tasks and self._stopping:
                self._channel_tasks = []

    async def begin_shutdown(self) -> None:
        """Close every channel ingress lane while keeping delivery adapters alive."""
        for channel in self.channels.values():
            channel.begin_shutdown()

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        if self._stopping:
            return
        self._stopping = True
        if self._run_stop is not None:
            self._run_stop.set()
        logger.info("Stopping all channels...")
        await self.begin_shutdown()

        drained = await self._dispatcher.drain(timeout=5.0)
        if not drained:
            logger.warning("Outbound drain timed out; cancelling remaining deliveries")
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
        self._channel_tasks_by_name.clear()
        if self._ready_event is not None and not self._ready_event.is_set():
            self._ready_event.set()

    async def close(self) -> None:
        """Lifecycle alias used by the runtime owner registry."""
        await self.stop_all()

    def apply_live_config(self, config: Config) -> None:
        """Apply manager-owned delivery policy settings without restarting adapters."""
        self._dispatcher.apply_config(config)
        web = self.channels.get("web")
        if web is not None:
            web.config = config.channels.web

    def apply_transcription_config(self, config: Config) -> None:
        """Replace the stateless transcription adapter used by future media ingress."""
        from nanocat.providers.transcription import WhisperTranscriptionProvider

        cfg = config.transcription
        provider = (
            WhisperTranscriptionProvider(
                api_key=cfg.whisper.api_key,
                api_url=cfg.whisper.api_url,
                model=cfg.whisper.model,
            )
            if cfg.enabled
            else None
        )
        for channel in self.channels.values():
            channel._transcription_provider = provider

    async def reconfigure(
        self,
        config: Config,
        changed_paths: tuple[str, ...],
    ) -> None:
        """Rebuild affected non-Web adapters while retaining the manager task."""
        names = {
            path.split(".", 2)[1]
            for path in changed_paths
            if path.startswith("channels.") and len(path.split(".", 2)) >= 2
        }
        names.discard("web")
        names -= {
            "sendProgress",
            "sendToolHints",
            "outboundMaxAttempts",
            "outboundRetryDelayS",
        }
        if not names:
            return
        from nanocat.providers.transcription import WhisperTranscriptionProvider

        transcription_cfg = config.transcription
        transcription_provider = (
            WhisperTranscriptionProvider(
                api_key=transcription_cfg.whisper.api_key,
                api_url=transcription_cfg.whisper.api_url,
                model=transcription_cfg.whisper.model,
            )
            if transcription_cfg.enabled
            else None
        )
        async with self._reconfigure_lock:
            for name in sorted(names):
                descriptor = self.descriptors.get(name)
                section = getattr(config.channels, name, None)
                enabled = (
                    section.get("enabled", False)
                    if isinstance(section, dict)
                    else bool(getattr(section, "enabled", False))
                )
                replacement: BaseChannel | None = None
                if enabled:
                    if descriptor is None or section is None:
                        raise ValueError(f"Unknown channel `{name}`")
                    replacement = descriptor.factory(section, self.bus)
                    replacement._transcription_provider = transcription_provider
                    replacement._help_text = USER_TEXT.help
                    if getattr(replacement.config, "allow_from", None) == []:
                        raise ValueError(f'Channel "{name}" has empty allowFrom')

                previous = self.channels.get(name)
                previous_task = self._channel_tasks_by_name.pop(name, None)
                if previous is not None:
                    previous.begin_shutdown()
                    await previous.stop()
                if previous_task is not None:
                    await asyncio.gather(previous_task, return_exceptions=True)

                if replacement is None:
                    self.channels.pop(name, None)
                    continue
                self.channels[name] = replacement
                if self._run_stop is not None and not self._stopping:
                    task = asyncio.create_task(self._start_channel(name, replacement))
                    self._channel_tasks_by_name[name] = task
                    self._channel_tasks.append(task)
                    deadline = asyncio.get_running_loop().time() + self._startup_timeout_s
                    while not replacement.is_running and not task.done():
                        if asyncio.get_running_loop().time() >= deadline:
                            raise TimeoutError(f"Channel `{name}` reconnect timed out")
                        await asyncio.sleep(0.05)
                    if not replacement.is_running:
                        raise RuntimeError(f"Channel `{name}` reconnect failed")

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
