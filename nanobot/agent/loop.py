"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import tempfile
import uuid
from collections import deque
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import (
    MemoryConsolidator,
    NowledgeClient,
    NowledgeMemoryManager,
    NowledgeThreadManager,
)
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.gather import GatherTool
from nanobot.agent.tools.filesystem import (
    DeleteLinesTool,
    EditFileTool,
    FileHexTool,
    GrepFileTool,
    InsertLinesTool,
    ListDirTool,
    LoadImageTool,
    ReadFileTool,
    WriteFileTool,
)
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.todo import TodoTool
from nanobot.agent.tools.vision import ParseImageTool
from nanobot.agent.tools.wait import WaitTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import Config
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 16_000

    # Matches base64 data blobs in stringified MCP ImageContent blocks
    # e.g.  data='iVBORw0KGgo...'  or  data="iVBORw0KGgo..."
    _B64_BLOB_RE = re.compile(r"""data=['\"]([A-Za-z0-9+/=\n]{1024,})['\"]""")
    _MIME_IN_BLOB_RE = re.compile(r"""mimeType=['\"]([^'\"]+)['\"]""")
    _SESSION_NAME_UNSAFE = re.compile(r'[<>:"/\\|?*\t\r\n]')

    def __init__(
        self,
        bus: MessageBus,
        config: "Config",
        session_manager: SessionManager | None = None,
        cron_service: "CronService | None" = None,
    ):
        from nanobot.config.loader import set_runtime_config

        self.bus = bus
        self._config = config
        set_runtime_config(config)

        _mem = config.memory
        _nowledge_cfg = _mem.nowledge

        self.cron_service = cron_service
        self.context = ContextBuilder(
            config.workspace_path,
            nowledge_enabled=_nowledge_cfg.enabled,
        )
        self.sessions = session_manager or SessionManager(config.workspace_path)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            bus=bus,
            tools=self.tools,
        )

        self._running = False
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._background_tasks: list[asyncio.Task] = []
        self._processing_lock = asyncio.Lock()
        self._session_gen: dict[str, int] = {}
        self._pending_buf: dict[str, InboundMessage] = {}
        self._recent_logs: deque = deque(maxlen=10)
        logger.add(
            lambda msg: self._recent_logs.append(msg.strip()),
            format="[{time:HH:mm:ss}] [{level}] {message}",
        )

        # Nowledge Mem integration (optional)
        self.nowledge_client: NowledgeClient | None = (
            NowledgeClient(api_url=_nowledge_cfg.api_url, api_key=_nowledge_cfg.api_key)
            if _nowledge_cfg.enabled
            else None
        )
        self.thread_manager: NowledgeThreadManager | None = (
            NowledgeThreadManager(
                client=self.nowledge_client,
                sessions=self.sessions,
                source=_nowledge_cfg.thread_source,
            )
            if self.nowledge_client
            else None
        )
        self.nowledge_memory_manager: NowledgeMemoryManager | None = (
            NowledgeMemoryManager(client=self.nowledge_client)
            if self.nowledge_client and _nowledge_cfg.auto_extract_memories
            else None
        )

        self.memory_consolidator = MemoryConsolidator(
            sessions=self.sessions,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            threshold=_mem.consolidation_threshold,
            no_consolidate_turns=_mem.no_consolidate_history_num,
            nowledge_manager=self.nowledge_memory_manager,
        )
        self._register_default_tools()

    # ------------------------------------------------------------------
    # Config-derived properties (single source of truth)
    # ------------------------------------------------------------------

    @property
    def model(self) -> str:
        return self._config.agents.defaults.model

    @property
    def assistant_model(self) -> str:
        return self._config.agents.defaults.assistant_model or self.model

    @property
    def subagent_model(self) -> str:
        return self._config.agents.defaults.subagent_model or self.assistant_model

    @property
    def provider(self):
        from nanobot.providers.manager import get_provider

        return get_provider(self.model)

    @property
    def workspace(self):
        return self._config.workspace_path

    @property
    def max_iterations(self) -> int:
        return self._config.agents.defaults.max_tool_iterations

    @property
    def context_window_tokens(self) -> int:
        return self._config.agents.defaults.context_window_tokens

    @property
    def web_search_config(self):
        return self._config.tools.web.search

    @property
    def web_proxy(self):
        return self._config.tools.web.proxy

    @property
    def web_safety_check(self) -> bool:
        return self._config.tools.web.safety_check

    @property
    def exec_config(self):
        return self._config.tools.exec

    @property
    def filesystem_config(self):
        return self._config.tools.filesystem

    @property
    def tips(self):
        return self._config.tips

    @property
    def channels_config(self):
        return self._config.channels

    @property
    def _nowledge_auto_inject(self):
        return self._config.memory.nowledge.auto_inject

    @property
    def _mcp_servers(self):
        return self._config.tools.mcp_servers or {}

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        extra_read = [BUILTIN_SKILLS_DIR]
        self.tools.register(
            ReadFileTool(
                workspace=self.workspace,
                extra_allowed_dirs=extra_read,
            )
        )
        self.tools.register(
            LoadImageTool(
                workspace=self.workspace,
                extra_allowed_dirs=extra_read,
            )
        )
        self.tools.register(ParseImageTool(workspace=str(self.workspace)))
        for cls in (
            WriteFileTool,
            EditFileTool,
            ListDirTool,
            GrepFileTool,
            InsertLinesTool,
            DeleteLinesTool,
            FileHexTool,
        ):
            self.tools.register(cls(workspace=self.workspace, extra_allowed_dirs=extra_read))
        self.tools.register(
            ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                path_append=self.exec_config.path_append,
                deny_patterns=self.exec_config.deny_patterns or None,
                allow_patterns=self.exec_config.allow_patterns or None,
            )
        )
        self.tools.register(
            WebSearchTool(
                config=self.web_search_config,
                proxy=self.web_proxy,
            )
        )
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(WaitTool(send_callback=self.bus.publish_outbound))
        self.tools.register(TodoTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        self.tools.register(GatherTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
        if self.nowledge_client:
            from nanobot.agent.tools.nowledge import (
                MemoryAddTool,
                MemoryDeleteTool,
                MemorySearchTool,
                MemoryUpdateTool,
                ReadWorkingMemoryTool,
            )

            self.tools.register(MemorySearchTool(self.nowledge_client))
            self.tools.register(MemoryAddTool(self.nowledge_client))
            self.tools.register(MemoryUpdateTool(self.nowledge_client))
            self.tools.register(MemoryDeleteTool(self.nowledge_client))
            self.tools.register(ReadWorkingMemoryTool(self.nowledge_client))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        session: Session | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "wait", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))
        if session is not None:
            if todo_tool := self.tools.get("todo"):
                if hasattr(todo_tool, "set_context"):
                    todo_tool.set_context(channel, chat_id, session)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _extract_slash_command(text: str) -> str | None:
        """Extract normalized slash command name, e.g. '/model x' -> 'model'."""
        raw = text.strip()
        if not raw.startswith("/"):
            return None
        token = raw.split(maxsplit=1)[0][1:]
        if not token:
            return None
        # Telegram-style /cmd@botname support
        return token.split("@", 1)[0].strip().lower() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""

        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    # Slash commands that are independent queries and should NOT trigger
    # cancellation of an in-flight LLM turn.
    _STANDALONE_CMDS = frozenset(
        {"/status", "/help", "/new", "/stop", "/restart", "/context", "/whoami", "/compact"}
    )
    _STANDALONE_PREFIXES = ("/model", "/session")

    @staticmethod
    def _is_standalone_cmd(msg: InboundMessage) -> bool:
        """Return True if *msg* is an independent command that must not interrupt LLM turns."""
        cmd = msg.content.strip().lower()
        if cmd in AgentLoop._STANDALONE_CMDS:
            return True
        return cmd.startswith(AgentLoop._STANDALONE_PREFIXES)

    @staticmethod
    def _merge_messages(prev: InboundMessage, new: InboundMessage) -> InboundMessage:
        """Merge two inbound messages into one — concatenate text, combine media."""
        merged_media: list[str] = []
        if prev.media:
            merged_media.extend(prev.media)
        if new.media:
            merged_media.extend(new.media)
        return InboundMessage(
            channel=new.channel,
            sender_id=new.sender_id,
            chat_id=new.chat_id,
            content=f"{prev.content}\n\n{new.content}",
            media=merged_media,
            metadata=new.metadata or prev.metadata,
            session_key_override=new.session_key_override or prev.session_key_override,
        )

    def _intercept_oversized_image(self, result: Any) -> Any:
        """Intercept base64 image blobs in tool results.

        If a tool result string contains a base64 image blob (from e.g. MCP
        ImageContent stringified), decode it, save to a temp file, and replace
        the blob with a notice telling the model to use load_image to read it.
        """
        if not isinstance(result, str):
            return result

        match = self._B64_BLOB_RE.search(result)
        if not match:
            return result

        b64_data = match.group(1)
        try:
            raw = base64.b64decode(b64_data)
        except Exception:
            return result

        # Determine file extension from mimeType if present
        mime_match = self._MIME_IN_BLOB_RE.search(result)
        ext = ".png"
        if mime_match:
            mime = mime_match.group(1)
            import mimetypes as _mt

            ext = _mt.guess_extension(mime) or ext

        # Save to temp file
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="nanobot_img_")
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)

        size_mb = len(raw) / (1024 * 1024)
        logger.warning(
            "Intercepted image ({:.1f} MB) from tool result, saved to {}",
            size_mb,
            tmp_path,
        )

        notice = (
            f"[Image intercepted: the tool returned a raw base64 image ({size_mb:.1f} MB) "
            f"which cannot be passed as text context. "
            f"Saved to local file: {tmp_path}, use load_image(path) to read it.]"
        )
        # Replace the entire base64 blob region with the notice
        return result[: match.start()] + notice + result[match.end() :]

    @staticmethod
    def _bridge_image_tool_result(
        tool_name: str, result: Any
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """Convert load_image output to a tool text + synthetic user image message."""
        if tool_name != "load_image" or not isinstance(result, list):
            return None
        image_blocks = [
            block
            for block in result
            if isinstance(block, dict) and block.get("type") in {"image_url", "text"}
        ]
        if not any(block.get("type") == "image_url" for block in image_blocks):
            logger.debug(
                "Image bridge skipped for {}: missing image_url block (result_type={})",
                tool_name,
                type(result).__name__,
            )
            return None

        tool_text = (
            "Image read completed. Wait for the next user message carrying the image payload "
            "from this tool."
        )
        user_blocks = [
            {
                "type": "text",
                "text": "[Tool Return] Auto-forwarded image payload from load_image.",
            },
            *image_blocks,
        ]
        logger.info(
            "Image bridge prepared for {} with {} content blocks",
            tool_name,
            len(user_blocks),
        )
        return tool_text, user_blocks

    async def _auto_inject_memories(self, query: str) -> list[dict] | None:
        """Search Nowledge and return cleaned results for system prompt injection."""
        cfg = self._nowledge_auto_inject
        if not cfg.enabled or not self.nowledge_client:
            return None
        results = await self.nowledge_client.search_memories(query, limit=cfg.max_num)
        cleaned = []
        for r in results:
            score = r.get("similarity_score", 0)
            if score < cfg.score_threshold:
                continue
            mem = r.get("memory") or {}
            if not mem:
                continue
            item = {
                "id": mem.get("id"),
                "title": mem.get("title"),
                "score": score,
            }
            if cfg.with_content:
                content = mem.get("content") or ""
                if len(content) > cfg.max_length:
                    content = content[: cfg.max_length] + "[TRUNCATED, SEARCH IF USEFUL]"
                item["content"] = content
            cleaned.append(item)
        if cleaned:
            log = " / ".join(f"{mem['score'] * 100:.0f}% '{mem['title']}'" for mem in cleaned)
            logger.debug(f"Auto-injected {len(cleaned)} memories to system prompt ({log})")
        return cleaned or None

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        bypass_safety_check: bool = False,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        from nanobot.security import safety_bypass

        _bypass_token = safety_bypass.set(bypass_safety_check) if bypass_safety_check else None
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            tool_defs = self.tools.get_definitions()

            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=tool_defs,
                model=self.model,
            )

            if response.has_tool_calls:
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    tool_hint = self._tool_hint(response.tool_calls)
                    tool_hint = self._strip_think(tool_hint)
                    await on_progress(tool_hint, tool_hint=True)

                tool_call_dicts = [tc.to_openai_tool_call() for tc in response.tool_calls]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                # Attach short correlation ids for log tracing.
                _log_ids: dict[int, str] = {}
                for i, tc in enumerate(response.tool_calls):
                    _log_ids[i] = uuid.uuid4().hex[:4]

                # Log invocations before firing.
                for i, tc in enumerate(response.tool_calls):
                    tools_used.append(tc.name)
                    args_str = json.dumps(tc.arguments, ensure_ascii=False)
                    logger.debug("[{}] Tool call: {}({})", _log_ids[i], tc.name, args_str)

                async def _run_one(idx: int, tc: Any) -> tuple[int, Any, Any]:
                    result = await self.tools.execute(
                        tc.name, tc.arguments, bypass_safety_check, self.context.skills
                    )
                    return idx, tc, self._intercept_oversized_image(result)

                _results = await asyncio.gather(
                    *[_run_one(i, tc) for i, tc in enumerate(response.tool_calls)]
                )

                # Replay results in original order so messages are deterministic.
                for idx, tc, result in sorted(_results, key=lambda r: r[0]):
                    result_str = str(result)
                    if len(result_str) > 512:
                        result_str = (
                            result_str[:256]
                            + f"...[TRUNCATED {len(result_str) - 512} CHARS]..."
                            + result_str[-256:]
                        )
                    logger.debug("[{}] Tool {} result: {}", _log_ids[idx], tc.name, result_str)
                    if bridged := self._bridge_image_tool_result(tc.name, result):
                        tool_text, user_blocks = bridged
                        logger.info(
                            "Applying image bridge for tool_call_id={} ({})",
                            tc.id,
                            tc.name,
                        )
                        messages = self.context.add_tool_result(messages, tc.id, tc.name, tool_text)
                        messages.append({"role": "user", "content": user_blocks})
                        logger.debug(
                            "Injected synthetic user image message from tool {}",
                            tc.name,
                        )
                        continue

                    # Centralized result truncation (skip tools with own pagination).
                    _no_truncate = frozenset({"read_file", "grep_file"})
                    _max_chars = self._config.tools.max_return_chars
                    if tc.name not in _no_truncate and _max_chars > 0:
                        _str = result if isinstance(result, str) else str(result)
                        if len(_str) > _max_chars:
                            tmp = tempfile.NamedTemporaryFile(
                                mode="w", suffix=".txt", delete=False, encoding="utf-8"
                            )
                            tmp.write(_str)
                            tmp.close()
                            result = (
                                f"Tool return length {len(_str)} exceeds limit "
                                f"({_max_chars} chars). Full output written to: {tmp.name}\n"
                                f"Use appropriate tools to read this file selectively."
                            )
                            logger.info(
                                "[{}] Tool {} result truncated: {} → {}",
                                _log_ids[idx],
                                tc.name,
                                len(_str),
                                tmp.name,
                            )

                    messages = self.context.add_tool_result(messages, tc.id, tc.name, result)
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        if _bypass_token is not None:
            safety_bypass.reset(_bypass_token)
        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")
        asyncio.create_task(self._dispatch_restart_notify())

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            cmd = msg.content.strip().lower()
            if cmd == "/stop":
                await self._handle_stop(msg)
                self._pending_buf.pop(msg.session_key, None)
                self._session_gen.pop(msg.session_key, None)
            elif cmd == "/restart":
                await self._handle_restart(msg)
            elif self._is_standalone_cmd(msg):
                # Standalone commands (e.g. /status, /model, /context): queue normally,
                # do NOT interrupt an in-flight LLM turn.
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(
                    lambda t, k=msg.session_key: (
                        self._active_tasks.get(k, []) and self._active_tasks[k].remove(t)
                        if t in self._active_tasks.get(k, [])
                        else None
                    )
                )
            else:
                # Interruptible message: if a task is already running for this
                # session, hard-cancel it, merge the pending content, and
                # re-dispatch so that split messages (e.g. QQ text + image)
                # are processed as a single turn.
                sk = msg.session_key
                live = [t for t in self._active_tasks.get(sk, []) if not t.done()]

                if live:
                    for t in live:
                        t.cancel()
                    for t in live:
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass

                    prev = self._pending_buf.get(sk)
                    if prev:
                        msg = self._merge_messages(prev, msg)

                self._pending_buf[sk] = msg
                gen = self._session_gen.get(sk, 0) + 1
                self._session_gen[sk] = gen

                task = asyncio.create_task(self._dispatch(msg, gen=gen))
                self._active_tasks.setdefault(sk, []).append(task)
                task.add_done_callback(
                    lambda t, k=sk: (
                        self._active_tasks.get(k, []) and self._active_tasks[k].remove(t)
                        if t in self._active_tasks.get(k, [])
                        else None
                    )
                )

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = self.tips.stop_tasks.format(count=total) if total else self.tips.stop_idle
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
            )
        )

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self.tips.restart,
            )
        )

        try:
            from nanobot.config.paths import get_restart_notify_path

            notify_path = get_restart_notify_path()
            notify_path.write_text(
                json.dumps({"channel": msg.channel, "chat_id": msg.chat_id}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to persist restart notify: {}", e)

        async def _do_restart():
            await asyncio.sleep(1)
            # Use -m nanobot instead of sys.argv[0] for Windows compatibility
            # (sys.argv[0] may be just "nanobot" without full path on Windows)
            os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

        asyncio.create_task(_do_restart())

    async def _dispatch_restart_notify(self) -> None:
        """Send a restart-done notification if one was persisted before the last restart."""
        from nanobot.config.paths import get_restart_notify_path

        notify_path = get_restart_notify_path()
        if not notify_path.exists():
            return

        try:
            data = json.loads(notify_path.read_text(encoding="utf-8"))
            channel = data.get("channel")
            chat_id = data.get("chat_id")
        except Exception as e:
            logger.warning("Failed to read restart notify file: {}", e)
            notify_path.unlink(missing_ok=True)
            return

        # Remove before sending — avoids re-delivery if the send itself triggers another restart
        notify_path.unlink(missing_ok=True)

        if not channel or not chat_id:
            return

        # Wait for channels to initialize and connect before delivering
        await asyncio.sleep(5)

        await self.bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=self.tips.restart_done)
        )
        logger.info("Sent restart-done notification to {}:{}", channel, chat_id)

    async def _handle_model(self, msg: InboundMessage) -> OutboundMessage:
        """Handle /model command — query or update the active model."""
        from nanobot.config.loader import save_config
        from nanobot.providers.manager import clear_provider_cache

        raw_args = msg.content.strip()[len("/model") :].strip()
        parts = raw_args.split() if raw_args else []

        def _format_choices(models: list[str]) -> str:
            return "\n".join(f" {i + 1}. {model}" for i, model in enumerate(models)) or " (empty)"

        if not parts:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self.tips.model_info.format(
                    main_model=self.model,
                    max_model=self._config.agents.defaults.max_model,
                    assistant_model=self.assistant_model,
                    subagent_model=self.subagent_model,
                    provider_name=self.provider.name,
                    model_choice=_format_choices(self._config.agents.defaults.model_choice),
                ),
            )

        subcmd = parts[0].lower()

        if subcmd == "add":
            if len(parts) != 3:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(
                        error="Must provide provider and model name"
                    ),
                )
            full_model = f"{parts[1]}/{parts[2]}"
            try:
                if full_model not in self._config.agents.defaults.model_choice:
                    self._config.agents.defaults.model_choice.append(full_model)
                save_config(self._config)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_added.format(model_name=full_model),
                )
            except Exception as e:
                logger.error("Failed to add model: {}", e)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(error=str(e)),
                )

        if subcmd == "delete":
            if len(parts) != 2 or not parts[1].isdigit():
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(error="No delete number provided"),
                )
            choice_number = int(parts[1])
            try:
                models = self._config.agents.defaults.model_choice
                if choice_number < 1 or choice_number > len(models):
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=self.tips.model_choice_invalid.format(choice_number=choice_number),
                    )
                if len(models) <= 1:
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=self.tips.model_error.format(error="Can't delete the last model"),
                    )
                deleted_model = models.pop(choice_number - 1)
                save_config(self._config)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_deleted.format(model_name=deleted_model),
                )
            except Exception as e:
                logger.error("Failed to delete model: {}", e)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(error=str(e)),
                )

        if subcmd in ("agent", "subagent", "assistant", "max"):
            if len(parts) < 2 or not parts[1].isdigit():
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(
                        error="Usage: /model agent|subagent|assistant|max <N>"
                    ),
                )
            choice_number = int(parts[1])
            if choice_number < 1 or choice_number > len(self._config.agents.defaults.model_choice):
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_choice_invalid.format(choice_number=choice_number),
                )
            full_model = self._config.agents.defaults.model_choice[choice_number - 1]
            _targets: dict[str, str] = {
                "agent": "Main",
                "subagent": "Subagent",
                "assistant": "Assistant",
                "max": "Max",
            }
            try:
                if subcmd == "agent":
                    self._config.agents.defaults.model = full_model
                elif subcmd == "subagent":
                    self._config.agents.defaults.subagent_model = full_model
                elif subcmd == "assistant":
                    self._config.agents.defaults.assistant_model = full_model
                elif subcmd == "max":
                    self._config.agents.defaults.max_model = full_model
                save_config(self._config)
                clear_provider_cache()

                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_set.format(
                        target=_targets[subcmd], model_name=full_model
                    ),
                )
            except Exception as e:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(error=str(e)),
                )

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self.tips.model_error.format(error="Invalid command"),
        )

    def _validate_session_name(self, name: str) -> str | None:
        """Return an error reason string if *name* is not a valid save name, else None."""
        if not name:
            return "name is empty"
        if len(name) > 64:
            return "name too long (max 64 characters)"
        if self._SESSION_NAME_UNSAFE.search(name):
            return 'contains invalid characters (/ \\ : * ? " < > | and whitespace are not allowed)'
        if name in (".", ".."):
            return "name is reserved"
        return None

    async def _handle_session(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle /session save|load|delete commands."""

        def _reply(content: str) -> OutboundMessage:
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

        parts = msg.content.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        name = parts[2].strip() if len(parts) > 2 else ""

        # No subcommand or "list": show list
        if not sub or sub == "list":
            names = self.sessions.list_named(session.key)
            if not names:
                return _reply(self.tips.session_list_empty)
            return _reply(self.tips.session_list.format(items="\n".join(f"• {n}" for n in names)))

        if sub == "save" and name:
            if reason := self._validate_session_name(name):
                return _reply(self.tips.session_invalid_name.format(name=name, reason=reason))
            self.sessions.save(session)
            self.sessions.save_named(session, name)
            return _reply(self.tips.session_saved.format(name=name))

        if sub == "load" and name:
            if reason := self._validate_session_name(name):
                return _reply(self.tips.session_invalid_name.format(name=name, reason=reason))
            loaded = self.sessions.load_named(session.key, name)
            if loaded is None:
                return _reply(self.tips.session_not_found.format(name=name))
            return _reply(self.tips.session_loaded.format(name=name))

        if sub == "delete" and name:
            if reason := self._validate_session_name(name):
                return _reply(self.tips.session_invalid_name.format(name=name, reason=reason))
            deleted = self.sessions.delete_named(session.key, name)
            if not deleted:
                return _reply(self.tips.session_not_found.format(name=name))
            return _reply(self.tips.session_deleted.format(name=name))

        return _reply(self.tips.session_usage)

    async def _handle_context(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle /context command — show compact numeric context panel."""
        estimated_tokens, _ = self.memory_consolidator.estimate_session_prompt_tokens(session)
        context_window = max(0, self.context_window_tokens)
        usage_percent = (estimated_tokens / context_window) * 100 if context_window > 0 else 0.0
        overflow_tokens = max(0, estimated_tokens - context_window) if context_window > 0 else 0
        overflow_percent = (overflow_tokens / context_window) * 100 if context_window > 0 else 0.0

        history_messages = len(session.get_history(max_messages=0))
        messages_total = len(session.messages)
        messages_unconsolidated = max(0, messages_total - session.last_consolidated)
        unconsolidated_percent = (
            (messages_unconsolidated / messages_total) * 100 if messages_total > 0 else 0.0
        )

        content = self.tips.context_panel.format(
            model_name=self.model,
            estimated_prompt_tokens=estimated_tokens,
            context_window_tokens=context_window,
            context_usage_percent=f"{usage_percent:.2f}",
            overflow_tokens=overflow_tokens,
            overflow_percent=f"{overflow_percent:.2f}",
            messages_total=messages_total,
            messages_unconsolidated=messages_unconsolidated,
            unconsolidated_percent=f"{unconsolidated_percent:.2f}",
            history_messages=history_messages,
        )
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    async def _handle_whoami(self, msg: InboundMessage, session_key: str) -> OutboundMessage:
        """Handle /whoami command — show channel and chat routing IDs."""
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self.tips.whoami_info.format(
                channel=msg.channel,
                chat_id=msg.chat_id,
                session_key=session_key,
            ),
        )

    async def _dispatch(self, msg: InboundMessage, gen: int = 0) -> None:
        """Process a message under the global lock.

        When *gen* is non-zero, it carries the session generation number so
        that if a newer message arrived during processing (interrupt) this
        task can discard its results transparently.
        """
        if msg.content.strip().lower() == "/status":
            is_busy = self._processing_lock.locked()
            status = "🔴 **Busy**" if is_busy else "🟢 **Idle**"
            logs = (
                "\n".join(list(self._recent_logs)[-12:])
                if self._recent_logs
                else "(no recent logs)"
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"NanoBot is {status}\n\n---\n\n```log\n{logs}\n```",
                )
            )
            return

        async with self._processing_lock:
            try:
                response = await self._process_message(msg)

                # Discard if a newer message for the same session arrived
                # during processing (hard interrupt / merge).
                if gen and self._session_gen.get(msg.session_key, 0) != gen:
                    return

                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=msg.metadata or {},
                        )
                    )

                # Clear the pending buffer so the next message starts fresh.
                if gen and self._session_gen.get(msg.session_key, 0) == gen:
                    self._pending_buf.pop(msg.session_key, None)

            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=self.tips.error,
                    )
                )

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        transient: bool = False,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response.

        When transient=True the session is still saved but memory consolidation
        and Nowledge thread appending are skipped (used for cron/heartbeat).
        """
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            if not transient:
                await self.memory_consolidator.maybe_consolidate_by_tokens(session)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"), session)
            history = session.get_history(max_messages=0)
            # Subagent results should be assistant role, other system messages use user role
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            messages = self.context.build_messages(
                history=history,
                consolidated_memory=session.consolidated_memory,
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
                current_role=current_role,
            )
            n_initial_sys = len(messages)
            final_content, _, all_msgs = await self._run_agent_loop(messages)
            _old_msg_count_sys = len(session.messages)
            self._save_turn(session, all_msgs, n_initial_sys - 1)
            self.sessions.save(session)
            if not transient:
                self._schedule_background(
                    self.memory_consolidator.maybe_consolidate_by_tokens(session)
                )
                if self.thread_manager:
                    _new_msgs_sys = session.messages[_old_msg_count_sys:]
                    self._schedule_background(
                        self.thread_manager.append_turn(session, _new_msgs_sys)
                    )
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or self.tips.background_done,
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            session.metadata.pop("nowledge_thread_id", None)
            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            self._pending_buf.pop(key, None)
            self._session_gen.pop(key, None)

            return OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=self.tips.new_session
            )
        if cmd == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self.tips.help,
            )
        if cmd == "/compact":
            changed = await self.memory_consolidator.maybe_consolidate_by_tokens(
                session, force=True
            )
            content = self.tips.compact_completed if changed else self.tips.compact_failed
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)
        if cmd == "/context":
            return await self._handle_context(msg, session)
        if cmd == "/whoami":
            return await self._handle_whoami(msg, key)
        if msg.content.strip().lower().startswith("/approve"):
            parts = msg.content.strip().split()
            if len(parts) > 1:
                try:
                    duration = int(parts[1])
                except ValueError:
                    duration = 5
                until = datetime.now() + timedelta(minutes=duration)
                session.metadata["approve_until"] = until.isoformat()
                approve_text = f"for {duration} minute(s), expiring at {until.strftime('%H:%M:%S')}"
            else:
                session.metadata["approve_once"] = True
                approve_text = "for this turn only"
            self.sessions.save(session)
            msg = InboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                content=(
                    f"[APPROVE] You have been granted a temporary safety-check bypass "
                    f"{approve_text}. Please continue your task."
                ),
                metadata=msg.metadata,
                media=msg.media,
            )
            # Fall through to normal message processing
        # Handle /max command
        _max_restore: str | None = None
        if msg.content.strip().lower().startswith("/max"):
            max_model = self._config.agents.defaults.max_model
            if max_model:
                try:
                    from nanobot.providers.manager import clear_provider_cache

                    _max_restore = self._config.agents.defaults.model
                    self._config.agents.defaults.model = max_model
                    clear_provider_cache()
                except Exception as e:
                    return OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id, content=f"Error: {e}"
                    )
            # Rewrite message (remove /max prefix)
            prompt = msg.content.strip()[4:].strip()
            if not prompt:
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id, content="Usage: /max <prompt>"
                )
            msg = InboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                content=prompt,
                metadata=msg.metadata,
                media=msg.media,
            )
            # Fall through to normal processing
        if msg.content.strip().lower().startswith("/model"):
            return await self._handle_model(msg)
        if msg.content.strip().lower().startswith("/session"):
            return await self._handle_session(msg, session)
        if not transient:
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"), session)
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)
        injected_memories = await self._auto_inject_memories(msg.content) if not transient else None
        initial_messages = self.context.build_messages(
            history=history,
            consolidated_memory=session.consolidated_memory,
            injected_memories=injected_memories,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        # Check if a temporary safety-check bypass is active
        _bypass = False
        if session.metadata.pop("approve_once", None):
            _bypass = True
        else:
            _approve_str = session.metadata.get("approve_until")
            if _approve_str:
                try:
                    if datetime.fromisoformat(_approve_str) > datetime.now():
                        _bypass = True
                except (ValueError, TypeError):
                    pass

        n_initial = len(initial_messages)
        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            bypass_safety_check=_bypass,
        )

        _old_msg_count = len(session.messages)
        self._save_turn(session, all_msgs, n_initial - 1)
        self.sessions.save(session)
        if not transient:
            self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))
            if self.thread_manager:
                _new_msgs = session.messages[_old_msg_count:]
                self._schedule_background(self.thread_manager.append_turn(session, _new_msgs))

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        if final_content is None:
            final_content = self.tips.no_response

        # Restore model if /max was used
        if _max_restore is not None:
            from nanobot.providers.manager import clear_provider_cache

            self._config.agents.defaults.model = _max_restore
            clear_provider_cache()

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if (
                role == "tool"
                and isinstance(content, str)
                and len(content) > self._TOOL_RESULT_MAX_CHARS
            ):
                entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(
                    ContextBuilder._RUNTIME_CONTEXT_TAG
                ):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if (
                            c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                        ):
                            continue  # Strip runtime context from multimodal messages
                        if c.get("type") == "image_url" and c.get("image_url", {}).get(
                            "url", ""
                        ).startswith("data:image/"):
                            path = (c.get("_meta") or {}).get("path", "")
                            placeholder = f"[image: {path}]" if path else "[image]"
                            filtered.append({"type": "text", "text": placeholder})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        transient: bool = False,
    ) -> str:
        """Process a message directly (for CLI or cron usage).

        Set transient=True for background tasks (cron, heartbeat) to skip
        memory consolidation and Nowledge thread saving.
        """
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(
            msg, session_key=session_key, on_progress=on_progress, transient=transient
        )
        return response.content if response else ""
