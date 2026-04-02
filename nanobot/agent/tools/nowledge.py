"""Nowledge Mem built-in tools for semantic memory search and management."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.memory import NowledgeClient

import json


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
    ):
        if mem.get(key) is not None:
            if key == "updated_at" and mem.get("created_at") == mem.get("updated_at"):
                continue
            out[key] = mem[key]
    if r.get("similarity_score") is not None:
        out["similarity_score"] = r["similarity_score"]
    if r.get("relevance_reason"):
        out["relevance_reason"] = r["relevance_reason"]
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
    """Search Nowledge Mem memories by semantic query."""

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
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int = 5, **_: Any) -> str:
        results = await self._client.search_memories(query=query, limit=limit)
        if not results:
            return "No memories found."
        return json.dumps([_clean_search_result(r) for r in results], ensure_ascii=False)


class MemoryGetTool(Tool):
    """Get a memory by ID from Nowledge Mem."""

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
            },
            "required": ["memory_id"],
        }

    async def execute(self, memory_id: str, **_: Any) -> str:
        result = await self._client.get_memory(memory_id)
        if not result:
            return "Failed to get memory."
        return json.dumps(result, ensure_ascii=False)


class MemoryAddTool(Tool):
    """Save a new memory to Nowledge Mem."""

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
        result = await self._client.create_memory(
            content=content,
            title=title or None,
            labels=labels or None,
            importance=importance,
        )
        if not result:
            return "Failed to save memory (Nowledge Mem may be unavailable)."
        mem_id = result.get("id") or result.get("memory_id") or "unknown"
        return f"Memory saved (id: {mem_id})."


class MemoryUpdateTool(Tool):
    """Update an existing Nowledge Mem memory."""

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
            return "No fields provided to update."
        result = await self._client.update_memory(memory_id, **fields)
        if not result:
            return f"Failed to update memory {memory_id} (Nowledge Mem may be unavailable)."
        return f"Memory {memory_id} updated."


class MemoryDeleteTool(Tool):
    """Delete a memory from Nowledge Mem by ID"""

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
        success = await self._client.delete_memory(memory_id, cascade_delete=cascade_delete)
        if success:
            return f"Memory {memory_id} deleted."
        return f"Failed to delete memory {memory_id}."


class ReadWorkingMemoryTool(Tool):
    """Read today's Nowledge Mem working memory briefing."""

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
                "timeout": {
                    "type": "integer",
                    "description": "Just set to 5, unused",
                }
            },
        }

    async def execute(self, timeout: int, **_: Any) -> str:
        content = await self._client.get_working_memory()
        if not content:
            return "Working memory is empty."
        return content
