"""Structured application control service for rich channel adapters.

The TUI (and future rich adapters) call this service instead of injecting
slash-command text into the message bus. Every operation here reuses the same
business path as the textual command pipeline — ``CommandService`` /
``CommandRouter`` / ``RuntimeCommandHandlers`` / ``AgentLoop`` compatibility
callbacks — so there is exactly one implementation of each behavior.

All methods are coroutines and must run on the runtime event loop. Adapters
bridge from their own thread with ``asyncio.run_coroutine_threadsafe``.
Results are plain JSON-safe dicts; adapters never receive live service
objects.
"""

from __future__ import annotations

import shlex
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from loguru import logger

from nanocat.application.text_catalog import USER_TEXT
from nanocat.bus.events import InboundMessage
from nanocat.core.messages import ConversationRef

# ---------------------------------------------------------------------------
# Action catalog consumed by rich adapters (e.g. the TUI Action Center).
# Each entry describes one mouse-driven operation. ``command`` is a slash
# template executed through the normal command pipeline; ``control`` names a
# structured ApplicationControlService action instead.
# ---------------------------------------------------------------------------

_FIELD_TEXT = "text"
_FIELD_NUMBER = "number"
_FIELD_SELECT = "select"
_FIELD_FLAG = "flag"

ACTION_SPECS: tuple[dict[str, Any], ...] = (
    # -- session ------------------------------------------------------------
    {
        "id": "session.new",
        "label": "New session",
        "group": "Session",
        "summary": "Start a fresh conversation session",
        "control": "session.new",
    },
    {
        "id": "session.list",
        "label": "List sessions",
        "group": "Session",
        "summary": "Show recent sessions in the chat",
        "command": "/session list {limit}",
        "fields": (
            {"key": "limit", "label": "Limit", "kind": _FIELD_NUMBER, "default": "10"},
        ),
    },
    {
        "id": "session.view",
        "label": "View session…",
        "group": "Session",
        "summary": "Preview the last turns of a session",
        "command": "/session view {session_id}",
        "fields": (
            {"key": "session_id", "label": "Session ID", "kind": _FIELD_TEXT, "required": True},
        ),
    },
    # -- context ------------------------------------------------------------
    {
        "id": "compact.status",
        "label": "Context status",
        "group": "Context",
        "summary": "Show token budget and compaction state",
        "control": "compact.status",
    },
    {
        "id": "compact.run",
        "label": "Compact now",
        "group": "Context",
        "summary": "Compact older conversation history",
        "control": "compact.run",
        "confirm": True,
    },
    # -- model --------------------------------------------------------------
    {
        "id": "model.state",
        "label": "Model settings",
        "group": "Model",
        "summary": "Show active models and reasoning effort",
        "control": "models.state",
    },
    {
        "id": "model.add",
        "label": "Add model choice…",
        "group": "Model",
        "summary": "Add a provider/model pair to the model catalog",
        "command": "/model add {provider} {model}",
        "fields": (
            {"key": "provider", "label": "Provider", "kind": _FIELD_TEXT, "required": True},
            {"key": "model", "label": "Model name", "kind": _FIELD_TEXT, "required": True},
        ),
    },
    {
        "id": "model.delete",
        "label": "Delete model choice…",
        "group": "Model",
        "summary": "Remove a model from the catalog by number",
        "command": "/model delete {number}",
        "confirm": True,
        "fields": (
            {"key": "number", "label": "Choice number", "kind": _FIELD_NUMBER, "required": True},
        ),
    },
    # -- runtime ------------------------------------------------------------
    {
        "id": "runtime.stop_turn",
        "label": "Stop current turn",
        "group": "Runtime",
        "summary": "Cancel the running turn, subagents and pending approvals",
        "control": "turn.cancel",
    },
    {
        "id": "runtime.logs",
        "label": "Recent logs…",
        "group": "Runtime",
        "summary": "Show a snapshot of recent runtime logs",
        "command": "/logs {count}",
        "fields": (
            {"key": "count", "label": "Lines", "kind": _FIELD_NUMBER, "default": "12"},
        ),
    },
    {
        "id": "runtime.whoami",
        "label": "Identity",
        "group": "Runtime",
        "summary": "Show channel, chat and session routing IDs",
        "command": "/whoami",
    },
    {
        "id": "help",
        "label": "Command help…",
        "group": "Runtime",
        "summary": "Show the slash-command reference",
        "command": "/help {query}",
        "fields": (
            {"key": "query", "label": "Command or group (optional)", "kind": _FIELD_TEXT},
        ),
    },
    # -- cron ---------------------------------------------------------------
    {
        "id": "cron.list",
        "label": "List cron jobs",
        "group": "Schedule",
        "summary": "Show all scheduled jobs",
        "command": "/cron list",
    },
    {
        "id": "cron.show",
        "label": "Show cron job…",
        "group": "Schedule",
        "summary": "Show one job definition",
        "command": "/cron show {job_id}",
        "fields": (
            {"key": "job_id", "label": "Job ID", "kind": _FIELD_TEXT, "required": True},
        ),
    },
    {
        "id": "cron.add",
        "label": "Add cron job…",
        "group": "Schedule",
        "summary": "Schedule a recurring or one-shot instruction",
        "command": (
            "/cron add --message {message}"
            "{?name: --name {name}}"
            "{?every_seconds: --every-seconds {every_seconds}}"
            "{?cron_expr: --cron-expr {cron_expr}}"
            "{?tz: --tz {tz}}"
            "{?at: --at {at}}"
            "{?notify: --notify {notify}}"
            "{delete_after_run: --delete-after-run}"
        ),
        "fields": (
            {"key": "message", "label": "Instruction", "kind": _FIELD_TEXT, "required": True},
            {"key": "name", "label": "Name", "kind": _FIELD_TEXT},
            {"key": "every_seconds", "label": "Every N seconds", "kind": _FIELD_NUMBER},
            {"key": "cron_expr", "label": "Cron expression", "kind": _FIELD_TEXT},
            {"key": "tz", "label": "Timezone (for cron expr)", "kind": _FIELD_TEXT},
            {"key": "at", "label": "Run at (ISO datetime)", "kind": _FIELD_TEXT},
            {
                "key": "notify",
                "label": "Notify",
                "kind": _FIELD_SELECT,
                "options": ("never", "always", "smart"),
            },
            {"key": "delete_after_run", "label": "Delete after run", "kind": _FIELD_FLAG},
        ),
    },
    {
        "id": "cron.run",
        "label": "Run cron job now…",
        "group": "Schedule",
        "summary": "Trigger a job immediately",
        "command": "/cron run {job_id}",
        "fields": (
            {"key": "job_id", "label": "Job ID", "kind": _FIELD_TEXT, "required": True},
        ),
    },
    {
        "id": "cron.enable",
        "label": "Enable cron job…",
        "group": "Schedule",
        "summary": "Enable a scheduled job",
        "command": "/cron enable {job_id}",
        "fields": (
            {"key": "job_id", "label": "Job ID", "kind": _FIELD_TEXT, "required": True},
        ),
    },
    {
        "id": "cron.disable",
        "label": "Disable cron job…",
        "group": "Schedule",
        "summary": "Disable a scheduled job",
        "command": "/cron disable {job_id}",
        "fields": (
            {"key": "job_id", "label": "Job ID", "kind": _FIELD_TEXT, "required": True},
        ),
    },
    {
        "id": "cron.remove",
        "label": "Remove cron job…",
        "group": "Schedule",
        "summary": "Delete a scheduled job",
        "command": "/cron remove {job_id}",
        "confirm": True,
        "fields": (
            {"key": "job_id", "label": "Job ID", "kind": _FIELD_TEXT, "required": True},
        ),
    },
    # -- memory -------------------------------------------------------------
    {
        "id": "memory.status",
        "label": "Memory status",
        "group": "Memory",
        "summary": "Show Nowledge service and configuration state",
        "command": "/memory status",
    },
    {
        "id": "memory.processing",
        "label": "Memory processing",
        "group": "Memory",
        "summary": "Show background memory processing state",
        "command": "/memory processing",
    },
    {
        "id": "memory.spaces",
        "label": "Memory spaces",
        "group": "Memory",
        "summary": "List Nowledge spaces",
        "command": "/memory spaces",
    },
    {
        "id": "memory.search",
        "label": "Search memory…",
        "group": "Memory",
        "summary": "Search stored memories",
        "command": "/memory search {query}{?limit: --limit {limit}}{?mode: --mode {mode}}{?space_id: --space-id {space_id}}",
        "fields": (
            {"key": "query", "label": "Query", "kind": _FIELD_TEXT, "required": True},
            {"key": "limit", "label": "Limit", "kind": _FIELD_NUMBER, "default": "5"},
            {
                "key": "mode",
                "label": "Mode",
                "kind": _FIELD_SELECT,
                "options": ("fast", "deep"),
            },
            {"key": "space_id", "label": "Space ID", "kind": _FIELD_TEXT},
        ),
    },
    {
        "id": "memory.show",
        "label": "Show memory…",
        "group": "Memory",
        "summary": "Show one memory entry",
        "command": "/memory show {memory_id}{?space_id: --space-id {space_id}}",
        "fields": (
            {"key": "memory_id", "label": "Memory ID", "kind": _FIELD_TEXT, "required": True},
            {"key": "space_id", "label": "Space ID", "kind": _FIELD_TEXT},
        ),
    },
    {
        "id": "memory.add",
        "label": "Add memory…",
        "group": "Memory",
        "summary": "Store a new memory",
        "command": (
            "/memory add --content {content}"
            "{?title: --title {title}}"
            "{?importance: --importance {importance}}"
            "{?labels: --labels {labels}}"
            "{?unit_type: --unit-type {unit_type}}"
            "{?space_id: --space-id {space_id}}"
        ),
        "fields": (
            {"key": "content", "label": "Content", "kind": _FIELD_TEXT, "required": True},
            {"key": "title", "label": "Title", "kind": _FIELD_TEXT},
            {"key": "importance", "label": "Importance (0-1)", "kind": _FIELD_NUMBER},
            {"key": "labels", "label": "Labels (comma separated)", "kind": _FIELD_TEXT},
            {"key": "unit_type", "label": "Unit type", "kind": _FIELD_TEXT},
            {"key": "space_id", "label": "Space ID", "kind": _FIELD_TEXT},
        ),
    },
    {
        "id": "memory.update",
        "label": "Update memory…",
        "group": "Memory",
        "summary": "Patch an existing memory",
        "command": (
            "/memory update {memory_id}"
            "{?content: --content {content}}"
            "{?title: --title {title}}"
            "{?importance: --importance {importance}}"
            "{?labels: --labels {labels}}"
            "{?unit_type: --unit-type {unit_type}}"
            "{?space_id: --space-id {space_id}}"
        ),
        "fields": (
            {"key": "memory_id", "label": "Memory ID", "kind": _FIELD_TEXT, "required": True},
            {"key": "content", "label": "Content", "kind": _FIELD_TEXT},
            {"key": "title", "label": "Title", "kind": _FIELD_TEXT},
            {"key": "importance", "label": "Importance (0-1)", "kind": _FIELD_NUMBER},
            {"key": "labels", "label": "Labels (comma separated)", "kind": _FIELD_TEXT},
            {"key": "unit_type", "label": "Unit type", "kind": _FIELD_TEXT},
            {"key": "space_id", "label": "Space ID", "kind": _FIELD_TEXT},
        ),
    },
    {
        "id": "memory.delete",
        "label": "Delete memory…",
        "group": "Memory",
        "summary": "Delete a memory entry",
        "command": "/memory delete {memory_id}{?space_id: --space-id {space_id}}",
        "confirm": True,
        "fields": (
            {"key": "memory_id", "label": "Memory ID", "kind": _FIELD_TEXT, "required": True},
            {"key": "space_id", "label": "Space ID", "kind": _FIELD_TEXT},
        ),
    },
    {
        "id": "memory.preview",
        "label": "Preview distillation",
        "group": "Memory",
        "summary": "Preview distilling the current session thread",
        "command": "/memory preview{?space_id: --space-id {space_id}}",
        "fields": (
            {"key": "space_id", "label": "Space ID", "kind": _FIELD_TEXT},
        ),
    },
    {
        "id": "memory.distill",
        "label": "Distill session",
        "group": "Memory",
        "summary": "Distill the current session thread into memory",
        "command": "/memory distill{?space_id: --space-id {space_id}}",
        "confirm": True,
        "fields": (
            {"key": "space_id", "label": "Space ID", "kind": _FIELD_TEXT},
        ),
    },
)


def build_command_text(template: str, values: Mapping[str, Any]) -> str:
    """Expand a slash template with shlex-quoted form values.

    ``{key}`` substitutes a quoted value. ``{?key: ...}`` includes the inner
    text only when *key* has a non-empty value. ``{flag: ...}`` includes the
    inner text only when the flag value is truthy.
    """
    import re

    def _quote(value: Any) -> str:
        return shlex.quote(str(value))

    def _optional(match: "re.Match[str]") -> str:
        key, inner = match.group(1), match.group(2)
        value = values.get(key)
        if value is None or str(value).strip() == "":
            return ""
        return _substitute(inner)

    def _flag(match: "re.Match[str]") -> str:
        key, inner = match.group(1), match.group(2)
        return _substitute(inner) if values.get(key) else ""

    def _plain(match: "re.Match[str]") -> str:
        key = match.group(1)
        value = values.get(key)
        return _quote(value) if value is not None else ""

    def _substitute(text: str) -> str:
        text = re.sub(r"\{(\w+)\}", _plain, text)
        return text

    # One level of nesting: option/flag bodies contain `{key}` placeholders.
    text = re.sub(r"\{\?(\w+):((?:[^{}]|\{\w+\})*)\}", _optional, template)
    text = re.sub(r"\{(\w+):((?:[^{}]|\{\w+\})*)\}", _flag, text)
    text = _substitute(text)
    return " ".join(text.split())


def _jsonable(value: Any) -> Any:
    """Convert dataclasses / mappings / sequences into JSON-safe values."""
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _ok(data: Mapping[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "data": _jsonable(dict(data or {})), **extra}


def _err(message: str, *, code: str = "control_error", **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": message, **extra}


class ApplicationControlService:
    """Structured query/action facade shared by rich channel adapters.

    The service deliberately delegates to the existing command pipeline and
    the ``AgentLoop`` compatibility engine; it owns no business logic of its
    own. It never exposes live objects to adapters — only JSON-safe dicts.
    """

    def __init__(
        self,
        *,
        engine: Any,
        config: Any,
        session_manager: Any,
        intervention: Any | None,
        supervisor: Any | None,
    ) -> None:
        self._engine = engine
        self._config = config
        self._sessions = session_manager
        self._intervention = intervention
        self._supervisor = supervisor

    def set_supervisor(self, supervisor: Any) -> None:
        """Bind the runtime supervisor once the composition root creates it."""
        self._supervisor = supervisor

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    async def execute(self, action: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Run one structured control action on the runtime loop."""
        handler = getattr(self, f"_action_{action.replace('.', '_')}", None)
        if handler is None:
            return _err(f"Unknown control action: {action}", code="unknown_action")
        try:
            return await handler(dict(params))
        except Exception as e:
            logger.exception("Control action {} failed", action)
            return _err(str(e) or type(e).__name__)

    @staticmethod
    def action_catalog() -> list[dict[str, Any]]:
        """Return the adapter-facing action catalog (Action Center entries)."""
        return _jsonable(list(ACTION_SPECS))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _conversation(self, params: Mapping[str, Any]) -> ConversationRef:
        return ConversationRef(
            channel=str(params.get("channel") or "tui"),
            chat_id=str(params.get("chat_id") or "local"),
        )

    def _session_key(self, conversation: ConversationRef) -> str:
        return conversation.session_key

    def _active_session(self, conversation: ConversationRef) -> Any:
        return self._sessions.get_or_create(conversation.channel, conversation.chat_id)

    def _session_busy(self, session_key: str) -> bool:
        checker = getattr(self._engine, "is_session_busy", None)
        if callable(checker):
            return bool(checker(session_key))
        tasks = getattr(self._engine, "_active_tasks", {}).get(session_key, [])
        return any(not task.done() for task in tasks)

    def _session_display_events(self, session: Any) -> list[dict[str, Any]]:
        """Flatten session history into adapter-ready display events."""
        from nanocat.channels.tui_app.events import (
            flatten_content,
            parse_subagent_result,
        )

        events: list[dict[str, Any]] = []
        for msg in session.get_history():
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = flatten_content(msg.get("content")).strip()
            if not text:
                continue
            if role == "user":
                sub = parse_subagent_result(text)
                if sub is not None:
                    events.append({"kind": "subagent", "payload": _jsonable(sub)})
                else:
                    events.append({"kind": "user", "text": text})
            elif msg.get("tool_calls"):
                events.append({"kind": "progress", "text": text})
            else:
                events.append({"kind": "bot", "text": text})
        return events

    def _session_brief(self, item: Mapping[str, Any], active_id: str | None) -> dict[str, Any]:
        return {
            "id": str(item.get("id") or ""),
            "name": item.get("name") or "",
            "chat_id": str(item.get("chat_id") or ""),
            "last_active": str(item.get("last_active") or ""),
            "created_at": str(item.get("created_at") or ""),
            "message_count": int(item.get("message_count") or 0),
            "turn_count": int(item.get("turn_count") or 0),
            "active": item.get("id") == active_id,
        }

    def _model_state(self) -> dict[str, Any]:
        defaults = self._config.agents.defaults
        return {
            "choices": list(defaults.model_choice or []),
            "slots": {
                "agent": defaults.model,
                "subagent": defaults.subagent_model or "",
                "assistant": defaults.assistant_model or "",
            },
            "effective": {
                "agent": self._engine.model,
                "subagent": self._engine.subagent_model,
                "assistant": self._engine.assistant_model,
            },
            "reasoning_effort": defaults.reasoning_effort or "auto",
            "provider": self._config.get_provider_name(self._engine.model) or "unknown",
        }

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    async def _action_runtime_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session = self._active_session(conversation)
        session_key = self._session_key(conversation)
        principal_id = str(params.get("principal_id") or "local")
        yolo = False
        pending = 0
        if self._intervention is not None:
            yolo = self._intervention.has_session_grant(conversation, principal_id, "tool:*")
            pending = 1 if self._intervention.current_pending(conversation, principal_id) else 0
        compact = self._engine.memory_compactor.status(session)
        active_id = session.id
        items = self._sessions.list_sessions(
            conversation.channel, min_turns=0, limit=int(params.get("limit") or 30)
        )
        return _ok(
            {
                "identity": {
                    "channel": conversation.channel,
                    "chat_id": conversation.chat_id,
                    "session_id": session.id,
                    "session_name": session.name or "",
                    "session_key": session.key,
                    "principal_id": principal_id,
                },
                "busy": self._session_busy(session_key),
                "approval": {"yolo": yolo, "pending": pending},
                "models": self._model_state(),
                "compact": {
                    "estimated_prompt_tokens": compact["estimated_prompt_tokens"],
                    "context_window_tokens": compact["context_window_tokens"],
                    "context_usage_percent": compact["context_usage_percent"],
                    "compaction_available": compact["compaction_available"],
                },
                "sessions": [self._session_brief(item, active_id) for item in items],
                "catalog": self.action_catalog(),
            }
        )

    async def _action_sessions_list(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session = self._active_session(conversation)
        limit = int(params.get("limit") or 50)
        min_turns = int(params.get("min_turns") or 0)
        items = self._sessions.list_sessions(conversation.channel, min_turns=min_turns, limit=limit)
        return _ok({"sessions": [self._session_brief(i, session.id) for i in items]})

    async def _action_sessions_get(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session_id = str(params.get("session_id") or "").strip()
        target = self._sessions.get_session(conversation.channel, session_id)
        if target is None:
            return _err(f"Session `{session_id}` not found.", code="not_found")
        preview = self._engine._format_session_turns(target)
        return _ok({"session": {"id": target.id, "name": target.name or ""}, "preview": preview})

    async def _action_sessions_history(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session_id = str(params.get("session_id") or "").strip()
        target = self._sessions.get_session(conversation.channel, session_id)
        if target is None:
            return _err(f"Session `{session_id}` not found.", code="not_found")
        return _ok(
            {
                "session_id": target.id,
                "session_name": target.name or "",
                "events": self._session_display_events(target),
            }
        )

    async def _action_models_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return _ok({"models": self._model_state()})

    async def _action_compact_status(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session = self._active_session(conversation)
        status = dict(self._engine.memory_compactor.status(session))
        checkpoint = status.get("checkpoint")
        if checkpoint is not None and hasattr(checkpoint, "to_dict"):
            status["checkpoint"] = checkpoint.to_dict()
        else:
            status["checkpoint"] = _jsonable(checkpoint)
        return _ok({"compact": status})

    async def _action_logs_tail(self, params: dict[str, Any]) -> dict[str, Any]:
        count = max(1, min(int(params.get("count") or 12), 200))
        lines = list(getattr(self._engine, "_recent_logs", []))
        return _ok(
            {
                "busy": bool(self._engine._any_session_busy()),
                "lines": lines[-count:],
            }
        )

    # ------------------------------------------------------------------
    # session actions
    # ------------------------------------------------------------------

    async def _action_session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session_key = self._session_key(conversation)
        if not self._engine.try_reserve_session_operation(session_key):
            return _err(USER_TEXT.command_idle_only, code="invalid_state")
        try:
            old = self._active_session(conversation)
            if self._intervention is not None:
                await self._intervention.cancel_session(session_key)
            old.metadata.pop("nowledge_thread_id", None)
            new = self._sessions._new_session(conversation.channel, conversation.chat_id)
            self._sessions.save(new)  # persist immediately so list/rename can see it
            engine = self._engine
            engine._pending_buf.pop(session_key, None)
            engine._session_gen.pop(session_key, None)
            return _ok(
                {
                    "session_id": new.id,
                    "previous_session_id": old.id,
                    "message": "New session started.",
                }
            )
        finally:
            self._engine.release_session_operation(session_key)

    async def _action_session_switch(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            return _err("Missing session_id", code="invalid_argument")
        session_key = self._session_key(conversation)
        if not self._engine.try_reserve_session_operation(session_key):
            return _err(USER_TEXT.command_idle_only, code="invalid_state")
        try:
            ok = self._sessions.set_active(conversation.channel, conversation.chat_id, session_id)
            if not ok:
                return _err(f"Session `{session_id}` not found.", code="not_found")
            if self._intervention is not None:
                await self._intervention.cancel_session(session_key)
            target = self._sessions.get_session(conversation.channel, session_id)
            return _ok(
                {
                    "session_id": session_id,
                    "session_name": (target.name if target else None) or "",
                    "events": self._session_display_events(target) if target else [],
                    "message": f"Switched to session `{session_id}`.",
                }
            )
        finally:
            self._engine.release_session_operation(session_key)

    async def _action_session_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session_key = self._session_key(conversation)
        if not self._engine.try_reserve_session_operation(session_key):
            return _err(USER_TEXT.command_idle_only, code="invalid_state")
        session_id = str(params.get("session_id") or "").strip()
        name = str(params.get("name") or "").strip() or None
        try:
            if self._sessions.get_session(conversation.channel, session_id) is None:
                return _err(f"Session `{session_id}` not found.", code="not_found")
            self._sessions.set_name(conversation.channel, session_id, name)
            return _ok({"session_id": session_id, "name": name or ""})
        finally:
            self._engine.release_session_operation(session_key)

    async def _action_session_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session_id = str(params.get("session_id") or "").strip()
        session_key = self._session_key(conversation)
        active = self._active_session(conversation)
        if not self._engine.try_reserve_session_operation(session_key):
            return _err(USER_TEXT.command_idle_only, code="invalid_state")
        try:
            return await self._delete_session(conversation, session_id, session_key, active)
        finally:
            self._engine.release_session_operation(session_key)

    async def _delete_session(
        self,
        conversation: ConversationRef,
        session_id: str,
        session_key: str,
        active: Any,
    ) -> dict[str, Any]:
        replacement_id = None
        if active.id == session_id:
            if self._intervention is not None:
                await self._intervention.cancel_session(session_key)
            active.metadata.pop("nowledge_thread_id", None)
            replacement = self._sessions._new_session(conversation.channel, conversation.chat_id)
            self._sessions.save(replacement)
            replacement_id = replacement.id
            self._engine._pending_buf.pop(session_key, None)
            self._engine._session_gen.pop(session_key, None)
        if not self._sessions.delete_session(conversation.channel, session_id):
            return _err(f"Session `{session_id}` could not be deleted.", code="delete_failed")
        return _ok(
            {
                "deleted": session_id,
                "replacement_session_id": replacement_id,
                "events": [] if replacement_id else None,
                "message": f"Session `{session_id}` deleted.",
            }
        )

    # ------------------------------------------------------------------
    # model actions
    # ------------------------------------------------------------------

    async def _action_model_select(self, params: dict[str, Any]) -> dict[str, Any]:
        from nanocat.config.loader import save_config

        slot = str(params.get("slot") or "").strip().lower()
        model = str(params.get("model") or "").strip()
        defaults = self._config.agents.defaults
        if slot not in {"agent", "subagent", "assistant"}:
            return _err("Slot must be one of agent|subagent|assistant", code="invalid_argument")
        if model not in (defaults.model_choice or []):
            return _err(f"Model `{model}` is not in the model catalog.", code="invalid_argument")
        if slot == "agent":
            defaults.model = model
        elif slot == "subagent":
            defaults.subagent_model = model
        else:
            defaults.assistant_model = model
        save_config(self._config)
        return _ok({"models": self._model_state(), "message": f"{slot.title()} model → `{model}`"})

    async def _action_model_set_effort(self, params: dict[str, Any]) -> dict[str, Any]:
        from nanocat.config.loader import save_config

        requested = str(params.get("value") or "").strip().casefold()
        clear = requested in {"auto", "none", "off"}
        if not clear and requested not in {"low", "medium", "high", "xhigh", "max"}:
            return _err(
                "Effort must be one of: auto, low, medium, high, xhigh, max",
                code="invalid_argument",
            )
        effort = None if clear else requested
        self._config.agents.defaults.reasoning_effort = effort
        save_config(self._config)
        self._engine.provider_resolver.update_reasoning_effort(effort)
        return _ok(
            {
                "models": self._model_state(),
                "message": f"Reasoning effort set to `{effort or 'auto'}`.",
            }
        )

    # ------------------------------------------------------------------
    # model catalog actions (stable model identity, no list indices)
    # ------------------------------------------------------------------

    def _slot_references(self, model: str) -> list[str]:
        """Return the slot names currently pointing at *model*."""
        defaults = self._config.agents.defaults
        refs = []
        if defaults.model == model:
            refs.append("agent")
        if defaults.subagent_model == model:
            refs.append("subagent")
        if defaults.assistant_model == model:
            refs.append("assistant")
        if getattr(defaults, "vision_model", None) == model:
            refs.append("vision")
        if getattr(defaults, "compaction_model", None) == model:
            refs.append("compaction")
        return refs

    async def _action_model_add(self, params: dict[str, Any]) -> dict[str, Any]:
        from nanocat.config.loader import save_config

        provider = str(params.get("provider") or "").strip()
        model = str(params.get("model") or "").strip()
        if not provider or not model:
            return _err("Provider and model name are both required.", code="invalid_argument")
        full = f"{provider}/{model}"
        choices = self._config.agents.defaults.model_choice
        if full not in choices:
            choices.append(full)
            save_config(self._config)
        return _ok({"models": self._model_state(), "message": f"Added `{full}`."})

    async def _action_model_update(self, params: dict[str, Any]) -> dict[str, Any]:
        """Edit one catalog entry in place; slot references follow the rename."""
        from nanocat.config.loader import save_config

        old = str(params.get("old_model") or "").strip()
        provider = str(params.get("provider") or "").strip()
        model = str(params.get("model") or "").strip()
        choices = self._config.agents.defaults.model_choice
        if old not in choices:
            return _err(f"Model `{old}` is not in the catalog.", code="not_found")
        if not provider or not model:
            return _err("Provider and model name are both required.", code="invalid_argument")
        new = f"{provider}/{model}"
        if new != old and new in choices:
            return _err(f"Model `{new}` already exists.", code="conflict")
        defaults = self._config.agents.defaults
        for slot in self._slot_references(old):
            if slot == "agent":
                defaults.model = new
            elif slot == "subagent":
                defaults.subagent_model = new
            elif slot == "assistant":
                defaults.assistant_model = new
            elif slot == "vision":
                defaults.vision_model = new
            elif slot == "compaction":
                defaults.compaction_model = new
        choices[choices.index(old)] = new
        save_config(self._config)
        return _ok({"models": self._model_state(), "message": f"Updated `{old}` → `{new}`."})

    async def _action_model_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        from nanocat.config.loader import save_config

        model = str(params.get("model") or "").strip()
        choices = self._config.agents.defaults.model_choice
        if model not in choices:
            return _err(f"Model `{model}` is not in the catalog.", code="not_found")
        if len(choices) <= 1:
            return _err("Can't remove the last model choice.", code="invalid_state")
        refs = self._slot_references(model)
        if refs:
            return _err(
                f"`{model}` is assigned to: {', '.join(refs)}. Reassign those slots first.",
                code="invalid_state",
            )
        choices.remove(model)
        save_config(self._config)
        return _ok({"models": self._model_state(), "message": f"Removed `{model}`."})

    # ------------------------------------------------------------------
    # context actions
    # ------------------------------------------------------------------

    async def _action_compact_run(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        session_key = self._session_key(conversation)
        if not self._engine.try_reserve_session_operation(session_key):
            return _err(USER_TEXT.command_idle_only, code="invalid_state")
        session = self._active_session(conversation)
        try:
            compactor = self._engine.memory_compactor
            before = compactor.status(session)
            changed = await compactor.maybe_compact_by_tokens(session, force=True)
            after = compactor.status(session)
            checkpoint = after.get("checkpoint")
            checkpoint_data = checkpoint.to_dict() if checkpoint is not None else None
            return _ok(
                {
                    "changed": bool(changed),
                    "tokens_before": before["estimated_prompt_tokens"],
                    "tokens_after": after["estimated_prompt_tokens"],
                    "context_usage_percent": after["context_usage_percent"],
                    "checkpoint": checkpoint_data,
                    "failure_count": after["failure_count"],
                    "message": (
                        "Compaction completed."
                        if changed
                        else "Nothing to compact; original history was kept."
                    ),
                }
            )
        finally:
            self._engine.release_session_operation(session_key)

    # ------------------------------------------------------------------
    # runtime actions
    # ------------------------------------------------------------------

    async def _action_turn_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        engine = self._engine
        dispatcher = getattr(engine, "command_dispatcher", None)
        if dispatcher is None:
            return _err("Command dispatcher is unavailable.", code="invalid_state")
        msg = InboundMessage(
            channel=conversation.channel,
            sender_id=str(params.get("principal_id") or "local"),
            principal_id=str(params.get("principal_id") or "local"),
            chat_id=conversation.chat_id,
            content="/stop",
        )
        response = await dispatcher.execute(msg, publish=False)
        return _ok({"message": response.content if response else ""})

    async def _action_runtime_restart(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        if self._supervisor is None:
            return _err("Runtime supervisor is unavailable.", code="invalid_state")
        if self._intervention is not None:
            await self._intervention.cancel_session(self._session_key(conversation))
        self._engine.turns.cancel_session(self._session_key(conversation), "restart requested")
        await self._supervisor.request_restart(conversation.channel, conversation.chat_id)
        return _ok({"accepted": True, "message": "Restarting NanoCat, will be back soon..."})

    # ------------------------------------------------------------------
    # approval actions (delegate to the single intervention business path)
    # ------------------------------------------------------------------

    async def _action_approval_respond(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        principal_id = str(params.get("principal_id") or "local")
        action = str(params.get("approval_action") or "").strip().lower()
        command = {
            "once": "/approve once",
            "turn": "/approve turn",
            "forever": "/approve forever",
            "cancel": "/approve cancel",
            "deny": "/deny",
        }.get(action)
        if command is None:
            return _err(f"Unknown approval action: {action}", code="invalid_argument")
        msg = InboundMessage(
            channel=conversation.channel,
            sender_id=principal_id,
            principal_id=principal_id,
            chat_id=conversation.chat_id,
            content=command,
        )
        dispatcher = getattr(self._engine, "command_dispatcher", None)
        response = (
            await dispatcher.execute(msg, publish=False)
            if dispatcher is not None
            else await self._engine.command_service.dispatch_intervention(msg)
        )
        state = (response.metadata or {}).get("intervention_state") if response else None
        mode = (response.metadata or {}).get("_intervention_mode") if response else None
        return _ok(
            {
                "state": state or "unknown",
                "mode": mode or "",
                "message": response.content if response else "",
            }
        )

    # ------------------------------------------------------------------
    # generic command execution (Action Center slash templates)
    # ------------------------------------------------------------------

    async def _action_command_execute(self, params: dict[str, Any]) -> dict[str, Any]:
        conversation = self._conversation(params)
        principal_id = str(params.get("principal_id") or "local")
        text = str(params.get("text") or "").strip()
        if not text.startswith("/"):
            return _err("Command text must start with '/'.", code="invalid_argument")

        engine = self._engine
        msg = InboundMessage(
            channel=conversation.channel,
            sender_id=principal_id,
            principal_id=principal_id,
            chat_id=conversation.chat_id,
            content=text,
        )

        inspection = engine.command_router.inspect(text)
        command_name = inspection.spec.name if inspection.spec is not None else None
        if command_name in {"stop", "restart"}:
            return _err(
                f"`/{command_name}` must be executed through its structured control action.",
                code="invalid_argument",
            )

        dispatcher = getattr(engine, "command_dispatcher", None)
        if dispatcher is None:
            return _err("Command dispatcher is unavailable.", code="invalid_state")
        response = await dispatcher.execute(msg, publish=False)
        if response is None:
            return _err(f"Command `{text}` was not handled.", code="not_handled")
        return _ok({"content": response.content, "routed": "command"})
