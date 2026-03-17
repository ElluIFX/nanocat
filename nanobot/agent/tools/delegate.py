"""Delegate tool for inline subagent execution with result collection."""

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


class DelegateTool(Tool):
    """Tool to delegate tasks to subagents and collect their results inline."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "delegate"

    @property
    def description(self) -> str:
        return (
            "Delegate one or more tasks to subagents that run concurrently. "
            "Unlike spawn (which runs in the background and resumes the conversation later), "
            "delegate BLOCKS until all subagents finish, then returns their results directly "
            "in the current turn. Use this when you need the results now to decide the next "
            "step, and want to offload work to avoid filling your own context. "
            "Ideal for parallel information gathering, multi-step research, or any set of "
            "independent subtasks whose outputs you need before proceeding."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": (
                        "List of subtasks to run concurrently. "
                        "All tasks start at the same time; results are collected once all finish."
                    ),
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Full description of the task for the subagent",
                            },
                            "label": {
                                "type": "string",
                                "description": "Short label for this task (for readability)",
                            },
                        },
                        "required": ["task"],
                    },
                },
            },
            "required": ["tasks"],
        }

    async def execute(self, tasks: list[dict[str, Any]], **kwargs: Any) -> str:
        """Run all tasks concurrently and return aggregated results."""
        task_tuples = [(t["task"], t.get("label")) for t in tasks]
        results = await self._manager.run_and_collect(task_tuples)

        parts = [f"Delegate results ({len(results)} task{'s' if len(results) != 1 else ''}):"]
        for i, r in enumerate(results, 1):
            status_tag = "OK" if r["status"] == "ok" else "ERROR"
            parts.append(f"\n[{i}] {r['label']} — {status_tag}\n{r['result']}")

        return "\n".join(parts)
