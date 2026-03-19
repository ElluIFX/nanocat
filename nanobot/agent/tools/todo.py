"""Todo tool for managing multi-step task lists within a session."""

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage
from nanobot.session.manager import Session

_SESSION_KEY = "_todo_lists"

_STATUS_ORDER = {"PENDING": 0, "INPROGRESS": 1, "COMPLETED": 2}
_STATUS_MARK = {"PENDING": "[ ]", "INPROGRESS": "[-]", "COMPLETED": "[x]"}


@dataclass
class TodoItem:
    index: int
    task: str
    status: str = "PENDING"  # PENDING | INPROGRESS | COMPLETED


@dataclass
class TodoList:
    id: str
    name: str
    tasks: list[TodoItem] = field(default_factory=list)


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _render_md(todo: TodoList) -> str:
    lines = [f"**{todo.name}** `#{todo.id}`"]
    for item in todo.tasks:
        lines.append(f"{_STATUS_MARK[item.status]} {item.index}. {item.task}")
    return "\n".join(lines)


def _get_store(session: Session) -> dict[str, Any]:
    if _SESSION_KEY not in session.metadata:
        session.metadata[_SESSION_KEY] = {}
    return session.metadata[_SESSION_KEY]


def _load(session: Session, todo_id: str) -> TodoList | None:
    store = _get_store(session)
    raw = store.get(todo_id)
    if raw is None:
        return None
    return TodoList(
        id=raw["id"],
        name=raw["name"],
        tasks=[TodoItem(**t) for t in raw["tasks"]],
    )


def _save(session: Session, todo: TodoList) -> None:
    store = _get_store(session)
    store[todo.id] = {
        "id": todo.id,
        "name": todo.name,
        "tasks": [asdict(t) for t in todo.tasks],
    }


class TodoTool(Tool):
    """Tool for managing structured todo lists within the current session.

    IMPORTANT: When the user asks you to perform any multi-step task, you MUST:
    1. First call todo/create to plan all steps before executing anything.
    2. Use todo/update to mark each step INPROGRESS before starting it, and COMPLETED when done.
    3. Use todo/append if new steps are discovered during execution.
    4. Call todo/complete when all tasks are done.

    Never start executing a multi-step task without first creating a todo list.
    Never ask the user "what should I do next?" mid-pipeline — update the todo and proceed.
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._session: Session | None = None

    def set_context(self, channel: str, chat_id: str, session: Session | None = None) -> None:
        self._default_channel = channel
        self._default_chat_id = chat_id
        if session is not None:
            self._session = session

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        self._send_callback = callback

    async def _notify(self, todo: TodoList) -> None:
        if self._send_callback and self._default_channel and self._default_chat_id:
            msg = OutboundMessage(
                channel=self._default_channel,
                chat_id=self._default_chat_id,
                content=_render_md(todo),
                media=[],
                metadata={},
            )
            try:
                await self._send_callback(msg)
            except Exception:
                pass

    @property
    def name(self) -> str:
        return "todo"

    @property
    def description(self) -> str:
        return (
            "Manage structured todo lists for multi-step tasks within the current session. "
            "IMPORTANT USAGE RULES:\n"
            "- Whenever the user requests a task with 2 or more steps, you MUST call todo with action=create "
            "to plan all steps BEFORE executing anything.\n"
            "- Mark each step INPROGRESS before starting it, and COMPLETED immediately after finishing.\n"
            "- Never interrupt the pipeline to ask the user what to do next — update the todo and keep going.\n"
            "- Call action=complete when all tasks are finished to close the list.\n\n"
            "Actions: create | check | update | append | complete"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "check", "update", "append", "complete"],
                    "description": "Action to perform",
                },
                "name": {
                    "type": "string",
                    "description": "create: todo list name",
                },
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "create: list of task descriptions",
                },
                "id": {
                    "type": "string",
                    "description": "check/update/append/complete: todo list ID",
                },
                "index": {
                    "type": "integer",
                    "description": "update: task index (1-based)",
                },
                "status": {
                    "type": "string",
                    "enum": ["PENDING", "INPROGRESS", "COMPLETED"],
                    "description": "update: new status (only upgrades allowed: PENDING→INPROGRESS→COMPLETED)",
                },
                "task": {
                    "type": "string",
                    "description": "append: task description to add",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        if self._session is None:
            return "Error: TodoTool has no session context"

        if action == "create":
            return await self._create(**kwargs)
        if action == "check":
            return await self._check(**kwargs)
        if action == "update":
            return await self._update(**kwargs)
        if action == "append":
            return await self._append(**kwargs)
        if action == "complete":
            return await self._complete(**kwargs)
        return f"Error: Unknown action '{action}'"

    async def _create(self, name: str | None = None, tasks: list[str] | None = None, **_: Any) -> str:
        if not name:
            return "Error: 'name' is required for create"
        if not tasks:
            return "Error: 'tasks' is required for create"
        todo = TodoList(
            id=_short_id(),
            name=name,
            tasks=[TodoItem(index=i + 1, task=t) for i, t in enumerate(tasks)],
        )
        _save(self._session, todo)
        await self._notify(todo)
        return f"Created todo list '{name}' with ID {todo.id} ({len(tasks)} tasks)"

    async def _check(self, id: str | None = None, **_: Any) -> str:
        if not id:
            return "Error: 'id' is required for check"
        todo = _load(self._session, id)
        if todo is None:
            return f"Error: Todo list '{id}' not found"
        return json.dumps(
            {"id": todo.id, "name": todo.name, "tasks": [asdict(t) for t in todo.tasks]},
            ensure_ascii=False,
        )

    async def _update(
        self, id: str | None = None, index: int | None = None, status: str | None = None, **_: Any
    ) -> str:
        if not id or index is None or not status:
            return "Error: 'id', 'index', and 'status' are required for update"
        todo = _load(self._session, id)
        if todo is None:
            return f"Error: Todo list '{id}' not found"
        item = next((t for t in todo.tasks if t.index == index), None)
        if item is None:
            return f"Error: Task index {index} not found"
        if _STATUS_ORDER.get(status, -1) <= _STATUS_ORDER.get(item.status, -1):
            return f"Error: Cannot downgrade status from {item.status} to {status}"
        item.status = status
        _save(self._session, todo)
        await self._notify(todo)
        return f"Task {index} updated to {status}"

    async def _append(self, id: str | None = None, task: str | None = None, **_: Any) -> str:
        if not id or not task:
            return "Error: 'id' and 'task' are required for append"
        todo = _load(self._session, id)
        if todo is None:
            return f"Error: Todo list '{id}' not found"
        new_index = max((t.index for t in todo.tasks), default=0) + 1
        todo.tasks.append(TodoItem(index=new_index, task=task))
        _save(self._session, todo)
        await self._notify(todo)
        return f"Appended task {new_index}: {task}"

    async def _complete(self, id: str | None = None, **_: Any) -> str:
        if not id:
            return "Error: 'id' is required for complete"
        todo = _load(self._session, id)
        if todo is None:
            return f"Error: Todo list '{id}' not found"
        incomplete = [t for t in todo.tasks if t.status != "COMPLETED"]
        if incomplete:
            names = ", ".join(f"#{t.index} {t.task}" for t in incomplete)
            return f"Error: {len(incomplete)} task(s) not completed: {names}"
        store = _get_store(self._session)
        store.pop(id, None)
        return f"Todo list '{todo.name}' completed and removed"
