"""Approval card widget driven by scheduler-owned intervention payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static

# action value -> (button id suffix, label, variant)
_ACTION_BUTTONS = {
    "approve_once": ("approve-once", "Approve once", "success"),
    "approve_turn": ("approve-turn", "Approve for turn", "warning"),
    "approve_forever": ("approve-forever", "Approve forever", "warning"),
    "reject": ("deny", "Deny", "error"),
}
_DEFAULT_ACTIONS = ("approve_once", "approve_turn", "reject")


class ApprovalCard(Vertical):
    """Interactive presentation of one scheduler-owned approval request.

    Buttons are generated from the request's ``allowed_actions`` so the UI can
    never offer an action the scheduler did not allow. Decisions are posted as
    messages; the app forwards them through the structured control port — the
    widget never talks to the intervention broker directly.
    """

    class Decision(Message):
        def __init__(self, request_id: str, action: str) -> None:
            self.request_id = request_id
            self.action = action
            super().__init__()

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(classes="approval-card")
        self.request_id = str(payload["request_id"])
        self.capability = str(payload.get("capability") or "unknown")
        self.tool_name = str(payload.get("tool_name") or "unknown")
        self.tool_params = str(payload.get("tool_params") or "{}")
        self.operation = str(payload.get("operation") or "Sensitive operation")
        self.approval_flow = str(payload.get("approval_flow") or "manual")
        self.review_reason = str(payload.get("review_reason") or "")
        self.expires = str(payload.get("expires") or "unknown")
        self.expires_at: datetime | None = payload.get("expires_at")
        actions = tuple(a for a in payload.get("allowed_actions") or () if a in _ACTION_BUTTONS)
        self.allowed_actions = actions or _DEFAULT_ACTIONS
        self._state = "pending"

    def compose(self):
        yield Static(self._details(), classes="approval-details")
        yield Static("Waiting for your decision", classes="approval-status")
        with Horizontal(classes="approval-actions"):
            for action in self.allowed_actions:
                suffix, label, variant = _ACTION_BUTTONS[action]
                if self.approval_flow == "auto_review" and action == "approve_once":
                    label = "Approve"
                yield Button(label, id=f"decision-{suffix}", variant=variant)

    def _details(self) -> Text:
        text = Text()
        text.append("Sensitive operation requires approval", style="bold bright_yellow")
        text.append(f"\nTool: {self.tool_name}")
        text.append(f"\nParameters: {self.tool_params}")
        text.append(f"\nOperation: {self.operation}")
        if self.review_reason:
            text.append(f"\nAuto review: {self.review_reason}", style="yellow")
        text.append(f"\nExpires: {self.expires}")
        return text

    def update_payload(self, payload: dict[str, Any]) -> None:
        """Refresh duplicate prompt facts without reopening a terminal card."""
        if payload.get("expires_at") is not None:
            self.expires_at = payload["expires_at"]
        self.capability = str(payload.get("capability") or self.capability)
        self.tool_name = str(payload.get("tool_name") or self.tool_name)
        self.tool_params = str(payload.get("tool_params") or self.tool_params)
        self.operation = str(payload.get("operation") or self.operation)
        self.approval_flow = str(payload.get("approval_flow") or self.approval_flow)
        self.review_reason = str(payload.get("review_reason") or self.review_reason)
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
            "approved_once": "bold green",
            "approved_turn": "bold green",
            "approved_forever": "bold green",
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
        button_id = event.button.id or ""
        if not button_id.startswith("decision-") or self._state != "pending":
            return
        suffix = button_id[len("decision-") :]
        action = next(
            (name for name, spec in _ACTION_BUTTONS.items() if spec[0] == suffix),
            None,
        )
        if action is None or action not in self.allowed_actions:
            return
        event.stop()
        self.post_message(self.Decision(self.request_id, action))
