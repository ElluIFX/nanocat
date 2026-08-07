"""Nowledge Mem built-in tools for semantic memory search and management."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanocat.agent.nowledge_client import NowledgeRequestError
from nanocat.agent.tools.base import Tool, tool_err, tool_ok

if TYPE_CHECKING:
    from nanocat.agent.nowledge_client import NowledgeClient


def _clean_search_result(r: dict) -> dict:
    """Strip low-signal fields from a Nowledge search result entry."""
    mem = r.get("memory") or {}
    entities = r.get("related_entities") or []
    out: dict = {}
    for key in (
        "id",
        "title",
        "created_at",
        "updated_at",
        "content",
        "importance",
        "confidence",
        "unit_type",
        "temporal_context",
        "space_id",
        "source",
        "source_thread_id",
        "source_message_indices",
        "source_message_range",
        "event_start",
        "event_end",
        "is_latest",
        "version",
        "is_crystal",
    ):
        if mem.get(key) is not None:
            if key == "updated_at" and mem.get("created_at") == mem.get("updated_at"):
                continue
            out[key] = mem[key]
    if r.get("similarity_score") is not None:
        out["similarity_score"] = r["similarity_score"]
    if r.get("relevance_reason"):
        out["relevance_reason"] = r["relevance_reason"]
    if r.get("related_memory_links"):
        out["related_memory_links"] = r["related_memory_links"]
    if entities:
        cleaned_entities = []
        for e in entities:
            entry = {}
            for key in ("name", "entity_type", "description", "confidence"):
                if e.get(key) is not None:
                    entry[key] = e[key]
            if entry:
                cleaned_entities.append(entry)
        if cleaned_entities:
            out["related_entities"] = cleaned_entities
    return out


class MemorySearchTool(Tool):
    def __init__(self, client: NowledgeClient):
        self._client = client

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search long-term memories in Nowledge Mem using a semantic query. "
            "Use this to recall past decisions, facts, preferences, or insights. "
            "Search before answering questions about past work or context."
            "The json result will be cleaned before returned."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-20).",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "deep"],
                    "description": "Search mode; use deep for historical or time-sensitive recall.",
                    "default": "fast",
                },
                "space_id": {
                    "type": "string",
                    "description": "Optional Nowledge Space to search.",
                },
                "unit_type": {
                    "type": "string",
                    "description": "Optional memory type filter.",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        limit: int = 5,
        mode: str = "fast",
        space_id: str | None = None,
        unit_type: str | None = None,
        **_: Any,
    ) -> str:
        try:
            results = await self._client.search_memories(
                query=query,
                limit=limit,
                mode=mode,
                space_id=space_id,
                unit_type=unit_type,
            )
        except NowledgeRequestError as exc:
            return tool_err("Nowledge memory search failed", code=exc.code)
        return tool_ok(results=[_clean_search_result(r) for r in results])


class MemoryGetTool(Tool):
    def __init__(self, client: NowledgeClient):
        self._client = client

    @property
    def name(self) -> str:
        return "memory_get"

    @property
    def description(self) -> str:
        return "Get complete content of a memory by ID from Nowledge Mem."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory to get.",
                },
                "space_id": {
                    "type": "string",
                    "description": "Optional Space guard for the lookup.",
                },
            },
            "required": ["memory_id"],
        }

    async def execute(self, memory_id: str, space_id: str | None = None, **_: Any) -> str:
        try:
            result = await self._client.get_memory(memory_id, space_id=space_id)
        except NowledgeRequestError as exc:
            return tool_err("Nowledge memory lookup failed", code=exc.code)
        if not result:
            return tool_err("Failed to get memory.")
        return tool_ok(memory=result)


class MemoryAddTool(Tool):
    def __init__(self, client: NowledgeClient):
        self._client = client

    @property
    def name(self) -> str:
        return "memory_add"

    @property
    def description(self) -> str:
        return (
            "Save a new memory to Nowledge Mem. "
            "Use for important decisions, facts, preferences, or insights worth remembering across sessions. "
            "Before adding, consider searching first to avoid creating near-duplicates."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory content (markdown supported). Must be self-contained.",
                },
                "title": {
                    "type": "string",
                    "description": "Short descriptive title for the memory.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Categorization labels. E.g.: "
                        "decision, preference, fact, procedure, insight, experience."
                    ),
                },
                "importance": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 1.0,
                    "description": (
                        "Relevance score: 0.8-1.0 critical, 0.4-0.7 useful, 0.1-0.3 background context."
                    ),
                },
            },
            "required": ["content"],
        }

    async def execute(
        self,
        content: str,
        title: str | None = None,
        labels: list[str] | None = None,
        importance: float = 0.5,
        **_: Any,
    ) -> str:
        try:
            result = await self._client.create_memory(
                content=content,
                title=title or None,
                labels=labels or None,
                importance=importance,
            )
        except NowledgeRequestError as exc:
            return tool_err("Nowledge memory creation failed", code=exc.code)
        if not result:
            return tool_err("Failed to save memory (Nowledge Mem may be unavailable).")
        mem_id = result.get("id") or result.get("memory_id") or "unknown"
        return tool_ok(id=mem_id)


class MemoryUpdateTool(Tool):
    def __init__(self, client: NowledgeClient):
        self._client = client

    @property
    def name(self) -> str:
        return "memory_update"

    @property
    def description(self) -> str:
        return (
            "Update an existing memory in Nowledge Mem by its ID. "
            "Use when new information supplements, corrects, or supersedes a known memory "
            "rather than creating a new duplicate entry."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory to update (from memory_search results).",
                },
                "content": {
                    "type": "string",
                    "description": "New content to replace the current content.",
                },
                "title": {
                    "type": "string",
                    "description": "New title for the memory.",
                },
                "importance": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 1.0,
                    "description": "New importance score.",
                },
            },
            "required": ["memory_id"],
        }

    async def execute(
        self,
        memory_id: str,
        content: str | None = None,
        title: str | None = None,
        importance: float | None = None,
        **_: Any,
    ) -> str:
        fields: dict[str, Any] = {}
        if content is not None:
            fields["content"] = content
        if title is not None:
            fields["title"] = title
        if importance is not None:
            fields["importance"] = importance
        if not fields:
            return tool_err("No fields provided to update.")
        try:
            result = await self._client.update_memory(memory_id, **fields)
        except NowledgeRequestError as exc:
            return tool_err("Nowledge memory update failed", code=exc.code)
        if not result:
            return tool_err(f"Failed to update memory {memory_id} (Nowledge Mem may be unavailable).")
        return tool_ok(id=memory_id, updated=True)


class MemoryDeleteTool(Tool):
    def __init__(self, client: "NowledgeClient"):
        self._client = client

    @property
    def name(self) -> str:
        return "memory_delete"

    @property
    def description(self) -> str:
        return (
            "Delete a memory from Nowledge Mem by ID. "
            "Use memory_search to find the memory you want to delete. "
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory to delete.",
                },
                "cascade_delete": {
                    "type": "boolean",
                    "default": True,
                    "description": ("Delete related entities."),
                },
            },
            "required": ["memory_id"],
        }

    async def execute(self, memory_id: str, cascade_delete: bool = True, **_: Any) -> str:
        try:
            success = await self._client.delete_memory(memory_id, cascade_delete=cascade_delete)
        except NowledgeRequestError as exc:
            return tool_err("Nowledge memory deletion failed", code=exc.code)
        if success:
            return tool_ok(id=memory_id, deleted=True)
        return tool_err(f"Failed to delete memory {memory_id}.")


class ReadWorkingMemoryTool(Tool):
    def __init__(self, client: NowledgeClient):
        self._client = client

    @property
    def name(self) -> str:
        return "read_working_memory"

    @property
    def description(self) -> str:
        return (
            "Read the Nowledge Mem working memory — a daily briefing summarizing recent context, "
            "priorities, and important memories. "
            "Call this at the start of a session or when you need to understand current priorities."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
            },
            "required": [],
        }

    async def execute(self, **_: Any) -> str:
        try:
            content = await self._client.get_working_memory()
        except NowledgeRequestError as exc:
            return tool_err("Nowledge Working Memory read failed", code=exc.code)
        return tool_ok(content=content or "")
