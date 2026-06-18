"""Local TUI channel — a split-screen Textual chat front-end (pseudo-channel).

Activated by ``nanocat tui``, which forces local mode: every network channel is
disabled and the agent talks only to this terminal UI. Left pane is the chat
(scrollable transcript + a working spinner + input box), right pane is the live
log. Both panes are mouse-scrollable; log lines are colored by level.

Threading model: the Textual app owns the **main** thread/loop, while the whole
nanocat runtime (agent, bus, dispatcher, cron, heartbeat) runs on a **separate**
background loop (wired by the launcher). This keeps the UI compositor from being
starved by the agent's synchronous work. The bridge is deliberately one-way per
direction:

  * inbound  — input box → ``run_coroutine_threadsafe`` onto the runtime loop;
  * outbound + logs — ``send()`` / loguru sink push onto a thread-safe queue that
    the app drains on a timer from its own loop.

The channel never mutates Textual widgets directly.
"""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Any

from loguru import logger
from pydantic import Field

from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.channels.base import BaseChannel
from nanocat.config.schema import Base

try:
    from rich.box import ROUNDED
    from rich.highlighter import ReprHighlighter
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from textual import events, on
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widgets import Button, Label, RichLog, Select, Static, TextArea

    _TEXTUAL_OK = True
    _HL = ReprHighlighter()
except ImportError:  # textual is an optional dependency (extras: tui)
    _TEXTUAL_OK = False

# Model-slot categories offered by the toolbar (maps to `/model <category> <N>`).
_MODEL_CATEGORIES = [
    ("Agent", "agent"),
    ("Subagent", "subagent"),
    ("Assistant", "assistant"),
    ("Max", "max"),
]

# Idle hint shown in the status line (TextArea has no placeholder).
_INPUT_HINT = "/help  ·  Shift+Enter newline  ·  Shift+drag select  ·  Esc stop"


_LEVEL_STYLES = {
    "TRACE": "dim",
    "DEBUG": "bright_black",
    "INFO": "bright_blue",
    "SUCCESS": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Collapse + truncate log messages so a long multi-line agent reply can't flood
# the (now narrower) log pane.
_LOG_MSG_MAX = 240

# Borderless-table log layout: fixed time / level columns, message folds in its
# own column so wrapped lines never run under the time/level columns.
_COL_TIME = 8
_COL_LEVEL = 8

# Chat message bubble styling (borders, not dividers — so markdown rules inside
# a reply can't be mistaken for message boundaries).
_USER_BORDER = "cyan"
_BOT_BORDER = "green"


def _flatten_content(content: Any) -> str:
    """Flatten a history message's content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


def _summarize_args(args: Any, limit: int = 64) -> str:
    """One-line preview of tool-call arguments for the chat pane."""
    if not isinstance(args, dict) or not args:
        return ""
    if len(args) == 1:
        value = next(iter(args.values()))
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    else:
        text = json.dumps(args, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text[: limit - 1] + "…" if len(text) > limit else text


class TuiConfig(Base):
    """Local TUI config. In local mode the sender is always the terminal user."""

    enabled: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


if _TEXTUAL_OK:

    class _ChatInput(TextArea):
        """Multi-line chat input: Enter submits, Shift/Alt+Enter inserts a newline."""

        _NEWLINE_KEYS = frozenset({"shift+enter", "alt+enter", "ctrl+enter", "ctrl+j"})

        class Submitted(Message):
            def __init__(self, value: str) -> None:
                self.value = value
                super().__init__()

        async def _on_key(self, event: events.Key) -> None:
            if event.key == "enter":
                event.stop()
                event.prevent_default()
                self.post_message(self.Submitted(self.text))
                return
            if event.key in self._NEWLINE_KEYS:
                event.stop()
                event.prevent_default()
                self.insert("\n")
                return
            await super()._on_key(event)

    class _QuitConfirm(ModalScreen[bool]):
        """Centered confirmation dialog for the toolbar Quit button."""

        CSS = """
        _QuitConfirm { align: center middle; }
        #quit-dialog {
            width: 44; height: auto; padding: 1 2;
            border: round $error; background: $surface;
        }
        #quit-dialog Label { width: 100%; text-align: center; margin-bottom: 1; }
        #quit-buttons { height: auto; align-horizontal: center; }
        #quit-buttons Button { margin: 0 1; }
        """

        def compose(self) -> ComposeResult:
            with Vertical(id="quit-dialog"):
                yield Label("Quit NanoCat?")
                with Horizontal(id="quit-buttons"):
                    yield Button("Cancel", id="cancel")
                    yield Button("Quit", id="confirm", variant="error")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(event.button.id == "confirm")

    class _ChatApp(App):
        """Two-pane terminal app: chat on the left, live log on the right."""

        # Disable Textual's own text selection so the mouse stays free for the
        # toolbar; native terminal selection still works via Shift+drag.
        ALLOW_SELECT = False

        CSS = """
        Horizontal { height: 1fr; }
        #chat-pane { width: 2fr; border: round $accent; }
        #chat { height: 1fr; padding: 0 1; overflow-x: hidden; }
        #status { height: 1; color: $text-muted; padding: 0 1; }
        #prompt { border: none; height: 4; padding: 0 1; }
        #toolbar { height: 1; padding: 0 1; }
        #toolbar-spacer { width: 1fr; }
        #toolbar Button { height: 1; border: none; width: auto; min-width: 6; margin: 0 1 0 0; }
        #toolbar Select { height: 1; margin: 0 1 0 0; }
        #toolbar #cat-select { width: 20; }
        #toolbar #model-select { width: 38; }
        #toolbar SelectCurrent { border: none; height: 1; padding: 0 1; }
        #toolbar Select:focus > SelectCurrent { border: none; }
        #send { margin: 0; }
        #log-pane { width: 1fr; border: round $secondary; }
        #log-header { height: 1; padding: 0 1; }
        #log { height: 1fr; padding: 0 1; overflow-x: hidden; }
        """

        # Ctrl+C is left to the screen's default "copy selected text" binding;
        # quit is via the toolbar button (with confirm) or Ctrl+Q.
        BINDINGS = [("escape", "abort", "Abort")]

        def __init__(self, channel: "TuiChannel") -> None:
            super().__init__()
            self._channel = channel
            self._chat: RichLog | None = None
            self._log: RichLog | None = None
            self._status: Static | None = None
            self._send: Button | None = None
            self._busy = False
            self._spin = 0
            self._suppress_next_model = False

        def compose(self) -> ComposeResult:
            with Horizontal():
                with Vertical(id="chat-pane"):
                    yield RichLog(id="chat", wrap=True, markup=False, highlight=False, min_width=0)
                    yield Static("", id="status")
                    yield _ChatInput(id="prompt", soft_wrap=True, show_line_numbers=False)
                    with Horizontal(id="toolbar"):
                        yield Button("Quit", id="quit", variant="error")
                        yield Select(
                            _MODEL_CATEGORIES, id="cat-select", value="agent", allow_blank=False
                        )
                        yield Select([], id="model-select", prompt="Model", allow_blank=True)
                        yield Static(id="toolbar-spacer")
                        yield Button("Send", id="send", variant="success")
                with Vertical(id="log-pane"):
                    yield Static(id="log-header")
                    yield RichLog(id="log", wrap=True, markup=False, highlight=False, min_width=0)

        def on_mount(self) -> None:
            self._chat = self.query_one("#chat", RichLog)
            self._log = self.query_one("#log", RichLog)
            self._status = self.query_one("#status", Static)
            self._send = self.query_one("#send", Button)
            self.query_one("#log-header", Static).update(self._log_header())
            self._populate_models()
            self._status.update(_INPUT_HINT)
            self.query_one("#prompt", _ChatInput).focus()
            self.set_interval(0.05, self._drain)
            self.set_interval(0.1, self._tick_spinner)

        def action_abort(self) -> None:
            """Esc during a turn cancels it; otherwise no-op."""
            if self._busy:
                self._channel.submit_threadsafe("/stop")
                self._set_busy(False)

        def _populate_models(self) -> None:
            try:
                from nanocat.config.loader import get_runtime_config

                defaults = get_runtime_config().agents.defaults
                models = list(defaults.model_choice or [])
                current = defaults.model
            except Exception:
                models, current = [], None
            sel = self.query_one("#model-select", Select)
            sel.set_options((m, str(i)) for i, m in enumerate(models, 1))
            # Default the dropdown to the agent's active model (shown when the
            # user hasn't picked anything) without triggering a switch.
            self._show_model(current, models)

        def _show_model(self, model: str | None, models: list[str]) -> None:
            """Display *model* in the dropdown without firing a switch."""
            sel = self.query_one("#model-select", Select)
            if model in models:
                newval = str(models.index(model) + 1)
                if sel.value != newval:
                    self._suppress_next_model = True  # consumed by the resulting Changed
                    sel.value = newval
            elif sel.value is not Select.BLANK:
                # BLANK is ignored by the handler, so no suppression needed (and
                # setting it must NOT leave a stale flag that swallows the next pick).
                sel.value = Select.BLANK

        def _sync_model_to_category(self, category: str) -> None:
            """Refresh the model dropdown to show *category*'s current model."""
            try:
                from nanocat.config.loader import get_runtime_config

                d = get_runtime_config().agents.defaults
                models = list(d.model_choice or [])
                target = {
                    "agent": d.model,
                    "subagent": d.subagent_model,
                    "assistant": d.assistant_model,
                    "max": d.max_model,
                }.get(category)
            except Exception:
                return
            self._show_model(target, models)

        # --- toolbar actions -------------------------------------------------

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "quit":
                self.push_screen(_QuitConfirm(), self._on_quit_decision)
            elif event.button.id == "send":
                if self._busy:
                    self._channel.submit_threadsafe("/stop")
                    self._set_busy(False)
                else:
                    self._submit_current()

        def _on_quit_decision(self, confirmed: bool | None) -> None:
            if confirmed:
                self.exit()

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id == "cat-select":
                # Switching category refreshes the model dropdown to that slot.
                if event.value is not Select.BLANK:
                    self._sync_model_to_category(str(event.value))
                return
            if event.select.id != "model-select":
                return
            if event.value is Select.BLANK:
                return
            if self._suppress_next_model:  # programmatic display, not a user pick
                self._suppress_next_model = False
                return
            category = self.query_one("#cat-select", Select).value
            if category is Select.BLANK:
                category = "agent"
            # Keep the dropdown showing the picked model (don't reset to blank).
            self._channel.submit_threadsafe(f"/model {category} {event.value}")

        @on(_ChatInput.Submitted)
        def _on_prompt_submit(self, event: "_ChatInput.Submitted") -> None:
            self._submit_current()

        def _submit_current(self) -> None:
            inp = self.query_one("#prompt", _ChatInput)
            text = inp.text.strip()
            inp.text = ""
            if not text:
                return
            self._write_user(text)
            self._set_busy(True)
            self._channel.submit_threadsafe(text)

        def _set_busy(self, value: bool) -> None:
            self._busy = value
            if self._send is not None:
                self._send.label = "Stop" if value else "Send"
                self._send.variant = "error" if value else "success"
            if not value and self._status is not None:
                self._status.update(_INPUT_HINT)

        def _write_user(self, text: str) -> None:
            assert self._chat is not None
            self._chat.write(
                Panel(
                    Text(text),
                    title="You",
                    title_align="left",
                    border_style=_USER_BORDER,
                    box=ROUNDED,
                    padding=(0, 1),
                )
            )
            self._chat.write("")

        def _write_bot(self, text: str) -> None:
            assert self._chat is not None
            self._chat.write(
                Panel(
                    Markdown(text),
                    title="NanoCat",
                    title_align="left",
                    border_style=_BOT_BORDER,
                    box=ROUNDED,
                    padding=(0, 1),
                )
            )
            self._chat.write("")

        def _write_tool_event(self, payload: dict) -> None:
            """Render a tool-call batch: dim 'running' lines on start, a green/red
            status line per tool on completion."""
            assert self._chat is not None
            phase = payload.get("phase")
            for call in payload.get("calls", []):
                name = str(call.get("name", "?"))
                line = Text()
                if phase == "start":
                    line.append("  ⟳ ", style="yellow")
                    line.append(name, style="bold yellow")
                    arg = _summarize_args(call.get("args"))
                    if arg:
                        line.append(f"  {arg}", style="dim")
                else:
                    ok = call.get("status") == "ok"
                    line.append("  ✓ " if ok else "  ✗ ", style="green" if ok else "red")
                    line.append(name, style="green" if ok else "red")
                    preview = " ".join(str(call.get("preview") or "").split())
                    if preview:
                        line.append(f"  {preview[:100]}", style="dim")
                self._chat.write(line)

        def _tick_spinner(self) -> None:
            # Idle status (the hint) is owned by _set_busy / on_mount; here we
            # only animate the spinner while a turn is in flight.
            if self._status is None or not self._busy:
                return
            self._spin = (self._spin + 1) % len(_SPINNER)
            self._status.update(f"{_SPINNER[self._spin]} NanoCat is working…")

        def _drain(self) -> None:
            assert self._chat is not None and self._log is not None
            q = self._channel._display_q
            while True:
                try:
                    kind, payload = q.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    self._log.write(self._format_log(*payload))
                elif kind == "chat_user":
                    self._write_user(payload)
                elif kind == "chat_bot":
                    self._set_busy(False)
                    self._write_bot(payload)
                elif kind == "chat_progress":
                    self._chat.write(Text(payload, style="dim italic"))
                elif kind == "tool_event":
                    self._write_tool_event(payload)

        @staticmethod
        def _log_table() -> "Table":
            """Borderless table with fixed time / level columns + folding message."""
            t = Table(box=None, show_header=False, expand=True, padding=(0, 1, 0, 0))
            t.add_column(width=_COL_TIME, no_wrap=True)
            t.add_column(width=_COL_LEVEL, no_wrap=True)
            t.add_column(ratio=1, overflow="fold")
            return t

        @classmethod
        def _log_header(cls) -> "Table":
            t = cls._log_table()
            t.add_row(
                Text("TIME", style="bold grey50"),
                Text("LEVEL", style="bold grey50"),
                Text("MESSAGE", style="bold grey50"),
            )
            return t

        @classmethod
        def _format_log(cls, ts: str, level: str, message: str) -> "Table":
            """One log row: time | level | message, message folding in its column.

            The message is collapsed to a single logical line and truncated; the
            table keeps wrapped continuation lines under the message column
            instead of flowing beneath time/level.
            """
            clean = " ".join(message.split())
            if len(clean) > _LOG_MSG_MAX:
                clean = clean[: _LOG_MSG_MAX - 1] + "…"
            msg = Text(clean)
            _HL.highlight(msg)
            t = cls._log_table()
            t.add_row(
                Text(ts, style="grey50"),
                Text(level, style=_LEVEL_STYLES.get(level, "white")),
                msg,
            )
            return t


class TuiChannel(BaseChannel):
    """Local split-screen terminal UI exposed as a pseudo-channel."""

    name = "tui"
    display_name = "Local TUI"
    wants_tool_events = True

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return TuiConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = TuiConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: TuiConfig = config
        self._display_q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._app: Any = None
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._log_sink_id: int | None = None
        self._install_log_sink()

    def _install_log_sink(self) -> None:
        """Route loguru into the log pane and drop the default stderr sink.

        Only the default stderr handler (id 0) is removed, so any file sink the
        runtime installed survives; the pane sink is added on top. Done at
        construction so boot-time logs are buffered and replayed once the app
        starts draining. The level follows ``NANOCAT_LOG_LEVEL`` (INFO, or DEBUG
        under --verbose).
        """
        import os

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

    def _log_sink(self, message: Any) -> None:
        r = message.record
        self._display_q.put(
            ("log", (r["time"].strftime("%H:%M:%S"), r["level"].name, r["message"]))
        )

    def preload_history(self, history: list[dict[str, Any]]) -> None:
        """Queue prior session turns so they render as bubbles before live ones.

        Only user messages and assistant text replies are shown; tool calls and
        empty assistant turns are skipped.
        """
        for msg in history:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _flatten_content(msg.get("content")).strip()
            if not text:
                continue
            self._display_q.put(("chat_user" if role == "user" else "chat_bot", text))

    def bind_runtime_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the background runtime loop used to publish inbound messages."""
        self._runtime_loop = loop

    def submit_threadsafe(self, text: str) -> None:
        """Forward terminal input to the agent from the UI thread (non-blocking)."""
        loop = self._runtime_loop
        if loop is None:
            logger.warning("TUI received input before the runtime loop was bound")
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_message(sender_id="local", chat_id="local", content=text),
            loop,
        )

    def run_ui(self) -> None:
        """Run the Textual app on the current (main) thread; blocks until quit."""
        if not _TEXTUAL_OK:
            raise RuntimeError(
                "The 'tui' channel requires Textual. Install it with: pip install textual"
            )
        self._app = _ChatApp(self)
        self._app.run()

    async def start(self) -> None:
        """Participate in the bus as a send target; the UI runs on the main thread.

        Stays alive on the runtime loop until ``stop()`` is called so the
        dispatcher can route outbound messages here.
        """
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

    async def send(self, msg: OutboundMessage) -> None:
        meta = msg.metadata or {}
        if meta.get("_tool_event"):
            self._display_q.put(("tool_event", meta["_tool_event"]))
            return
        if meta.get("_tool_hint"):
            return  # superseded by the structured tool-event rendering
        if meta.get("_progress"):
            if msg.content:
                self._display_q.put(("chat_progress", msg.content))
            return
        if msg.content:
            self._display_q.put(("chat_bot", msg.content))
        for media_path in msg.media or []:
            self._display_q.put(("chat_progress", f"[media: {media_path}]"))
