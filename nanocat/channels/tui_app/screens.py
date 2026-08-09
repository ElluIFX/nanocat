"""Modal screens for the NanoCat TUI: confirms, action center, forms, settings."""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    RichLog,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from nanocat.application.control import build_command_text


class ConfirmScreen(ModalScreen[bool]):
    """Centered yes/no confirmation dialog."""

    def __init__(self, question: str, *, confirm_label: str = "Confirm", danger: bool = True):
        super().__init__()
        self._question = question
        self._confirm_label = confirm_label
        self._danger = danger

    def compose(self) -> ComposeResult:
        classes = "dialog dialog-danger" if self._danger else "dialog"
        with Vertical(classes=classes):
            yield Label(self._question, classes="dialog-title")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    self._confirm_label,
                    id="confirm",
                    variant="error" if self._danger else "primary",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)


class ResultScreen(ModalScreen[None]):
    """Scrollable markdown result viewer used by the Action Center."""

    def __init__(self, title: str, content: str):
        super().__init__()
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        from rich.markdown import Markdown

        with Vertical(classes="dialog dialog-tall"):
            yield Label(self._title, classes="dialog-title")
            log = RichLog(classes="dialog-body", wrap=True, markup=False, highlight=False)
            yield log
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", id="close", variant="primary")
        log.write(Markdown(self._content) if self._content.strip() else "(no output)")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ActionCenterScreen(ModalScreen[dict[str, Any] | None]):
    """Searchable mouse-first launcher covering every registered action."""

    def __init__(self, catalog: list[dict[str, Any]]):
        super().__init__()
        self._catalog = catalog
        self._visible: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="action-center"):
            yield Input(placeholder="Type to filter actions…", id="ac-search")
            yield OptionList(id="ac-list")
            yield Static(
                "Enter/click to run · Esc to close · dangerous actions ask first",
                id="ac-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#ac-search", Input).focus()
        self._apply_filter("")

    def _apply_filter(self, query: str) -> None:
        query = query.strip().lower()
        option_list = self.query_one("#ac-list", OptionList)
        option_list.clear_options()
        self._visible = []
        for spec in self._catalog:
            haystack = f"{spec.get('group', '')} {spec.get('label', '')} {spec.get('summary', '')}"
            if query and query not in haystack.lower():
                continue
            label = f"[{spec.get('group', '-')}] {spec.get('label', spec.get('id'))}"
            option_list.add_option(Option(label, id=str(spec.get("id"))))
            self._visible.append(spec)
        if not self._visible:
            option_list.add_option(Option("No matching action", id="__none", disabled=True))

    @on(Input.Changed, "#ac-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    @on(Input.Submitted, "#ac-search")
    def _on_submit(self, event: Input.Submitted) -> None:
        self.query_one("#ac-list", OptionList).focus()

    @on(OptionList.OptionSelected, "#ac-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = str(event.option.id or "")
        spec = next((s for s in self._catalog if str(s.get("id")) == option_id), None)
        self.dismiss(spec)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ActionFormScreen(ModalScreen[dict[str, Any] | None]):
    """Auto-generated parameter form for one catalog action."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__()
        self._spec = spec

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield Label(str(self._spec.get("label") or "Action"), classes="dialog-title")
            summary = str(self._spec.get("summary") or "")
            if summary:
                yield Static(summary, classes="settings-note")
            with VerticalScroll(classes="dialog-body"):
                for field in self._spec.get("fields") or ():
                    key = str(field.get("key"))
                    label = str(field.get("label") or key)
                    required = bool(field.get("required"))
                    yield Static(f"{label}{' *' if required else ''}", classes="field-label")
                    kind = field.get("kind")
                    if kind == "select":
                        options = [(str(o), str(o)) for o in field.get("options") or ()]
                        yield Select(options, id=f"field-{key}", allow_blank=True)
                    elif kind == "flag":
                        yield Checkbox(label, id=f"field-{key}")
                    else:
                        yield Input(
                            placeholder=label,
                            id=f"field-{key}",
                            value=str(field.get("default") or ""),
                        )
            yield Static("", classes="dialog-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Run", id="run", variant="primary")

    def _collect(self) -> dict[str, Any] | None:
        values: dict[str, Any] = {}
        for field in self._spec.get("fields") or ():
            key = str(field.get("key"))
            kind = field.get("kind")
            if kind == "select":
                value = self.query_one(f"#field-{key}", Select).value
                if value is None or value is Select.BLANK or value is Select.NULL:
                    value = ""
                else:
                    value = str(value)
            elif kind == "flag":
                values[key] = bool(self.query_one(f"#field-{key}", Checkbox).value)
                continue
            else:
                value = self.query_one(f"#field-{key}", Input).value.strip()
            if field.get("required") and not value:
                status = self.query_one(".dialog-status", Static)
                status.update(f"Missing required field: {field.get('label') or key}")
                return None
            if value:
                values[key] = value
        return values

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        values = self._collect()
        if values is not None:
            self.dismiss(values)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


def build_action_command(spec: dict[str, Any], values: dict[str, Any]) -> str:
    """Expand the spec's slash template with collected form values."""
    return build_command_text(str(spec.get("command") or ""), values)


class SessionDetailScreen(ModalScreen[str | None]):
    """Session preview with switch / rename / delete actions."""

    def __init__(self, session: dict[str, Any], preview: str):
        super().__init__()
        self._session = session
        self._preview = preview

    @property
    def session(self) -> dict[str, Any]:
        return self._session

    def compose(self) -> ComposeResult:
        session = self._session
        title = session.get("name") or "Unnamed session"
        with Vertical(classes="dialog dialog-tall"):
            yield Label(f"{title} ({session.get('id')})", classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                yield Static(self._preview or "(no preview)")
            yield Static("Rename:", classes="field-label")
            yield Input(
                placeholder="New session name (empty keeps current)",
                id="rename-input",
                value=str(session.get("name") or ""),
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", id="close")
                yield Button("Delete", id="delete", variant="error")
                yield Button("Rename", id="rename", variant="warning")
                yield Button("Switch", id="switch", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "close":
            self.dismiss(None)
        elif button_id == "switch":
            self.dismiss("switch")
        elif button_id == "delete":
            self.dismiss("delete")
        elif button_id == "rename":
            self.dismiss("rename")

    def rename_value(self) -> str:
        return self.query_one("#rename-input", Input).value.strip()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ModelEditScreen(ModalScreen[tuple | None]):
    """Edit one model-catalog entry (provider + model), or add a new one."""

    def __init__(self, old_model: str | None):
        super().__init__()
        self._old_model = old_model

    def compose(self) -> ComposeResult:
        provider, _, model = (self._old_model or "/").partition("/")
        title = "Edit model choice" if self._old_model else "Add model choice"
        with Vertical(classes="dialog"):
            yield Label(title, classes="dialog-title")
            yield Static("Provider *", classes="field-label")
            yield Input(placeholder="e.g. deepseek", id="me-provider", value=provider)
            yield Static("Model name *", classes="field-label")
            yield Input(placeholder="e.g. deepseek-v4-flash", id="me-model", value=model)
            yield Static("", classes="dialog-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                if self._old_model:
                    yield Button("Delete", id="delete", variant="error")
                yield Button("Save", id="save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "cancel":
            self.dismiss(None)
            return
        if button_id == "delete":
            self.dismiss(("delete", "", ""))
            return
        provider = self.query_one("#me-provider", Input).value.strip()
        model = self.query_one("#me-model", Input).value.strip()
        if not provider or not model:
            self.query_one(".dialog-status", Static).update(
                "Provider and model name are both required."
            )
            return
        self.dismiss(("save", provider, model))

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ModelManagerScreen(ModalScreen[None]):
    """Second-level panel for managing the model catalog (add/edit/delete)."""

    def __init__(self, models: dict[str, Any]):
        super().__init__()
        self._models = models

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield Label("Model catalog", classes="dialog-title")
            yield Static(
                "Slots referencing a model follow edits; assigned models cannot be removed.",
                classes="settings-note",
            )
            yield ListView(id="mm-list")
            yield Static("", classes="dialog-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", id="mm-close")
                yield Button("Add model", id="mm-add", variant="primary")

    def on_mount(self) -> None:
        self._render_list()

    def _render_list(self) -> None:
        list_view = self.query_one("#mm-list", ListView)
        list_view.clear()
        choices = [str(m) for m in self._models.get("choices") or []]
        slots = self._models.get("slots") or {}
        assigned = {m: s for s, m in slots.items() if m}
        items = []
        for choice in choices:
            badge = f"  [{assigned[choice]}]" if choice in assigned else ""
            item = ListItem(Label(f"{choice}{badge}"))
            item.model_id = choice
            items.append(item)
        if items:
            list_view.extend(items)

    def _status(self, text: str) -> None:
        self.query_one(".dialog-status", Static).update(text)

    @on(ListView.Selected, "#mm-list")
    def _on_selected(self, event: ListView.Selected) -> None:
        model_id = getattr(event.item, "model_id", None)
        if model_id:
            self.app.push_screen(
                ModelEditScreen(model_id),
                lambda result: self._on_edit_done(model_id, result),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "mm-close":
            self.dismiss(None)
        elif button_id == "mm-add":
            self.app.push_screen(
                ModelEditScreen(None),
                lambda result: self._on_edit_done(None, result),
            )

    def _on_edit_done(self, old_model: str | None, result: tuple | None) -> None:
        if not result:
            return
        action, provider, model = result
        app = self.app
        if action == "delete" and old_model:
            self.app.push_screen(
                ConfirmScreen(f"Remove `{old_model}` from the catalog?"),
                lambda confirmed: self._remove_model(old_model, confirmed),
            )
        elif action == "save":
            if old_model is None:
                app.run_control(
                    "model.add",
                    {"provider": provider, "model": model},
                    self._on_mutation,
                )
            else:
                app.run_control(
                    "model.update",
                    {"old_model": old_model, "provider": provider, "model": model},
                    self._on_mutation,
                )

    def _remove_model(self, model_id: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self.app.run_control("model.remove", {"model": model_id}, self._on_mutation)

    def _on_mutation(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self._status(str(result.get("error") or "Model change failed."))
            return
        data = result.get("data") or {}
        models = data.get("models")
        if models:
            self._models = models
            self._render_list()
            refresh = getattr(self.app, "refresh_models", None)
            if callable(refresh):
                refresh(models)
        self._status(str(data.get("message") or "Done."))

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class SettingsScreen(ModalScreen[None]):
    """Model, effort, context and runtime settings drawer."""

    _SLOT_LABELS = (("agent", "Agent"), ("subagent", "Subagent"), ("assistant", "Assistant"))
    _EFFORT_OPTIONS = (
        ("Auto", "auto"),
        ("Low", "low"),
        ("Medium", "medium"),
        ("High", "high"),
        ("XHigh", "xhigh"),
        ("Max", "max"),
    )

    def __init__(self, models: dict[str, Any], identity: dict[str, Any], compact: dict[str, Any]):
        super().__init__()
        self._models = models
        self._identity = identity
        self._compact = compact
        self._suppress_slot = False
        self._suppress_effort = False

    def compose(self) -> ComposeResult:
        choices = [str(m) for m in self._models.get("choices") or []]
        slots = self._models.get("slots") or {}
        effort = str(self._models.get("reasoning_effort") or "auto")
        with Vertical(id="settings-dialog"):
            yield Label("Settings", classes="dialog-title")
            with VerticalScroll(id="settings-scroll"):
                yield Static("Models", classes="settings-section")
                for slot, label in self._SLOT_LABELS:
                    slot_value = str(slots.get(slot) or "")
                    with Horizontal(classes="settings-row"):
                        yield Label(label)
                        yield Select(
                            [(m, m) for m in choices],
                            id=f"set-slot-{slot}",
                            allow_blank=True,
                            **({"value": slot_value} if slot_value in choices else {}),
                        )
                with Horizontal(classes="settings-row"):
                    yield Label("Effort")
                    yield Select(
                        list(self._EFFORT_OPTIONS),
                        id="set-effort",
                        value=effort if effort in {v for _, v in self._EFFORT_OPTIONS} else "auto",
                        allow_blank=False,
                    )
                with Horizontal(classes="settings-row"):
                    yield Label("Catalog")
                    yield Button("Manage models…", id="set-manage", variant="primary")
                yield Static("Context", classes="settings-section")
                yield Static(self._compact_text(), id="set-ctx")
                with Horizontal(classes="settings-row"):
                    yield Button("Refresh", id="set-ctx-refresh")
                    yield Button("Compact now", id="set-compact", variant="warning")
                yield Static("Runtime", classes="settings-section")
                yield Static(self._identity_text(), id="set-identity")
                with Horizontal(classes="settings-row"):
                    yield Button("Quit NanoCat", id="set-quit")
            yield Static("", id="settings-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close", id="set-close", variant="primary")

    def _compact_text(self) -> str:
        compact = self._compact or {}
        used = compact.get("estimated_prompt_tokens")
        window = compact.get("context_window_tokens")
        percent = compact.get("context_usage_percent")
        if used is None or window is None:
            return "Context status unavailable."
        return f"Context: {used} / {window} tokens ({float(percent or 0):.0f}%)"

    def _identity_text(self) -> str:
        identity = self._identity or {}
        return (
            f"Channel: {identity.get('channel', '-')} · Chat: {identity.get('chat_id', '-')}\n"
            f"Session: {identity.get('session_id', '-')} · Key: {identity.get('session_key', '-')}"
        )

    def _status(self, text: str) -> None:
        self.query_one("#settings-status", Static).update(text)

    def on_select_changed(self, event: Select.Changed) -> None:
        app = self.app
        select_id = event.select.id or ""
        if select_id.startswith("set-slot-"):
            if event.value is Select.BLANK or event.value is Select.NULL:
                return
            if self._suppress_slot:
                self._suppress_slot = False
                return
            slot = select_id[len("set-slot-") :]
            app.run_control(
                "model.select",
                {"slot": slot, "model": str(event.value)},
                self._on_model_changed,
            )
        elif select_id == "set-effort":
            if self._suppress_effort:
                self._suppress_effort = False
                return
            app.run_control(
                "model.set_effort",
                {"value": str(event.value)},
                self._on_model_changed,
            )

    def _on_model_changed(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self._status(f"Model change failed: {result.get('error', 'unknown error')}")
            return
        data = result.get("data") or {}
        models = data.get("models")
        if models:
            self._models = models
        message = data.get("message") or "Model settings updated."
        self._status(str(message))
        app = self.app
        refresh = getattr(app, "refresh_models", None)
        if callable(refresh) and models:
            refresh(models)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app = self.app
        button_id = event.button.id or ""
        if button_id == "set-close":
            self.dismiss(None)
        elif button_id == "set-manage":
            app.push_screen(
                ModelManagerScreen(self._models),
                lambda _result: self._refresh_after_manager(),
            )
        elif button_id == "set-ctx-refresh":
            app.run_control("compact.status", {}, self._on_compact_status)
        elif button_id == "set-compact":
            self._status("Compacting…")
            app.run_control("compact.run", {}, self._on_compact_run)
        elif button_id == "set-quit":
            confirm_quit = getattr(app, "confirm_quit", None)
            if callable(confirm_quit):
                confirm_quit()

    def _refresh_after_manager(self) -> None:
        """Reload the catalog into the slot selects after the manager closes."""
        self.app.run_control("models.state", {}, self._apply_models_state)

    def _apply_models_state(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            return
        models = (result.get("data") or {}).get("models") or {}
        if not models:
            return
        self._models = models
        choices = [str(m) for m in models.get("choices") or []]
        slots = models.get("slots") or {}
        for slot, _label in self._SLOT_LABELS:
            select = self.query_one(f"#set-slot-{slot}", Select)
            current = str(slots.get(slot) or "")
            desired = current if current in choices else Select.BLANK
            select.set_options([(m, m) for m in choices])
            if select.value != desired:
                self._suppress_slot = True
                select.value = desired

    def _on_compact_status(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self._status(f"Context status failed: {result.get('error', 'unknown error')}")
            return
        self._compact = (result.get("data") or {}).get("compact") or {}
        self.query_one("#set-ctx", Static).update(self._compact_text())

    def _on_compact_run(self, result: dict[str, Any]) -> None:
        if not result.get("ok"):
            self._status(f"Compaction failed: {result.get('error', 'unknown error')}")
            return
        data = result.get("data") or {}
        before = data.get("tokens_before")
        after = data.get("tokens_after")
        if data.get("changed"):
            self._status(f"Compacted: {before} → {after} tokens.")
        else:
            self._status(str(data.get("message") or "Nothing to compact."))
        refresh = getattr(self.app, "refresh_snapshot", None)
        if callable(refresh):
            refresh()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
