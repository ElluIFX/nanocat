"""Local TUI channel — thin adapter between the runtime and the Textual app.

Activated by ``nanocat tui``, which forces local mode: every network channel
is disabled and the agent talks only to this terminal UI. The Textual
application itself lives in :mod:`nanocat.channels.tui_app`; this module only
owns the transport concerns:

  * inbound  — composer text → ``run_coroutine_threadsafe`` onto the runtime
    loop (normal chat messages only; control actions use the control port);
  * outbound + logs — ``send()`` / loguru sink push onto a bounded thread-safe
    queue that the app drains on a timer from its own loop;
  * control — ``submit_control()`` forwards structured
    :class:`ApplicationControlService` actions to the runtime loop and returns
    a Future the app awaits off-thread.

The channel never mutates Textual widgets directly.
"""

from __future__ import annotations

import asyncio
import os
import queue
from concurrent.futures import Future
from typing import Any

from loguru import logger
from pydantic import Field

from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.channels.base import BaseChannel
from nanocat.config.schema import Base
from nanocat.core.ports import ChannelCapabilities

_DISPLAY_QUEUE_MAX = 4096


class TuiConfig(Base):
    """Local TUI config. In local mode the sender is always the terminal user."""

    enabled: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class TuiChannel(BaseChannel):
    """Local terminal UI exposed as a pseudo-channel."""

    name = "tui"
    display_name = "Local TUI"
    capabilities = ChannelCapabilities(
        progress=True,
        tool_events=True,
        media=False,
        interactive_reply=True,
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return TuiConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = TuiConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: TuiConfig = config
        self._display_q: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=_DISPLAY_QUEUE_MAX)
        self._app: Any = None
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._log_sink_id: int | None = None
        self._control: Any = None
        self.dropped_logs = 0
        self._install_log_sink()

    # ------------------------------------------------------------------
    # logging
    # ------------------------------------------------------------------

    def _install_log_sink(self) -> None:
        """Route loguru into the log drawer while keeping any file sink.

        Done at construction so boot-time logs are buffered and replayed once
        the app starts draining. The level follows ``NANOCAT_LOG_LEVEL``.
        """
        try:
            logger.remove(0)
        except ValueError:
            pass  # already removed (e.g. second construction in the same process)
        self._log_sink_id = logger.add(
            self._log_sink,
            level=os.environ.get("NANOCAT_LOG_LEVEL", "INFO"),
            enqueue=False,
            backtrace=False,
            diagnose=False,
        )

    def _enqueue(self, item: tuple[str, Any]) -> None:
        """Bounded display-queue put; only log records may be dropped."""
        try:
            self._display_q.put_nowait(item)
            return
        except queue.Full:
            if item[0] == "log":
                self.dropped_logs += 1
                return
        # Control/chat events must not be lost: evict buffered log records to
        # make room while preserving every non-log event and its order.
        held: list[tuple[str, Any]] = []
        evicted = False
        for _ in range(64):
            try:
                held.append(self._display_q.get_nowait())
            except queue.Empty:
                break
            if held[-1][0] == "log":
                self.dropped_logs += 1
                held.pop()
                evicted = True
                break
        for kept in held:
            try:
                self._display_q.put_nowait(kept)
            except queue.Full:
                break
        try:
            self._display_q.put_nowait(item)
        except queue.Full:
            if evicted:
                logger.warning("TUI display queue full; dropped {} event", item[0])
            else:
                logger.warning("TUI display queue full of control events; dropped {}", item[0])

    def _log_sink(self, message: Any) -> None:
        r = message.record
        self._enqueue(("log", (r["time"].strftime("%H:%M:%S"), r["level"].name, r["message"])))

    # ------------------------------------------------------------------
    # runtime bridges
    # ------------------------------------------------------------------

    def preload_history(self, history: list[dict[str, Any]]) -> None:
        """Queue prior session turns so they render before live ones."""
        from nanocat.channels.tui_app.events import flatten_content, parse_subagent_result

        for msg in history:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = flatten_content(msg.get("content")).strip()
            if not text:
                continue
            if role == "user":
                if sub := parse_subagent_result(text):
                    self._enqueue(("chat_subagent", sub))
                else:
                    self._enqueue(("chat_user", text))
            elif msg.get("tool_calls"):
                self._enqueue(("chat_progress", text))
            else:
                self._enqueue(("chat_bot", text))

    def bind_runtime_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the background runtime loop used to publish inbound messages."""
        self._runtime_loop = loop

    def bind_control(self, control: Any) -> None:
        """Attach the structured application control service."""
        self._control = control

    def submit_control(self, action: str, params: dict[str, Any]) -> Future | None:
        """Run one control action on the runtime loop; returns its Future."""
        loop = self._runtime_loop
        control = self._control
        if loop is None or control is None:
            logger.warning("TUI control action {} before control service was bound", action)
            return None
        return asyncio.run_coroutine_threadsafe(control.execute(action, params), loop)

    def submit_threadsafe(self, text: str, media: list[str] | None = None) -> None:
        """Forward terminal input to the agent from the UI thread (non-blocking)."""
        loop = self._runtime_loop
        if loop is None:
            logger.warning("TUI received input before the runtime loop was bound")
            return
        future = asyncio.run_coroutine_threadsafe(
            self._handle_message(sender_id="local", chat_id="local", content=text, media=media),
            loop,
        )

        def _log_failure(done: Future) -> None:
            if done.cancelled():
                logger.warning("TUI inbound publish was cancelled")
                return
            error = done.exception()
            if error is not None:
                logger.warning("TUI inbound publish failed: {}", error)

        future.add_done_callback(_log_failure)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def run_ui(self) -> None:
        """Run the Textual app on the current (main) thread; blocks until quit."""
        try:
            from nanocat.channels.tui_app.app import NanoCatApp
        except ImportError as e:
            raise RuntimeError(
                "The 'tui' channel requires Textual. Install it with: pip install textual"
            ) from e
        self._app = NanoCatApp(self)
        self._app.run()

    async def start(self) -> None:
        """Participate in the bus as a send target; the UI runs on the main thread."""
        self._running = True
        self._stop_event = asyncio.Event()
        try:
            await self._stop_event.wait()
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._app is not None:
            try:
                self._app.exit()
            except Exception as e:
                logger.debug("TUI app exit failed: {}", e)
        if self._log_sink_id is not None:
            logger.remove(self._log_sink_id)
            self._log_sink_id = None
        self._control = None
        await self._cancel_owned_tasks()

    # ------------------------------------------------------------------
    # outbound projection
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        from nanocat.channels.tui_app.events import intervention_payload

        meta = msg.metadata or {}
        if meta.get("_intervention"):
            payload = intervention_payload(msg)
            if payload is not None:
                if payload.get("mode"):
                    self._enqueue(("approval_mode", payload))
                if payload.get("request_id"):
                    self._enqueue(
                        (
                            "approval_update" if meta.get("_intervention_update") else "approval",
                            payload,
                        )
                    )
                return
        if meta.get("_tool_event"):
            self._enqueue(("tool_event", meta["_tool_event"]))
            return
        if meta.get("_tool_hint"):
            return  # superseded by the structured tool-event rendering
        if meta.get("_progress"):
            if msg.content:
                self._enqueue(("chat_progress", msg.content))
            return
        if msg.content:
            self._enqueue(("chat_bot", msg.content))
        for media_path in msg.media or []:
            self._enqueue(("chat_progress", f"[media: {media_path}]"))
