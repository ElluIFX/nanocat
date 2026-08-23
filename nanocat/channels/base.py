"""Base channel interface for chat platforms."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from nanocat.application.text_catalog import USER_TEXT
from nanocat.bus.events import InboundMessage, OutboundMessage
from nanocat.bus.queue import BusFullError, MessageBus
from nanocat.core.ports import ChannelCapabilities


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the NanoCat message bus.
    """

    name: str = "base"
    display_name: str = "Base"
    # Channels that render structured tool-call events set this True; otherwise
    # the dispatcher drops `_tool_event` messages for them.
    wants_tool_events: bool = False

    def __init__(self, config: Any, bus: MessageBus):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.bus = bus
        self.__running: bool = False
        self._accepting_ingress: bool = True
        self._transcription_provider: Any = None
        self._owned_tasks: set[asyncio.Task[Any]] = set()

    @property
    def capabilities(self) -> ChannelCapabilities:
        """Return SDK-neutral capabilities for dispatcher and UI projection."""
        return ChannelCapabilities(tool_events=self.wants_tool_events)

    def _track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Register a channel-owned task for deterministic shutdown."""
        self._owned_tasks.add(task)
        task.add_done_callback(self._owned_tasks.discard)
        return task

    async def _cancel_owned_tasks(self) -> None:
        """Cancel and drain tasks created by the channel adapter."""
        tasks = tuple(task for task in self._owned_tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owned_tasks.clear()

    @property
    def _running(self) -> bool:
        return self.__running

    @_running.setter
    def _running(self, value: bool) -> None:
        self.__running = value

    async def transcribe_audio(self, file_path: str | Path) -> str:
        """Transcribe an audio file via Whisper. Returns empty string when transcription is disabled or fails."""
        if self._transcription_provider is None:
            return ""
        try:
            return await self._transcription_provider.transcribe(file_path)
        except Exception as e:
            logger.warning("{}: audio transcription failed: {}", self.name, e)
            return ""

    @abstractmethod
    async def start(self) -> None:
        """
        Start the channel and begin listening for messages.

        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.

        Args:
            msg: The message to send.
        """
        pass

    def is_allowed(self, sender_id: str) -> bool:
        """Check if *sender_id* is permitted.  Empty list → deny all; ``"*"`` → allow all."""
        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            logger.warning("{}: allow_from is empty — all access denied", self.name)
            return False
        if "*" in allow_list:
            return True
        return str(sender_id) in allow_list

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> Literal["accepted", "access_denied", "busy", "shutting_down"]:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions and forwards to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            metadata: Optional channel-specific metadata.
            session_key: Optional session key override (e.g. thread-scoped sessions).
        """
        if not self._accepting_ingress:
            return "shutting_down"
        if not self.is_allowed(sender_id):
            logger.warning(
                "Access denied for sender {} on channel {}. "
                "Add them to allowFrom list in config to grant access.",
                sender_id,
                self.name,
            )
            return "access_denied"

        normalized_metadata = metadata or {}
        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=normalized_metadata,
            session_key_override=session_key,
            request_id=str(normalized_metadata.get("request_id") or "") or None,
            turn_id=str(normalized_metadata.get("turn_id") or "") or None,
            principal_id=str(sender_id),
        )

        try:
            await self.bus.publish_inbound(msg)
        except BusFullError:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=self.name,
                    chat_id=str(chat_id),
                    content=USER_TEXT.command_lane_busy,
                    metadata={"_control": True, "_command": "ingress"},
                )
            )
            return "busy"
        return "accepted"

    def begin_shutdown(self) -> None:
        """Stop admitting new inbound events while outbound delivery remains live."""
        self._accepting_ingress = False

    def resume_ingress(self) -> None:
        """Enable inbound admission during the channel startup handshake."""
        self._accepting_ingress = True

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """Return default config for onboard. Override in plugins to auto-populate config.json."""
        return {"enabled": False}

    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self.__running
