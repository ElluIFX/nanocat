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
import re
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.channels.base import BaseChannel
from nanocat.config.schema import Base
from nanocat.core.ports import ChannelCapabilities

try:
    from rich.box import ROUNDED
    from rich.highlighter import ReprHighlighter
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.style import Style
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
]
_EFFORT_OPTIONS = [
    ("Auto", "auto"),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
    ("XHigh", "xhigh"),
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
_SUBAGENT_BORDER = "yellow"
_APPROVAL_BORDER = "bright_yellow"

_INTERVENTION_FIELD_RE = re.compile(r"^(Capability|Operation|Expires):\s*(.*)$", re.MULTILINE)
_SENSITIVE_DISPLAY_RE = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|token|api[-_ ]?key)\s*[:=]\s*[^\s,;]+"
)


def _parse_subagent_result(text: str) -> dict[str, Any] | None:
    """Return the parsed announce dict if *text* is a subagent result JSON, else None."""
    s = text.lstrip()
    if not s.startswith("{") or "subagent_id" not in s:
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) and "subagent_id" in obj and "result" in obj else None


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


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _grab_clipboard_images() -> list[str]:
    """Return file paths for any image currently on the OS clipboard.

    A raw bitmap (e.g. from a screenshot tool) is written as PNG into the TUI
    media dir; copied image *files* (from a file manager) are used in place.
    Returns [] when the clipboard holds no image (plain text, empty, …).

    ponytail: ImageGrab covers Windows/macOS out of the box; on Linux it needs
    xclip/wl-paste installed — acceptable, the tool just no-ops without them.
    """
    try:
        from PIL import Image, ImageGrab

        from nanocat.config.paths import get_media_dir
    except Exception:
        return []
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return []
    if isinstance(data, Image.Image):
        out = get_media_dir("tui") / f"paste_{uuid.uuid4().hex[:8]}.png"
        try:
            data.save(out, "PNG")
        except Exception:
            return []
        return [str(out)]
    if isinstance(data, list):
        return [
            str(p)
            for f in data
            if (p := Path(str(f))).is_file() and p.suffix.lower() in _IMAGE_EXTS
        ]
    return []


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


def _redact_intervention_text(value: Any, limit: int = 240) -> str:
    """Keep scheduler-provided approval facts safe for terminal rendering."""
    text = str(value or "").strip()
    text = _SENSITIVE_DISPLAY_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text[: limit - 1] + "…" if len(text) > limit else text


def _intervention_payload(msg: OutboundMessage) -> dict[str, Any] | None:
    """Normalize scheduler-owned intervention metadata for the TUI bridge."""
    metadata = msg.metadata or {}
    request_id = str(metadata.get("request_id") or "").strip()
    mode = str(metadata.get("_intervention_mode") or "").strip().lower()
    if mode not in {"auto", "yolo"}:
        mode = ""
    if not request_id and not mode:
        return None
    if metadata.get("_intervention_update"):
        return {
            "request_id": request_id,
            "state": str(metadata.get("intervention_state") or "updated"),
            "detail": _redact_intervention_text(msg.content),
            "mode": mode,
        }

    fields = dict(_INTERVENTION_FIELD_RE.findall(msg.content or ""))
    raw_expiry = str(metadata.get("expires_at") or "").strip()
    expiry_text = raw_expiry or str(fields.get("Expires") or "").strip()
    expires_at: datetime | None = None
    if expiry_text:
        try:
            expires_at = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = expires_at.astimezone(timezone.utc)
        except ValueError:
            try:
                expires_at = datetime.strptime(expiry_text, "%Y-%m-%d %H:%M:%S UTC").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                expires_at = None
    return {
        "request_id": request_id,
        "capability": _redact_intervention_text(
            metadata.get("capability") or fields.get("Capability")
        ),
        "operation": _redact_intervention_text(
            metadata.get("operation") or fields.get("Operation")
        ),
        "expires": _redact_intervention_text(fields.get("Expires") or raw_expiry),
        "expires_at": expires_at,
        "state": "pending",
        "mode": mode,
    }


class TuiConfig(Base):
    """Local TUI config. In local mode the sender is always the terminal user."""

    enabled: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


if _TEXTUAL_OK:

    class _ChatInput(TextArea):
        """Multi-line chat input: Enter submits, Shift/Alt+Enter inserts a newline.

        Up/Down navigate input history when the text is single-line (no newlines).
        """

        _NEWLINE_KEYS = frozenset({"shift+enter", "alt+enter", "ctrl+enter", "ctrl+j"})

        class Submitted(Message):
            def __init__(self, value: str) -> None:
                self.value = value
                super().__init__()

        # Placeholder token regexes (capture group 1 = 1-based index into the buffer).
        _IMAGE_RE = r"\[Image (\d+)\]"
        _PASTE_RE = r"\[Pasted text #(\d+) \+\d+ lines\]"

        def on_mount(self) -> None:
            self._history: list[str] = []
            self._hist_idx: int = -1  # -1 = live input, 0..n-1 = navigating
            self._draft: str = ""  # saved text before entering history nav
            self._pending_media: list[str] = []  # clipboard images staged for the next send
            self._pending_pastes: list[str] = []  # large pasted text blocks staged for the send
            self._reconciling = False  # guard against the text-rewrite re-entering reconcile

        def _attach_clipboard_image(self) -> bool:
            """If the OS clipboard holds an image, stage it and insert an
            ``[Image N]`` placeholder. Returns True when an image was attached
            (caller should swallow the paste); False lets normal text paste run."""
            paths = _grab_clipboard_images()
            if not paths:
                return False
            for path in paths:
                self._pending_media.append(path)
                self.insert(f"[Image {len(self._pending_media)}] ")
            return True

        def take_pending_media(self) -> list[str]:
            """Return staged media whose ``[Image N]`` placeholder is still in the
            text (so deleting a placeholder drops its image), then reset."""
            text = self.text
            media = [p for i, p in enumerate(self._pending_media, 1) if f"[Image {i}]" in text]
            self._pending_media = []
            return media

        def take_pending_pastes(self, text: str) -> str:
            """Expand surviving ``[Pasted text #N ...]`` placeholders in *text* back
            to the full stored block (so the agent gets the real content), then
            reset. Deleted placeholders simply drop their block."""

            def _expand(m: "re.Match[str]") -> str:
                i = int(m.group(1))
                return self._pending_pastes[i - 1] if 1 <= i <= len(self._pending_pastes) else m[0]

            expanded = re.sub(self._PASTE_RE, _expand, text)
            self._pending_pastes = []
            return expanded

        def _reconcile_kind(
            self, buffer: list, pattern: str, renumber: "Callable[[int, re.Match[str]], str]"
        ) -> list:
            """Drop buffer entries whose placeholder was deleted from the text and
            renumber the survivors so the visible ids stay contiguous (delete 2 of
            1,2,3 → remaining become 1,2). The buffer is the source of truth; only
            its still-referenced entries survive, in order. Rewrites the text only
            when a placeholder actually vanished. Returns the trimmed buffer."""
            if self._reconciling or not buffer:
                return buffer
            text = self.text
            n = len(buffer)
            present = {
                int(m.group(1)) for m in re.finditer(pattern, text) if 1 <= int(m.group(1)) <= n
            }
            if len(present) == n:
                return buffer  # every staged entry still referenced — nothing deleted
            survivors = [i for i in range(1, n + 1) if i in present]
            remap = {old: new for new, old in enumerate(survivors, 1)}
            new_buffer = [buffer[old - 1] for old in survivors]
            new_text = re.sub(
                pattern,
                lambda m: renumber(remap[int(m.group(1))], m) if int(m.group(1)) in remap else m[0],
                text,
            )
            if new_text != text:
                cursor = self.cursor_location  # renumbered tokens sit after it → col stays valid
                self._reconciling = True
                try:
                    self.text = new_text
                    self.move_cursor(cursor)
                finally:
                    self._reconciling = False
            return new_buffer

        def _reconcile_placeholders(self) -> None:
            self._pending_media = self._reconcile_kind(
                self._pending_media, self._IMAGE_RE, lambda new, m: f"[Image {new}]"
            )
            self._pending_pastes = self._reconcile_kind(
                self._pending_pastes,
                self._PASTE_RE,
                # Replace only the #N index, preserving the "+M lines" suffix.
                lambda new, m: re.sub(r"#\d+", f"#{new}", m[0]),
            )

        def _active_token_spans(self, line: str) -> list[tuple[int, int]]:
            """Char spans of *real* placeholder tokens (images + pasted blocks) on a
            line — only those whose index is currently staged, so hand-typed lookalikes
            stay ordinary text. The buffers are the single source of truth."""
            spans: list[tuple[int, int]] = []
            for pat, buf in (
                (self._IMAGE_RE, self._pending_media),
                (self._PASTE_RE, self._pending_pastes),
            ):
                n = len(buf)
                spans += [
                    (m.start(), m.end())
                    for m in re.finditer(pat, line)
                    if 1 <= int(m.group(1)) <= n
                ]
            return spans

        def _build_highlight_map(self) -> None:
            # Runs after every edit; tint real placeholder tokens so they read as
            # attachments, not prose (images blue, pasted blocks cyan). render_line
            # wants BYTE offsets, so re-derive spans from the UTF-8 line (the
            # placeholders are ASCII, landing on clean codepoint boundaries even
            # when CJK text precedes them).
            super()._build_highlight_map()
            theme = getattr(self, "_theme", None)  # unset during __init__'s first call
            if theme is None:
                return
            theme.syntax_styles.setdefault("nanocat_image", Style(color="bright_blue", bold=True))
            theme.syntax_styles.setdefault("nanocat_paste", Style(color="bright_cyan", bold=True))
            kinds = (
                (rb"\[Image (\d+)\]", len(self._pending_media), "nanocat_image"),
                (
                    rb"\[Pasted text #(\d+) \+\d+ lines\]",
                    len(self._pending_pastes),
                    "nanocat_paste",
                ),
            )
            for row, line in enumerate(self.document.lines):
                lb = line.encode("utf-8")
                for pat, n, style in kinds:
                    for m in re.finditer(pat, lb):
                        if 1 <= int(m.group(1)) <= n:
                            self._highlights[row].append((m.start(), m.end(), style))

        # Treat real placeholder tokens as atomic: left/right cursor movement and
        # backspace/delete (which both derive their target from these two hooks)
        # jump over / remove the whole placeholder instead of one char at a time.
        # Cursor columns are character indices, so str-regex spans align directly.
        def get_cursor_left_location(self) -> "tuple[int, int]":
            row, col = self.cursor_location
            for start, end in self._active_token_spans(self.document.lines[row]):
                if start < col <= end:
                    return (row, start)
            return super().get_cursor_left_location()

        def get_cursor_right_location(self) -> "tuple[int, int]":
            row, col = self.cursor_location
            for start, end in self._active_token_spans(self.document.lines[row]):
                if start <= col < end:
                    return (row, end)
            return super().get_cursor_right_location()

        async def _on_paste(self, event: events.Paste) -> None:
            # Terminal bracketed paste: an image on the clipboard yields no text,
            # so peek the OS clipboard first and attach it instead of inserting.
            if self._attach_clipboard_image():
                event.stop()
                event.prevent_default()
                return
            # A multi-line block ("有分行") is collapsed to a [Pasted text #N +M lines]
            # token; the full text is staged and re-expanded on submit. Single-line
            # pastes insert normally. Bracketed paste uses CR for line breaks, so
            # normalize before deciding and staging.
            text = event.text.replace("\r\n", "\n").replace("\r", "\n")
            if "\n" in text.strip():
                event.stop()
                event.prevent_default()
                self._pending_pastes.append(text)
                lines = text.strip().count("\n") + 1
                self.insert(f"[Pasted text #{len(self._pending_pastes)} +{lines} lines] ")
                return
            await super()._on_paste(event)

        def action_paste(self) -> None:
            # Ctrl+V binding: same image-first check before the normal paste.
            if self._attach_clipboard_image():
                return
            super().action_paste()

        async def _on_key(self, event: events.Key) -> None:
            if event.key == "enter":
                event.stop()
                event.prevent_default()
                text = self.text
                if text.strip():
                    self._history.append(text)
                self._hist_idx = -1
                self._draft = ""
                self.post_message(self.Submitted(text))
                return
            if event.key in self._NEWLINE_KEYS:
                event.stop()
                event.prevent_default()
                self.insert("\n")
                return
            if event.key in ("up", "down") and "\n" not in self.text:
                if event.key == "up" and self._history:
                    event.stop()
                    event.prevent_default()
                    if self._hist_idx == -1:
                        self._draft = self.text
                        self._hist_idx = len(self._history) - 1
                    elif self._hist_idx > 0:
                        self._hist_idx -= 1
                    self.text = self._history[self._hist_idx]
                    self.move_cursor((0, len(self.text)))
                    return
                if event.key == "down" and self._hist_idx != -1:
                    event.stop()
                    event.prevent_default()
                    self._hist_idx += 1
                    if self._hist_idx >= len(self._history):
                        self._hist_idx = -1
                        self.text = self._draft
                    else:
                        self.text = self._history[self._hist_idx]
                    self.move_cursor((0, len(self.text)))
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

    class _ApprovalCard(Vertical):
        """Interactive presentation of one scheduler-owned approval request."""

        class Decision(Message):
            def __init__(self, request_id: str, action: str) -> None:
                self.request_id = request_id
                self.action = action
                super().__init__()

        def __init__(self, payload: dict[str, Any]) -> None:
            super().__init__(classes="approval-card")
            self.request_id = str(payload["request_id"])
            self.capability = str(payload.get("capability") or "unknown")
            self.operation = str(payload.get("operation") or "Sensitive operation")
            self.expires = str(payload.get("expires") or "unknown")
            self.expires_at: datetime | None = payload.get("expires_at")
            self._state = "pending"

        def compose(self) -> ComposeResult:
            yield Static(self._details(), classes="approval-details")
            yield Static("Waiting for your decision", classes="approval-status")
            with Horizontal(classes="approval-actions"):
                yield Button("Approve once", id="approve-once", variant="success")
                yield Button("Approve for turn", id="approve-turn", variant="warning")
                yield Button("Deny", id="deny", variant="error")

        def _details(self) -> Text:
            text = Text()
            text.append("Sensitive operation requires approval", style="bold bright_yellow")
            text.append(f"\nCapability: {self.capability}")
            text.append(f"\nOperation: {self.operation}")
            text.append(f"\nExpires: {self.expires}")
            return text

        def update_payload(self, payload: dict[str, Any]) -> None:
            """Refresh duplicate prompt facts without reopening a terminal card."""
            if payload.get("expires_at") is not None:
                self.expires_at = payload["expires_at"]
            self.capability = str(payload.get("capability") or self.capability)
            self.operation = str(payload.get("operation") or self.operation)
            self.expires = str(payload.get("expires") or self.expires)
            if self.is_attached:
                self.query_one(".approval-details", Static).update(self._details())

        def set_state(self, state: str, detail: str) -> None:
            """Render a monotonic terminal/submitting state and disable actions."""
            if self._state not in {"pending", "submitting"} and state == "pending":
                return
            self._state = state
            status_style = {
                "accepted": "bold green",
                "rejected": "bold red",
                "expired": "bold yellow",
                "cancelled": "bold yellow",
                "failed": "bold red",
                "submitting": "bold cyan",
            }.get(state, "dim")
            self.query_one(".approval-status", Static).update(Text(detail, style=status_style))
            if state != "pending":
                for button in self.query(Button):
                    button.disabled = True

        def on_button_pressed(self, event: Button.Pressed) -> None:
            action = {
                "approve-once": "once",
                "approve-turn": "turn",
                "deny": "deny",
            }.get(event.button.id)
            if action is None or self._state != "pending":
                return
            event.stop()
            self.post_message(self.Decision(self.request_id, action))

    class _ChatApp(App):
        """Two-pane terminal app: chat on the left, live log on the right."""

        # Disable Textual's own text selection so the mouse stays free for the
        # toolbar; native terminal selection still works via Shift+drag.
        ALLOW_SELECT = False

        CSS = """
        Horizontal { height: 1fr; }
        #chat-pane { width: 2fr; border: round $accent; }
        #approvals { height: auto; max-height: 16; overflow-y: auto; padding: 0 1; }
        .approval-card { height: auto; border: round $warning; background: $surface; padding: 1; margin: 0 0 1 0; }
        .approval-details { height: auto; }
        .approval-status { height: auto; margin: 1 0 0 0; }
        .approval-actions { height: 3; margin: 1 0 0 0; }
        .approval-actions Button { margin: 0 1 0 0; min-width: 14; }
        #chat { height: 1fr; padding: 0 1; overflow-x: hidden; }
        #status { height: 1; color: $text-muted; padding: 0 1; }
        #prompt { border: none; height: 4; padding: 0 1; }
        #toolbar { height: 1; width: 100%; padding: 0 1; overflow: hidden; }
        #toolbar-spacer { width: 1fr; min-width: 0; }
        #toolbar Button { height: 1; border: none; width: auto; min-width: 6; margin: 0 1 0 0; padding: 0 1; }
        #toolbar Select { height: 1; margin: 0 1 0 0; min-width: 0; }
        #toolbar #cat-select { width: 16; }
        #toolbar #model-select { width: 36; }
        #toolbar #effort-select { width: 14; }
        #approval-mode-status { display: none; }
        #approval-mode { min-width: 8; }
        #send { min-width: 8; }
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
            self._approvals: Vertical | None = None
            self._log: RichLog | None = None
            self._status: Static | None = None
            self._send: Button | None = None
            self._approval_mode_button: Button | None = None
            self._approval_mode_status: Static | None = None
            self._yolo_enabled = False
            self._busy = False
            self._spin = 0
            self._suppress_next_model = False
            self._suppress_next_effort = False
            self._approval_cards: dict[str, _ApprovalCard] = {}
            self._approval_terminal_ids: set[str] = set()
            self._approval_terminal_order: deque[str] = deque(maxlen=256)

        def compose(self) -> ComposeResult:
            with Horizontal():
                with Vertical(id="chat-pane"):
                    yield Vertical(id="approvals")
                    yield RichLog(id="chat", wrap=True, markup=False, highlight=False, min_width=0)
                    yield Static("", id="status")
                    yield _ChatInput(id="prompt", soft_wrap=True, show_line_numbers=False)
                    with Horizontal(id="toolbar"):
                        yield Button("Quit", id="quit", variant="error")
                        yield Select(
                            _MODEL_CATEGORIES, id="cat-select", value="agent", allow_blank=False
                        )
                        yield Select([], id="model-select", prompt="Model", allow_blank=True)
                        yield Select(
                            _EFFORT_OPTIONS,
                            id="effort-select",
                            prompt="Effort",
                            value=Select.NULL,
                            allow_blank=True,
                        )
                        yield Static(id="toolbar-spacer")
                        yield Static(id="approval-mode-status")
                        yield Button("AUTO", id="approval-mode", variant="success")
                        yield Button("Send", id="send", variant="success")
                with Vertical(id="log-pane"):
                    yield Static(id="log-header")
                    yield RichLog(id="log", wrap=True, markup=False, highlight=False, min_width=0)

        def on_mount(self) -> None:
            self._chat = self.query_one("#chat", RichLog)
            self._approvals = self.query_one("#approvals", Vertical)
            self._log = self.query_one("#log", RichLog)
            self._status = self.query_one("#status", Static)
            self._send = self.query_one("#send", Button)
            self._approval_mode_button = self.query_one("#approval-mode", Button)
            self._approval_mode_status = self.query_one("#approval-mode-status", Static)
            self.query_one("#log-header", Static).update(self._log_header())
            self._populate_models()
            self._populate_effort()
            self._set_approval_mode(False)
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

        def _populate_effort(self) -> None:
            try:
                from nanocat.config.loader import get_runtime_config

                effort = get_runtime_config().agents.defaults.reasoning_effort or "auto"
            except Exception:
                effort = "auto"
            self._show_effort(effort)

        def _show_effort(self, effort: str) -> None:
            """Display the configured reasoning effort without submitting a command."""
            values = {value for _, value in _EFFORT_OPTIONS}
            selected = effort if effort in values else "auto"
            sel = self.query_one("#effort-select", Select)
            if sel.value != selected:
                self._suppress_next_effort = True
                sel.value = selected

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
                }.get(category)
            except Exception:
                return
            self._show_model(target, models)

        # --- toolbar actions -------------------------------------------------

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "quit":
                self.push_screen(_QuitConfirm(), self._on_quit_decision)
            elif event.button.id == "approval-mode":
                self._set_approval_mode(not self._yolo_enabled)
                self._channel.submit_threadsafe(
                    "/approve forever" if self._yolo_enabled else "/approve cancel"
                )
            elif event.button.id == "send":
                if self._busy:
                    if self.query_one("#prompt", _ChatInput).text.strip():
                        self._submit_current()  # Steer: inject the typed interjection
                    else:
                        self._channel.submit_threadsafe("/stop")  # Stop: abort the turn
                        self._set_busy(False)
                else:
                    self._submit_current()

        @on(_ApprovalCard.Decision)
        def _on_approval_decision(self, event: _ApprovalCard.Decision) -> None:
            card = self._approval_cards.get(event.request_id)
            if card is None or card._state != "pending":
                return
            card.set_state("submitting", "Sending your decision to the scheduler…")
            command = "/deny" if event.action == "deny" else f"/approve {event.action}"
            # The UI only submits the canonical command through the normal TUI
            # ingress; it never touches the runtime broker or its event loop.
            self._channel.submit_threadsafe(command)

        def _on_quit_decision(self, confirmed: bool | None) -> None:
            if confirmed:
                self.exit()

        def _set_approval_mode(self, yolo_enabled: bool) -> None:
            """Update the local approval-mode indicator without waiting for ingress."""
            self._yolo_enabled = yolo_enabled
            if self._approval_mode_button is None or self._approval_mode_status is None:
                return
            if yolo_enabled:
                self._approval_mode_button.label = "YOLO"
                self._approval_mode_button.variant = "error"
                self._approval_mode_status.update(
                    Text("YOLO · session approvals bypassed", style="bold red")
                )
            else:
                self._approval_mode_button.label = "AUTO"
                self._approval_mode_button.variant = "success"
                self._approval_mode_status.update(
                    Text("AUTO · sensitive actions require approval", style="green")
                )

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id == "cat-select":
                # Switching category refreshes the model dropdown to that slot.
                if event.value is not Select.BLANK:
                    self._sync_model_to_category(str(event.value))
                return
            if event.select.id == "effort-select":
                if event.value is Select.BLANK or event.value is Select.NULL:
                    return
                if self._suppress_next_effort:
                    self._suppress_next_effort = False
                    return
                self._channel.submit_threadsafe(f"/model effort {event.value}")
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

        @on(TextArea.Changed, "#prompt")
        def _on_prompt_changed(self, event: "TextArea.Changed") -> None:
            # Drop deleted image/paste tokens from their buffers + renumber survivors
            # first, so the button state reflects the reconciled text.
            self.query_one("#prompt", _ChatInput)._reconcile_placeholders()
            # While busy, the action button reflects whether there's text to send:
            # text → yellow "Steer" (interject), empty → red "Stop" (abort).
            self._refresh_busy_button()

        def _refresh_busy_button(self) -> None:
            if self._send is None or not self._busy:
                return
            has_text = bool(self.query_one("#prompt", _ChatInput).text.strip())
            self._send.label = "Send" if has_text else "Stop"
            self._send.variant = "warning" if has_text else "error"

        def _submit_current(self) -> None:
            inp = self.query_one("#prompt", _ChatInput)
            display = inp.text  # placeholder form, shown verbatim in the chat bubble
            media = inp.take_pending_media()  # filters images by placeholder; reads inp.text
            send = inp.take_pending_pastes(display).strip()  # expand paste blocks for the agent
            inp.text = ""
            if not display.strip() and not media:
                return
            self._write_user(display.strip() or "[image]")
            self._set_busy(True)
            self._channel.submit_threadsafe(send, media=media or None)

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

        def _write_subagent(self, payload: dict) -> None:
            assert self._chat is not None
            label = str(payload.get("label") or "").strip()
            title = f"Subagent · {label}" if label else "Subagent"
            self._chat.write(
                Panel(
                    Text(str(payload.get("result") or "")),
                    title=title,
                    title_align="left",
                    border_style=_SUBAGENT_BORDER,
                    box=ROUNDED,
                    padding=(0, 1),
                )
            )
            self._chat.write("")

        def _write_progress(self, text: str) -> None:
            """Muted, quote-barred line for the agent's interim 'thinking' notes —
            visually distinct from final reply bubbles and tool-status lines."""
            assert self._chat is not None
            for raw in text.splitlines() or [text]:
                line = Text()
                line.append("  │ ", style=_BOT_BORDER)
                line.append(raw, style="italic grey50")
                self._chat.write(line)

        def _write_tool_event(self, payload: dict) -> None:
            """Render a tool-call batch: dim 'running' lines on start, a green/red
            status line per tool on completion."""
            assert self._chat is not None
            phase = payload.get("phase")
            for call in payload.get("calls", []):
                name = str(call.get("name", "?"))
                line = Text()
                if phase == "start":
                    line.append("  ⚒ ", style="yellow")
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

        def _show_approval(self, payload: dict[str, Any]) -> None:
            request_id = str(payload["request_id"])
            if request_id in self._approval_terminal_ids:
                return
            card = self._approval_cards.get(request_id)
            if card is not None:
                card.update_payload(payload)
                return
            if self._approvals is None:
                return
            card = _ApprovalCard(payload)
            self._approval_cards[request_id] = card
            self._approvals.mount(card)

        def _write_approval_status(self, payload: dict[str, Any]) -> None:
            assert self._chat is not None
            state = str(payload.get("state") or "updated").upper()
            self._chat.write(
                Panel(
                    Text(str(payload.get("detail") or "Approval request updated.")),
                    title=f"Approval · {state}",
                    title_align="left",
                    border_style=_APPROVAL_BORDER,
                    box=ROUNDED,
                    padding=(0, 1),
                )
            )
            self._chat.write("")

        def _remember_terminal_approval(self, request_id: str) -> None:
            if request_id in self._approval_terminal_ids:
                return
            if len(self._approval_terminal_order) == self._approval_terminal_order.maxlen:
                oldest = self._approval_terminal_order.popleft()
                self._approval_terminal_ids.discard(oldest)
            self._approval_terminal_order.append(request_id)
            self._approval_terminal_ids.add(request_id)

        def _update_approval(self, payload: dict[str, Any]) -> None:
            request_id = str(payload["request_id"])
            if request_id in self._approval_terminal_ids:
                return
            card = self._approval_cards.get(request_id)
            if card is None:
                self._write_approval_status(payload)
                self._remember_terminal_approval(request_id)
                return
            state = str(payload.get("state") or "updated")
            card.set_state(state, str(payload.get("detail") or "Approval request updated."))
            if state not in {"pending", "submitting"}:
                self._approval_cards.pop(request_id, None)
                self._write_approval_status(payload)
                self._remember_terminal_approval(request_id)
                if card.is_attached:
                    card.remove()

        def _expire_approvals(self) -> None:
            now = datetime.now(timezone.utc)
            for request_id, card in tuple(self._approval_cards.items()):
                if card._state != "pending" or card.expires_at is None or now < card.expires_at:
                    continue
                self._update_approval(
                    {
                        "request_id": request_id,
                        "state": "expired",
                        "detail": "This approval request expired before a decision was accepted.",
                    }
                )

        def _tick_spinner(self) -> None:
            # Idle status (the hint) is owned by _set_busy / on_mount; here we
            # only animate the spinner while a turn is in flight.
            self._expire_approvals()
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
                elif kind == "chat_subagent":
                    self._write_subagent(payload)
                elif kind == "chat_progress":
                    self._write_progress(payload)
                elif kind == "tool_event":
                    self._write_tool_event(payload)
                elif kind == "approval":
                    self._show_approval(payload)
                elif kind == "approval_update":
                    self._update_approval(payload)
                elif kind == "approval_mode":
                    self._set_approval_mode(payload.get("mode") == "yolo")

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
        """Queue prior session turns so they render before live ones.

        User messages and final assistant replies become bubbles; interim
        assistant turns (those that carried tool calls) render as muted progress
        notes, matching how they appeared live, instead of full reply bubbles.
        Empty and non-chat (tool/system) turns are skipped.
        """
        for msg in history:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _flatten_content(msg.get("content")).strip()
            if not text:
                continue
            if role == "user":
                if sub := _parse_subagent_result(text):
                    self._display_q.put(("chat_subagent", sub))
                else:
                    self._display_q.put(("chat_user", text))
            elif msg.get("tool_calls"):
                self._display_q.put(("chat_progress", text))
            else:
                self._display_q.put(("chat_bot", text))

    def bind_runtime_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the background runtime loop used to publish inbound messages."""
        self._runtime_loop = loop

    def submit_threadsafe(self, text: str, media: list[str] | None = None) -> None:
        """Forward terminal input to the agent from the UI thread (non-blocking)."""
        loop = self._runtime_loop
        if loop is None:
            logger.warning("TUI received input before the runtime loop was bound")
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_message(sender_id="local", chat_id="local", content=text, media=media),
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
        if self._log_sink_id is not None:
            logger.remove(self._log_sink_id)
            self._log_sink_id = None
        await self._cancel_owned_tasks()

    async def send(self, msg: OutboundMessage) -> None:
        meta = msg.metadata or {}
        if meta.get("_intervention"):
            payload = _intervention_payload(msg)
            if payload is not None:
                if payload.get("mode"):
                    self._display_q.put(("approval_mode", payload))
                if payload.get("request_id"):
                    self._display_q.put(
                        (
                            "approval_update" if meta.get("_intervention_update") else "approval",
                            payload,
                        )
                    )
                return
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
