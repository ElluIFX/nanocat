"""Composite widgets for the NanoCat TUI shell."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, ListItem, Static


class HeaderBar(Horizontal):
    """Top bar: brand, session/model state, and the global action buttons."""

    def __init__(self) -> None:
        super().__init__(id="hdr")

    def compose(self):
        yield Static("🐱 NanoCat", id="hdr-brand")
        yield Button("≡ Sessions", id="hdr-sessions-btn")
        yield Static("—", id="hdr-session")
        yield Button("model", id="hdr-model")
        yield Static("", id="hdr-ctx")
        yield Static("", id="hdr-busy")
        yield Static("", id="hdr-spacer")
        yield Button("Actions", id="hdr-actions")
        yield Button("Logs", id="hdr-logs")
        yield Button("⚙", id="hdr-settings")

    def set_session(self, name: str, session_id: str) -> None:
        label = name or session_id or "—"
        self.query_one("#hdr-session", Static).update(label)

    def set_model(self, model: str) -> None:
        short = model.split("/", 1)[-1] if "/" in model else model
        self.query_one("#hdr-model", Button).label = short or "model"
        self.query_one("#hdr-model", Button).tooltip = model

    def set_context(self, usage_percent: float | None) -> None:
        widget = self.query_one("#hdr-ctx", Static)
        if usage_percent is None:
            widget.update("")
            return
        widget.update(f"ctx {usage_percent:.0f}%")

    def set_busy(self, busy: bool, frame: str = "") -> None:
        self.query_one("#hdr-busy", Static).update(frame if busy else "")

    def set_log_notify(self, notify: bool) -> None:
        """Mark unread log activity while the drawer stays closed."""
        button = self.query_one("#hdr-logs", Button)
        button.set_class(notify, "-notify")
        button.label = "Logs •" if notify else "Logs"


class StatusPanel(Vertical):
    """Right-side system status summary; hidden while the log drawer is open."""

    def __init__(self) -> None:
        super().__init__(id="status-pane")

    def compose(self):
        yield Static("STATUS", id="sp-title")
        yield Static("—", id="sp-session")
        yield Static("", id="sp-model")
        yield Static("", id="sp-effort")
        yield Static("", id="sp-context")
        yield Static("", id="sp-runtime")
        yield Static("", id="sp-approval")
        yield Static("", id="sp-spacer")
        yield Static("Ctrl+L · logs", id="sp-hint")

    @staticmethod
    def _bar(percent: float, width: int = 12) -> str:
        filled = max(0, min(width, round(percent / 100 * width)))
        return "█" * filled + "░" * (width - filled)

    def update_state(
        self,
        *,
        identity: dict[str, Any],
        models: dict[str, Any],
        compact: dict[str, Any],
        approval: dict[str, Any],
        busy: bool,
        frame: str = "",
    ) -> None:
        session_name = identity.get("session_name") or identity.get("session_id") or "—"
        session_line = Text()
        session_line.append("session  ", style="#8b949e")
        session_line.append(str(session_name), style="#e6edf3")
        self.query_one("#sp-session", Static).update(session_line)

        effective = (models.get("effective") or {}).get("agent") or ""
        model_line = Text()
        model_line.append("model    ", style="#8b949e")
        model_line.append(str(effective or "—"), style="#56d4dd")
        self.query_one("#sp-model", Static).update(model_line)

        effort_line = Text()
        effort_line.append("effort   ", style="#8b949e")
        effort_line.append(str(models.get("reasoning_effort") or "auto"), style="#e6edf3")
        self.query_one("#sp-effort", Static).update(effort_line)

        percent = compact.get("context_usage_percent")
        context_line = Text()
        context_line.append("context  ", style="#8b949e")
        if percent is None:
            context_line.append("—", style="#8b949e")
        else:
            pct = float(percent)
            style = "#3fb950" if pct < 60 else "#d29922" if pct < 85 else "#f85149"
            context_line.append(f"{self._bar(pct)} ", style=style)
            context_line.append(f"{pct:.0f}%", style="#e6edf3")
        self.query_one("#sp-context", Static).update(context_line)

        runtime_line = Text()
        runtime_line.append("runtime  ", style="#8b949e")
        if busy:
            runtime_line.append(f"{frame or '●'} working", style="#d29922")
        else:
            runtime_line.append("● idle", style="#3fb950")
        self.query_one("#sp-runtime", Static).update(runtime_line)

        approval_line = Text()
        approval_line.append("approval ", style="#8b949e")
        if approval.get("yolo"):
            approval_line.append("YOLO · bypassed", style="#f85149")
        else:
            approval_line.append("AUTO", style="#3fb950")
        pending = int(approval.get("pending") or 0)
        if pending:
            approval_line.append(f"  {pending} pending", style="#d29922")
        self.query_one("#sp-approval", Static).update(approval_line)


class SessionListItem(ListItem):
    """One session entry in the sidebar list."""

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self.session = session
        if session.get("active"):
            self.add_class("session-active")

    def compose(self):
        session = self.session
        name = session.get("name") or "Unnamed session"
        sid = session.get("id") or ""
        turns = session.get("turn_count") or 0
        last = str(session.get("last_active") or "")[:16].replace("T", " ")
        meta = f"{sid} · {turns} turns · {last}"
        if session.get("active"):
            name = f"● {name}"
        yield Label(name, classes="session-name")
        yield Label(meta, classes="session-meta")
