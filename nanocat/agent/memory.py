"""Memory system for session compaction and Nowledge integration."""

from __future__ import annotations

import asyncio
import json
import traceback
import uuid
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import httpx
from loguru import logger

from nanocat.utils.helpers import estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from nanocat.session.manager import Session, SessionManager


_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


def _ensure_text(value: Any) -> str:
    """Normalize arbitrary values to plain text."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_tool_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(marker in text for marker in _TOOL_CHOICE_ERROR_MARKERS)


def _format_messages(messages: list[dict[str, object]]) -> str:
    """Render raw messages as a stable text transcript for helper agents."""
    lines: list[str] = []
    for message in messages:
        content = message.get("content")
        if not content:
            continue
        text = _ensure_text(content)
        lines.append(
            f"[{str(message.get('timestamp', '?'))[:16]}] {str(message.get('role', '?')).upper()}: {text}"
        )
    return "\n".join(lines)


def _strip_fenced_block(text: str) -> str:
    """Strip a single surrounding markdown fence if present."""
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return cleaned


def _build_extract_memories_tool() -> list[dict[str, Any]]:
    """Return the extraction tool schema used by the Nowledge helper agent."""
    return [
        {
            "type": "function",
            "function": {
                "name": "record_memories",
                "description": "Return the durable memories worth storing in Nowledge.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {
                                        "type": "string",
                                        "description": "Self-contained durable memory content.",
                                    },
                                    "title": {
                                        "type": "string",
                                        "description": "Short descriptive title.",
                                    },
                                    "labels": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Tags such as decision, preference, workflow.",
                                    },
                                    "importance": {
                                        "type": "number",
                                        "minimum": 0.1,
                                        "maximum": 1.0,
                                        "description": "0.8-1.0 critical, 0.5-0.7 useful, 0.1-0.4 context.",
                                    },
                                },
                                "required": ["content"],
                            },
                        }
                    },
                    "required": ["memories"],
                },
            },
        }
    ]


class MemoryStore:
    """Static long-term memory backed by MEMORY.md in the workspace root."""

    def __init__(self, workspace: Path):
        self.memory_file = workspace / "MEMORY.md"
        self._migrate_legacy(workspace)

    def _migrate_legacy(self, workspace: Path) -> None:
        """Move MEMORY.md from the old memory/ sub-directory if it exists."""
        legacy = workspace / "memory" / "MEMORY.md"
        if legacy.exists() and not self.memory_file.exists():
            try:
                legacy.rename(self.memory_file)
            except Exception:
                pass

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"# --- MEMORY.md ---\n\n{long_term}" if long_term else ""


class NowledgeMemoryManager:
    """Owns LLM-based extraction of durable memories and writes them to Nowledge."""

    def __init__(self, client: NowledgeClient):
        self.client = client

    @property
    def model(self) -> str:
        from nanocat.config.loader import get_runtime_config

        cfg = get_runtime_config().agents.defaults
        return cfg.assistant_model or cfg.model

    @property
    def provider(self):
        from nanocat.providers.manager import get_provider

        return get_provider(self.model)

    async def extract_and_store(self, messages: list[dict[str, object]]) -> None:
        """Extract durable memories from raw messages and store them in Nowledge."""
        if not messages:
            return

        tool_def = _build_extract_memories_tool()
        prompt = (
            "Review these raw session messages and store only durable cross-session knowledge.\n\n"
            "Include facts, stable preferences, constraints, decisions with rationale, or reusable workflows.\n"
            "Skip temporary status, routine chatter, tool noise, and anything unlikely to matter later.\n"
            "Prefer precision over recall. If nothing is worth storing, return an empty memories array.\n\n"
            f"## Raw Messages\n{_format_messages(messages)}"
        )
        chat_messages = [
            {
                "role": "system",
                "content": (
                    "You are a Nowledge memory extraction agent. "
                    "Call the record_memories tool with only durable memories worth saving. "
                    "Return an empty memories array when there is nothing important to store."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            forced = {"type": "function", "function": {"name": "record_memories"}}
            response = await self.provider.chat_with_retry(
                messages=chat_messages,
                tools=tool_def,
                model=self.model,
                max_tokens=1200,
                temperature=0.0,
                tool_choice=forced,
            )
            if response.finish_reason == "error" and _is_tool_choice_unsupported(response.content):
                logger.warning(
                    "Nowledge extraction: forced tool_choice unsupported, retrying with auto"
                )
                response = await self.provider.chat_with_retry(
                    messages=chat_messages,
                    tools=tool_def,
                    model=self.model,
                    max_tokens=1200,
                    temperature=0.0,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.debug("Nowledge extraction produced no tool call")
                return

            args = _normalize_tool_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Nowledge extraction: unexpected tool arguments")
                return

            extracted = args.get("memories") or []
            if not isinstance(extracted, list):
                logger.warning("Nowledge extraction: invalid memories payload")
                return

            valid_items = [
                item for item in extracted if isinstance(item, dict) and item.get("content")
            ]
            for item in valid_items:
                try:
                    await self.client.create_memory(
                        content=_ensure_text(item["content"]),
                        title=_ensure_text(item["title"]) if item.get("title") else None,
                        labels=[
                            _ensure_text(label)
                            for label in item.get("labels", [])
                            if isinstance(label, (str, int, float))
                        ]
                        or None,
                        importance=float(item.get("importance", 0.5)),
                    )
                except Exception:
                    logger.warning("Failed to store extracted memory in Nowledge")
        except Exception:
            logger.exception("Nowledge extraction failed")


class MemoryCompactor:
    """Owns session compaction policy, locking, and offset updates."""

    _MAX_COMPACTION_ROUNDS = 5
    _MAX_FAILURES_BEFORE_SKIP = 3
    _MIN_TARGET_CHARS = 600
    _CHARS_PER_TOKEN = 4

    def __init__(
        self,
        sessions: SessionManager,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        threshold: float = 0.5,
        no_compact_turns: int = 3,
        nowledge_manager: NowledgeMemoryManager | None = None,
    ):
        from nanocat.config.loader import get_runtime_config

        self.store = MemoryStore(get_runtime_config().workspace_path)
        self.sessions = sessions
        self.threshold = threshold
        self.no_compact_turns = max(0, no_compact_turns)
        self.nowledge_manager = nowledge_manager
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._consecutive_failures = 0

    @property
    def model(self) -> str:
        from nanocat.config.loader import get_runtime_config

        cfg = get_runtime_config().agents.defaults
        return cfg.assistant_model or cfg.model

    @property
    def provider(self):
        from nanocat.providers.manager import get_provider

        return get_provider(self.model)

    @property
    def workspace(self):
        from nanocat.config.loader import get_runtime_config

        return get_runtime_config().workspace_path

    @property
    def context_window_tokens(self) -> int:
        from nanocat.config.loader import get_runtime_config

        return get_runtime_config().agents.defaults.context_window_tokens

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared compaction lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    @staticmethod
    def _channel_and_chat(session: Session) -> tuple[str | None, str | None]:
        return (session.channel, session.chat_id)

    def _estimate_prompt_tokens(
        self,
        session: Session,
        history: list[dict[str, Any]],
        compacted_memory: str,
    ) -> tuple[int, str]:
        channel, chat_id = self._channel_and_chat(session)
        probe_messages = self._build_messages(
            history=history,
            compacted_memory=compacted_memory,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        return self._estimate_prompt_tokens(session, history, session.compacted_memory)

    def pick_compaction_boundary(self, session: Session) -> int | None:
        """Pick the raw-message boundary that preserves the newest N completed turns."""
        turns = session.get_completed_turn_boundaries(start_idx=session.last_compacted)
        if not turns:
            return None
        if self.no_compact_turns <= 0:
            return turns[-1][1]
        if len(turns) <= self.no_compact_turns:
            return None
        return turns[-self.no_compact_turns][0]

    def _target_compacted_chars(self, session: Session, boundary_idx: int) -> int:
        """Estimate a target character budget for the updated compacted memory block."""
        raw_history = session.get_history_from(boundary_idx, max_messages=0)
        target_tokens = int(self.context_window_tokens * self.threshold)
        baseline_tokens, _ = self._estimate_prompt_tokens(session, raw_history, "")
        available_tokens = max(128, target_tokens - baseline_tokens)
        return max(self._MIN_TARGET_CHARS, available_tokens * self._CHARS_PER_TOKEN)

    async def _compact_text(
        self,
        existing_memory: str,
        raw_messages: list[dict[str, object]],
        target_chars: int,
    ) -> str | None:
        """Run the assistant compaction agent and return plain-text compacted memory."""
        if not raw_messages:
            return existing_memory

        prompt = (
            "You are updating the session's compacted memory block.\n\n"
            "You will receive two inputs:\n"
            "1. Existing compacted memory from earlier conversation history.\n"
            "2. New raw conversation messages that are about to be compressed.\n\n"
            "Produce a single updated compacted memory block in plain Markdown only.\n"
            "Prioritize the new raw messages. Preserve older compacted details only when they still matter.\n"
            "Keep durable facts, active goals, unresolved problems, stable preferences, important decisions, "
            "and reusable workflows. Drop stale or low-value detail when space is tight.\n"
            f"Target length: about {target_chars} characters.\n"
            "Do not use code fences. Do not explain your reasoning.\n\n"
            f"## Existing Compacted Memory\n{existing_memory or '(empty)'}\n\n"
            f"## New Raw Messages To Compress\n{_format_messages(raw_messages)}"
        )
        response = await self.provider.chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a session memory compaction agent. "
                        "Return only the updated compacted memory block as plain Markdown."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            model=self.model,
            max_tokens=max(512, min(4096, target_chars // 2)),
            temperature=0.0,
        )
        if response.finish_reason == "error":
            logger.warning("Memory compaction failed: {}", (response.content or "")[:200])
            return None
        text = _strip_fenced_block((response.content or "").strip())
        return text or None

    def _fail_or_skip(self, messages: list[dict[str, object]]) -> bool:
        """Increment failure count; after threshold, skip this chunk and reset."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_SKIP:
            return False
        logger.warning(
            "Memory compaction degraded after {} failures; skipping {} messages",
            self._consecutive_failures,
            len(messages),
        )
        self._consecutive_failures = 0
        return True

    async def compact_messages(
        self,
        session: Session,
        messages: list[dict[str, object]],
        boundary_idx: int,
    ) -> bool:
        """Extract Nowledge memories if enabled, then update the session compacted memory."""
        try:
            if self.nowledge_manager and not session.skip_next_nowledge_extraction:
                await self.nowledge_manager.extract_and_store(messages)
                session.skip_next_nowledge_extraction = True
                self.sessions.save(session)

            updated = await self._compact_text(
                existing_memory=session.compacted_memory,
                raw_messages=messages,
                target_chars=self._target_compacted_chars(session, boundary_idx),
            )
            if updated is None:
                self._fail_or_skip(messages)
                self.sessions.save(session)
                return False

            session.compacted_memory = updated
            session.skip_next_nowledge_extraction = False
            session.last_compacted = boundary_idx
            self.sessions.save(session)
            self._consecutive_failures = 0
            logger.info("Memory compaction done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory compaction failed")
            self._fail_or_skip(messages)
            self.sessions.save(session)
            return False

    async def maybe_compact_by_tokens(self, session: Session, force: bool = False) -> bool:
        """Compress older raw history into the session compacted memory."""
        if not session.messages or self.context_window_tokens <= 0:
            return False

        lock = self.get_lock(session.key)
        async with lock:
            target = int(self.context_window_tokens * self.threshold)
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return False
            if not force and estimated <= target:
                logger.debug(
                    "Token compaction idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    target,
                    source,
                )
                return False

            did_compact = False

            for round_num in range(self._MAX_COMPACTION_ROUNDS):
                if not force and estimated <= target:
                    return did_compact

                boundary_idx = self.pick_compaction_boundary(session)
                if boundary_idx is None:
                    logger.debug(
                        "Token compaction: no safe turn boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return did_compact

                chunk = session.messages[session.last_compacted : boundary_idx]
                if not chunk:
                    return did_compact

                logger.info(
                    "Token compaction round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    target,
                    source,
                    len(chunk),
                )
                if not await self.compact_messages(session, chunk, boundary_idx):
                    return did_compact
                did_compact = True

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return did_compact
            return did_compact


# ---------------------------------------------------------------------------
# NowledgeClient
# ---------------------------------------------------------------------------


class NowledgeClient:
    """Async HTTP client for the Nowledge Mem REST API."""

    def __init__(self, api_url: str = "http://127.0.0.1:14242", api_key: str | None = None):
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def is_available(self) -> bool:
        """Return True if the Nowledge Mem server is reachable."""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self._api_url}/health", timeout=2.0)
                return r.status_code == 200
        except Exception:
            return False

    async def search_memories(self, query: str, limit: int = 5) -> list[dict]:
        """Search memories by semantic query. Returns list of result dicts."""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self._api_url}/memories/search",
                    headers=self._headers(),
                    json={"query": query, "limit": limit},
                    timeout=10.0,
                )
                r.raise_for_status()
                return r.json() or []
        except Exception:
            logger.warning("Nowledge memory search failed")
            return []

    async def get_memory(self, memory_id: str) -> dict:
        """Get a memory by ID. Returns the memory dict or empty dict on failure."""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self._api_url}/memories/{memory_id}", headers=self._headers(), timeout=10.0
                )
                r.raise_for_status()
                return r.json() or {}
        except Exception:
            logger.warning("Nowledge get_memory failed for id={}", memory_id)

    async def create_memory(
        self,
        content: str,
        title: str | None = None,
        labels: list[str] | None = None,
        importance: float = 0.5,
    ) -> dict:
        """Create a new memory. Returns the created memory dict or empty dict on failure."""
        payload: dict[str, Any] = {"content": content, "importance": importance}
        if title:
            payload["title"] = title
        if labels:
            payload["labels"] = labels
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self._api_url}/memories",
                    headers=self._headers(),
                    json=payload,
                    timeout=10.0,
                )
                r.raise_for_status()
                return r.json() or {}
        except Exception:
            logger.warning("Nowledge create_memory failed")
            return {}

    async def update_memory(self, memory_id: str, **fields: Any) -> dict:
        """Update an existing memory by ID. Returns updated memory or empty dict on failure."""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.patch(
                    f"{self._api_url}/memories/{memory_id}",
                    headers=self._headers(),
                    json=fields,
                    timeout=10.0,
                )
                r.raise_for_status()
                return r.json() or {}
        except Exception:
            logger.warning("Nowledge update_memory failed for id={}", memory_id)
            logger.error(traceback.format_exc())
            return {}

    async def delete_memory(self, memory_id: str, cascade_delete: bool = True) -> bool:
        """
        Delete a memory by ID.

        Args:
            memory_id (str): The memory's unique identifier.
            cascade_delete (bool, optional): Whether to delete related entities.

        Returns:
            bool: True if deletion succeeded, False otherwise.
        """
        try:
            params = {"cascade_delete": cascade_delete}
            async with httpx.AsyncClient() as client:
                r = await client.delete(
                    f"{self._api_url}/memories/{memory_id}",
                    headers=self._headers(),
                    params=params,
                    timeout=10.0,
                )
                r.raise_for_status()
                return True
        except Exception:
            logger.warning(
                "Nowledge delete_memory failed for id={} with cascade_delete={}",
                memory_id,
                cascade_delete,
            )
            logger.error(traceback.format_exc())
            return False

    async def get_working_memory(self) -> str:
        """Fetch today's working memory briefing. Returns markdown string or empty string."""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self._api_url}/agent/working-memory",
                    headers=self._headers(),
                    timeout=10.0,
                )
                r.raise_for_status()
                data = r.json() or {}
                return data.get("content") or data.get("markdown") or str(data) if data else ""
        except Exception:
            logger.warning("Nowledge get_working_memory failed")
            logger.error(traceback.format_exc())
            return ""

    async def create_thread(
        self,
        thread_id: str,
        title: str,
        messages: list[dict],
        source: str = "nanocat",
    ) -> str | None:
        """Create a new conversation thread. Returns thread_id or None on failure."""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self._api_url}/threads",
                    headers=self._headers(),
                    json={
                        "thread_id": thread_id,
                        "title": title,
                        "messages": messages,
                        "source": source,
                    },
                    timeout=15.0,
                )
                r.raise_for_status()
                data = r.json() or {}
                return data.get("thread", {}).get("thread_id")
        except Exception:
            logger.warning("Nowledge create_thread failed")
            logger.error(traceback.format_exc())
            return None

    async def append_messages(
        self,
        thread_id: str,
        messages: list[dict],
        idempotency_key: str | None = None,
    ) -> bool:
        """Append messages to an existing thread. Returns True on success."""
        payload: dict[str, Any] = {"messages": messages, "deduplicate": True}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self._api_url}/threads/{thread_id}/append",
                    headers=self._headers(),
                    json=payload,
                    timeout=15.0,
                )
                r.raise_for_status()
                return True
        except Exception:
            logger.warning("Nowledge append_messages failed for thread={}", thread_id)
            logger.error(traceback.format_exc())
            return False


# ---------------------------------------------------------------------------
# NowledgeThreadManager
# ---------------------------------------------------------------------------


class NowledgeThreadManager:
    """Manages per-session Nowledge thread lifecycle (create on first turn, append thereafter)."""

    _MAX_MSG_CHARS = 800

    def __init__(
        self,
        client: NowledgeClient,
        sessions: SessionManager,
        source: str = "nanocat",
    ):
        self._client = client
        self._sessions = sessions
        self._source = source
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    def _format_messages(self, messages: list[dict]) -> list[dict]:
        """Convert session messages to Nowledge thread message format.

        Only user messages and final assistant text replies are kept.
        Tool calls and tool results are dropped entirely.
        Content is truncated to _MAX_MSG_CHARS.
        """
        result: list[dict] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content") or ""

            if role not in ("user", "assistant"):
                continue

            # Skip assistant messages that are only tool-call dispatches (no text)
            if role == "assistant" and msg.get("tool_calls"):
                continue

            if not content:
                continue

            result.append({"role": role, "content": content[: self._MAX_MSG_CHARS]})

        return result

    @staticmethod
    def _extract_title(session: Session) -> str:
        """Derive a thread title using the current date as the title."""
        return f"Conversation from {session.channel}_{session.chat_id}"

    async def append_turn(self, session: Session, new_messages: list[dict]) -> None:
        """Append new-turn messages to the session's Nowledge thread.

        Creates the thread on the first call for this session. All operations are
        serialized per session_key to prevent concurrent thread creation.
        """
        formatted = self._format_messages(new_messages)
        if not formatted:
            return

        lock = self._get_lock(session.key)
        async with lock:
            thread_id: str | None = session.metadata.get("nowledge_thread_id")

            if thread_id is None:
                thread_id = str(uuid.uuid4())
                title = self._extract_title(session)
                got_id = await self._client.create_thread(
                    thread_id=thread_id,
                    title=title,
                    messages=formatted,
                    source=self._source,
                )
                if got_id != thread_id:
                    logger.error("Unmatched thread ID: got={}, expected={}", got_id, thread_id)
                    return
                session.metadata["nowledge_thread_id"] = thread_id
                self._sessions.save(session)
                logger.info("Created Nowledge thread {} for session {}", thread_id, session.key)
            else:
                idem_key = f"{session.key}:{len(session.messages)}"
                await self._client.append_messages(thread_id, formatted, idempotency_key=idem_key)
