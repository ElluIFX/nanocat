"""NanoCat Textual application shell.

Layout: header bar on top; a collapsible sessions sidebar on the left; the
chat pane in the center (approvals, transcript, activity line, composer); a
log drawer on the right that stays hidden until toggled. All control actions
go through ``ApplicationControlService`` via the channel's control bridge —
no widget injects slash-command text into the message bus.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable

from rich.box import ROUNDED
from rich.highlighter import ReprHighlighter
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, ListView, RichLog, Select, Static, TextArea

from nanocat.channels.tui_app.approval import ApprovalCard
from nanocat.channels.tui_app.composer import ChatInput
from nanocat.channels.tui_app.screens import (
    ActionCenterScreen,
    ActionFormScreen,
    ConfirmScreen,
    ResultScreen,
    SessionDetailScreen,
    SettingsScreen,
    build_action_command,
)
from nanocat.channels.tui_app.theme import THEME_CSS
from nanocat.channels.tui_app.widgets import HeaderBar, SessionListItem, StatusPanel

_HL = ReprHighlighter()

_LEVEL_STYLES = {
    "TRACE": "dim",
    "DEBUG": "bright_black",
    "INFO": "bright_blue",
    "SUCCESS": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}
_LEVEL_ORDER = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "SUCCESS": 2, "WARNING": 3, "ERROR": 4, "CRITICAL": 5}

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_USER_BORDER = "cyan"
_BOT_BORDER = "green"
_SUBAGENT_BORDER = "yellow"
_APPROVAL_BORDER = "bright_yellow"

_INPUT_HINT = "Enter send · Shift+Enter newline · Ctrl+K actions · Ctrl+L logs · Esc stop"
_LOG_MSG_MAX = 240
_COL_TIME = 8
_COL_LEVEL = 8
_LOG_BUFFER_LIMIT = 2000
_LOG_RENDER_LIMIT = 500
_DRAIN_BUDGET = 120


class NanoCatApp(App):
    """Mouse-first terminal shell for the local NanoCat runtime."""

    # Disable Textual's own text selection so the mouse stays free for the
    # widgets; native terminal selection still works via Shift+drag.
    ALLOW_SELECT = False

    CSS = THEME_CSS

    BINDINGS = [
        ("escape", "abort", "Stop"),
        ("ctrl+k", "actions", "Actions"),
        ("ctrl+l", "toggle_logs", "Logs"),
        ("ctrl+b", "toggle_sessions", "Sessions"),
        ("ctrl+c", "quit_hint", "Quit"),
        ("ctrl+q", "quit_hint", None),
    ]

    _QUIT_HINT_WINDOW_S = 1.5

    def __init__(self, channel: Any) -> None:
        super().__init__()
        self._channel = channel
        self._header: HeaderBar | None = None
        self._status_panel: StatusPanel | None = None
        self._chat: RichLog | None = None
        self._approvals: Vertical | None = None
        self._activity: Static | None = None
        self._send: Button | None = None
        self._approval_mode_button: Button | None = None
        self._yolo_enabled = False
        self._busy = False
        self._spin = 0
        self._pulse = False
        self._pulse_tick = 0
        self._active_tool = ""
        self._quit_armed_at = 0.0
        self._log_notify = False
        self._sessions: list[dict[str, Any]] = []
        self._catalog: list[dict[str, Any]] = []
        self._identity: dict[str, Any] = {}
        self._models: dict[str, Any] = {}
        self._compact: dict[str, Any] = {}
        self._approval_cards: dict[str, ApprovalCard] = {}
        self._approval_terminal_ids: set[str] = set()
        self._approval_terminal_order: deque[str] = deque(maxlen=256)
        self._log_records: deque[tuple[str, str, str]] = deque(maxlen=_LOG_BUFFER_LIMIT)
        self._log_paused = False

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        with Horizontal(id="main"):
            with Vertical(id="sessions-pane"):
                yield Button("+ New session", id="sb-new")
                yield Input(placeholder="Filter sessions…", id="sb-filter")
                yield ListView(id="sb-list")
            with Vertical(id="chat-pane"):
                yield Vertical(id="approvals")
                yield RichLog(id="chat", wrap=True, markup=False, highlight=False, min_width=0)
                yield Static(_INPUT_HINT, id="activity")
                yield ChatInput(id="prompt", soft_wrap=True, show_line_numbers=False)
                with Horizontal(id="composer-bar"):
                    yield Button("📎", id="attach")
                    yield Button("AUTO", id="approval-mode", classes="-auto")
                    yield Static(id="composer-spacer")
                    yield Button("Send", id="send", classes="-send")
            with Vertical(id="right-pane"):
                yield StatusPanel()
                with Vertical(id="log-pane"):
                    with Horizontal(id="log-bar"):
                        yield Static("Logs", id="log-title")
                        yield Input(placeholder="filter", id="log-filter")
                        yield Select(
                            [
                                ("All", "ALL"),
                                ("Info+", "INFO"),
                                ("Warn+", "WARNING"),
                                ("Error", "ERROR"),
                            ],
                            id="log-level",
                            value="ALL",
                            allow_blank=False,
                        )
                        yield Button("⏸", id="log-pause")
                        yield Button("🗑", id="log-clear")
                        yield Button("✕", id="log-close")
                    yield RichLog(id="log", wrap=True, markup=False, highlight=False, min_width=0)

    def on_mount(self) -> None:
        self._header = self.query_one("#hdr", HeaderBar)
        self._status_panel = self.query_one("#status-pane", StatusPanel)
        self._chat = self.query_one("#chat", RichLog)
        self._approvals = self.query_one("#approvals", Vertical)
        self._activity = self.query_one("#activity", Static)
        self._send = self.query_one("#send", Button)
        self._approval_mode_button = self.query_one("#approval-mode", Button)
        self.query_one("#prompt", ChatInput).focus()
        self.set_interval(0.05, self._drain)
        self.set_interval(0.1, self._tick)
        self.set_interval(15.0, self.refresh_snapshot)
        self._apply_width_classes()
        self.refresh_snapshot()

    def on_resize(self) -> None:
        self._apply_width_classes()

    def _apply_width_classes(self) -> None:
        try:
            width = self.size.width
        except Exception:
            return
        self.set_class(bool(width) and width < 100, "narrow")
        sidebar = self.query_one("#sessions-pane", Vertical)
        if width and width < 100 and sidebar.display is True and not self._sidebar_touched:
            sidebar.display = False

    _sidebar_touched = False

    # ------------------------------------------------------------------
    # control bridge
    # ------------------------------------------------------------------

    def run_control(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        on_done: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Run a structured control action; *on_done* fires on the UI thread."""
        payload = {
            "channel": "tui",
            "chat_id": "local",
            "principal_id": "local",
            **(params or {}),
        }
        sync_handler = getattr(self._channel, "control_sync_handler", None)
        if sync_handler is not None:
            try:
                result = sync_handler(action, payload)
            except Exception as e:
                result = {"ok": False, "error": str(e) or type(e).__name__}
            if on_done is not None:
                on_done(result)
            return
        future = self._channel.submit_control(action, payload)
        if future is None:
            if on_done is not None:
                on_done({"ok": False, "error": "control service unavailable"})
            return

        def _wait() -> None:
            try:
                result = future.result()
            except Exception as e:
                result = {"ok": False, "error": str(e) or type(e).__name__}
            if on_done is not None:
                self.call_from_thread(on_done, result)

        threading.Thread(target=_wait, daemon=True).start()

    def refresh_snapshot(self) -> None:
        self.run_control("runtime.snapshot", {}, self._apply_snapshot)

    def _apply_snapshot(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        data = result.get("data") or {}
        self._identity = data.get("identity") or {}
        self._models = data.get("models") or {}
        self._compact = data.get("compact") or {}
        self._catalog = data.get("catalog") or []
        sessions = data.get("sessions") or []
        self._sessions = list(sessions)
        approval = data.get("approval") or {}
        if self._header is not None:
            self._header.set_session(
                str(self._identity.get("session_name") or ""),
                str(self._identity.get("session_id") or ""),
            )
            effective = (self._models.get("effective") or {}).get("agent") or ""
            self._header.set_model(str(effective))
            percent = self._compact.get("context_usage_percent")
            self._header.set_context(float(percent) if percent is not None else None)
        self._set_approval_mode(bool(approval.get("yolo")))
        self._update_status_panel()
        self._render_sessions()

    def _update_status_panel(self, frame: str = "") -> None:
        if self._status_panel is None:
            return
        approval = {"yolo": self._yolo_enabled, "pending": len(self._approval_cards)}
        self._status_panel.update_state(
            identity=self._identity,
            models=self._models,
            compact=self._compact,
            approval=approval,
            busy=self._busy,
            frame=frame,
        )

    def refresh_models(self, models: dict[str, Any]) -> None:
        self._models = models
        if self._header is not None:
            effective = (models.get("effective") or {}).get("agent") or ""
            self._header.set_model(str(effective))

    # ------------------------------------------------------------------
    # sessions sidebar
    # ------------------------------------------------------------------

    def _render_sessions(self, filter_text: str = "") -> None:
        list_view = self.query_one("#sb-list", ListView)
        filter_text = filter_text.strip().lower()
        items = []
        for session in self._sessions:
            haystack = f"{session.get('name', '')} {session.get('id', '')}"
            if filter_text and filter_text not in haystack.lower():
                continue
            items.append(SessionListItem(session))
        list_view.clear()
        if items:
            list_view.extend(items)

    @on(Input.Changed, "#sb-filter")
    def _on_session_filter(self, event: Input.Changed) -> None:
        self._render_sessions(event.value)

    @on(ListView.Selected, "#sb-list")
    def _on_session_selected(self, event: ListView.Selected) -> None:
        item = event.item
        session = getattr(item, "session", None)
        if not session:
            return
        session_id = str(session.get("id") or "")
        self.run_control(
            "sessions.get",
            {"session_id": session_id},
            lambda result: self._open_session_detail(session, result),
        )

    def _open_session_detail(self, session: dict[str, Any], result: dict[str, Any]) -> None:
        preview = ""
        if result.get("ok"):
            preview = str((result.get("data") or {}).get("preview") or "")
        screen = SessionDetailScreen(session, preview)
        self.push_screen(screen, lambda action: self._on_session_action(screen, action))

    def _on_session_action(self, screen: SessionDetailScreen, action: str | None) -> None:
        session_id = str(screen.session.get("id") or "")
        if action == "switch":
            self.run_control(
                "session.switch",
                {"session_id": session_id},
                self._on_session_switched,
            )
        elif action == "delete":
            name = str(screen.session.get("name") or session_id)
            self.push_screen(
                ConfirmScreen(f"Delete session `{name}`? The file is moved to trash."),
                lambda confirmed: self._delete_session(session_id, confirmed),
            )
        elif action == "rename":
            name = screen.rename_value()
            if name:
                self.run_control(
                    "session.rename",
                    {"session_id": session_id, "name": name},
                    lambda result: self._after_session_mutation(result, "Session renamed."),
                )

    def _delete_session(self, session_id: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self.run_control(
            "session.delete",
            {"session_id": session_id},
            lambda result: self._after_session_mutation(result, "Session deleted."),
        )

    def _on_session_switched(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.notify(str(result.get("error") or "Switch failed"), severity="error")
            return
        data = result.get("data") or {}
        self._replace_transcript(data.get("events") or [])
        self.refresh_snapshot()
        self.notify(str(data.get("message") or "Session switched."))

    def _after_session_mutation(self, result: dict[str, Any], success: str) -> None:
        if not result.get("ok"):
            self.notify(str(result.get("error") or "Action failed"), severity="error")
            return
        data = result.get("data") or {}
        events = data.get("events")
        if isinstance(events, list):
            self._replace_transcript(events)
        self.refresh_snapshot()
        self.notify(str(data.get("message") or success))

    def _replace_transcript(self, events: list[dict[str, Any]]) -> None:
        assert self._chat is not None
        self._chat.clear()
        for event in events:
            kind = event.get("kind")
            if kind == "user":
                self._write_user(str(event.get("text") or ""))
            elif kind == "bot":
                self._write_bot(str(event.get("text") or ""))
            elif kind == "progress":
                self._write_progress(str(event.get("text") or ""))
            elif kind == "subagent":
                self._write_subagent(event.get("payload") or {})

    # ------------------------------------------------------------------
    # header / composer actions
    # ------------------------------------------------------------------

    def action_abort(self) -> None:
        """Esc: cancel the running turn; otherwise close open drawers."""
        if self._busy:
            self.run_control("turn.cancel", {}, self._on_turn_cancelled)
            return
        if self._logs_open():
            self.action_toggle_logs()
            return
        sidebar = self.query_one("#sessions-pane", Vertical)
        if sidebar.display:
            self.action_toggle_sessions()

    def action_quit_hint(self) -> None:
        """Ctrl+C (or legacy Ctrl+Q): double-press within the window to quit."""
        now = monotonic()
        if now - self._quit_armed_at <= self._QUIT_HINT_WINDOW_S:
            self._quit_armed_at = 0.0
            self.exit()
            return
        self._quit_armed_at = now
        self.notify("Press Ctrl+C again to quit", severity="warning")

    def action_actions(self) -> None:
        self._open_action_center()

    def _logs_open(self) -> bool:
        return self.query_one("#right-pane", Vertical).has_class("-logs")

    def action_toggle_logs(self) -> None:
        right_pane = self.query_one("#right-pane", Vertical)
        opening = not self._logs_open()
        right_pane.set_class(opening, "-logs")
        self.query_one("#log-pane", Vertical).set_class(opening, "-open")
        self.query_one("#hdr-logs", Button).set_class(opening, "-active")
        if opening:
            self._set_log_notify(False)
            # The hidden pane has a zero-width region; rendering before the
            # relayout would wrap every line to width 0 (visually blank), so
            # wait until the drawer has been laid out again.
            self.call_after_refresh(self._render_logs)

    def _set_log_notify(self, notify: bool) -> None:
        self._log_notify = notify
        if self._header is not None:
            self._header.set_log_notify(notify)

    def action_toggle_sessions(self) -> None:
        self._sidebar_touched = True
        sidebar = self.query_one("#sessions-pane", Vertical)
        sidebar.display = not sidebar.display
        self.query_one("#hdr-sessions-btn", Button).set_class(bool(sidebar.display), "-active")
        if sidebar.display:
            self.refresh_snapshot()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "hdr-sessions-btn":
            self.action_toggle_sessions()
        elif button_id == "hdr-logs":
            self.action_toggle_logs()
        elif button_id == "hdr-actions":
            self._open_action_center()
        elif button_id in {"hdr-settings", "hdr-model"}:
            self._open_settings()
        elif button_id == "sb-new":
            self.run_control("session.new", {}, self._on_new_session)
        elif button_id == "approval-mode":
            target = "cancel" if self._yolo_enabled else "forever"
            self.run_control(
                "approval.respond",
                {"approval_action": target},
                self._on_approval_mode_changed,
            )
        elif button_id == "attach":
            prompt = self.query_one("#prompt", ChatInput)
            if prompt.attach_clipboard_image():
                prompt.focus()
            else:
                self.notify("No image on the clipboard.", severity="warning")
        elif button_id == "send":
            if self._busy:
                if self.query_one("#prompt", ChatInput).text.strip():
                    self._submit_current()
                else:
                    self.run_control("turn.cancel", {}, self._on_turn_cancelled)
            else:
                self._submit_current()
        elif button_id == "log-pause":
            self._log_paused = not self._log_paused
            self.query_one("#log-pause", Button).label = "▶" if self._log_paused else "⏸"
        elif button_id == "log-clear":
            self._log_records.clear()
            self.query_one("#log", RichLog).clear()
        elif button_id == "log-close":
            self.action_toggle_logs()

    def _on_new_session(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.notify(str(result.get("error") or "Failed to start a new session"),
                        severity="error")
            return
        self._replace_transcript([])
        self.refresh_snapshot()
        self.notify("New session started.")

    def _on_turn_cancelled(self, result: dict[str, Any]) -> None:
        self._set_busy(False)
        if not result.get("ok"):
            self.notify(str(result.get("error") or "Stop failed"), severity="error")

    def _on_approval_mode_changed(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.notify(str(result.get("error") or "Approval mode change failed"),
                        severity="error")
            return
        mode = str((result.get("data") or {}).get("mode") or "")
        if mode in {"auto", "yolo"}:
            self._set_approval_mode(mode == "yolo")

    def _set_approval_mode(self, yolo_enabled: bool) -> None:
        self._yolo_enabled = yolo_enabled
        if self._approval_mode_button is None:
            return
        if yolo_enabled:
            self._approval_mode_button.label = "YOLO"
            self._approval_mode_button.remove_class("-auto")
            self._approval_mode_button.add_class("-yolo")
            self._approval_mode_button.tooltip = "Session approvals bypassed — click to re-enable"
        else:
            self._approval_mode_button.label = "AUTO"
            self._approval_mode_button.remove_class("-yolo")
            self._approval_mode_button.add_class("-auto")
            self._approval_mode_button.tooltip = "Sensitive actions require approval"

    # ------------------------------------------------------------------
    # settings + action center
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        self.push_screen(
            SettingsScreen(self._models, self._identity, self._compact),
            lambda _result: self.refresh_snapshot(),
        )

    def confirm_quit(self) -> None:
        self.push_screen(ConfirmScreen("Quit NanoCat?"), self._quit_confirmed)

    def _quit_confirmed(self, confirmed: bool | None) -> None:
        if confirmed:
            self.exit()

    def _open_action_center(self) -> None:
        if self._catalog:
            self.push_screen(ActionCenterScreen(self._catalog), self._on_action_picked)
            return
        # First open before the initial snapshot landed: fetch, then open.
        self.run_control(
            "runtime.snapshot",
            {},
            lambda result: self._open_action_center_loaded(result),
        )

    def _open_action_center_loaded(self, result: dict[str, Any]) -> None:
        self._apply_snapshot(result)
        self.push_screen(ActionCenterScreen(self._catalog), self._on_action_picked)

    def _on_action_picked(self, spec: dict[str, Any] | None) -> None:
        if not spec:
            return
        if spec.get("confirm"):
            self.push_screen(
                ConfirmScreen(f"Run `{spec.get('label')}`?"),
                lambda confirmed: self._action_form_or_run(spec, confirmed),
            )
            return
        self._action_form_or_run(spec, True)

    def _action_form_or_run(self, spec: dict[str, Any], confirmed: bool | None) -> None:
        if not confirmed:
            return
        if spec.get("fields"):
            self.push_screen(
                ActionFormScreen(spec),
                lambda values: self._execute_action(spec, values),
            )
            return
        self._execute_action(spec, {})

    def _execute_action(self, spec: dict[str, Any], values: dict[str, Any] | None) -> None:
        if values is None:
            return
        control_action = spec.get("control")
        if control_action:
            self.run_control(
                str(control_action),
                dict(values),
                lambda result: self._on_control_action_result(str(spec.get("id")), spec, result),
            )
            return
        text = build_action_command(spec, values)
        if not text.startswith("/"):
            self.notify("This action produced no command.", severity="error")
            return
        self.run_control(
            "command_execute",
            {"text": text},
            lambda result: self._on_command_action_result(spec, result),
        )

    def _on_control_action_result(
        self, spec_id: str, spec: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not result.get("ok"):
            self.notify(str(result.get("error") or "Action failed"), severity="error")
            return
        data = result.get("data") or {}
        if spec_id == "session.new":
            self._on_new_session(result)
            return
        if spec_id == "runtime.stop_turn":
            self._on_turn_cancelled(result)
            return
        if spec_id == "compact.status":
            compact = data.get("compact") or {}
            self._compact = compact
            if self._header is not None:
                percent = compact.get("context_usage_percent")
                self._header.set_context(float(percent) if percent is not None else None)
            self.push_screen(
                ResultScreen(
                    str(spec.get("label") or "Context status"),
                    self._format_compact_status(compact),
                )
            )
            return
        if spec_id == "model.state":
            self._open_settings()
            return
        message = str(data.get("message") or "Done.")
        self.notify(message)
        self.refresh_snapshot()

    @staticmethod
    def _format_compact_status(compact: dict[str, Any]) -> str:
        lines = [
            f"- Estimated prompt tokens: **{compact.get('estimated_prompt_tokens', '-')}**",
            f"- Context window: **{compact.get('context_window_tokens', '-')}**",
            f"- Usage: **{float(compact.get('context_usage_percent') or 0):.1f}%**",
            f"- Messages total: **{compact.get('messages_total', '-')}**",
            f"- Completed turns: **{compact.get('completed_turns', '-')}**",
            f"- Compaction enabled: **{'yes' if compact.get('compaction_enabled') else 'no'}**",
            f"- Compaction model: `{compact.get('compaction_model', '-')}`",
            f"- Failure count: **{compact.get('failure_count', 0)}**",
        ]
        checkpoint = compact.get("checkpoint") or {}
        if checkpoint:
            lines.append(
                f"- Checkpoint: `{checkpoint.get('created_at', '-')}` "
                f"(messages {checkpoint.get('source_start', '?')}–{checkpoint.get('source_end', '?')})"
            )
        return "\n".join(lines)

    def _on_command_action_result(self, spec: dict[str, Any], result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self.notify(str(result.get("error") or "Action failed"), severity="error")
            return
        data = result.get("data") or {}
        content = str(data.get("content") or "")
        self.push_screen(ResultScreen(str(spec.get("label") or "Result"), content))
        self.refresh_snapshot()

    # ------------------------------------------------------------------
    # composer
    # ------------------------------------------------------------------

    @on(ChatInput.Submitted)
    def _on_prompt_submit(self, event: ChatInput.Submitted) -> None:
        self._submit_current()

    @on(TextArea.Changed, "#prompt")
    def _on_prompt_changed(self, event: TextArea.Changed) -> None:
        self.query_one("#prompt", ChatInput)._reconcile_placeholders()
        self._refresh_busy_button()

    def _refresh_busy_button(self) -> None:
        if self._send is None:
            return
        if not self._busy:
            self._send.label = "Send"
            self._send.remove_class("-stop")
            self._send.remove_class("-steer")
            self._send.add_class("-send")
            return
        has_text = bool(self.query_one("#prompt", ChatInput).text.strip())
        self._send.label = "Send" if has_text else "Stop"
        self._send.remove_class("-send")
        self._send.remove_class("-stop")
        self._send.remove_class("-steer")
        self._send.add_class("-steer" if has_text else "-stop")

    def _submit_current(self) -> None:
        prompt = self.query_one("#prompt", ChatInput)
        display = prompt.text  # placeholder form, shown verbatim in the chat bubble
        media = prompt.take_pending_media()
        send = prompt.take_pending_pastes(display).strip()
        prompt.text = ""
        if not display.strip() and not media:
            return
        self._write_user(display.strip() or "[image]")
        self._set_busy(True)
        self._channel.submit_threadsafe(send, media=media or None)

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        if not value:
            self._active_tool = ""
        self._refresh_busy_button()
        if self._activity is not None and not value:
            self._activity.update(_INPUT_HINT)
        if self._header is not None:
            self._header.set_busy(value)
        self._update_status_panel()

    # ------------------------------------------------------------------
    # transcript rendering
    # ------------------------------------------------------------------

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
        """Muted, quote-barred line for the agent's interim 'thinking' notes."""
        assert self._chat is not None
        for raw in text.splitlines() or [text]:
            line = Text()
            line.append("  │ ", style=_BOT_BORDER)
            line.append(raw, style="italic grey50")
            self._chat.write(line)

    def _write_tool_event(self, payload: dict) -> None:
        """Render a tool-call batch and track the live activity line."""
        assert self._chat is not None
        from nanocat.channels.tui_app.events import summarize_args

        phase = payload.get("phase")
        for call in payload.get("calls", []):
            name = str(call.get("name", "?"))
            line = Text()
            if phase == "start":
                self._active_tool = name
                line.append("  ⚒ ", style="yellow")
                line.append(name, style="bold yellow")
                arg = summarize_args(call.get("args"))
                if arg:
                    line.append(f"  {arg}", style="dim")
            else:
                if self._active_tool == name:
                    self._active_tool = ""
                ok = call.get("status") == "ok"
                line.append("  ✓ " if ok else "  ✗ ", style="green" if ok else "red")
                line.append(name, style="green" if ok else "red")
                preview = " ".join(str(call.get("preview") or "").split())
                if preview:
                    line.append(f"  {preview[:100]}", style="dim")
            self._chat.write(line)

    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------

    @on(ApprovalCard.Decision)
    def _on_approval_decision(self, event: ApprovalCard.Decision) -> None:
        card = self._approval_cards.get(event.request_id)
        if card is None or card._state != "pending":
            return
        card.set_state("submitting", "Sending your decision to the scheduler…")
        action = {
            "approve_once": "once",
            "approve_turn": "turn",
            "approve_forever": "forever",
            "reject": "deny",
        }.get(event.action, "deny")
        self.run_control("approval.respond", {"approval_action": action}, lambda _r: None)

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
        card = ApprovalCard(payload)
        self._approval_cards[request_id] = card
        self._approvals.mount(card)
        self._update_status_panel()
        self.notify("Sensitive operation needs approval", severity="warning")

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
            self._update_status_panel()

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

    # ------------------------------------------------------------------
    # log drawer
    # ------------------------------------------------------------------

    @staticmethod
    def _log_table() -> "Table":
        table = Table(box=None, show_header=False, expand=True, padding=(0, 1, 0, 0))
        table.add_column(width=_COL_TIME, no_wrap=True)
        table.add_column(width=_COL_LEVEL, no_wrap=True)
        table.add_column(ratio=1, overflow="fold")
        return table

    @classmethod
    def _format_log(cls, ts: str, level: str, message: str) -> "Table":
        clean = " ".join(message.split())
        if len(clean) > _LOG_MSG_MAX:
            clean = clean[: _LOG_MSG_MAX - 1] + "…"
        msg = Text(clean)
        _HL.highlight(msg)
        table = cls._log_table()
        table.add_row(
            Text(ts, style="grey50"),
            Text(level, style=_LEVEL_STYLES.get(level, "white")),
            msg,
        )
        return table

    def _log_filter_values(self) -> tuple[str, int]:
        try:
            text = self.query_one("#log-filter", Input).value.strip().lower()
            selected = self.query_one("#log-level", Select).value
        except Exception:
            return "", 0
        minimum = {"ALL": 0, "INFO": 2, "WARNING": 3, "ERROR": 4}.get(str(selected), 0)
        return text, minimum

    def _log_visible(self, record: tuple[str, str, str], text: str, minimum: int) -> bool:
        ts, level, message = record
        if _LEVEL_ORDER.get(level, 2) < minimum:
            return False
        if text and text not in message.lower():
            return False
        return True

    def _render_logs(self, _retries: int = 0) -> None:
        log_widget = self.query_one("#log", RichLog)
        if log_widget.scrollable_content_region.width <= 0:
            # Drawer hidden or not laid out yet; retry shortly, then give up.
            if _retries < 10:
                self.call_after_refresh(self._render_logs, _retries + 1)
            return
        log_widget.clear()
        text, minimum = self._log_filter_values()
        visible = [r for r in self._log_records if self._log_visible(r, text, minimum)]
        for record in visible[-_LOG_RENDER_LIMIT:]:
            log_widget.write(self._format_log(*record))
        dropped = getattr(self._channel, "dropped_logs", 0)
        title = "Logs" + (f" (+{dropped} dropped)" if dropped else "")
        self.query_one("#log-title", Static).update(title)

    @on(Input.Changed, "#log-filter")
    def _on_log_filter(self, event: Input.Changed) -> None:
        self._render_logs()

    @on(Select.Changed, "#log-level")
    def _on_log_level(self, event: Select.Changed) -> None:
        self._render_logs()

    # ------------------------------------------------------------------
    # display-queue drain
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if not self.is_running:
            return
        self._expire_approvals()
        # Restrained pulse on pending approval cards (≈0.5 s toggle).
        self._pulse_tick += 1
        if any(card._state == "pending" for card in self._approval_cards.values()):
            if self._pulse_tick % 5 == 0:
                self._pulse = not self._pulse
                for card in self._approval_cards.values():
                    card.set_class(self._pulse, "-pulse")
        elif self._pulse:
            self._pulse = False
            for card in self._approval_cards.values():
                card.remove_class("-pulse")
        if not self._busy:
            return
        self._spin = (self._spin + 1) % len(_SPINNER)
        frame = _SPINNER[self._spin]
        if self._header is not None:
            self._header.set_busy(True, frame)
        if self._activity is not None:
            suffix = f" · ⚒ {self._active_tool}" if self._active_tool else ""
            self._activity.update(f"{frame} NanoCat is working{suffix}…")
        self._update_status_panel(frame)

    def _drain(self) -> None:
        if self._chat is None or not self.is_running:
            return
        display_queue = self._channel._display_q
        try:
            log_pane_open = self._logs_open()
        except Exception:
            return
        budget = _DRAIN_BUDGET
        while budget > 0:
            try:
                kind, payload = display_queue.get_nowait()
            except queue.Empty:
                break
            budget -= 1
            if kind == "log":
                self._log_records.append(payload)
                if log_pane_open and not self._log_paused:
                    text, minimum = self._log_filter_values()
                    if self._log_visible(payload, text, minimum):
                        self.query_one("#log", RichLog).write(self._format_log(*payload))
                elif not log_pane_open and not self._log_notify:
                    self._set_log_notify(True)
            elif kind == "chat_user":
                self._write_user(payload)
            elif kind == "chat_bot":
                self._set_busy(False)
                self._write_bot(payload)
                # a finished turn may have changed session/compaction state
                self.refresh_snapshot()
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
