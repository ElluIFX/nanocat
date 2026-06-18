"""Spawn tool for creating and managing background subagents."""

from typing import TYPE_CHECKING, Any

from nanocat.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanocat.agent.subagent import SubagentManager


class SpawnTool(Tool):
    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = f"{channel}:{chat_id}"

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn one or more subagents to run tasks in the background — returns immediately "
            "with per-task ids. Each subagent reports back when done. Use for fire-and-forget "
            "work that doesn't block the current turn. Use gather instead if you need results now."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string", "description": "Task for the subagent"},
                            "label": {"type": "string", "description": "Short display label"},
                        },
                        "required": ["task"],
                    },
                },
            },
            "required": ["tasks"],
        }

    async def execute(self, tasks: list[dict[str, Any]], **kwargs: Any) -> str:
        return await self._manager.spawn_many(
            tasks, self._origin_channel, self._origin_chat_id, self._session_key
        )


class SubagentListTool(Tool):
    def __init__(self, manager: "SubagentManager"):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "subagent_list"

    @property
    def description(self) -> str:
        return "List running background subagents (id, label, uptime)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return self._mgr.list()


class SubagentSteerTool(Tool):
    def __init__(self, manager: "SubagentManager"):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "subagent_steer"

    @property
    def description(self) -> str:
        return "Inject a message into a running subagent's conversation."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string"},
                "text": {"type": "string", "description": "Message to inject"},
            },
            "required": ["subagent_id", "text"],
        }

    async def execute(self, subagent_id: str, text: str, **kwargs: Any) -> str:
        return await self._mgr.steer(subagent_id, text)


class SubagentStopTool(Tool):
    def __init__(self, manager: "SubagentManager"):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "subagent_stop"

    @property
    def description(self) -> str:
        return "Stop a running subagent."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"subagent_id": {"type": "string"}},
            "required": ["subagent_id"],
        }

    async def execute(self, subagent_id: str, **kwargs: Any) -> str:
        return await self._mgr.stop(subagent_id)
