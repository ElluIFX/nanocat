"""Headless TUI regression suite driven by Textual's Pilot.

Runs the real NanoCatApp against a fake channel/control bridge — no runtime,
no network. Execute with:  uv run python tests/tui_regression.py
"""

from __future__ import annotations

import asyncio
import queue
import sys
import traceback
from typing import Any

from nanocat.channels.tui_app.app import NanoCatApp
from nanocat.channels.tui_app.screens import (
    ActionCenterScreen,
    ActionFormScreen,
    ConfirmScreen,
    ModelEditScreen,
    ModelManagerScreen,
    ResultScreen,
    SessionDetailScreen,
    SettingsScreen,
)

SNAPSHOT = {
    "ok": True,
    "data": {
        "identity": {
            "channel": "tui",
            "chat_id": "local",
            "session_id": "abc123",
            "session_name": "Test session",
            "session_key": "tui:local:abc123",
            "principal_id": "local",
        },
        "busy": False,
        "approval": {"yolo": False, "pending": 0},
        "models": {
            "choices": ["deepseek/deepseek-v4-flash", "openai/gpt-4o"],
            "slots": {
                "agent": "deepseek/deepseek-v4-flash",
                "subagent": "",
                "assistant": "",
            },
            "effective": {
                "agent": "deepseek/deepseek-v4-flash",
                "subagent": "deepseek/deepseek-v4-flash",
                "assistant": "deepseek/deepseek-v4-flash",
            },
            "reasoning_effort": "auto",
            "provider": "deepseek",
        },
        "compact": {
            "estimated_prompt_tokens": 1200,
            "context_window_tokens": 128000,
            "context_usage_percent": 0.9,
            "compaction_available": True,
        },
        "sessions": [
            {
                "id": "abc123",
                "name": "Test session",
                "chat_id": "local",
                "last_active": "2026-08-09T10:00:00",
                "created_at": "2026-08-09T09:00:00",
                "message_count": 4,
                "turn_count": 2,
                "active": True,
            },
            {
                "id": "def456",
                "name": "Older session",
                "chat_id": "local",
                "last_active": "2026-08-08T10:00:00",
                "created_at": "2026-08-08T09:00:00",
                "message_count": 8,
                "turn_count": 4,
                "active": False,
            },
        ],
        "catalog": [],
    },
}


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, dict(params)))
        if action == "runtime.snapshot":
            from nanocat.application.control import ApplicationControlService

            data = dict(SNAPSHOT["data"])
            data["catalog"] = ApplicationControlService.action_catalog()
            return {"ok": True, "data": data}
        if action == "sessions.get":
            return {"ok": True, "data": {"preview": "[Q] hello\n\n[A] world"}}
        if action == "session.switch":
            return {
                "ok": True,
                "data": {
                    "session_id": params.get("session_id"),
                    "session_name": "Older session",
                    "events": [{"kind": "user", "text": "old question"}],
                    "message": "Switched.",
                },
            }
        if action == "session.new":
            return {"ok": True, "data": {"session_id": "fff000", "message": "New session started."}}
        if action == "session.delete":
            return {"ok": True, "data": {"deleted": params.get("session_id"), "message": "Deleted."}}
        if action == "session.rename":
            return {"ok": True, "data": {"session_id": params.get("session_id"), "name": params.get("name"), "message": "Renamed."}}
        if action == "approval.respond":
            mode = "yolo" if params.get("approval_action") == "forever" else "auto"
            return {"ok": True, "data": {"state": "approved", "mode": mode, "message": "ok"}}
        if action == "turn.cancel":
            return {"ok": True, "data": {"cancelled": 1, "message": "Stopped 1 task(s)."}}
        if action == "model.select":
            models = dict(SNAPSHOT["data"]["models"])
            slots = dict(models["slots"])
            slots[params["slot"]] = params["model"]
            models["slots"] = slots
            models["effective"] = {**models["effective"], params["slot"]: params["model"]}
            return {"ok": True, "data": {"models": models, "message": "Model set."}}
        if action == "model.add":
            models = dict(SNAPSHOT["data"]["models"])
            full = f"{params.get('provider')}/{params.get('model')}"
            choices = list(models["choices"])
            if full not in choices:
                choices.append(full)
            models["choices"] = choices
            return {"ok": True, "data": {"models": models, "message": f"Added {full}."}}
        if action == "model.update":
            models = dict(SNAPSHOT["data"]["models"])
            old = params.get("old_model")
            new = f"{params.get('provider')}/{params.get('model')}"
            choices = [new if m == old else m for m in models["choices"]]
            models["choices"] = choices
            return {"ok": True, "data": {"models": models, "message": f"Updated {old}."}}
        if action == "model.remove":
            models = dict(SNAPSHOT["data"]["models"])
            choices = [m for m in models["choices"] if m != params.get("model")]
            models["choices"] = choices
            return {"ok": True, "data": {"models": models, "message": "Removed."}}
        if action == "command_execute":
            return {
                "ok": True,
                "data": {"content": f"executed: {params.get('text')}", "routed": "structured"},
            }
        if action == "compact.status":
            return {"ok": True, "data": {"compact": dict(SNAPSHOT["data"]["compact"])}}
        return {"ok": True, "data": {}}


class FakeChannel:
    def __init__(self) -> None:
        self._display_q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.control_sync_handler = FakeControl()
        self.dropped_logs = 0
        self.sent: list[tuple[str, list[str] | None]] = []

    def submit_threadsafe(self, text: str, media: list[str] | None = None) -> None:
        self.sent.append((text, media))


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  — ' + detail) if detail and not condition else ''}")


async def test_shell_and_snapshot() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        hdr = app.query_one("#hdr-session")
        check("snapshot sets session label", "Test session" in str(hdr.render()))
        model_btn = app.query_one("#hdr-model")
        check("snapshot sets model label", "deepseek-v4-flash" in str(model_btn.label))
        ctx = app.query_one("#hdr-ctx")
        check("snapshot sets context usage", "ctx" in str(ctx.render()))
        log_pane = app.query_one("#log-pane")
        check("log drawer hidden by default", not log_pane.has_class("-open"))
        sidebar = app.query_one("#sessions-pane")
        check("sessions sidebar present", sidebar.display)
        items = list(app.query("#sb-list SessionListItem"))
        check("sessions rendered from snapshot", len(items) == 2, f"got {len(items)}")


async def test_log_drawer() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        channel._display_q.put(("log", ("10:00:00", "INFO", "runtime started")))
        channel._display_q.put(("log", ("10:00:01", "ERROR", "something broke")))
        await pilot.pause(0.2)
        log_widget = app.query_one("#log")
        check("logs buffered while hidden", len(app._log_records) == 2)
        check("hidden drawer not rendered", len(log_widget.lines) == 0)
        check("status panel visible by default", app.query_one("#status-pane").display)
        check("log notify dot set while closed", app._log_notify)
        await pilot.click("#hdr-logs")
        await pilot.pause()
        check("drawer opens via header button", app._logs_open())
        check("log pane shown", app.query_one("#log-pane").has_class("-open"))
        check("status panel hidden while logs open", not app.query_one("#status-pane").display)
        check("notify dot cleared on open", not app._log_notify)
        check("buffered logs rendered on open", len(log_widget.lines) >= 2)
        app.query_one("#log-filter").value = "broke"
        await pilot.pause()
        check("log filter narrows output", len(log_widget.lines) == 1)
        await pilot.click("#log-close")
        await pilot.pause()
        check("drawer closes", not app._logs_open())
        check("status panel restored", app.query_one("#status-pane").display)
        # regression: reopening must re-render after relayout (was blank)
        channel._display_q.put(("log", ("10:00:02", "WARNING", "late entry")))
        await pilot.pause(0.2)
        await pilot.click("#hdr-logs")
        await pilot.pause(0.4)
        svg = app.export_screenshot()
        check("second open renders content", "10:00" in svg)
        await pilot.click("#hdr-logs")
        await pilot.pause()
        await pilot.click("#hdr-logs")
        await pilot.pause(0.4)
        check("third open renders content", "10:00" in app.export_screenshot())


async def test_status_panel_and_quit() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        model_line = str(app.query_one("#sp-model").render())
        check("status panel shows model", "deepseek-v4-flash" in model_line, model_line)
        runtime_line = str(app.query_one("#sp-runtime").render())
        check("status panel shows idle", "idle" in runtime_line, runtime_line)
        context_line = str(app.query_one("#sp-context").render())
        check("status panel context bar", "█" in context_line or "░" in context_line)
        app._set_busy(True)
        await pilot.pause(0.3)
        runtime_line = str(app.query_one("#sp-runtime").render())
        check("status panel shows working", "working" in runtime_line, runtime_line)
        app._set_busy(False)

        # double Ctrl+C quits; single press only arms the hint
        exits = []
        app.exit = lambda: exits.append(True)  # type: ignore[assignment]
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        check("first ctrl+c arms hint", app._quit_armed_at > 0 and not exits)
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        check("second ctrl+c quits", bool(exits))

        # ctrl+q no longer quits directly; it routes to the same hint
        exits.clear()
        app._quit_armed_at = 0.0
        await pilot.press("ctrl+q")
        await pilot.pause(0.1)
        check("ctrl+q only arms hint", app._quit_armed_at > 0 and not exits)

        # ctrl+c with a selection in the composer still copies instead of quitting
        prompt = app.query_one("#prompt")
        prompt.focus()
        prompt.text = "copy me"
        prompt.selection = ((0, 0), (0, 4))
        app._quit_armed_at = 0.0
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        check("ctrl+c with selection does not arm quit", app._quit_armed_at == 0.0)
        prompt.text = ""
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        check("ctrl+c without selection arms quit", app._quit_armed_at > 0)


async def test_composer_submit() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.text = "hello nanocat"
        await pilot.click("#send")
        await pilot.pause()
        check("send button submits via bus", channel.sent == [("hello nanocat", None)])
        check("composer cleared", prompt.text == "")
        check("busy state set", app._busy)
        send_btn = app.query_one("#send")
        check("busy button becomes stop", "Stop" in str(send_btn.label))
        channel._display_q.put(("chat_bot", "hi there"))
        await pilot.pause(0.2)
        check("bot reply clears busy", not app._busy)


async def test_stop_button_uses_control() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app._set_busy(True)
        await pilot.click("#send")  # empty prompt while busy => stop
        await pilot.pause()
        actions = [a for a, _ in channel.control_sync_handler.calls]
        check("stop uses turn.cancel control action", "turn.cancel" in actions)
        check("no slash command injected", not any(t.startswith("/") for t, _ in channel.sent))


async def test_approval_card_flow() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        channel._display_q.put(
            (
                "approval",
                {
                    "request_id": "req-1",
                    "capability": "exec",
                    "operation": "Run command",
                    "tool_name": "exec",
                    "tool_params": '{"command": "rm -rf /"}',
                    "approval_flow": "manual",
                    "review_reason": "",
                    "allowed_actions": ("approve_once", "approve_turn", "reject"),
                    "expires": "soon",
                    "expires_at": None,
                    "state": "pending",
                    "mode": "",
                },
            )
        )
        await pilot.pause(0.2)
        card = app._approval_cards.get("req-1")
        check("approval card mounted", card is not None)
        if card is not None:
            labels = [str(b.label) for b in card.query("Button")]
            check(
                "card buttons follow allowed_actions",
                len(labels) == 3 and any("once" in label for label in labels),
                str(labels),
            )
            await pilot.click("#decision-approve-once")
            await pilot.pause()
            calls = channel.control_sync_handler.calls
            approval_calls = [p for a, p in calls if a == "approval.respond"]
            check(
                "decision routed through control port",
                approval_calls and approval_calls[-1].get("approval_action") == "once",
                str(calls),
            )
        channel._display_q.put(
            (
                "approval_update",
                {
                    "request_id": "req-1",
                    "state": "approved_once",
                    "detail": "Approved.",
                    "mode": "",
                },
            )
        )
        await pilot.pause(0.2)
        check("terminal update removes card", "req-1" not in app._approval_cards)


async def test_yolo_toggle() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#approval-mode")
        await pilot.pause()
        calls = channel.control_sync_handler.calls
        mode_calls = [p for a, p in calls if a == "approval.respond"]
        check(
            "yolo toggle sends forever grant",
            mode_calls and mode_calls[-1].get("approval_action") == "forever",
        )
        check("yolo state applied from authoritative result", app._yolo_enabled)
        check("button shows YOLO", "YOLO" in str(app.query_one("#approval-mode").label))


async def test_session_flows() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        app._write_user("old content")
        await pilot.click("#sb-new")
        await pilot.pause()
        actions = [a for a, _ in channel.control_sync_handler.calls]
        check("new session via control", "session.new" in actions)

        # open the detail modal for the inactive session, then switch
        app._on_session_selected(type("E", (), {"item": app.query("#sb-list SessionListItem")[1]})())
        await pilot.pause(0.2)
        check("session detail opens", isinstance(app.screen, SessionDetailScreen))
        if isinstance(app.screen, SessionDetailScreen):
            await pilot.click("#switch")
        await pilot.pause(0.2)
        calls = [p for a, p in channel.control_sync_handler.calls if a == "session.switch"]
        check("switch issued", calls and calls[-1].get("session_id") == "def456", str(calls))


async def test_action_center() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#hdr-actions")
        await pilot.pause()
        check("action center opens", isinstance(app.screen, ActionCenterScreen))
        options = app.screen.query_one("#ac-list")
        check("catalog populated", options.option_count > 20, f"{options.option_count}")
        app.screen.query_one("#ac-search").value = "memory search"
        await pilot.pause()
        check("search filters", options.option_count == 1, f"{options.option_count}")
        # select it → form opens
        app.screen._on_selected(type("E", (), {"option": type("O", (), {"id": "memory.search"})()})())
        await pilot.pause(0.2)
        check("form opens for parameterized action", isinstance(app.screen, ActionFormScreen))
        app.screen.query_one("#field-query").value = "edge intelligence"
        await pilot.click("#run")
        await pilot.pause(0.2)
        exec_calls = [p for a, p in channel.control_sync_handler.calls if a == "command_execute"]
        check(
            "form executes structured command",
            exec_calls and "edge intelligence" in str(exec_calls[-1].get("text")),
            str(exec_calls),
        )
        check("result screen shown", isinstance(app.screen, ResultScreen))


async def test_settings_model_select() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#hdr-settings")
        await pilot.pause()
        check("settings opens", isinstance(app.screen, SettingsScreen))
        screen = app.screen
        settings_scroll = screen.query_one("#settings-scroll")
        check("settings content has scroll viewport", settings_scroll.styles.height.value == 1)
        check(
            "settings content can scroll",
            settings_scroll.max_scroll_y > 0,
        )
        check("settings has no restart control", not list(screen.query("#set-restart")))
        select = screen.query_one("#set-slot-agent")
        select.value = "openai/gpt-4o"
        await pilot.pause(0.2)
        select_calls = [p for a, p in channel.control_sync_handler.calls if a == "model.select"]
        check(
            "model select uses structured action",
            select_calls
            and select_calls[-1].get("slot") == "agent"
            and select_calls[-1].get("model") == "openai/gpt-4o",
            str(select_calls),
        )
        check(
            "header model refreshed",
            "gpt-4o" in str(app.query_one("#hdr-model").label),
        )


async def test_model_manager() -> None:
    channel = FakeChannel()
    app = NanoCatApp(channel)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.click("#hdr-settings")
        await pilot.pause()
        screen = app.screen
        check("settings opens", isinstance(screen, SettingsScreen))
        screen.query_one("#settings-scroll").scroll_end(animate=False)
        await pilot.pause()
        await pilot.click("#set-manage")
        await pilot.pause(0.2)
        manager = app.screen
        check("model manager opens", isinstance(manager, ModelManagerScreen))
        check("model manager is centered", manager.styles.align == ("center", "middle"))
        items = list(manager.query("#mm-list ListItem"))
        check("catalog entries listed", len(items) == 2, f"{len(items)}")

        # add a new model
        await pilot.click("#mm-add")
        await pilot.pause(0.2)
        check("edit screen opens for add", isinstance(app.screen, ModelEditScreen))
        app.screen.query_one("#me-provider").value = "anthropic"
        app.screen.query_one("#me-model").value = "claude-opus-4"
        await pilot.click("#save")
        await pilot.pause(0.2)
        add_calls = [p for a, p in channel.control_sync_handler.calls if a == "model.add"]
        check(
            "add uses structured action",
            add_calls and add_calls[-1].get("provider") == "anthropic",
            str(add_calls),
        )
        items = list(manager.query("#mm-list ListItem"))
        check("list refreshed after add", len(items) == 3, f"{len(items)}")

        # edit an existing entry
        manager._on_selected(type("E", (), {"item": items[0]})())
        await pilot.pause(0.2)
        edit = app.screen
        check("edit screen opens for edit", isinstance(edit, ModelEditScreen))
        edit.query_one("#me-model").value = "deepseek-v4-pro"
        await pilot.click("#save")
        await pilot.pause(0.2)
        update_calls = [p for a, p in channel.control_sync_handler.calls if a == "model.update"]
        check(
            "update carries old and new identity",
            update_calls
            and update_calls[-1].get("old_model") == "deepseek/deepseek-v4-flash"
            and update_calls[-1].get("model") == "deepseek-v4-pro",
            str(update_calls),
        )

        # delete with confirmation
        manager._on_selected(type("E", (), {"item": manager.query('#mm-list ListItem')[1]})())
        await pilot.pause(0.2)
        await pilot.click("#delete")
        await pilot.pause(0.2)
        check("delete asks confirmation", isinstance(app.screen, ConfirmScreen))
        await pilot.click("#confirm")
        await pilot.pause(0.2)
        remove_calls = [p for a, p in channel.control_sync_handler.calls if a == "model.remove"]
        check("remove uses stable model identity", bool(remove_calls), str(remove_calls))
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        check(
            "action catalog has no restart option",
            not any(str(spec.get("id")) == "runtime.restart" for spec in app._catalog),
        )


async def main() -> int:
    tests = [
        test_shell_and_snapshot,
        test_log_drawer,
        test_status_panel_and_quit,
        test_composer_submit,
        test_stop_button_uses_control,
        test_approval_card_flow,
        test_yolo_toggle,
        test_session_flows,
        test_action_center,
        test_settings_model_select,
        test_model_manager,
    ]
    for test in tests:
        try:
            await test()
        except Exception:
            check(test.__name__, False, traceback.format_exc(limit=3))
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
