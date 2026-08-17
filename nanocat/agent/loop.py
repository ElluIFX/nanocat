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
import weakref
from collections import deque
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from loguru import logger

from nanocat.agent.context import ContextBuilder
from nanocat.agent.context_artifacts import ContextArtifactStore, ContextLookupTool
from nanocat.agent.context_budget import ContextBudget
from nanocat.agent.memory import (
    MemoryCompactor,
    NowledgeThreadManager,
)
from nanocat.agent.nowledge_client import NowledgeClient, NowledgeRequestError
from nanocat.agent.subagent import (
    SubagentGatherTool,
    SubagentKillTool,
    SubagentListTool,
    SubagentSpawnTool,
    SubagentSteerTool,
)
from nanocat.agent.tools.cron import CronTool
from nanocat.agent.tools.filesystem import (
    DeleteLinesTool,
    DeleteTool,
    EditFileTool,
    FileHexTool,
    GrepFileTool,
    InsertLinesTool,
    ListDirTool,
    LoadImageTool,
    ReadFileTool,
    WriteFileTool,
)
from nanocat.agent.tools.http import HttpRequestTool
from nanocat.agent.tools.message import AskTool, MessageTool
from nanocat.agent.tools.proc import (
    ProcListTool,
    ProcReadTool,
    ProcSendTool,
    ProcStartTool,
    ProcStopTool,
)
from nanocat.agent.tools.shell import ExecTool
from nanocat.agent.tools.ssh import SSHCloseTool, SSHListTool, SSHOpenTool, SSHReadTool, SSHSendTool
from nanocat.agent.tools.todo import TodoTool
from nanocat.agent.tools.wait import WaitTool
from nanocat.agent.tools.web import WebFetchTool, WebSearchTool
from nanocat.application.auto_approval import AutoApprovalReviewer
from nanocat.application.command_handlers import RuntimeCommandHandlers
from nanocat.application.command_router import CommandRouter
from nanocat.application.command_service import CommandCallbacks, CommandService
from nanocat.application.intervention import parse_intervention_action
from nanocat.application.mcp_host import MCPHost
from nanocat.application.providers import RuntimeProviderResolver
from nanocat.application.text_catalog import USER_TEXT
from nanocat.application.tool_executor import (
    ToolExecutionContext,
    ToolExecutor,
    ToolTurnAbortedError,
)
from nanocat.application.tool_host import ToolHost
from nanocat.application.turns import TurnCoordinator, TurnState
from nanocat.bus.events import InboundMessage, OutboundMessage
from nanocat.bus.queue import BusClosedError, MessageBus
from nanocat.core.commands import CommandErrorCode, CommandResult
from nanocat.core.intervention import InterventionAction, InterventionState
from nanocat.core.messages import ConversationRef
from nanocat.security.policy import SecurityPolicy
from nanocat.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanocat.config.schema import Config
    from nanocat.cron.service import CronService

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
    _NAME_GEN_PROMPT = (
        "Summarize this conversation in 10 characters or fewer. "
        "Output ONLY the title. No punctuation, no special symbols. "
        "Language must match the conversation."
    )

    def __init__(
        self,
        bus: MessageBus,
        config: "Config",
        session_manager: SessionManager | None = None,
        cron_service: "CronService | None" = None,
        intervention_broker: Any | None = None,
    ):
        from nanocat.config.loader import set_runtime_config

        self.bus = bus
        self._config = config
        self._provider_resolver = RuntimeProviderResolver(config)
        self._runtime_supervisor: Any | None = None
        self.command_router = CommandRouter.legacy_compatibility()
        self.intervention = intervention_broker
        set_runtime_config(config)

        _defaults = config.agents.defaults
        _mem = config.memory

        self.cron_service = cron_service
        self.context = ContextBuilder(
            config.workspace_path,
            nowledge_enabled=_mem.enabled,
            nowledge_tools_enabled=(
                _mem.enabled and config.tools.enabled_builtin_tools.memory_tools
            ),
        )
        from nanocat.config.paths import get_sessions_dir

        self.sessions = session_manager or SessionManager(get_sessions_dir())
        self.sessions.set_name_generator(self._generate_session_name)
        self.context_artifacts = ContextArtifactStore(self.sessions.sessions_dir)
        self.context_budget = ContextBudget(config)
        self.tool_host = ToolHost(bus=bus, config=config, provider_resolver=self._provider_resolver)
        self.tools = self.tool_host.registry
        self.subagents = self.tool_host.subagents
        self.subagents.set_context_artifacts(self.context_artifacts)
        self.ssh = self.tool_host.ssh
        self.procs = self.tool_host.processes
        self.http_sessions = self.tool_host.http_sessions

        runtime_limits = config.runtime
        self._turn_slots = asyncio.Semaphore(runtime_limits.max_concurrent_turns)
        self._running = False
        self._run_stop: asyncio.Event | None = None
        self.turns = TurnCoordinator()
        self.mcp_host = MCPHost(self._mcp_servers, self.tools)
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._session_gen: dict[str, int] = {}
        self._pending_buf: dict[str, InboundMessage] = {}
        self._steer_buf: dict[str, list[InboundMessage]] = {}
        self._steer_events: dict[str, asyncio.Event] = {}
        self._progressed: dict[str, bool] = {}
        self._exclusive_sessions: set[str] = set()
        self._command_dispatcher: Any | None = None
        self._nowledge_working_memory_loaded: set[str] = set()
        self.subagents._steer_inject = self._steer_buf
        self.subagents._is_live = lambda sk: any(
            not t.done() for t in self._active_tasks.get(sk, [])
        )
        self._recent_logs: deque = deque(maxlen=1000)
        self._recent_log_sink_id = logger.add(
            lambda msg: self._recent_logs.append(msg.strip()),
            format="[{time:HH:mm:ss}] [{level}] {message}",
        )

        # Nowledge Mem integration (optional)
        self.nowledge_client: NowledgeClient | None = (
            NowledgeClient(
                api_url=_mem.api_url,
                api_key=_mem.api_key,
                space_id=_mem.space_id,
                source=_mem.thread_source,
                preferred_language=_mem.distill_preferred_language,
                request_timeout=_mem.request_timeout_s,
                max_request_attempts=_mem.max_request_attempts,
                retry_delay=_mem.retry_delay_s,
                health_timeout=_mem.health_timeout_s,
                health_cache_seconds=_mem.health_cache_seconds,
                max_connections=_mem.max_connections,
                max_keepalive_connections=_mem.max_keepalive_connections,
            )
            if _mem.enabled
            else None
        )
        self.thread_manager: NowledgeThreadManager | None = (
            NowledgeThreadManager(
                client=self.nowledge_client,
                sessions=self.sessions,
                source=_mem.thread_source,
                space_id=_mem.space_id,
                artifact_store=self.context_artifacts,
                max_message_chars=_mem.thread_message_max_chars,
                auto_distill_enabled=_mem.auto_distill_enabled,
                distill_min_messages=_mem.distill_min_messages,
                distill_extraction_level=_mem.distill_extraction_level,
                distill_preferred_language=_mem.distill_preferred_language,
            )
            if self.nowledge_client and _mem.thread_capture_enabled
            else None
        )
        self.command_handlers = RuntimeCommandHandlers(
            self.cron_service,
            self.nowledge_client,
            memory_settings={
                "enabled": _mem.enabled,
                "spaceId": _mem.space_id,
                "threadSource": _mem.thread_source,
                "threadCaptureEnabled": _mem.thread_capture_enabled,
                "threadMessageMaxChars": _mem.thread_message_max_chars,
                "autoDistillEnabled": _mem.auto_distill_enabled,
                "distillMinMessages": _mem.distill_min_messages,
                "distillExtractionLevel": _mem.distill_extraction_level,
                "distillPreferredLanguage": _mem.distill_preferred_language,
                "workingMemoryEnabled": _mem.working_memory_enabled,
                "workingMemoryTimeoutS": _mem.working_memory_timeout_s,
                "workingMemoryMaxChars": _mem.working_memory_max_chars,
                "memoryToolsEnabled": config.tools.enabled_builtin_tools.memory_tools,
                "autoInject": _mem.auto_inject.model_dump(by_alias=True),
                "requestTimeoutS": _mem.request_timeout_s,
                "maxRequestAttempts": _mem.max_request_attempts,
                "retryDelayS": _mem.retry_delay_s,
                "healthTimeoutS": _mem.health_timeout_s,
                "healthCacheSeconds": _mem.health_cache_seconds,
                "maxConnections": _mem.max_connections,
                "maxKeepaliveConnections": _mem.max_keepalive_connections,
            },
        )
        self.command_service = CommandService(
            self.command_router,
            self.command_handlers,
            CommandCallbacks(
                new_session=self._command_new_session,
                logs=self._command_logs,
                intervention_response=self._command_intervention_response,
                compact=self._command_compact,
                whoami=self._handle_whoami,
                model=self._handle_model,
                session=self._handle_session,
            ),
        )

        self.memory_compactor = MemoryCompactor(
            sessions=self.sessions,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            threshold=_defaults.compaction_threshold,
            no_compact_turns=_defaults.no_compact_history_num,
            enabled=_defaults.compaction_enabled,
            provider_resolver=self._provider_resolver,
            config=config,
        )
        self._register_default_tools()
        auto_reviewer = (
            AutoApprovalReviewer(
                self._provider_resolver,
                _defaults.assistant_model or _defaults.model,
                config.workspace_path,
            )
            if config.tools.policy.auto_approve_mode
            else None
        )
        self.tool_executor = ToolExecutor(
            self.tools,
            SecurityPolicy(
                config,
                self.workspace,
            ),
            intervention_broker,
            auto_reviewer,
            max_concurrent_calls=runtime_limits.max_concurrent_tool_calls,
        )
        self.subagents.set_tool_executor(self.tool_executor)

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
        return self._provider_resolver.resolve(self.model)

    @property
    def provider_resolver(self) -> RuntimeProviderResolver:
        """Expose the runtime-owned provider resolver to composed services."""
        return self._provider_resolver

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
    def cmd_config(self):
        return self._config.tools.cmd

    @property
    def tips(self):
        return USER_TEXT

    @property
    def channels_config(self):
        return self._config.channels

    @property
    def _nowledge_auto_inject(self):
        return self._config.memory.auto_inject

    @property
    def _mcp_servers(self):
        return self._config.tools.mcp_servers or {}

    def _reg(self, tool) -> None:
        """Register a built-in tool unless disabled in enabled_builtin_tools."""
        if getattr(self._config.tools.enabled_builtin_tools, tool.name, True):
            self.tools.register(tool)

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        self.tools.register(ContextLookupTool(self.context_artifacts))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        if self._config.tools.enabled_builtin_tools.ask:
            self.tools.register(
                AskTool(
                    send_callback=self.bus.publish_outbound,
                    reply_waiter=self._wait_for_reply,
                )
            )
        if self._config.tools.enabled_builtin_tools.file_tools:
            for cls in (
                ReadFileTool,
                WriteFileTool,
                EditFileTool,
                ListDirTool,
                GrepFileTool,
                InsertLinesTool,
                DeleteLinesTool,
                FileHexTool,
            ):
                self.tools.register(
                    cls(
                        workspace=self.workspace,
                    )
                )
        self._reg(
            DeleteTool(
                workspace=self.workspace,
                force_to_trash=self._config.tools.filesystem.force_del_to_trash,
            )
        )

        try:
            from nanocat.agent.tools.vision import ParseImageTool, ScreenshotTool

            if self._config.tools.enabled_builtin_tools.image_tools:
                self.tools.register(
                    LoadImageTool(
                        workspace=self.workspace,
                        vision_model=self.model,
                    )
                )
                self.tools.register(
                    ParseImageTool(
                        workspace=str(self.workspace),
                        provider_resolver=self._provider_resolver,
                        config=self._config,
                    )
                )
            if self._config.tools.enabled_builtin_tools.screenshot:
                self.tools.register(ScreenshotTool())
        except ImportError:
            logger.debug("Vision tools are not available due to PIL missing, skipping registration")

        self._reg(
            ExecTool(
                working_dir=str(self.workspace),
                timeout=self.cmd_config.timeout,
                path_append=self.cmd_config.path_append or None,
                env=self.cmd_config.env or None,
            )
        )
        self._reg(
            WebSearchTool(
                config=self.web_search_config,
                proxy=self.web_proxy,
            )
        )
        self._reg(WebFetchTool(proxy=self.web_proxy))
        self._reg(HttpRequestTool(self.http_sessions, proxy=self.web_proxy))
        self._reg(WaitTool(send_callback=self.bus.publish_outbound))
        self._reg(TodoTool(send_callback=self.bus.publish_outbound))
        if self._config.tools.enabled_builtin_tools.subagent_tools:
            for tool in (
                SubagentSpawnTool(manager=self.subagents),
                SubagentGatherTool(manager=self.subagents),
                SubagentListTool(manager=self.subagents),
                SubagentSteerTool(manager=self.subagents),
                SubagentKillTool(manager=self.subagents),
            ):
                self.tools.register(tool)
        if self._config.tools.enabled_builtin_tools.ssh_tools:
            for tool in (
                SSHOpenTool(self.ssh),
                SSHSendTool(self.ssh),
                SSHReadTool(self.ssh),
                SSHCloseTool(self.ssh),
                SSHListTool(self.ssh),
            ):
                self.tools.register(tool)
        if self._config.tools.enabled_builtin_tools.proc_tools:
            for tool in (
                ProcStartTool(
                    self.procs,
                    working_dir=str(self.workspace),
                    env=self.cmd_config.env or None,
                    path_append=self.cmd_config.path_append or None,
                ),
                ProcSendTool(self.procs),
                ProcReadTool(self.procs),
                ProcStopTool(self.procs),
                ProcListTool(self.procs),
            ):
                self.tools.register(tool)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
        if self.nowledge_client and self._config.tools.enabled_builtin_tools.memory_tools:
            from nanocat.agent.tools.nowledge import (
                MemoryAddTool,
                MemoryDeleteTool,
                MemoryGetTool,
                MemorySearchTool,
                MemoryThreadGetTool,
                MemoryThreadSearchTool,
                MemoryUpdateTool,
                ReadWorkingMemoryTool,
            )

            self.tools.register(MemorySearchTool(self.nowledge_client))
            self.tools.register(MemoryGetTool(self.nowledge_client))
            self.tools.register(MemoryAddTool(self.nowledge_client))
            self.tools.register(MemoryUpdateTool(self.nowledge_client))
            self.tools.register(MemoryDeleteTool(self.nowledge_client))
            self.tools.register(MemoryThreadSearchTool(self.nowledge_client))
            self.tools.register(MemoryThreadGetTool(self.nowledge_client))
            self.tools.register(ReadWorkingMemoryTool(self.nowledge_client))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        await self.mcp_host.connect()

    async def _wait_for_reply(self, session_key: str, timeout: float | None) -> str | None:
        """Block until the user's next message for *session_key* arrives, or timeout.

        Backs the ``ask`` tool. The turn is in-flight while ``ask`` runs, so an
        incoming reply lands in the steer buffer (see ``run``); consume it here so
        it is delivered as the ask answer instead of steering the turn. Returns the
        reply text, or None on timeout / loop shutdown. A ``/stop`` or runtime stop
        wakes this wait immediately.
        """
        deadline = None if timeout is None else asyncio.get_event_loop().time() + timeout
        reply_event = self._steer_events.setdefault(session_key, asyncio.Event())
        while self._running:
            # Clear before checking the buffer.  A message arriving after this
            # point sets the event, while a message already buffered is found
            # by the check below, so neither path loses a wakeup.
            reply_event.clear()
            buf = self._steer_buf.get(session_key)
            if buf:
                msg = buf.pop(0)
                if not buf:
                    self._steer_buf.pop(session_key, None)
                content = msg.content
                return content if isinstance(content, str) else self._preview_text(content)
            waiters: set[asyncio.Task[Any]] = {
                asyncio.create_task(reply_event.wait(), name=f"nanocat.ask.{session_key}")
            }
            stop_task: asyncio.Task[Any] | None = None
            if self._run_stop is not None:
                stop_task = asyncio.create_task(
                    self._run_stop.wait(), name=f"nanocat.ask-stop.{session_key}"
                )
                waiters.add(stop_task)
            remaining = None
            if deadline is not None:
                remaining = max(0.0, deadline - asyncio.get_event_loop().time())
                if remaining == 0:
                    for task in waiters:
                        task.cancel()
                    await asyncio.gather(*waiters, return_exceptions=True)
                    return None
            done, pending = await asyncio.wait(
                waiters,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                return None
            if stop_task is not None and stop_task in done:
                return None
        return None

    def _wake_reply_waiter(self, session_key: str) -> None:
        """Wake an ask tool waiting for the next message in one session."""
        event = self._steer_events.get(session_key)
        if event is not None:
            event.set()

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    def _strip_pulse(self, text: str | None) -> str | None:
        """Strip (and debug-log) the PULSE block from any user-facing text when enabled."""
        if not text or not self._config.agents.defaults.pulse_enabled:
            return text
        from nanocat.agent.pulse import extract_pulse, strip_pulse

        if pulse := extract_pulse(text):
            logger.debug("[PULSE]\n{}", pulse)
        return strip_pulse(text) or None

    def _any_session_busy(self) -> bool:
        """Return whether at least one user turn is actively executing."""
        active_states = {TurnState.RUNNING, TurnState.WAITING_FOR_USER}
        return any(record.state in active_states for record in self.turns.snapshot())

    @property
    def command_dispatcher(self) -> Any | None:
        """Return the runtime-owned command dispatcher when attached."""
        return self._command_dispatcher

    def set_command_dispatcher(self, dispatcher: Any) -> None:
        """Attach the runtime-owned command dispatcher."""
        self._command_dispatcher = dispatcher

    def is_session_busy(self, session_key: str) -> bool:
        """Return whether a session has queued, active, or exclusive work."""
        if session_key in self._exclusive_sessions:
            return True
        if self.bus.pending_inbound(session_key) > 0:
            return True
        tasks = self._active_tasks.get(session_key, ())
        if any(not task.done() for task in tasks):
            return True
        lock = self._session_locks.get(session_key)
        if lock is not None and lock.locked():
            return True
        return any(
            record.session_key == session_key
            and record.state in {TurnState.RUNNING, TurnState.WAITING_FOR_USER}
            for record in self.turns.snapshot()
        )

    def try_reserve_session_operation(self, session_key: str) -> bool:
        """Atomically reserve a session operation from the event-loop thread."""
        if self.is_session_busy(session_key):
            return False
        self._exclusive_sessions.add(session_key)
        return True

    def release_session_operation(self, session_key: str) -> None:
        """Release a previously reserved session operation."""
        self._exclusive_sessions.discard(session_key)

    async def _wait_for_session_operation(self, session_key: str) -> None:
        """Yield while an exclusive command owns the session."""
        while session_key in self._exclusive_sessions:
            await asyncio.sleep(0)

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
            event_id=new.event_id or prev.event_id,
            correlation_id=new.correlation_id or prev.correlation_id,
            priority=new.priority if new.priority is not None else prev.priority,
            deadline_at=new.deadline_at or prev.deadline_at,
            request_id=new.request_id or prev.request_id,
            turn_id=new.turn_id or prev.turn_id,
            principal_id=new.principal_id or prev.principal_id,
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
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="nanocat_img_")
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

        notice = json.dumps(
            {
                "intercepted": True,
                "type": "image",
                "message": "Tool returned a raw base64 image that cannot be passed as text",
                "size_mb": round(size_mb, 1),
                "saved_to": tmp_path,
                "hint": "use load_image(path) to view it",
            },
            ensure_ascii=False,
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

    @staticmethod
    def _memory_recall_intent(query: str) -> bool:
        """Return whether a message explicitly asks for historical context."""
        markers = (
            "之前",
            "上次",
            "以前",
            "曾经",
            "历史",
            "记得",
            "回顾",
            "why did we",
            "previous",
            "last time",
            "earlier",
            "before",
        )
        lowered = query.lower()
        return any(marker in lowered for marker in markers)

    @classmethod
    def _memory_search_eligible(cls, query: str, min_length: int = 4) -> bool:
        """Skip commands, acknowledgements, and empty chatter before searching."""
        text = query.strip()
        if not text or text.startswith("/"):
            return False
        if len(text) < min_length and not cls._memory_recall_intent(text):
            return False
        return True

    async def _auto_inject_memories(self, query: str, session: Session) -> list[dict] | None:
        """Retrieve bounded Nowledge context before the provider call."""
        cfg = self._nowledge_auto_inject
        if (
            not cfg.enabled
            or not self.nowledge_client
            or not self._memory_search_eligible(query, cfg.query_min_length)
        ):
            return None
        recall_intent = self._memory_recall_intent(query)
        search_query = query.strip()
        if recall_intent and len(search_query) < cfg.short_recall_max_length:
            previous = [
                str(message.get("content") or "").strip()
                for message in reversed(session.messages)
                if message.get("role") == "user" and message.get("content")
            ][: cfg.recall_context_messages]
            if previous:
                search_query = "\n".join([*reversed(previous), search_query])[: cfg.query_max_length]
        search_query = search_query[: cfg.query_max_length]
        mode = cfg.mode
        if mode == "auto":
            mode = "deep" if cfg.deep_on_recall and recall_intent else "fast"
        try:
            results = await self.nowledge_client.search_memories(
                search_query,
                limit=cfg.max_num,
                mode=mode,
                include_entities=False,
                space_id=self._config.memory.space_id,
            )
        except NowledgeRequestError as exc:
            logger.debug("Nowledge auto-inject skipped: {}", exc.code)
            return None

        recent_ids = [str(item) for item in session.metadata.get("_nowledge_auto_inject_ids", [])]
        seen = set(recent_ids)
        cleaned: list[dict] = []
        for r in results:
            try:
                score = float(r.get("similarity_score", 0) or 0)
            except (TypeError, ValueError):
                score = 0.0
            if score < cfg.min_score:
                continue
            mem = r.get("memory") or {}
            if not mem:
                continue
            memory_id = mem.get("id")
            if not memory_id or memory_id in seen:
                continue
            content = str(mem.get("content") or "")
            if len(content) > cfg.preview_length:
                content = content[: cfg.preview_length] + "…"
            item = {
                "id": memory_id,
                "title": mem.get("title"),
                "type": mem.get("unit_type"),
                "summary": content,
                "relevance": score,
                "relevance_reason": r.get("relevance_reason"),
                "recorded_at": mem.get("created_at"),
                "event_time": mem.get("event_start") or mem.get("event_end"),
                "space_id": mem.get("space_id"),
                "source": mem.get("source"),
                "source_thread_id": mem.get("source_thread_id"),
                "source_range": mem.get("source_range"),
            }
            cleaned.append(item)
            seen.add(memory_id)
            recent_ids.append(memory_id)
        if cleaned:
            session.metadata["_nowledge_auto_inject_ids"] = (
                recent_ids[-cfg.dedupe_window :] if cfg.dedupe_window else []
            )
            log = " / ".join(f"{mem['relevance'] * 100:.0f}%" for mem in cleaned)
            logger.debug("Auto-injected {} Nowledge memories ({})", len(cleaned), log)
        return cleaned or None

    async def _load_working_memory(self, session: Session) -> str | None:
        """Load Working Memory once per runtime session without blocking the turn budget."""
        if (
            not self.nowledge_client
            or not self._config.memory.working_memory_enabled
            or session.key in self._nowledge_working_memory_loaded
        ):
            return None
        self._nowledge_working_memory_loaded.add(session.key)
        try:
            content = await asyncio.wait_for(
                self.nowledge_client.get_working_memory(space_id=self._config.memory.space_id),
                timeout=self._config.memory.working_memory_timeout_s,
            )
            if not content:
                return None
            return content[: self._config.memory.working_memory_max_chars]
        except (NowledgeRequestError, asyncio.TimeoutError) as exc:
            logger.debug("Nowledge Working Memory skipped for {}: {}", session.key, exc)
            return None

    async def _drain_steer(
        self,
        session_key: str,
        messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None,
    ) -> bool:
        pending = self._steer_buf.pop(session_key, None)
        if not pending:
            return False
        steer_text = "\n\n".join(m.content for m in pending if m.content)
        steer_media = [p for m in pending for p in (m.media or [])]
        content = self.context._build_user_content(steer_text, steer_media or None)
        messages.append({"role": "user", "content": content})
        if on_progress and steer_text:
            await on_progress(f"↪ {steer_text[:80]}")
        return True

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        tool_context: ToolExecutionContext,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_tool_event: Callable[[dict], Awaitable[None]] | None = None,
        session_key: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        messages = initial_messages

        # Cacheable PULSE spec in system; per-turn trigger is in the user message.
        if self._config.agents.defaults.pulse_enabled:
            from nanocat.agent.pulse import PULSE_PROMPT

            sys_msg = dict(messages[0])
            sys_msg["content"] += "\n\n" + PULSE_PROMPT
            messages = [sys_msg, *messages[1:]]

        iteration = 0
        final_content = None
        tools_used: list[str] = []
        turn_model = tool_context.model or self.model

        while iteration < self.max_iterations:
            iteration += 1

            if session_key:
                await self._drain_steer(session_key, messages, on_progress)

            tool_defs = self.tools.get_definitions()

            budget = self.context_budget.inspect(messages, tool_defs)
            if budget.over_budget:
                reduced = self.context_budget.trim(messages, budget.target_tokens)
                if reduced != messages:
                    logger.warning(
                        "Context preflight trimmed {}: {} -> {} estimated tokens",
                        session_key or tool_context.session_key,
                        budget.estimated_tokens,
                        self.context_budget.inspect(reduced, tool_defs).estimated_tokens,
                    )
                    messages = reduced
                    budget = self.context_budget.inspect(messages, tool_defs)
            if budget.over_budget:
                logger.error(
                    "Context remains over provider budget for {}: {} > {} estimated tokens",
                    session_key or tool_context.session_key,
                    budget.estimated_tokens,
                    budget.usable_tokens,
                )
                final_content = (
                    "The conversation context is still too large after safe trimming. "
                    "Please start a new session or ask me to compact the session."
                )
                break

            provider = self._provider_resolver.resolve(turn_model)
            response = await provider.chat_with_retry(
                messages=messages,
                tools=tool_defs,
                model=turn_model,
            )
            if response.finish_reason == "error" and self._is_context_overflow(response.content):
                reduced = self.context_budget.trim(
                    messages,
                    max(1024, budget.target_tokens // 2),
                )
                if reduced != messages:
                    logger.warning(
                        "Provider rejected context for {}; retrying with reduced history",
                        session_key or tool_context.session_key,
                    )
                    messages = reduced
                    response = await provider.chat_with_retry(
                        messages=messages,
                        tools=tool_defs,
                        model=turn_model,
                    )

            if response.has_tool_calls:
                if session_key:
                    self._progressed[session_key] = True
                if on_progress:
                    thought = self._strip_pulse(self._strip_think(response.content))
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
                    logger.info("[{}] Tool call: {}({})", _log_ids[i], tc.name, args_str)

                # Structured tool-call events for rich channels (e.g. the TUI);
                # other channels drop them. Text-only `_tool_hint` is unchanged.
                if on_tool_event:
                    await on_tool_event(
                        {
                            "phase": "start",
                            "calls": [
                                {"id": tc.id, "name": tc.name, "args": tc.arguments}
                                for tc in response.tool_calls
                            ],
                        }
                    )

                batch_results = await self.tool_executor.execute_batch(
                    [(tc.name, tc.arguments) for tc in response.tool_calls],
                    tool_context,
                    fallback_skill_loader=self.context.skills,
                )
                _results = [
                    (idx, tc, self._intercept_oversized_image(result))
                    for idx, (tc, result) in enumerate(
                        zip(response.tool_calls, batch_results, strict=True)
                    )
                ]

                if on_tool_event:
                    await on_tool_event(
                        {
                            "phase": "end",
                            "calls": [
                                {
                                    "id": tc.id,
                                    "name": tc.name,
                                    "status": "error" if self._tool_result_failed(r) else "ok",
                                    "preview": self._preview_text(r)[:160],
                                }
                                for _, tc, r in sorted(_results, key=lambda x: x[0])
                            ],
                        }
                    )

                # Replay results in original order so messages are deterministic.
                for idx, tc, result in sorted(_results, key=lambda r: r[0]):
                    if bridged := self._bridge_image_tool_result(tc.name, result):
                        tool_text, user_blocks = bridged
                        logger.info(
                            "[{}] Tool {} result: image bridge ({} blocks)",
                            _log_ids[idx],
                            tc.name,
                            len(user_blocks),
                        )
                        logger.info(
                            "Applying image bridge for tool_call_id={} ({})",
                            tc.id,
                            tc.name,
                        )
                        self.context_artifacts.capture(
                            tool_context.session_key,
                            tc.name,
                            tc.id,
                            result,
                        )
                        messages = self.context.add_tool_result(messages, tc.id, tc.name, tool_text)
                        messages.append({"role": "user", "content": user_blocks})
                        logger.debug(
                            "Injected synthetic user image message from tool {}",
                            tc.name,
                        )
                        continue

                    result = self.context_artifacts.capture(
                        tool_context.session_key,
                        tc.name,
                        tc.id,
                        result,
                    )
                    logger.info(
                        "[{}] Tool {} result: {}",
                        _log_ids[idx],
                        tc.name,
                        self._preview_text(result)[:320],
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
                if session_key and self._steer_buf.get(session_key):
                    if on_progress and (shown := self._strip_pulse(clean)):
                        await on_progress(shown)
                    await self._drain_steer(session_key, messages, on_progress)
                    continue
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        final_content = self._strip_pulse(final_content)

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        self._run_stop = asyncio.Event()
        await self._connect_mcp()
        logger.info("Agent loop started")
        self._schedule_background(self._dispatch_restart_notify())

        while self._running:
            try:
                msg = await self._consume_inbound_or_stop()
                if msg is None:
                    break
            except BusClosedError:
                if not self._running or self.bus.closed:
                    break
                raise
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            if (
                self.intervention is not None
                and self.intervention.defer(
                    ConversationRef(msg.channel, msg.chat_id, msg.session_key), msg
                )
            ):
                continue
            if msg.channel == "system":
                # Standalone commands and system messages (e.g. subagent results):
                # Queue system messages normally without command routing.
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

                # Interject: if the in-flight turn has already produced output,
                # queue this as a steer for the running loop to pick up at its
                # next step — don't cancel, keep its tool calls / thoughts.
                if live and self._progressed.get(sk):
                    self._steer_buf.setdefault(sk, []).append(msg)
                    self._wake_reply_waiter(sk)
                    continue

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

    async def _consume_inbound_or_stop(self) -> InboundMessage | None:
        """Wait for input or stop without polling the bus on a fixed interval."""
        if self._run_stop is None:
            return await self.bus.consume_inbound()
        consume_task = asyncio.create_task(self.bus.consume_inbound())
        stop_task = asyncio.create_task(self._run_stop.wait())
        done, _ = await asyncio.wait((consume_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        if consume_task in done:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            return consume_task.result()
        consume_task.cancel()
        await asyncio.gather(consume_task, return_exceptions=True)
        return None

    async def _handle_stop(self, msg: InboundMessage) -> OutboundMessage:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        self._pending_buf.pop(msg.session_key, None)
        self._session_gen.pop(msg.session_key, None)
        self._steer_buf.pop(msg.session_key, None)
        self._wake_reply_waiter(msg.session_key)
        self._progressed.pop(msg.session_key, None)
        total = cancelled + sub_cancelled
        content = self.tips.stop_tasks.format(count=total) if total else self.tips.stop_idle
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            event_id=msg.event_id,
            correlation_id=msg.correlation_id,
            request_id=msg.request_id,
            principal_id=msg.principal_id,
            metadata={"_control": True, "_command": "stop"},
        )

    async def _handle_restart(self, msg: InboundMessage) -> OutboundMessage:
        """Restart the process in-place via os.execv."""
        response = OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self.tips.restart,
            event_id=msg.event_id,
            correlation_id=msg.correlation_id,
            request_id=msg.request_id,
            principal_id=msg.principal_id,
            metadata={"_control": True, "_command": "restart"},
        )

        if self._runtime_supervisor is not None:
            await self._runtime_supervisor.request_restart(msg.channel, msg.chat_id)
            return response

        try:
            from nanocat.config.paths import get_restart_notify_path

            notify_path = get_restart_notify_path()
            notify_path.write_text(
                json.dumps({"channel": msg.channel, "chat_id": msg.chat_id}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to persist restart notify: {}", e)

        async def _do_restart():
            await asyncio.sleep(1)
            # Use -m nanocat instead of sys.argv[0] for Windows compatibility
            # (sys.argv[0] may be just "nanocat" without full path on Windows)
            os.execv(sys.executable, [sys.executable, "-m", "nanocat"] + sys.argv[1:])

        asyncio.create_task(_do_restart())
        return response

    async def _dispatch_restart_notify(self) -> None:
        """Send a restart-done notification if one was persisted before the last restart."""
        if self._runtime_supervisor is not None:
            notify_path = self._runtime_supervisor.runtime.paths.restart_notification
        else:
            from nanocat.config.paths import get_restart_notify_path

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
        from nanocat.config.loader import save_config

        raw_args = msg.content.strip()[len("/model") :].strip()
        parts = raw_args.split() if raw_args else []

        def _format_choices(models: list[str]) -> str:
            return "\n\n".join(f"{i + 1}. `{model}`" for i, model in enumerate(models)) or "(empty)"

        if not parts:
            defaults = self._config.agents.defaults
            provider_name = self._config.get_provider_name(self.model) or "unknown"
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self.tips.model_info.format(
                    agent_model=self.model,
                    assistant_model=self.assistant_model,
                    subagent_model=self.subagent_model,
                    provider_name=provider_name,
                    max_tokens=defaults.max_tokens
                    if defaults.max_tokens is not None
                    else "unlimited",
                    temperature=defaults.temperature
                    if defaults.temperature is not None
                    else "default",
                    reasoning_effort=defaults.reasoning_effort or "auto",
                    model_choice=_format_choices(defaults.model_choice),
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

        if subcmd == "effort":
            if len(parts) != 2:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(
                        error="Usage: /model effort auto|low|medium|high|xhigh|max"
                    ),
                )
            requested = parts[1].casefold()
            clear_effort = requested in {"auto", "none", "off"}
            if not clear_effort and requested not in {"low", "medium", "high", "xhigh", "max"}:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(
                        error="Effort must be one of: auto, low, medium, high, xhigh, max"
                    ),
                )
            effort = None if clear_effort else requested
            try:
                self._config.agents.defaults.reasoning_effort = effort
                save_config(self._config)
                self._provider_resolver.update_reasoning_effort(effort)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Reasoning effort set to `{effort or 'auto'}`.",
                )
            except Exception as e:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(error=str(e)),
                )

        if subcmd in ("agent", "subagent", "assistant"):
            if len(parts) < 2 or not parts[1].isdigit():
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.tips.model_error.format(
                        error="Usage: /model agent|subagent|assistant <N>"
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
            }
            try:
                if subcmd == "agent":
                    self._config.agents.defaults.model = full_model
                elif subcmd == "subagent":
                    self._config.agents.defaults.subagent_model = full_model
                elif subcmd == "assistant":
                    self._config.agents.defaults.assistant_model = full_model
                save_config(self._config)

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

    async def _generate_session_name(self, session: Session, _channel: str) -> str | None:
        """Generate a ≤10-char session name from text-only conversation history."""
        # Extract text-only messages (user + assistant, no tool calls/results)
        text_lines = []
        for msg in session.messages:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            if role == "assistant" and msg.get("tool_calls"):
                continue
            # Flatten multimodal block-list content to plain text before truncating;
            # appending a raw list here breaks the later "\n".join(...).
            text = self._preview_text(msg.get("content"))
            if not text:
                continue
            text_lines.append(text[:200])
        if not text_lines:
            return None

        history_text = "\n".join(text_lines)
        messages = [
            {"role": "system", "content": self._NAME_GEN_PROMPT},
            {"role": "user", "content": f"Conversation:\n{history_text}"},
        ]
        try:
            provider = self._provider_resolver.resolve(self.assistant_model)
            # Budget must leave room for the title AFTER any chain-of-thought: reasoning
            # models (e.g. deepseek-v4*) spend the whole allowance on reasoning and return
            # empty content if the cap is tiny, so keep it comfortably above the CoT length.
            resp = await provider.chat(
                messages,
                model=self.assistant_model,
                max_tokens=1024,
                reasoning_effort=None,
            )
            # Clean: remove quotes, punctuation, extra whitespace
            name = (resp.content or "").strip().strip("\"'\"'").strip()
            if not name:
                logger.warning(
                    "Session name generation returned empty content (model={}, "
                    "finish_reason={}, has_reasoning={})",
                    self.assistant_model,
                    resp.finish_reason,
                    bool(resp.reasoning_content),
                )
                return None
            return name[:10]
        except Exception as e:
            logger.warning("Session name generation failed (model={}): {}", self.assistant_model, e)
            return None

    async def _handle_session(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle /session list|view|switch commands."""

        def _reply(content: str) -> OutboundMessage:
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

        parts = msg.content.strip().split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2].strip() if len(parts) > 2 else ""

        # /session (no subcommand) — show help instead of falling back to list
        if not sub:
            return _reply(self.tips.session_usage)

        # /session list [N=10]
        if sub == "list":
            try:
                limit = int(arg) if arg else 10
            except ValueError:
                limit = 10
            items = self.sessions.list_sessions(msg.channel, min_turns=3, limit=limit)
            if not items:
                return _reply(self.tips.session_list_empty)
            lines = []
            for i, item in enumerate(items):
                sid = item["id"]
                name = item["name"] or "Unnamed session"
                last = item.get("last_active", "")[:16].replace("T", " ")
                turn_count = item.get("turn_count", 0)
                lines.append(f"{i + 1}. `{sid}` · {name} · {turn_count} turns · {last}")
            return _reply(self.tips.session_list.format(items="\n".join(lines)))

        # /session view [id]
        if sub == "view" and arg:
            s = self.sessions.get_session(msg.channel, arg)
            if s is None:
                return _reply(self.tips.session_not_found.format(session_id=arg))
            turns_text = self._format_session_turns(s)
            return _reply(
                self.tips.session_view.format(
                    name=s.name or "Unnamed session",
                    id=s.id,
                    turns=turns_text,
                )
            )

        # /session switch [id]
        if sub == "switch" and arg:
            ok = self.sessions.set_active(msg.channel, msg.chat_id, arg)
            if not ok:
                return _reply(self.tips.session_not_found.format(session_id=arg))
            if self.intervention is not None:
                await self.intervention.cancel_session(msg.session_key)
            s = self.sessions.get_session(msg.channel, arg)
            name = (s.name if s else None) or "Unnamed session"
            return _reply(self.tips.session_switched.format(session_id=arg, name=name))

        return _reply(self.tips.session_usage)

    @staticmethod
    def _preview_text(content: Any) -> str:
        """Flatten a message's content (str or block list) into one readable line.

        Multimodal messages store content as a list of blocks; rendering that list
        directly leaks JSON fragments (``[{...}]``) into the preview, so pull out the
        text blocks, mark images as ``[image]``, and collapse all whitespace.
        """
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and block.get("text"):
                        parts.append(str(block["text"]))
                    elif block.get("type") == "image_url":
                        parts.append("[image]")
                elif isinstance(block, str):
                    parts.append(block)
            text = " ".join(parts)
        else:
            text = "" if content is None else str(content)
        return " ".join(text.split())

    @staticmethod
    def _tool_result_failed(result: Any) -> bool:
        """True when a tool result is a JSON envelope reporting ``ok: false``.

        Tools return a ``{"ok": bool, ...}`` JSON string; content payloads (raw
        text, image-block lists) are treated as success."""
        if not isinstance(result, str):
            return False
        s = result.lstrip()
        if not s.startswith("{"):
            return False
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            return False
        return isinstance(obj, dict) and obj.get("ok") is False

    @staticmethod
    def _is_context_overflow(content: str | None) -> bool:
        """Recognize provider errors that can be recovered by trimming history."""
        text = (content or "").casefold()
        markers = (
            "context length",
            "context window",
            "maximum context",
            "max context",
            "prompt is too long",
            "too many tokens",
            "token limit",
            "request too large",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _format_session_turns(session: Session, turns: int = 3, width: int = 46) -> str:
        """Render the last N turns as a clean one-line-per-message text preview."""
        boundaries = session.get_completed_turn_boundaries()
        if not boundaries:
            return "(no completed turns)"

        recent = boundaries[-turns:]
        first_num = len(boundaries) - len(recent) + 1
        turn_texts = []
        for offset, (start, end) in enumerate(recent):
            lines = []
            for m in session.messages[start:end]:
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                if role == "assistant" and m.get("tool_calls"):
                    continue
                text = AgentLoop._preview_text(m.get("content"))
                if not text:
                    continue
                if len(text) > width:
                    text = text[: width - 1].rstrip() + "…"
                prefix = "[Q]" if role == "user" else "[A]"
                lines.append(f"`{prefix}` {text}")
            if lines:
                turn_texts.append(f"### Turn {first_num + offset}:\n\n" + "\n\n".join(lines))
        return "\n\n".join(turn_texts) if turn_texts else "> (no preview)"

    async def _handle_compact_status(
        self,
        msg: InboundMessage,
        session: Session,
    ) -> OutboundMessage:
        """Return context and compaction state without changing the session."""
        status = self.memory_compactor.status(session)
        checkpoint = status["checkpoint"]
        checkpoint_status = "none"
        if checkpoint is not None:
            checkpoint_status = (
                f"{checkpoint.created_at} "
                f"(messages {checkpoint.source_start}–{checkpoint.source_end})"
            )
        content = self.tips.compact_status.format(
            model_name=self.model,
            estimated_prompt_tokens=status["estimated_prompt_tokens"],
            context_window_tokens=status["context_window_tokens"],
            context_usage_percent=status["context_usage_percent"],
            overflow_tokens=status["overflow_tokens"],
            overflow_percent=status["overflow_percent"],
            messages_total=status["messages_total"],
            messages_uncompacted=status["messages_uncompacted"],
            uncompacted_percent=status["uncompacted_percent"],
            history_messages=status["history_messages"],
            completed_turns=status["completed_turns"],
            compaction_enabled="yes" if status["compaction_enabled"] else "no",
            compaction_available="yes" if status["compaction_available"] else "no",
            compaction_model=status["compaction_model"],
            compaction_threshold=status["compaction_threshold"],
            keep_recent_turns=status["keep_recent_turns"],
            last_compacted=status["last_compacted"],
            checkpoint_status=checkpoint_status,
            failure_count=status["failure_count"],
            estimator=status["estimator"],
        )
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    async def _handle_whoami(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Handle /whoami command — show channel, chat and session routing IDs."""
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self.tips.whoami_info.format(
                channel=msg.channel,
                chat_id=msg.chat_id,
                session_id=session.id,
                session_key=session.key,
            ),
        )

    async def _command_new_session(self, msg: InboundMessage, session: Session) -> str:
        """Reset the active conversation for the application command service."""
        if self.intervention is not None:
            await self.intervention.cancel_session(msg.session_key)
        session.metadata.pop("nowledge_thread_id", None)
        self.sessions._new_session(msg.channel, msg.chat_id)
        self._pending_buf.pop(msg.session_key, None)
        self._session_gen.pop(msg.session_key, None)
        return self.tips.new_session

    async def _command_compact(
        self,
        msg: InboundMessage,
        session: Session,
        subcommand: str | None = None,
    ) -> OutboundMessage:
        """Run explicit compaction or return its read-only status panel."""
        if subcommand == "status":
            return await self._handle_compact_status(msg, session)

        before = self.memory_compactor.status(session)
        changed = await self.memory_compactor.maybe_compact_by_tokens(session, force=True)
        after = self.memory_compactor.status(session)
        checkpoint = after["checkpoint"]
        if changed and checkpoint is not None:
            content = self.tips.compact_completed.format(
                token_before=checkpoint.token_before,
                token_after=checkpoint.token_after,
                context_window_tokens=after["context_window_tokens"],
                context_usage_percent=after["context_usage_percent"],
                source_start=checkpoint.source_start,
                source_end=checkpoint.source_end,
                messages_uncompacted=after["messages_uncompacted"],
                messages_total=after["messages_total"],
                revision=checkpoint.session_revision,
                compaction_model=checkpoint.compaction_model or after["compaction_model"],
            )
        else:
            if after["failure_count"] > before["failure_count"]:
                reason = "Compaction provider failed; original history was kept."
            elif not after["compaction_available"]:
                reason = "No completed turns are eligible for compaction."
            else:
                reason = "Compaction did not advance the checkpoint; original history was kept."
            content = self.tips.compact_failed.format(
                reason=reason,
                estimated_prompt_tokens=after["estimated_prompt_tokens"],
                context_window_tokens=after["context_window_tokens"],
                context_usage_percent=after["context_usage_percent"],
                completed_turns=after["completed_turns"],
                failure_count=after["failure_count"],
            )
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    def _command_logs(self, msg: InboundMessage) -> OutboundMessage:
        """Return the requested tail of the in-memory runtime log buffer."""
        raw_args = msg.content.strip()[len("/logs") :].strip()
        parts = raw_args.split() if raw_args else []
        if len(parts) > 1 or (parts and not parts[0].isdigit()):
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Usage: /logs [N], where N is a positive integer.",
            )

        tail_lines = int(parts[0]) if parts else 12
        if tail_lines <= 0:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Usage: /logs [N], where N is a positive integer.",
            )

        all_lines = "\n".join(self._recent_logs).splitlines()
        logs = "\n".join(all_lines[-tail_lines:]) if all_lines else "(no recent logs)"
        is_busy = self._any_session_busy()
        status = "🔴 **Busy**" if is_busy else "🟢 **Idle**"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"NanoCat is {status}\n\n## Recent logs (tail {tail_lines})\n\n```log\n{logs}\n```",
        )

    async def _command_intervention_response(self, msg: InboundMessage) -> OutboundMessage:
        """Resolve an intervention response without exposing broker details to ingress."""
        action = parse_intervention_action(msg.content)
        if action is None:
            raise ValueError("not an intervention response")
        conversation = ConversationRef(msg.channel, msg.chat_id, msg.session_key)
        principal_id = msg.principal_id or msg.sender_id
        accepted = False
        if action.error or self.intervention is None:
            result = CommandResult(
                ok=False,
                code=CommandErrorCode.INTERVENTION_NOT_FOUND,
                title=USER_TEXT.intervention_invalid if action.error else USER_TEXT.intervention_no_pending,
                message=action.error or "",
            )
            resolved = None
        elif (
            action.action is not InterventionAction.REVOKE_SESSION
            and (pending := self.intervention.current_pending(conversation, principal_id)) is not None
            and action.action not in pending.allowed_actions
        ):
            result = CommandResult(
                ok=False,
                code=CommandErrorCode.INTERVENTION_NOT_FOUND,
                title=USER_TEXT.intervention_action_rejected,
            )
            resolved = None
        else:
            resolved = await self.intervention.resolve(
                principal_id,
                conversation,
                action.action,
            )
            accepted = resolved is not None
            if accepted and action.action is InterventionAction.REVOKE_SESSION:
                title = USER_TEXT.intervention_yolo_disabled
            elif accepted and action.action is InterventionAction.APPROVE_FOREVER:
                title = USER_TEXT.intervention_yolo_enabled
            elif accepted and action.action is InterventionAction.APPROVE_TURN:
                title = USER_TEXT.intervention_turn_approved
            elif accepted and action.action is InterventionAction.REJECT:
                title = USER_TEXT.intervention_denied
            elif accepted:
                title = USER_TEXT.intervention_approved
            else:
                title = USER_TEXT.intervention_no_pending
            result = CommandResult(
                ok=accepted,
                code="ok" if accepted else CommandErrorCode.INTERVENTION_NOT_FOUND,
                title=title,
            )
        mode = None
        if resolved is not None and accepted:
            if resolved.state is InterventionState.APPROVED_FOREVER:
                mode = "yolo"
            elif resolved.state is InterventionState.REVOKED:
                mode = "auto"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self.command_router.feedback(result),
            event_id=msg.event_id,
            correlation_id=msg.correlation_id,
            metadata={
                "_control": True,
                "_intervention": True,
                "_intervention_update": True,
                "request_id": resolved.request_id if resolved and resolved.request_id else None,
                "intervention_state": resolved.state.value if resolved else "rejected",
                "_intervention_mode": mode,
                "_intervention_action": action.action.value if action.action else "unknown",
            },
        )

    async def _dispatch(self, msg: InboundMessage, gen: int = 0) -> None:
        """Process a message under the session lock and runtime turn budget.

        When *gen* is non-zero, it carries the session generation number so
        that if a newer message arrived during processing (interrupt) this
        task can discard its results transparently.
        """
        await self._wait_for_session_operation(msg.session_key)
        session_lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        async with session_lock:
            async with self._turn_slots:
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
                    self.turns.cancel_session(msg.session_key, "turn task cancelled")
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
                finally:
                    sk = msg.session_key
                    if not gen or self._session_gen.get(sk, 0) == gen:
                        self._progressed.pop(sk, None)
                        orphans = self._steer_buf.pop(sk, None)
                        if orphans:
                            merged = orphans[0]
                            for extra in orphans[1:]:
                                merged = self._merge_messages(merged, extra)
                            await self.bus.publish_inbound(merged)

    async def close_mcp(self) -> None:
        """Drain pending background archives, terminate ssh/proc sessions, close MCP."""
        active_tasks = tuple(
            task
            for tasks in self._active_tasks.values()
            for task in tasks
            if task is not asyncio.current_task() and not task.done()
        )
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        self._active_tasks.clear()
        self._exclusive_sessions.clear()

        background_tasks = tuple(
            task
            for task in self._background_tasks
            if task is not asyncio.current_task() and not task.done()
        )
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        await self.mcp_host.close()
        await self.tool_host.close()
        await self.sessions.close()
        await self._provider_resolver.close()
        if self.nowledge_client is not None:
            await self.nowledge_client.close()
        if self._recent_log_sink_id is not None:
            logger.remove(self._recent_log_sink_id)
            self._recent_log_sink_id = None

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)

        def _forget(done: asyncio.Task) -> None:
            try:
                self._background_tasks.remove(done)
            except ValueError:
                pass

        task.add_done_callback(_forget)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        if self._run_stop is not None:
            self._run_stop.set()
        for event in self._steer_events.values():
            event.set()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for tasks in self._active_tasks.values():
            for task in tasks:
                if task is not current and not task.done():
                    task.cancel()
        logger.info("Agent loop stopping")

    def set_runtime_supervisor(self, supervisor: Any) -> None:
        """Attach the composition owner for runtime-control commands."""
        self._runtime_supervisor = supervisor

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        transient: bool = False,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response.

        When transient=True the session is still saved but memory compaction
        and Nowledge thread appending are skipped (used for cron/heartbeat).
        """
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            principal_id = msg.principal_id or msg.sender_id
            logger.info("Processing system message from {}", principal_id)
            session = self.sessions.get_or_create(channel, chat_id)
            if not transient:
                await self.memory_compactor.maybe_compact_by_tokens(session)
            history = session.get_history(max_messages=0)
            # System messages (subagent results, etc.) enter as a user-role event so
            # the agent responds to them; matches the steer-injection path's role.
            current_role = "user"
            messages = self.context.build_messages(
                history=history,
                compacted_memory=session.compacted_memory,
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
                current_role=current_role,
                ssh_sessions=self.ssh.context_block(),
                proc_sessions=self.procs.context_block(),
                subs=self.subagents.context_block(),
            )
            n_initial_sys = len(messages)
            turn_id = uuid.uuid4().hex
            self.turns.start(turn_id, msg.session_key, principal_id)
            tool_context = ToolExecutionContext(
                turn_id=turn_id,
                session_key=msg.session_key,
                conversation=ConversationRef(channel, chat_id, msg.session_key),
                principal_id=principal_id,
                state_hook=lambda state: self.turns.transition(turn_id, state),
                message_id=msg.metadata.get("message_id"),
                session=session,
                model=self.model,
                user_input=msg.content,
            )
            try:
                final_content, _, all_msgs = await self._run_agent_loop(
                    messages,
                    tool_context=tool_context,
                )
            except ToolTurnAbortedError as exc:
                self._forget_message_turn(tool_context.turn_id)
                self.turns.fail(turn_id, exc.message)
                return OutboundMessage(channel=channel, chat_id=chat_id, content=exc.message)
            except asyncio.CancelledError:
                self._forget_message_turn(tool_context.turn_id)
                self.turns.cancel(turn_id, "turn task cancelled")
                raise
            except Exception:
                self._forget_message_turn(tool_context.turn_id)
                raise
            finally:
                if self.intervention is not None:
                    await self.intervention.finish_turn(tool_context.turn_id)
            self.turns.complete(turn_id)
            _old_msg_count_sys = len(session.messages)
            self._save_turn(
                session,
                all_msgs,
                n_initial_sys - 1,
                anchor=messages[-1] if messages else None,
            )
            self.sessions.save(session)
            if not transient:
                self._schedule_background(self.memory_compactor.maybe_compact_by_tokens(session))
                if self.thread_manager:
                    _new_msgs_sys = session.messages[_old_msg_count_sys:]
                    self._schedule_background(
                        self.thread_manager.append_turn_and_distill(session, _new_msgs_sys)
                    )
            if (message_tool := self.tools.get("message")) and isinstance(
                message_tool, MessageTool
            ):
                sent_in_turn = message_tool.sent_in_turn(tool_context.turn_id)
                message_tool.forget_turn(tool_context.turn_id)
                if sent_in_turn:
                    return None
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or self.tips.background_done,
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        if session_key is not None:
            session = self.sessions.get_system_session(session_key)
        else:
            session = self.sessions.get_or_create(msg.channel, msg.chat_id)
            session.metadata["_last_principal_id"] = msg.principal_id or msg.sender_id

        if not transient:
            await self.memory_compactor.maybe_compact_by_tokens(session)

        history = session.get_history(max_messages=0)
        working_memory = await self._load_working_memory(session) if not transient else None
        injected_memories = (
            await self._auto_inject_memories(msg.content, session) if not transient else None
        )
        initial_messages = self.context.build_messages(
            history=history,
            compacted_memory=session.compacted_memory,
            injected_memories=injected_memories,
            working_memory=working_memory,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            pulse=self._config.agents.defaults.pulse_enabled,
            ssh_sessions=self.ssh.context_block(),
            proc_sessions=self.procs.context_block(),
            subs=self.subagents.context_block(),
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

        async def _bus_tool_event(payload: dict) -> None:
            meta = dict(msg.metadata or {})
            meta["_tool_event"] = payload
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata=meta,
                )
            )

        n_initial = len(initial_messages)
        turn_id = uuid.uuid4().hex
        principal_id = msg.principal_id or msg.sender_id
        self.turns.start(turn_id, msg.session_key, principal_id)
        tool_context = ToolExecutionContext(
            turn_id=turn_id,
            session_key=msg.session_key,
            conversation=ConversationRef(msg.channel, msg.chat_id, msg.session_key),
            principal_id=principal_id,
            state_hook=lambda state: self.turns.transition(turn_id, state),
            message_id=msg.metadata.get("message_id"),
            session=session,
            model=self.model,
            user_input=msg.content,
        )
        try:
            final_content, _, all_msgs = await self._run_agent_loop(
                initial_messages,
                on_progress=on_progress or _bus_progress,
                on_tool_event=None if transient else _bus_tool_event,
                session_key=None if transient else msg.session_key,
                tool_context=tool_context,
            )
        except ToolTurnAbortedError as exc:
            self._forget_message_turn(tool_context.turn_id)
            self.turns.fail(turn_id, exc.message)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=exc.message,
                metadata=msg.metadata or {},
            )
        except asyncio.CancelledError:
            self._forget_message_turn(tool_context.turn_id)
            self.turns.cancel(turn_id, "turn task cancelled")
            raise
        except Exception:
            self._forget_message_turn(tool_context.turn_id)
            raise
        finally:
            if self.intervention is not None:
                await self.intervention.finish_turn(tool_context.turn_id)
        self.turns.complete(turn_id)

        _old_msg_count = len(session.messages)
        self._save_turn(
            session,
            all_msgs,
            n_initial - 1,
            anchor=initial_messages[-1] if initial_messages else None,
        )
        self.sessions.save(session)
        if not transient:
            self._schedule_background(self.memory_compactor.maybe_compact_by_tokens(session))
            if self.thread_manager:
                _new_msgs = session.messages[_old_msg_count:]
                self._schedule_background(
                    self.thread_manager.append_turn_and_distill(session, _new_msgs)
                )

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool):
            sent_in_turn = mt.sent_in_turn(tool_context.turn_id)
            mt.forget_turn(tool_context.turn_id)
            if sent_in_turn:
                return None

        if final_content is None:
            final_content = self.tips.no_response

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
        )

    def _forget_message_turn(self, turn_id: str) -> None:
        """Release message delivery state for an aborted or failed turn."""
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.forget_turn(turn_id)

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        anchor: dict | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from nanocat.agent.pulse import strip_pulse

        if anchor is not None:
            for index, message in enumerate(messages):
                if message is anchor or message == anchor:
                    skip = index
                    break

        added = 0
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and isinstance(content, str) and "<pulse" in content.lower():
                content = strip_pulse(content)
                entry["content"] = content
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if (
                role == "tool"
                and isinstance(content, str)
                and len(content) > self._TOOL_RESULT_MAX_CHARS
            ):
                entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str):
                    stripped = ContextBuilder.strip_leading_ephemeral(content)
                    if stripped == content:
                        pass
                    elif stripped:
                        entry["content"] = stripped
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if (
                            c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and ContextBuilder.is_ephemeral_text_block(c["text"])
                        ):
                            continue
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
            added += 1
        session.revision += added
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        transient: bool = False,
        principal_id: str = "user",
        metadata: Mapping[str, Any] | None = None,
        deadline_at: datetime | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage).

        Set transient=True for background tasks (cron, heartbeat) to skip
        memory compaction and Nowledge thread saving.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id=principal_id,
            principal_id=principal_id,
            chat_id=chat_id,
            content=content,
            metadata=dict(metadata or {}),
        )
        if MessageBus.is_command_candidate(content):
            if self._command_dispatcher is None:
                raise RuntimeError("command dispatcher is unavailable")
            response = await self._command_dispatcher.execute(msg, publish=False)
            return response.content if response is not None else ""
        if MessageBus.is_escaped_text(content):
            msg = replace(msg, content=MessageBus.normalize_escaped_text(content))

        async def _run() -> str:
            await self._wait_for_session_operation(session_key)
            session_lock = self._session_locks.setdefault(session_key, asyncio.Lock())
            async with session_lock:
                async with self._turn_slots:
                    await self._connect_mcp()
                    response = await self._process_message(
                        msg, session_key=session_key, on_progress=on_progress, transient=transient
                    )
            return response.content if response else ""

        if deadline_at is None:
            return await _run()
        now = datetime.now(deadline_at.tzinfo) if deadline_at.tzinfo else datetime.now()
        remaining = (deadline_at - now).total_seconds()
        if remaining <= 0:
            raise TimeoutError("direct turn deadline expired")
        return await asyncio.wait_for(_run(), timeout=remaining)
