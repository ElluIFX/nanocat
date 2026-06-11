"""Gather tool for inline subagent execution with result collection."""

from typing import TYPE_CHECKING, Any

from nanocat.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanocat.agent.subagent import SubagentManager


class GatherTool(Tool):
    """Tool to gather results from concurrent subagents — blocks until all finish."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager

    @property
    def name(self) -> str:
        return "gather"

    @property
    def description(self) -> str:
        return (
            "Run one or more subtasks concurrently in subagents and return all results inline. "
            "BLOCKS until all finish — use when you need results before proceeding. "
            "Prefer over spawn when results are required now; prefer over doing it yourself to save context."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Subtasks to run concurrently. All start simultaneously; results collected when all finish.",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Full task description for the subagent",
                            },
                            "label": {
                                "type": "string",
                                "description": "Short display label",
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

        parts = [f"Gather results ({len(results)} task{'s' if len(results) != 1 else ''}):"]
        for i, r in enumerate(results, 1):
            status_tag = "OK" if r["status"] == "ok" else "ERROR"
            parts.append(f"\n[{i}] {r['label']} — {status_tag}\n{r['result']}")

        return "\n".join(parts)
