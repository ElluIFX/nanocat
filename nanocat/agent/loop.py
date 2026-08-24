"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import tempfile
import time
import uuid
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from loguru import logger

from nanocat.agent.context import ContextBuilder
from nanocat.agent.context_budget import ContextBudget
from nanocat.agent.memory import (
    MemoryCompactor,
    NowledgeThreadManager,
    format_runtime_transcript,
)
from nanocat.agent.nowledge_client import NowledgeClient, NowledgeRequestError
from nanocat.agent.runtime_files import RuntimeFileStore
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
from nanocat.agent.tools.ssh import (
    SSHCloseTool,
    SSHDownloadTool,
    SSHListTool,
    SSHOpenTool,
    SSHReadTool,
    SSHSendTool,
    SSHUploadTool,
)
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
from nanocat.application.vision_fallback import VisionFallbackService
from nanocat.bus.events import InboundMessage, OutboundMessage
from nanocat.bus.queue import BusClosedError, MessageBus
from nanocat.core.commands import CommandErrorCode, CommandResult
from nanocat.core.intervention import InterventionAction, InterventionState
from nanocat.core.messages import ConversationRef
from nanocat.observability.redaction import redact_mapping, redact_value
from nanocat.security.policy import SecurityPolicy
from nanocat.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanocat.config.schema import Config
    from nanocat.cron.service import CronService


@dataclass(slots=True)
class _TurnRunState:
    turn_messages: list[dict[str, Any]]


class _TurnProviderError(RuntimeError):
    """Provider failure that should terminate the turn without becoming assistant text."""


@dataclass(slots=True)
class _StoppedTurn:
    session: Session
    run_state: _TurnRunState
    transient: bool
    order_key: tuple[int, float] = (0, 0.0)
    turn_id: str | None = None
    artifact_refs: tuple[dict[str, Any], ...] = ()
    recovery_id: str | None = None
    terminal_content: str | None = None
    terminal_error: dict[str, Any] | None = None
    terminal_control: dict[str, Any] | None = None
    tool_error: str = "turn stopped before the tool result was recorded"
    tool_status: str = "cancelled"
    runtime_session_key: str | None = None


@dataclass(slots=True)
class _PendingTurn:
    message: InboundMessage
    session_key: str | None
    transient: bool
    history_committed: bool = False
    session: Session | None = None
    run_state: _TurnRunState | None = None
    ordinal: int = 0
    handed_off: bool = False


@dataclass(slots=True)
class _PendingDurabilityRecord:
    message: InboundMessage
    transient: bool
    order_key: tuple[int, float]
    terminal_content: str
    terminal_error: dict[str, Any] | None = None
    terminal_control: dict[str, Any] | None = None
    tool_error: str = "turn stopped before the tool result was recorded"
    tool_status: str = "cancelled"
    recovery_id: str | None = None


@dataclass(slots=True)
class _TurnCancellationClaim:
    messages: list[InboundMessage]
    task: asyncio.Task[Any] | None
    pending_turn: _PendingTurn | None


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
    _EMPTY_USER_PLACEHOLDER = "[empty user input]"
    _MAX_WORKING_MEMORY_SCOPES = 4_096
    _MAX_BACKGROUND_TASKS = 256

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
        provider_resolver: RuntimeProviderResolver | None = None,
        vision_fallback: VisionFallbackService | None = None,
        runtime_files: RuntimeFileStore | None = None,
        configuration: Any | None = None,
    ):
        from nanocat.application.configuration import ConfigurationService
        from nanocat.config.loader import get_config_path, set_runtime_config

        self.bus = bus
        self._config = config
        self.configuration = configuration or ConfigurationService(
            get_config_path(), effective_config=config
        )
        self._provider_resolver = provider_resolver or RuntimeProviderResolver(config)
        self._vision_fallback = vision_fallback or VisionFallbackService(
            self._provider_resolver,
            config,
            workspace=config.workspace_path,
        )
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
        runtime_file_config = config.runtime_files
        self.runtime_files = runtime_files or RuntimeFileStore(
            config.workspace_path,
            max_file_bytes=runtime_file_config.max_file_bytes,
            max_session_bytes=runtime_file_config.max_session_bytes,
            max_total_bytes=runtime_file_config.max_total_bytes,
        )
        self.context_budget = ContextBudget(config)
        self.tool_host = ToolHost(
            bus=bus,
            config=config,
            provider_resolver=self._provider_resolver,
            vision_fallback=self._vision_fallback,
        )
        self.tools = self.tool_host.registry
        self.subagents = self.tool_host.subagents
        self.subagents.set_runtime_files(self.runtime_files)
        self.ssh = self.tool_host.ssh
        self.procs = self.tool_host.processes
        self.http_sessions = self.tool_host.http_sessions

        runtime_limits = config.runtime
        self._turn_slots = asyncio.Semaphore(runtime_limits.max_concurrent_turns)
        self._turn_admission_slots = asyncio.BoundedSemaphore(
            max(1, self.bus.inbound.maxsize)
        )
        self._running = False
        self._run_stop: asyncio.Event | None = None
        self.turns = TurnCoordinator()
        self.mcp_host = MCPHost(self._mcp_servers, self.tools)
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._direct_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._turn_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_pending_turns: dict[asyncio.Task[Any], _PendingTurn] = {}
        self._handoff_messages: dict[int, InboundMessage] = {}
        self._handoff_sequence = count(1)
        self._stop_requested: set[str] = set()
        self._stopped_turns: dict[str, list[_StoppedTurn]] = {}
        self._ingress_ordinals = count(time.time_ns())
        self._superseded_tasks: set[asyncio.Task[Any]] = set()
        self._background_tasks: list[asyncio.Task] = []
        self._background_scopes: dict[asyncio.Task[Any], str] = {}
        self._background_keys: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._background_pending: dict[tuple[str, str], Awaitable[Any]] = {}
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._stop_operations: dict[str, asyncio.Task[OutboundMessage]] = {}
        self._turn_cancel_operations: dict[str, asyncio.Task[bool]] = {}
        self._session_gen: dict[str, int] = {}
        self._pending_buf: dict[str, InboundMessage] = {}
        self._steer_buf: dict[str, list[InboundMessage]] = {}
        self._web_steer_reservations: dict[str, tuple[int, int]] = {}
        self._web_turn_attachment_usage: dict[str, tuple[int, int]] = {}
        self._post_stop_buf: dict[str, list[InboundMessage]] = {}
        self._pending_durability: dict[str, list[_PendingDurabilityRecord]] = {}
        self._durability_retry_task: asyncio.Task[None] | None = None
        self._persistence_error_count = 0
        self._steer_events: dict[str, asyncio.Event] = {}
        self._progressed: dict[str, bool] = {}
        self._exclusive_sessions: set[str] = set()
        self._command_dispatcher: Any | None = None
        self._nowledge_working_memory_loaded: OrderedDict[str, None] = OrderedDict()
        self.subagents._steer_inject = self._steer_buf
        if self.intervention is not None:
            self.intervention.set_deferred_release(self._release_buffered_admission)
            self.intervention.set_deferred_failure(self._deferred_delivery_failed)
        self.subagents._is_live = lambda sk: any(
            not t.done() for t in self._active_tasks.get(sk, [])
        )
        self._recent_logs: deque = deque(maxlen=1000)
        self._recent_log_sink_id = logger.add(
            lambda msg: self._recent_logs.append(str(redact_value(msg.strip()))),
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
                runtime_files=self.runtime_files,
                workspace=config.workspace_path,
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
            runtime_files=self.runtime_files,
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

    async def apply_configuration(
        self,
        config: Any,
        changed_paths: tuple[str, ...],
    ) -> None:
        """Apply owner-backed settings to work admitted after this boundary."""
        changed = set(changed_paths)
        if "runtime.maxConcurrentTurns" in changed:
            self._turn_slots = asyncio.Semaphore(config.runtime.max_concurrent_turns)
            if self._command_dispatcher is not None:
                self._command_dispatcher.apply_concurrency_limit(
                    config.runtime.max_concurrent_turns
                )
        if "runtime.maxConcurrentToolCalls" in changed:
            self.tool_executor.apply_concurrency_limit(
                config.runtime.max_concurrent_tool_calls
            )
        if "tools.policy.autoApproveMode" in changed:
            reviewer = (
                AutoApprovalReviewer(
                    self._provider_resolver,
                    config.agents.defaults.assistant_model
                    or config.agents.defaults.model,
                    config.workspace_path,
                )
                if config.tools.policy.auto_approve_mode
                else None
            )
            self.tool_executor.apply_auto_reviewer(reviewer)
        if any(path.startswith("tools.cmd.") for path in changed):
            cmd = config.tools.cmd
            exec_tool = self.tools.get("exec")
            if exec_tool is not None:
                exec_tool.timeout = cmd.timeout
                exec_tool.path_append = list(cmd.path_append)
                exec_tool.extra_env = dict(cmd.env)
            proc_start = self.tools.get("proc_start")
            if proc_start is not None:
                proc_start._env = dict(cmd.env)
                proc_start._path_append = list(cmd.path_append)
        if "tools.filesystem.forceDelToTrash" in changed:
            delete_tool = self.tools.get("delete")
            if delete_tool is not None:
                delete_tool._force_to_trash = config.tools.filesystem.force_del_to_trash
        if any(path.startswith("tools.web.") for path in changed):
            proxy = config.tools.web.proxy
            search_tool = self.tools.get("web_search")
            if search_tool is not None:
                search_tool.config = config.tools.web.search
                search_tool.proxy = proxy
            fetch_tool = self.tools.get("web_fetch")
            if fetch_tool is not None:
                fetch_tool.proxy = proxy
            if "tools.web.proxy" in changed:
                self.http_sessions = self.tool_host.replace_http_sessions(proxy)
                http_tool = self.tools.get("http_request")
                if http_tool is not None:
                    http_tool._mgr = self.http_sessions
                    http_tool._proxy = proxy
        if any(path.startswith("tools.enabledBuiltinTools.") for path in changed):
            groups = {
                "file_tools": {
                    "read_file",
                    "write_file",
                    "edit_file",
                    "list_dir",
                    "grep_file",
                    "insert_lines",
                    "delete_lines",
                    "file_hex",
                },
                "image_tools": {"load_image"},
                "screenshot": {"screenshot"},
                "delete": {"delete"},
                "exec": {"exec"},
                "web_search": {"web_search"},
                "web_fetch": {"web_fetch"},
                "wait": {"wait"},
                "ask": {"ask"},
                "todo": {"todo"},
                "subagent_tools": {
                    "subagent_spawn",
                    "subagent_gather",
                    "subagent_list",
                    "subagent_steer",
                    "subagent_kill",
                },
                "ssh_tools": {
                    "ssh_open",
                    "ssh_send",
                    "ssh_read",
                    "ssh_close",
                    "ssh_list",
                    "ssh_upload",
                    "ssh_download",
                },
                "proc_tools": {
                    "proc_start",
                    "proc_send",
                    "proc_read",
                    "proc_stop",
                    "proc_list",
                },
                "http_request": {"http_request"},
                "memory_tools": {
                    "memory_search",
                    "memory_get",
                    "memory_add",
                    "memory_update",
                    "memory_delete",
                    "memory_thread_search",
                    "memory_thread_get",
                    "read_working_memory",
                },
            }
            enabled = config.tools.enabled_builtin_tools
            for field, names in groups.items():
                if not getattr(enabled, field):
                    for name in names:
                        self.tools.unregister(name)
            self._register_default_tools()
        if any(path.startswith("memory.") for path in changed):
            memory = config.memory
            new_client = (
                NowledgeClient(
                    api_url=memory.api_url,
                    api_key=memory.api_key,
                    space_id=memory.space_id,
                    source=memory.thread_source,
                    preferred_language=memory.distill_preferred_language,
                    request_timeout=memory.request_timeout_s,
                    max_request_attempts=memory.max_request_attempts,
                    retry_delay=memory.retry_delay_s,
                    health_timeout=memory.health_timeout_s,
                    health_cache_seconds=memory.health_cache_seconds,
                    max_connections=memory.max_connections,
                    max_keepalive_connections=memory.max_keepalive_connections,
                )
                if memory.enabled
                else None
            )
            new_thread_manager = (
                NowledgeThreadManager(
                    client=new_client,
                    sessions=self.sessions,
                    source=memory.thread_source,
                    space_id=memory.space_id,
                    runtime_files=self.runtime_files,
                    workspace=config.workspace_path,
                    max_message_chars=memory.thread_message_max_chars,
                    auto_distill_enabled=memory.auto_distill_enabled,
                    distill_min_messages=memory.distill_min_messages,
                    distill_extraction_level=memory.distill_extraction_level,
                    distill_preferred_language=memory.distill_preferred_language,
                )
                if new_client is not None and memory.thread_capture_enabled
                else None
            )
            memory_settings = {
                "enabled": memory.enabled,
                "spaceId": memory.space_id,
                "threadSource": memory.thread_source,
                "threadCaptureEnabled": memory.thread_capture_enabled,
                "threadMessageMaxChars": memory.thread_message_max_chars,
                "autoDistillEnabled": memory.auto_distill_enabled,
                "distillMinMessages": memory.distill_min_messages,
                "distillExtractionLevel": memory.distill_extraction_level,
                "distillPreferredLanguage": memory.distill_preferred_language,
                "workingMemoryEnabled": memory.working_memory_enabled,
                "workingMemoryTimeoutS": memory.working_memory_timeout_s,
                "workingMemoryMaxChars": memory.working_memory_max_chars,
                "memoryToolsEnabled": config.tools.enabled_builtin_tools.memory_tools,
                "autoInject": memory.auto_inject.model_dump(by_alias=True),
                "requestTimeoutS": memory.request_timeout_s,
                "maxRequestAttempts": memory.max_request_attempts,
                "retryDelayS": memory.retry_delay_s,
                "healthTimeoutS": memory.health_timeout_s,
                "healthCacheSeconds": memory.health_cache_seconds,
                "maxConnections": memory.max_connections,
                "maxKeepaliveConnections": memory.max_keepalive_connections,
            }
            old_client = self.nowledge_client
            self.nowledge_client = new_client
            self.thread_manager = new_thread_manager
            self.command_handlers._memory = new_client
            self.command_handlers._memory_settings = memory_settings
            for name in {
                "memory_search",
                "memory_get",
                "memory_add",
                "memory_update",
                "memory_delete",
                "memory_thread_search",
                "memory_thread_get",
                "read_working_memory",
            }:
                self.tools.unregister(name)
            if new_client is not None and config.tools.enabled_builtin_tools.memory_tools:
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

                self.tools.register(MemorySearchTool(new_client))
                self.tools.register(MemoryGetTool(new_client))
                self.tools.register(MemoryAddTool(new_client))
                self.tools.register(MemoryUpdateTool(new_client))
                self.tools.register(MemoryDeleteTool(new_client))
                self.tools.register(MemoryThreadSearchTool(new_client))
                self.tools.register(MemoryThreadGetTool(new_client))
                self.tools.register(ReadWorkingMemoryTool(new_client))
            if old_client is not None:
                await old_client.close()
        if any(path.startswith("runtimeFiles.") for path in changed):
            self.runtime_files.apply_limits(
                max_file_bytes=config.runtime_files.max_file_bytes,
                max_session_bytes=config.runtime_files.max_session_bytes,
                max_total_bytes=config.runtime_files.max_total_bytes,
            )
        if changed & {
            "agents.defaults.contextWindowTokens",
            "agents.defaults.maxTokens",
        }:
            self.context_budget = ContextBudget(config)
        defaults = config.agents.defaults
        if "agents.defaults.compactionThreshold" in changed:
            self.memory_compactor.threshold = defaults.compaction_threshold
        if "agents.defaults.noCompactHistoryNum" in changed:
            self.memory_compactor.no_compact_turns = defaults.no_compact_history_num
        if "agents.defaults.compactionEnabled" in changed:
            self.memory_compactor.enabled = defaults.compaction_enabled
        if "tools.mcpServers" in changed or any(
            path.startswith("tools.mcpServers.") for path in changed
        ):
            await self.mcp_host.reconfigure(config.tools.mcp_servers or {})

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
                        runtime_file_store=self.runtime_files,
                    )
                )
        self._reg(
            DeleteTool(
                workspace=self.workspace,
                force_to_trash=self._config.tools.filesystem.force_del_to_trash,
            )
        )

        try:
            from nanocat.agent.tools.vision import ScreenshotTool

            if self._config.tools.enabled_builtin_tools.image_tools:
                self.tools.register(
                    LoadImageTool(
                        workspace=self.workspace,
                        runtime_file_store=self.runtime_files,
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
        self._reg(
            HttpRequestTool(
                self.http_sessions,
                proxy=self.web_proxy,
                runtime_file_store=self.runtime_files,
            )
        )
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
                SSHUploadTool(self.ssh),
                SSHDownloadTool(self.ssh),
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
                self.release_web_steer(session_key, msg)
                self._release_buffered_admission(msg)
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
        """Strip and debug-log any PULSE block from user-facing text."""
        if not text:
            return text
        from nanocat.agent.pulse import extract_pulse, strip_pulse

        if pulse := extract_pulse(text):
            logger.debug("[PULSE]\n{}", pulse)
        return strip_pulse(text) or None

    def _any_session_busy(self) -> bool:
        """Return whether at least one user turn is actively executing."""
        active_states = {
            TurnState.RUNNING,
            TurnState.WAITING_FOR_USER,
            TurnState.CANCELLING,
        }
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
        if self.has_pending_session_durability(session_key):
            return True
        if session_key in self._exclusive_sessions:
            return True
        if self.bus.pending_inbound(session_key) > 0:
            return True
        if self.bus.pending_commands(session_key) > 0:
            return True
        if any(
            message.session_key == session_key
            for message in self._handoff_messages.values()
        ):
            return True
        dispatcher = self._command_dispatcher
        if dispatcher is not None and dispatcher.is_session_busy(session_key):
            return True
        stop_operation = self._stop_operations.get(session_key)
        if stop_operation is not None and not stop_operation.done():
            return True
        tasks = self._active_tasks.get(session_key, ())
        if any(not task.done() for task in tasks):
            return True
        lock = self._session_locks.get(session_key)
        if lock is not None and lock.locked():
            return True
        return any(
            record.session_key == session_key
            and record.state
            in {TurnState.RUNNING, TurnState.WAITING_FOR_USER, TurnState.CANCELLING}
            for record in self.turns.snapshot()
        )

    def try_reserve_session_operation(self, session_key: str) -> bool:
        """Atomically reserve a session operation from the event-loop thread."""
        if self.has_pending_session_durability(session_key):
            self.request_session_durability_retry(session_key)
            return False
        if self.is_session_busy(session_key):
            return False
        self._exclusive_sessions.add(session_key)
        return True

    def release_session_operation(self, session_key: str) -> None:
        """Release a previously reserved session operation."""
        self._exclusive_sessions.discard(session_key)

    def has_pending_session_durability(self, session_key: str) -> bool:
        """Return whether a session still owns history awaiting durable storage."""
        return bool(
            getattr(self, "_stopped_turns", {}).get(session_key)
            or getattr(self, "_pending_durability", {}).get(session_key)
        )

    def retry_session_durability(self, session_key: str) -> bool:
        """Retry one session in order without releasing its owner prematurely."""
        records = list(getattr(self, "_stopped_turns", {}).get(session_key, ()))
        if records:
            try:
                canonical = self._canonicalize_stopped_turns(records)
            except Exception:
                self._persistence_error_count += 1
                logger.exception("Failed to prepare durability retry for {}", session_key)
                return False
        pending_records = list(
            getattr(self, "_pending_durability", {}).get(session_key, ())
        )
        backlog: list[
            tuple[
                tuple[int, float],
                str,
                _StoppedTurn | _PendingDurabilityRecord,
            ]
        ] = [
            (stopped.order_key, "stopped", stopped) for stopped in canonical
        ] if records else []
        backlog.extend(
            (record.order_key, "pending", record)
            for record in pending_records
        )
        backlog.sort(key=lambda item: item[0])
        for index, (_order_key, kind, item) in enumerate(backlog):
            try:
                if kind == "stopped":
                    stopped = item
                    assert isinstance(stopped, _StoppedTurn)
                else:
                    pending_record = item
                    assert isinstance(pending_record, _PendingDurabilityRecord)
                    stopped = self._stopped_turn_from_pending(
                        _PendingTurn(
                            message=pending_record.message,
                            session_key=None,
                            transient=pending_record.transient,
                            ordinal=pending_record.order_key[0],
                        )
                    )
                    stopped = replace(
                        stopped,
                        order_key=pending_record.order_key,
                        recovery_id=pending_record.recovery_id,
                        terminal_content=pending_record.terminal_content,
                        terminal_error=pending_record.terminal_error,
                        terminal_control=pending_record.terminal_control,
                        tool_error=pending_record.tool_error,
                        tool_status=pending_record.tool_status,
                    )
                self._persist_stopped_turn(
                    stopped,
                    stopped.terminal_content or self.tips.turn_interrupted,
                    schedule_background=False,
                    tool_error=stopped.tool_error,
                    tool_status=stopped.tool_status,
                )
                self._unlink_stopped_recovery(stopped.recovery_id)
            except Exception:
                self._persistence_error_count += 1
                remaining = backlog[index:]
                retained_stopped = [
                    value
                    for _key, value_kind, value in remaining
                    if value_kind == "stopped" and isinstance(value, _StoppedTurn)
                ]
                retained_pending = [
                    value
                    for _key, value_kind, value in remaining
                    if value_kind == "pending"
                    and isinstance(value, _PendingDurabilityRecord)
                ]
                if retained_stopped:
                    self._stopped_turns[session_key] = retained_stopped
                else:
                    self._stopped_turns.pop(session_key, None)
                if retained_pending:
                    pending_store = getattr(self, "_pending_durability", None)
                    if pending_store is None:
                        pending_store = {}
                        self._pending_durability = pending_store
                    pending_store[session_key] = retained_pending
                else:
                    getattr(self, "_pending_durability", {}).pop(session_key, None)
                logger.exception("Durability retry failed for {}", session_key)
                return False
            if kind == "pending":
                pending_record = item
                assert isinstance(pending_record, _PendingDurabilityRecord)
                self.release_web_steer(session_key, pending_record.message)
                self._release_buffered_admission(pending_record.message)
        self._stopped_turns.pop(session_key, None)
        getattr(self, "_pending_durability", {}).pop(session_key, None)
        return True

    def _ensure_session_durability_retry(self, session_key: str) -> None:
        """Start the single bounded-backoff durability scheduler."""
        if not getattr(self, "_running", False):
            return
        current = getattr(self, "_durability_retry_task", None)
        if current is not None and not current.done():
            return
        try:
            task = asyncio.create_task(
                self._retry_session_durability_owned(),
                name="nanocat.durability-retry",
            )
        except RuntimeError:
            return
        self._durability_retry_task = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self._durability_retry_task is completed:
                self._durability_retry_task = None

        task.add_done_callback(forget)

    async def _retry_session_durability_owned(self) -> None:
        """Retry every retained session from one explicitly bounded task owner."""
        delays: dict[str, float] = {}
        next_attempts: dict[str, float] = {}
        loop = asyncio.get_running_loop()
        while self._running:
            session_keys = set(self._stopped_turns) | set(
                getattr(self, "_pending_durability", {})
            )
            if not session_keys:
                return
            stop_requested = getattr(self, "_stop_requested", set())
            eligible_session_keys = session_keys - stop_requested
            now = loop.time()
            attempted = False
            for session_key in sorted(eligible_session_keys):
                if next_attempts.get(session_key, 0.0) > now:
                    continue
                session_lock = self._session_locks.setdefault(
                    session_key,
                    asyncio.Lock(),
                )
                async with session_lock:
                    if session_key in stop_requested:
                        continue
                    attempted = True
                    persisted = self.retry_session_durability(session_key)
                if persisted:
                    delays.pop(session_key, None)
                    next_attempts.pop(session_key, None)
                    continue
                delay = min(delays.get(session_key, 0.05) * 2, 2.0)
                delays[session_key] = delay
                next_attempts[session_key] = loop.time() + delay
            stale = set(delays) - session_keys
            for session_key in stale:
                delays.pop(session_key, None)
                next_attempts.pop(session_key, None)
            if not attempted:
                nearest = min(
                    (
                        attempt_at
                        for session_key, attempt_at in next_attempts.items()
                        if session_key in eligible_session_keys
                    ),
                    default=loop.time() + 0.1,
                )
                await asyncio.sleep(max(0.01, min(nearest - loop.time(), 0.25)))
            else:
                await asyncio.sleep(0)

    async def _wait_for_session_operation(self, session_key: str) -> None:
        """Yield while an exclusive command owns the session."""
        while session_key in self._exclusive_sessions:
            await asyncio.sleep(0)

    async def _wait_for_stop_operation(self, session_key: str) -> None:
        """Do not start direct work while the session stop operation is settling."""
        while True:
            operation = self._stop_operations.get(session_key)
            if operation is None or operation.done():
                return
            await asyncio.shield(operation)

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
        metadata = dict(new.metadata or prev.metadata)
        artifact_refs: list[dict[str, Any]] = []
        seen_artifacts: set[str] = set()
        for source in (prev.metadata, new.metadata):
            values = source.get("_artifact_refs") if isinstance(source, dict) else None
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                artifact_id = str(value.get("id") or "")
                if not artifact_id or artifact_id in seen_artifacts:
                    continue
                seen_artifacts.add(artifact_id)
                artifact_refs.append(dict(value))
        if artifact_refs:
            metadata["_artifact_refs"] = artifact_refs[:16]
        return InboundMessage(
            channel=new.channel,
            sender_id=new.sender_id,
            chat_id=new.chat_id,
            content=f"{prev.content}\n\n{new.content}",
            timestamp=prev.timestamp,
            media=merged_media,
            metadata=metadata,
            session_key_override=new.session_key_override or prev.session_key_override,
            event_id=new.event_id or prev.event_id,
            correlation_id=new.correlation_id or prev.correlation_id,
            priority=new.priority if new.priority is not None else prev.priority,
            deadline_at=new.deadline_at or prev.deadline_at,
            request_id=new.request_id or prev.request_id,
            turn_id=new.turn_id or prev.turn_id,
            principal_id=new.principal_id or prev.principal_id,
            ingress_ordinal=min(
                value
                for value in (prev.ingress_ordinal, new.ingress_ordinal)
                if value > 0
            )
            if prev.ingress_ordinal > 0 or new.ingress_ordinal > 0
            else 0,
        )

    def _intercept_oversized_image(self, result: Any, storage_scope: str) -> Any:
        """Intercept base64 image blobs in tool results.

        If a tool result string contains a base64 image blob (from e.g. MCP
        ImageContent stringified), decode it, save a disposable runtime copy,
        and replace the blob with a bounded file reference.
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

        try:
            ref = self.runtime_files.write_bytes(
                storage_scope,
                "images",
                raw,
                source_name="tool_image_bridge",
                suffix=ext,
            )
        except Exception as exc:
            logger.warning("Image runtime copy failed for {}: {}", storage_scope, exc)
            ref = None

        size_mb = len(raw) / (1024 * 1024)
        if ref is not None:
            logger.warning(
                "Intercepted image ({:.1f} MB) from tool result, saved to {}",
                size_mb,
                ref.relative_path,
            )
        else:
            logger.warning(
                "Intercepted image ({:.1f} MB) exceeded runtime file capacity",
                size_mb,
            )

        notice = json.dumps(
            {
                "intercepted": True,
                "type": "image",
                "message": "Tool returned a raw base64 image that cannot be passed as text",
                "size_mb": round(size_mb, 1),
                "saved_to": ref.relative_path if ref is not None else None,
                "hint": (
                    "use load_image(path) to view it"
                    if ref is not None
                    else "the image could not be stored"
                ),
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
                search_query = "\n".join([*reversed(previous), search_query])[
                    : cfg.query_max_length
                ]
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
        ):
            return None
        if session.key in self._nowledge_working_memory_loaded:
            self._nowledge_working_memory_loaded.move_to_end(session.key)
            return None
        self._nowledge_working_memory_loaded[session.key] = None
        while (
            len(self._nowledge_working_memory_loaded)
            > self._MAX_WORKING_MEMORY_SCOPES
        ):
            self._nowledge_working_memory_loaded.popitem(last=False)
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
        run_state: _TurnRunState,
    ) -> bool:
        pending = self._steer_buf.pop(session_key, None)
        if not pending:
            return False
        for item in pending:
            self.release_web_steer(session_key, item)
            self._release_buffered_admission(item)
        message = self._build_steer_message(pending)
        messages.append(message)
        persisted_message = dict(message)
        artifact_refs = [
            ref
            for item in pending
            for ref in self._artifact_refs_from_message(item)
        ]
        if artifact_refs:
            persisted_message["artifact_refs"] = artifact_refs[:16]
        run_state.turn_messages.append(persisted_message)
        steer_text = "\n\n".join(m.content for m in pending if m.content)
        for item in pending:
            if item.channel != "web":
                continue
            metadata = dict(item.metadata or {})
            ingress_request_id = str(
                metadata.get("_web_ingress_request_id") or item.request_id or ""
            )
            metadata["_turn_control_event"] = {
                "type": "turn.steer_applied",
                "status": "completed",
                "summary": "Guidance applied",
                "phase": "applied",
            }
            metadata["node_id"] = (
                f"steer:{ingress_request_id}" if ingress_request_id else None
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=item.channel,
                    chat_id=item.chat_id,
                    content="",
                    event_id=item.event_id,
                    correlation_id=item.correlation_id,
                    request_id=ingress_request_id or item.request_id,
                    turn_id=str(metadata.get("turn_id") or "") or item.turn_id,
                    principal_id=item.principal_id,
                    metadata=metadata,
                )
            )
        if on_progress and steer_text:
            await on_progress(f"↪ {steer_text[:80]}")
        return True

    def _build_steer_message(self, pending: list[InboundMessage]) -> dict[str, Any]:
        """Build the same persistent user message for drained and stopped steer input."""
        steer_text = "\n\n".join(message.content for message in pending if message.content)
        steer_media = [path for message in pending for path in (message.media or [])]
        content = self.context._build_user_content(steer_text, steer_media or None)
        return {"role": "user", "content": content}

    def track_web_turn_attachments(
        self,
        turn_id: str,
        attachment_count: int,
        attachment_bytes: int,
    ) -> bool:
        """Track the aggregate attachment budget for one browser turn."""
        attachment_count = max(attachment_count, 0)
        attachment_bytes = max(attachment_bytes, 0)
        if attachment_count > 16 or attachment_bytes > 64 * 1024 * 1024:
            return False
        self._web_turn_attachment_usage[turn_id] = (
            attachment_count,
            attachment_bytes,
        )
        try:
            self.turns.add_terminal_finalizer(
                turn_id,
                lambda: self._web_turn_attachment_usage.pop(turn_id, None),
            )
        except (KeyError, ValueError):
            self._web_turn_attachment_usage.pop(turn_id, None)
            return False
        return True

    def reserve_web_steer(
        self,
        session_key: str,
        turn_id: str,
        content_chars: int,
        attachment_count: int,
        attachment_bytes: int,
    ) -> bool:
        """Reserve bounded memory for an explicit browser steer."""
        count, chars = self._web_steer_reservations.get(session_key, (0, 0))
        content_chars = max(content_chars, 0)
        used_count, used_bytes = self._web_turn_attachment_usage.get(turn_id, (0, 0))
        attachment_count = max(attachment_count, 0)
        attachment_bytes = max(attachment_bytes, 0)
        if (
            count >= 8
            or chars + content_chars > 65_536
            or used_count + attachment_count > 16
            or used_bytes + attachment_bytes > 64 * 1024 * 1024
        ):
            return False
        self._web_steer_reservations[session_key] = (count + 1, chars + content_chars)
        self._web_turn_attachment_usage[turn_id] = (
            used_count + attachment_count,
            used_bytes + attachment_bytes,
        )
        return True

    def activate_web_steer(self, session_key: str, message: InboundMessage) -> bool:
        """Transfer a queued browser steer to standalone dispatch after a race."""
        turn_id = self._message_turn_id(message)
        if turn_id is None or not self.turns.activate_join(turn_id):
            return False
        self._release_web_steer_capacity(session_key, len(message.content))
        if isinstance(message.metadata, dict):
            message.metadata.pop("_web_steer_reserved", None)
            message.metadata.pop("_web_joined_turn", None)
        return True

    def _hold_buffered_admission(self, message: InboundMessage) -> None:
        """Transfer the consumed turn slot to a bounded runtime buffer."""
        if isinstance(message.metadata, dict):
            message.metadata["_runtime_admission_held"] = True

    def _release_buffered_admission(self, message: Any) -> None:
        """Return a turn slot exactly once when buffered input leaves memory."""
        metadata = getattr(message, "metadata", None)
        if not isinstance(metadata, dict) or not metadata.pop(
            "_runtime_admission_held",
            False,
        ):
            return
        self._turn_admission_slots.release()

    async def _deferred_delivery_failed(self, message: InboundMessage) -> None:
        """Persist a terminal record when intervention replay cannot be admitted."""
        pending_turn = self._new_pending_turn(message)
        persisted = False
        try:
            persisted = await self._persist_pending_interruption_ordered(pending_turn)
        except asyncio.CancelledError:
            if turn_id := self._message_turn_id(message):
                self.turns.fail(
                    turn_id,
                    "deferred ingress cancellation retained for persistence",
                )
            raise
        if turn_id := self._message_turn_id(message):
            self.turns.fail(
                turn_id,
                "deferred ingress delivery failed"
                if persisted
                else "deferred ingress persistence is pending",
            )

    def release_web_steer(
        self,
        session_key: str,
        message: InboundMessage | int,
        *,
        turn_id: str | None = None,
        rollback_attachments: tuple[int, int] | None = None,
    ) -> tuple[TurnState, str] | None:
        """Release a browser steer reservation after consumption or rejection."""
        released_terminal: tuple[TurnState, str] | None = None
        if isinstance(message, InboundMessage):
            if not isinstance(message.metadata, dict) or not message.metadata.pop(
                "_web_steer_reserved",
                False,
            ):
                return None
            content_chars = len(message.content)
            if message.metadata.pop("_web_joined_turn", False):
                joined_turn_id = self._message_turn_id(message)
                if joined_turn_id is not None:
                    released_terminal = self.turns.release_join(joined_turn_id)
        else:
            content_chars = max(message, 0)
            if turn_id is not None:
                released_terminal = self.turns.release_join(turn_id)
        if turn_id is not None and rollback_attachments is not None:
            usage = self._web_turn_attachment_usage.get(turn_id)
            if usage is not None:
                used_count, used_bytes = usage
                rollback_count, rollback_bytes = rollback_attachments
                self._web_turn_attachment_usage[turn_id] = (
                    max(0, used_count - max(rollback_count, 0)),
                    max(0, used_bytes - max(rollback_bytes, 0)),
                )
        self._release_web_steer_capacity(session_key, content_chars)
        return released_terminal

    def _release_web_steer_capacity(
        self,
        session_key: str,
        content_chars: int,
    ) -> None:
        count, chars = self._web_steer_reservations.get(session_key, (0, 0))
        if count <= 1:
            self._web_steer_reservations.pop(session_key, None)
            return
        self._web_steer_reservations[session_key] = (
            count - 1,
            max(0, chars - content_chars),
        )

    def _trim_context_to_runtime_file(
        self,
        messages: list[dict[str, Any]],
        target_tokens: int,
        tool_context: ToolExecutionContext,
        *,
        source_id: str,
    ) -> list[dict[str, Any]]:
        """Trim old groups and expose a disposable copy to the current model call."""
        trimmed = self.context_budget.trim_with_omitted(messages, target_tokens)
        if not trimmed.omitted:
            return trimmed.messages
        try:
            ref = self.runtime_files.snapshot(
                tool_context.storage_scope,
                "context",
                format_runtime_transcript(trimmed.omitted),
                source_name="context_budget_trim",
                source_id=source_id,
                suffix=".md",
            )
        except Exception as exc:
            logger.warning("Context trim copy failed for {}: {}", tool_context.storage_scope, exc)
            ref = None
        if ref is None:
            return trimmed.messages
        notice = (
            "\n\nOlder messages were omitted from this model request to fit the context budget. "
            f"Use read_file or grep_file on `{ref.relative_path}` if details are needed."
        )
        reduced = list(trimmed.messages)
        if reduced and reduced[0].get("role") == "system":
            system = dict(reduced[0])
            system["content"] = f"{system.get('content') or ''}{notice}"
            reduced[0] = system
        else:
            reduced.insert(0, {"role": "system", "content": notice.strip()})
        return reduced

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        tool_context: ToolExecutionContext,
        run_state: _TurnRunState,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_thinking: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_tool_event: Callable[[dict], Awaitable[None]] | None = None,
        session_key: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        messages = initial_messages

        # Cacheable PULSE spec in system; per-turn trigger is in the user message.
        turn_pulse = tool_context.pulse_enabled
        turn_effort = tool_context.reasoning_effort
        if turn_pulse:
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
                await self._drain_steer(session_key, messages, on_progress, run_state)

            tool_defs = self.tools.get_definitions()

            budget = self.context_budget.inspect(messages, tool_defs)
            if budget.over_budget:
                reduced = self._trim_context_to_runtime_file(
                    messages,
                    budget.target_tokens,
                    tool_context,
                    source_id=f"{tool_context.turn_id}-{iteration}-preflight",
                )
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
            response = await self._vision_fallback.chat_with_fallback(
                provider,
                messages=messages,
                tools=tool_defs,
                model=turn_model,
                reasoning_effort=turn_effort,
            )
            if response.finish_reason == "error" and self._is_context_overflow(response.content):
                reduced = self._trim_context_to_runtime_file(
                    messages,
                    max(1024, budget.target_tokens // 2),
                    tool_context,
                    source_id=f"{tool_context.turn_id}-{iteration}-provider-retry",
                )
                if reduced != messages:
                    logger.warning(
                        "Provider rejected context for {}; retrying with reduced history",
                        session_key or tool_context.session_key,
                    )
                    messages = reduced
                    response = await self._vision_fallback.chat_with_fallback(
                        provider,
                        messages=messages,
                        tools=tool_defs,
                        model=turn_model,
                        reasoning_effort=turn_effort,
                    )

            if on_thinking and (
                response.reasoning_content not in (None, "", [])
                or response.thinking_blocks
            ):
                await on_thinking(
                    {
                        "reasoningContent": response.reasoning_content,
                        "thinkingBlocks": response.thinking_blocks,
                    }
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
                run_state.turn_messages.append(messages[-1])

                # Attach short correlation ids for log tracing.
                _log_ids: dict[int, str] = {}
                for i, tc in enumerate(response.tool_calls):
                    _log_ids[i] = uuid.uuid4().hex[:4]

                # Log invocations before firing.
                for i, tc in enumerate(response.tool_calls):
                    tools_used.append(tc.name)
                    logged_args = (
                        {"fields": sorted(tc.arguments)}
                        if tc.name.startswith("ssh_")
                        else redact_mapping(tc.arguments)
                    )
                    args_str = json.dumps(logged_args, ensure_ascii=False)
                    logger.info("[{}] Tool call: {}({})", _log_ids[i], tc.name, args_str)

                # Structured tool-call events for rich channels;
                # other channels drop them. Text-only `_tool_hint` is unchanged.
                tool_batch_started = time.monotonic()
                if on_tool_event:
                    await on_tool_event(
                        {
                            "phase": "start",
                            "calls": [
                                {
                                    "id": tc.id,
                                    "name": tc.name,
                                    "status": "running",
                                    "args": tc.arguments,
                                }
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
                    (idx, tc, self._intercept_oversized_image(result, tool_context.storage_scope))
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
                                    "resultChars": len(r),
                                    "resultPreview": self._working_result_preview(r),
                                    "durationMs": max(
                                        0,
                                        int((time.monotonic() - tool_batch_started) * 1000),
                                    ),
                                }
                                for _, tc, r in sorted(_results, key=lambda x: x[0])
                            ],
                        }
                    )

                # Replay all tool results before synthetic image-user messages so
                # parallel tool-call protocol groups remain contiguous.
                image_blocks: list[dict[str, Any]] = []
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
                        messages = self.context.add_tool_result(messages, tc.id, tc.name, tool_text)
                        run_state.turn_messages.append(messages[-1])
                        image_blocks.extend(user_blocks)
                        logger.debug(
                            "Injected synthetic user image message from tool {}",
                            tc.name,
                        )
                        continue

                    result = self.runtime_files.capture(
                        tool_context.storage_scope,
                        tc.name,
                        tc.id,
                        result,
                    )
                    if tc.name.startswith("ssh_"):
                        logger.info("[{}] Tool {} result recorded", _log_ids[idx], tc.name)
                    else:
                        logger.info(
                            "[{}] Tool {} result recorded ({} chars)",
                            _log_ids[idx],
                            tc.name,
                            len(result),
                        )
                    messages = self.context.add_tool_result(messages, tc.id, tc.name, result)
                    run_state.turn_messages.append(messages[-1])
                if image_blocks:
                    image_message = {"role": "user", "content": image_blocks}
                    messages.append(image_message)
                    run_state.turn_messages.append(image_message)
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned an error response ({} chars)", len(clean or ""))
                    raise _TurnProviderError(
                        clean or "The provider returned an unspecified error."
                    )
                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                run_state.turn_messages.append(messages[-1])
                if session_key and self._steer_buf.get(session_key):
                    if on_progress and (shown := self._strip_pulse(clean)):
                        await on_progress(shown)
                    await self._drain_steer(session_key, messages, on_progress, run_state)
                    continue
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )
            messages = self.context.add_assistant_message(messages, final_content)
            run_state.turn_messages.append(messages[-1])

        final_content = self._strip_pulse(final_content)
        if not final_content:
            final_content = self.tips.no_response

        last_message = run_state.turn_messages[-1]
        if last_message.get("role") == "assistant" and not last_message.get("tool_calls"):
            last_message["content"] = final_content
        else:
            messages = self.context.add_assistant_message(messages, final_content)
            run_state.turn_messages.append(messages[-1])

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        self._run_stop = asyncio.Event()
        self._recover_stopped_turn_journal()
        for session_key in {
            *self._stopped_turns.keys(),
            *self._post_stop_buf.keys(),
        }:
            self._ensure_session_durability_retry(session_key)
        await self._connect_mcp()
        logger.info("Agent loop started")
        self._schedule_background(self._dispatch_restart_notify())

        while self._running:
            await self._turn_admission_slots.acquire()
            try:
                msg = await self._consume_inbound_or_stop()
                if msg is None:
                    self._turn_admission_slots.release()
                    break
            except BusClosedError:
                self._turn_admission_slots.release()
                if not self._running or self.bus.closed:
                    break
                raise
            except Exception as e:
                self._turn_admission_slots.release()
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            handoff_id = next(self._handoff_sequence)
            self._handoff_messages[handoff_id] = msg

            message_turn_id = self._message_turn_id(msg)
            cancelling_record = (
                self.turns.get(message_turn_id) if message_turn_id is not None else None
            )
            if message_turn_id is not None and (
                message_turn_id in self._turn_cancel_operations
                or (
                    cancelling_record is not None
                    and cancelling_record.state is TurnState.CANCELLING
                )
            ):
                self._retain_cancelling_handoff(handoff_id, msg)
                continue

            if self.intervention is not None and self.intervention.defer(
                ConversationRef(msg.channel, msg.chat_id, msg.session_key), msg
            ):
                self._handoff_messages.pop(handoff_id, None)
                self._hold_buffered_admission(msg)
                continue
            if msg.session_key in self._stop_requested:
                self._handoff_messages.pop(handoff_id, None)
                self._post_stop_buf.setdefault(msg.session_key, []).append(msg)
                self._hold_buffered_admission(msg)
                continue
            if self.has_pending_session_durability(msg.session_key):
                self._handoff_messages.pop(handoff_id, None)
                self._post_stop_buf.setdefault(msg.session_key, []).append(msg)
                self._hold_buffered_admission(msg)
                self.request_session_durability_retry(msg.session_key)
                if turn_id := self._message_turn_id(msg):
                    record = self.turns.get(turn_id)
                    if record is not None:
                        self.turns.fail(
                            turn_id,
                            "ingress rejected while earlier history awaits durable storage",
                        )
                continue
            if msg.channel == "system":
                # Standalone commands and system messages (e.g. subagent results):
                # Queue system messages normally without command routing.
                pending_turn = self._new_pending_turn(msg)
                task = asyncio.create_task(self._dispatch(msg, pending_turn=pending_turn))
                self._track_turn_task(msg, task, pending_turn)
                task.add_done_callback(lambda _task: self._turn_admission_slots.release())
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(
                    lambda t, k=msg.session_key: (
                        self._active_tasks.get(k, []) and self._active_tasks[k].remove(t)
                        if t in self._active_tasks.get(k, [])
                        else None
                    )
                )
                task.add_done_callback(self._superseded_tasks.discard)
                self._handoff_messages.pop(handoff_id, None)
            else:
                # Interruptible message: if a task is already running for this
                # session, hard-cancel it, merge the pending content, and
                # re-dispatch so that split messages (e.g. QQ text + image)
                # are processed as a single turn.
                sk = msg.session_key
                runtime_handoff = bool(msg.metadata.pop("_web_handoff_ready", False))
                live = [] if runtime_handoff else [
                    task for task in self._active_tasks.get(sk, []) if not task.done()
                ]

                # Interject: if the in-flight turn has already produced output,
                # queue this as a steer for the running loop to pick up at its
                # next step — don't cancel, keep its tool calls / thoughts.
                explicit_steer = bool(msg.metadata.get("_web_steer"))
                implicit_steer = msg.channel != "web" and bool(self._progressed.get(sk))
                if live and (explicit_steer or implicit_steer):
                    self._handoff_messages.pop(handoff_id, None)
                    self._steer_buf.setdefault(sk, []).append(msg)
                    self._hold_buffered_admission(msg)
                    self._wake_reply_waiter(sk)
                    continue

                if explicit_steer and not runtime_handoff:
                    if not self.activate_web_steer(sk, msg):
                        self.release_web_steer(sk, msg)

                if live:
                    for t in live:
                        self._superseded_tasks.add(t)
                        t.cancel()
                    for t in live:
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass

                    if handoff_id not in self._handoff_messages:
                        continue

                    if not self._running:
                        self._handoff_messages.pop(handoff_id, None)
                        pending_message = self._pending_buf.pop(sk, None)
                        interrupted_message = (
                            self._merge_messages(pending_message, msg)
                            if pending_message is not None
                            else msg
                        )
                        self._persist_pending_interruption(
                            _PendingTurn(
                                message=interrupted_message,
                                session_key=None,
                                transient=False,
                            )
                        )
                        self._turn_admission_slots.release()
                        continue
                    if sk in self._stop_requested:
                        self._handoff_messages.pop(handoff_id, None)
                        self._post_stop_buf.setdefault(sk, []).append(msg)
                        self._hold_buffered_admission(msg)
                        continue

                    prev = self._pending_buf.get(sk)
                    if prev:
                        msg = self._merge_messages(prev, msg)

                self._pending_buf[sk] = msg
                gen = self._session_gen.get(sk, 0) + 1
                self._session_gen[sk] = gen

                pending_turn = self._new_pending_turn(msg)
                task = asyncio.create_task(
                    self._dispatch(msg, gen=gen, pending_turn=pending_turn)
                )
                self._track_turn_task(msg, task, pending_turn)
                task.add_done_callback(lambda _task: self._turn_admission_slots.release())
                self._active_tasks.setdefault(sk, []).append(task)
                task.add_done_callback(
                    lambda t, k=sk: (
                        self._active_tasks.get(k, []) and self._active_tasks[k].remove(t)
                        if t in self._active_tasks.get(k, [])
                        else None
                    )
                )
                task.add_done_callback(self._superseded_tasks.discard)
                self._handoff_messages.pop(handoff_id, None)

    @staticmethod
    def _message_turn_id(msg: InboundMessage) -> str | None:
        return msg.turn_id or str(msg.metadata.get("turn_id") or "") or None

    def _snapshot_terminal_deferred(
        self,
        msg: InboundMessage,
        response: OutboundMessage,
    ) -> None:
        """Capture terminal handoff state before asynchronous outbound delivery."""
        turn_id = response.turn_id or self._message_turn_id(msg)
        if turn_id is None or not self.turns.terminal_deferred(turn_id):
            return
        response.turn_id = turn_id
        response.metadata = {**response.metadata, "_terminal_deferred": True}

    @staticmethod
    def _artifact_refs_from_message(
        msg: InboundMessage,
    ) -> tuple[dict[str, Any], ...]:
        values = msg.metadata.get("_artifact_refs") if isinstance(msg.metadata, dict) else None
        if not isinstance(values, list):
            return ()
        allowed = {"id", "name", "size", "mediaType", "media_type", "kind"}
        return tuple(
            {key: value for key, value in item.items() if key in allowed}
            for item in values[:16]
            if isinstance(item, dict) and item.get("id")
        )

    def _new_pending_turn(self, msg: InboundMessage) -> _PendingTurn:
        """Create durable dispatch ownership before a task can be cancelled."""
        if msg.ingress_ordinal <= 0:
            msg.ingress_ordinal = self._next_ingress_ordinal()
        return _PendingTurn(
            message=msg,
            session_key=None,
            transient=False,
            ordinal=msg.ingress_ordinal,
        )

    def _next_ingress_ordinal(self) -> int:
        """Return a process-monotonic order value for direct compatibility ingress."""
        counter = getattr(self, "_ingress_ordinals", None)
        if counter is None:
            self._ingress_ordinals = counter = count(time.time_ns())
        return next(counter)

    @staticmethod
    def _message_order_key(message: InboundMessage) -> tuple[int, float]:
        """Return the immutable ingress order assigned before queue admission."""
        try:
            timestamp = message.timestamp.timestamp()
        except (AttributeError, OSError, OverflowError, ValueError):
            timestamp = 0.0
        return (max(int(message.ingress_ordinal or 0), 0), timestamp)

    def _sort_ingress_messages(
        self,
        messages: list[InboundMessage],
    ) -> list[InboundMessage]:
        """Sort messages from different runtime buffers by original ingress order."""
        for message in messages:
            if message.ingress_ordinal <= 0:
                message.ingress_ordinal = self._next_ingress_ordinal()
        return sorted(
            messages,
            key=lambda message: (
                *self._message_order_key(message),
                str(message.event_id or ""),
            ),
        )

    def _track_turn_task(
        self,
        msg: InboundMessage,
        task: asyncio.Task[Any],
        pending_turn: _PendingTurn,
    ) -> None:
        self._task_pending_turns[task] = pending_turn
        turn_id = self._message_turn_id(msg)
        if turn_id is not None:
            self._turn_tasks[turn_id] = task

        def forget(completed: asyncio.Task[Any], tracked_id: str = turn_id) -> None:
            self._task_pending_turns.pop(completed, None)
            if tracked_id is not None and self._turn_tasks.get(tracked_id) is completed:
                self._turn_tasks.pop(tracked_id, None)

        task.add_done_callback(forget)

    def _extract_handoff_messages(
        self,
        predicate: Callable[[InboundMessage], bool],
    ) -> list[InboundMessage]:
        """Transfer consumed pre-dispatch messages to a cancellation owner."""
        selected = [
            (handoff_id, message)
            for handoff_id, message in self._handoff_messages.items()
            if predicate(message)
        ]
        for handoff_id, _ in selected:
            self._handoff_messages.pop(handoff_id, None)
            self._turn_admission_slots.release()
        return [message for _, message in selected]

    def _retain_cancelling_handoff(
        self,
        handoff_id: int,
        message: InboundMessage,
    ) -> None:
        """Move a late exact-cancel handoff directly to the durability owner."""
        self._handoff_messages.pop(handoff_id, None)
        self._retain_pending_interruption(
            self._new_pending_turn(message),
            content=self.tips.turn_stopped,
            control_kind="stop",
        )
        self.release_web_steer(message.session_key, message)
        self._hold_buffered_admission(message)
        self._release_buffered_admission(message)

    async def cancel_turn(self, turn_id: str, detail: str = "cancelled by user") -> bool:
        """Cancel one turn and wait for its runtime-owned durable cleanup."""
        if not self.begin_cancel_turn(turn_id, detail):
            return False
        operation = self._turn_cancel_operations.get(turn_id)
        if operation is None:
            return False
        return await asyncio.shield(operation)

    def begin_cancel_turn(
        self, turn_id: str, detail: str = "cancelled by user"
    ) -> bool:
        """Accept exact cancellation while cleanup continues under runtime ownership."""
        if turn_id in self._turn_cancel_operations:
            return True
        record = self.turns.get(turn_id)
        if record is None or record.state in {
            TurnState.COMPLETED,
            TurnState.CANCELLED,
            TurnState.FAILED,
        }:
            return False

        self.turns.begin_cancel(turn_id, detail)
        claim = self._claim_turn_cancellation(turn_id, record.session_key)
        operation = asyncio.create_task(
            self._cancel_turn_owned(turn_id, record.session_key, claim),
            name=f"nanocat.turn-cancel.{turn_id}",
        )
        self._turn_cancel_operations[turn_id] = operation

        def forget(completed: asyncio.Task[bool], tracked_id: str = turn_id) -> None:
            if completed.cancelled():
                logger.error("Turn cancellation cleanup was cancelled for {}", tracked_id)
            elif exception := completed.exception():
                logger.error(
                    "Turn cancellation cleanup failed for {} ({})",
                    tracked_id,
                    type(exception).__name__,
                )
            if self._turn_cancel_operations.get(tracked_id) is completed:
                self._turn_cancel_operations.pop(tracked_id, None)

        operation.add_done_callback(forget)
        return True

    def _claim_turn_cancellation(
        self,
        turn_id: str,
        session_key: str,
    ) -> _TurnCancellationClaim:
        """Synchronously transfer queued state to the exact-cancel owner."""
        messages = list(self.bus.drain_inbound_turn(turn_id))
        messages.extend(
            self._extract_handoff_messages(
                lambda message: self._message_turn_id(message) == turn_id
            )
        )
        if self.intervention is not None:
            messages.extend(self.intervention.extract_deferred_turn(turn_id))

        buffered = self._steer_buf.get(session_key, [])
        messages.extend(
            message for message in buffered if self._message_turn_id(message) == turn_id
        )
        retained_steer = [
            message for message in buffered if self._message_turn_id(message) != turn_id
        ]
        if retained_steer:
            self._steer_buf[session_key] = retained_steer
        else:
            self._steer_buf.pop(session_key, None)

        task = self._turn_tasks.get(turn_id)
        pending = self._pending_buf.get(session_key)
        if pending is not None and self._message_turn_id(pending) == turn_id:
            self._pending_buf.pop(session_key, None)
            if task is None:
                messages.append(pending)

        post_stop = self._post_stop_buf.get(session_key, [])
        messages.extend(
            message for message in post_stop if self._message_turn_id(message) == turn_id
        )
        retained_post_stop = [
            message for message in post_stop if self._message_turn_id(message) != turn_id
        ]
        if retained_post_stop:
            self._post_stop_buf[session_key] = retained_post_stop
        else:
            self._post_stop_buf.pop(session_key, None)
        return _TurnCancellationClaim(
            messages=self._sort_ingress_messages(messages),
            task=task,
            pending_turn=self._task_pending_turns.get(task) if task is not None else None,
        )

    async def _cancel_turn_owned(
        self,
        turn_id: str,
        session_key: str,
        claim: _TurnCancellationClaim,
    ) -> bool:
        """Finish extraction and durable interruption recording under runtime ownership."""
        record = self.turns.get(turn_id)
        if record is None:
            return False
        claimed_pending = [self._new_pending_turn(message) for message in claim.messages]

        def retain_claimed_inputs() -> None:
            if claim.pending_turn is not None and not claim.pending_turn.history_committed:
                self._retain_pending_interruption(
                    claim.pending_turn,
                    content=self.tips.turn_stopped,
                    control_kind="stop",
                )
            for pending_turn in claimed_pending:
                if not pending_turn.history_committed:
                    self._retain_pending_interruption(
                        pending_turn,
                        content=self.tips.turn_stopped,
                        control_kind="stop",
                    )
                self.release_web_steer(session_key, pending_turn.message)
                self._release_buffered_admission(pending_turn.message)

        try:
            task = claim.task
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            if self.intervention is not None:
                claimed_pending.extend(
                    self._new_pending_turn(message)
                    for message in await self.intervention.settle_deferred_turn(turn_id)
                )
            claimed_pending.extend(
                self._new_pending_turn(message)
                for message in self._extract_handoff_messages(
                    lambda message: self._message_turn_id(message) == turn_id
                )
            )
            claimed_pending.extend(
                self._new_pending_turn(message)
                for message in self.bus.drain_inbound_turn(turn_id)
            )
            ordered_messages = self._sort_ingress_messages(
                [pending_turn.message for pending_turn in claimed_pending]
            )
            pending_by_message = {id(item.message): item for item in claimed_pending}
            seen_messages: set[int] = set()
            ordered_pending: list[_PendingTurn] = []
            for message in ordered_messages:
                message_id = id(message)
                if message_id in seen_messages:
                    continue
                seen_messages.add(message_id)
                ordered_pending.append(pending_by_message[message_id])
            claimed_pending = ordered_pending

            if claim.pending_turn is not None and not claim.pending_turn.history_committed:
                persisted = await self._persist_pending_interruption_ordered(
                    claim.pending_turn,
                    content=self.tips.turn_stopped,
                    control_kind="stop",
                )
                if not persisted:
                    raise RuntimeError("cancelled active turn could not be persisted")

            stopped_store = getattr(self, "_stopped_turns", {})
            stopped_records = stopped_store.get(session_key, [])
            claimed_records = [item for item in stopped_records if item.turn_id == turn_id]
            if claimed_records:
                for stopped in self._canonicalize_stopped_turns(claimed_records):
                    try:
                        stopped.terminal_control = {"kind": "stop"}
                        self._persist_stopped_turn(
                            stopped,
                            self.tips.turn_stopped,
                            schedule_background=False,
                        )
                    except Exception:
                        self._persistence_error_count += 1
                        logger.exception(
                            "Failed to persist exact-cancelled turn for {}",
                            session_key,
                        )
                        raise
                    current = stopped_store.get(session_key, [])
                    for index, candidate in enumerate(current):
                        if (
                            candidate.turn_id == stopped.turn_id
                            and candidate.order_key == stopped.order_key
                        ):
                            current.pop(index)
                            break
                    if current:
                        stopped_store[session_key] = current
                    else:
                        stopped_store.pop(session_key, None)

            for pending_turn in claimed_pending:
                persisted = await self._persist_pending_interruption_ordered(
                    pending_turn,
                    content=self.tips.turn_stopped,
                    control_kind="stop",
                )
                self.release_web_steer(session_key, pending_turn.message)
                self._release_buffered_admission(pending_turn.message)
                if not persisted:
                    raise RuntimeError("cancelled turn could not be persisted")
            if self.intervention is not None:
                await self.intervention.cancel_turn(turn_id)
        except asyncio.CancelledError:
            retain_claimed_inputs()
            self.turns.finalize_cancel(
                turn_id, "turn cancellation cleanup was cancelled"
            )
            raise
        except Exception:
            retain_claimed_inputs()
            logger.exception(
                "Turn cancellation cleanup retained for durability: {}",
                turn_id,
            )
            self.turns.finalize_cancel(
                turn_id,
                "turn stopped; cleanup continues under runtime ownership",
            )
            return True
        self.turns.finalize_cancel(turn_id, record.detail)
        return True

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
        """Share one cancellation operation across repeated stop commands."""
        task = self._stop_operations.get(msg.session_key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._handle_stop_locked(msg),
                name=f"nanocat.stop.{msg.session_key}",
            )
            self._stop_operations[msg.session_key] = task

            def _forget(
                completed: asyncio.Task[OutboundMessage], key: str = msg.session_key
            ) -> None:
                if self._stop_operations.get(key) is completed:
                    self._stop_operations.pop(key, None)

            task.add_done_callback(_forget)
        result = await asyncio.shield(task)
        return replace(
            result,
            channel=msg.channel,
            chat_id=msg.chat_id,
            event_id=msg.event_id,
            correlation_id=msg.correlation_id,
            request_id=msg.request_id,
            principal_id=msg.principal_id,
        )

    async def _handle_stop_locked(self, msg: InboundMessage) -> OutboundMessage:
        """Cancel all active tasks and subagents for the session."""
        stop_persistence_failed = False
        retry_stopped_records: list[_StoppedTurn] = []
        self._stop_requested.add(msg.session_key)
        session_turn_ids = [
            record.turn_id
            for record in self.turns.snapshot()
            if record.session_key == msg.session_key
            and record.state
            not in {TurnState.COMPLETED, TurnState.CANCELLED, TurnState.FAILED}
        ]
        for turn_id in session_turn_ids:
            self.turns.begin_cancel(turn_id, "stopped by user")
        deferred_by_intervention = (
            self.intervention.extract_deferred_session(msg.session_key)
            if self.intervention is not None
            else []
        )
        for message in deferred_by_intervention:
            self.release_web_steer(msg.session_key, message)
            self._release_buffered_admission(message)
        handoff_messages = self._extract_handoff_messages(
            lambda message: message.session_key == msg.session_key
        )
        queued_messages = self.bus.drain_inbound(msg.session_key)
        self._pending_buf.pop(msg.session_key, None)
        self._session_gen.pop(msg.session_key, None)
        pending_steer = self._steer_buf.pop(msg.session_key, None)
        for message in pending_steer or ():
            self.release_web_steer(msg.session_key, message)
            self._release_buffered_admission(message)
        self._wake_reply_waiter(msg.session_key)
        self._progressed.pop(msg.session_key, None)
        tasks = list(
            dict.fromkeys(
                [
                    *self._active_tasks.pop(msg.session_key, []),
                    *self._direct_tasks.get(msg.session_key, set()),
                ]
            )
        )
        task_pending_turns = tuple(
            pending_turn
            for task in tasks
            if (pending_turn := self._task_pending_turns.get(task)) is not None
        )
        cancelled = sum(1 for task in tasks if not task.done() and task.cancel())
        try:
            if self.intervention is not None:
                try:
                    await self.intervention.cancel_session(msg.session_key)
                except Exception:
                    logger.exception("Failed to cancel intervention for {}", msg.session_key)
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            exact_operations = [
                operation
                for turn_id, operation in tuple(self._turn_cancel_operations.items())
                if turn_id in session_turn_ids and operation is not asyncio.current_task()
            ]
            exact_results: list[Any] = []
            if exact_operations:
                exact_results = list(
                    await asyncio.gather(*exact_operations, return_exceptions=True)
                )
            if self.intervention is not None:
                late_deferred = await self.intervention.settle_deferred_session(
                    msg.session_key
                )
                for message in late_deferred:
                    self.release_web_steer(msg.session_key, message)
                    self._release_buffered_admission(message)
                deferred_by_intervention.extend(late_deferred)
            queued_messages.extend(self.bus.drain_inbound(msg.session_key))
            exact_persistence_failed = any(
                isinstance(result, BaseException) for result in exact_results
            )
            try:
                sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
            except Exception:
                sub_cancelled = 0
                logger.exception("Failed to cancel subagents for {}", msg.session_key)
            stopped_records = self._stopped_turns.pop(msg.session_key, [])
            persistence_failed = exact_persistence_failed
            for pending_turn in task_pending_turns:
                if pending_turn.history_committed:
                    continue
                try:
                    stopped_records.append(self._stopped_turn_from_pending(pending_turn))
                    pending_turn.history_committed = True
                except Exception:
                    persistence_failed = True
                    self._persistence_error_count += 1
                    self._post_stop_buf.setdefault(msg.session_key, []).append(
                        pending_turn.message
                    )
                    logger.exception(
                        "Failed to prepare cancelled dispatch for {}",
                        msg.session_key,
                    )
            for deferred_message in deferred_by_intervention:
                try:
                    stopped_records.append(
                        self._stopped_turn_from_pending(
                            self._new_pending_turn(deferred_message)
                        )
                    )
                except Exception:
                    persistence_failed = True
                    self._persistence_error_count += 1
                    self._post_stop_buf.setdefault(msg.session_key, []).append(
                        deferred_message
                    )
                    logger.exception(
                        "Failed to prepare deferred stopped input for {}",
                        msg.session_key,
                    )
            if pending_steer:
                merged = pending_steer[0]
                for extra in pending_steer[1:]:
                    merged = self._merge_messages(merged, extra)
                pending_turn = self._new_pending_turn(merged)
                try:
                    stopped_records.append(self._stopped_turn_from_pending(pending_turn))
                except Exception:
                    persistence_failed = True
                    self._persistence_error_count += 1
                    self._post_stop_buf.setdefault(msg.session_key, []).append(merged)
                    logger.exception(
                        "Failed to prepare stopped steer input for {}",
                        msg.session_key,
                    )
            for queued in [*handoff_messages, *queued_messages]:
                self.release_web_steer(msg.session_key, queued)
                try:
                    stopped_records.append(
                        self._stopped_turn_from_pending(
                            self._new_pending_turn(queued)
                        )
                    )
                except Exception:
                    persistence_failed = True
                    self._persistence_error_count += 1
                    self._post_stop_buf.setdefault(msg.session_key, []).append(queued)
                    logger.exception(
                        "Failed to prepare queued stopped input for {}",
                        msg.session_key,
                    )
            total = cancelled + sub_cancelled
            if not total and stopped_records:
                total = len(stopped_records)
            content = self.tips.stop_tasks.format(count=total) if total else self.tips.stop_idle
            stopped_records = self._canonicalize_stopped_turns(stopped_records)
            for stopped in stopped_records:
                try:
                    stopped.terminal_control = {"kind": "stop"}
                    self._persist_stopped_turn(stopped, self.tips.turn_stopped)
                except Exception:
                    persistence_failed = True
                    self._persistence_error_count += 1
                    retry_stopped_records.append(
                        replace(
                            stopped,
                            terminal_content=self.tips.turn_stopped,
                            terminal_control={"kind": "stop"},
                        )
                    )
                    logger.exception(
                        "Failed to persist stopped turn for {}",
                        msg.session_key,
                    )
            if persistence_failed:
                stop_persistence_failed = True
                content = self.tips.stop_persist_failed
            if retry_stopped_records:
                self._stopped_turns[msg.session_key] = list(retry_stopped_records)
            for turn_id in session_turn_ids:
                record = self.turns.get(turn_id)
                if record is None or record.state in {
                    TurnState.COMPLETED,
                    TurnState.CANCELLED,
                    TurnState.FAILED,
                }:
                    continue
                if persistence_failed:
                    self.turns.finalize_fail(
                        turn_id, "stopped turn persistence failed"
                    )
                else:
                    self.turns.finalize_cancel(turn_id, "stopped by user")
        finally:
            self._stop_requested.discard(msg.session_key)
            if retry_stopped_records:
                self._stopped_turns.setdefault(
                    msg.session_key,
                    list(retry_stopped_records),
                )
            else:
                self._stopped_turns.pop(msg.session_key, None)
            if self.has_pending_session_durability(msg.session_key):
                self._wake_durability_retry(msg.session_key)
        deferred = self._post_stop_buf.pop(msg.session_key, [])
        stopped_deferred = [
            message
            for message in deferred
            if self._message_turn_id(message) in session_turn_ids
        ]
        requeue_deferred = [
            message
            for message in deferred
            if self._message_turn_id(message) not in session_turn_ids
        ]
        if stopped_deferred:
            grouped: dict[str, InboundMessage] = {}
            for message in stopped_deferred:
                group_key = self._message_turn_id(message) or f"event:{message.event_id}"
                existing = grouped.get(group_key)
                grouped[group_key] = (
                    self._merge_messages(existing, message)
                    if existing is not None
                    else message
                )
            records: list[_StoppedTurn] = []
            try:
                records = self._canonicalize_stopped_turns(
                    [
                        self._stopped_turn_from_pending(self._new_pending_turn(message))
                        for message in grouped.values()
                    ]
                )
                for record_index, stopped in enumerate(records):
                    stopped.terminal_control = {"kind": "stop"}
                    self._persist_stopped_turn(stopped, self.tips.turn_stopped)
            except Exception:
                self._persistence_error_count += 1
                stop_persistence_failed = True
                content = self.tips.stop_persist_failed
                if records:
                    self._stopped_turns.setdefault(msg.session_key, []).extend(
                        replace(record, terminal_content=content)
                        for record in records[record_index:]
                    )
                else:
                    self._post_stop_buf.setdefault(msg.session_key, []).extend(
                        grouped.values()
                    )
                for failed_turn_id in {
                    self._message_turn_id(message) for message in stopped_deferred
                } - {None}:
                    self.turns.finalize_fail(
                        failed_turn_id,
                        "late stopped turn persistence failed",
                    )
                logger.exception(
                    "Failed to persist input joined to stopped turn for {}",
                    msg.session_key,
                )
            finally:
                for message in stopped_deferred:
                    self.release_web_steer(msg.session_key, message)
                    self._release_buffered_admission(message)
        if requeue_deferred:
            for message in requeue_deferred:
                self._release_buffered_admission(message)
            merged = requeue_deferred[0]
            for extra in requeue_deferred[1:]:
                merged = self._merge_messages(merged, extra)
            try:
                await self.bus.publish_inbound(merged)
            except Exception:
                logger.exception(
                    "Failed to requeue input received during stop for {}", msg.session_key
                )
                pending_requeue = self._new_pending_turn(merged)
                if not self._persist_pending_interruption(
                    pending_requeue,
                    content=self.tips.turn_stopped,
                    control_kind="stop",
                ):
                    stop_persistence_failed = True
                    content = self.tips.stop_persist_failed
        if self.has_pending_session_durability(msg.session_key):
            self._wake_durability_retry(msg.session_key)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            event_id=msg.event_id,
            correlation_id=msg.correlation_id,
            request_id=msg.request_id,
            principal_id=msg.principal_id,
            metadata={
                "_control": True,
                "_command": "stop",
                "persistence_failed": stop_persistence_failed,
            },
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
                def add(current: Any) -> Mapping[str, Any]:
                    choices = list(current.agents.defaults.model_choice)
                    if full_model in choices:
                        return {}
                    return {"agents.defaults.modelChoice": [*choices, full_model]}

                await self.configuration.mutate_runtime(add)
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
                deleted: list[str] = []

                def remove(current: Any) -> Mapping[str, Any]:
                    current_models = list(current.agents.defaults.model_choice)
                    if choice_number < 1 or choice_number > len(current_models):
                        raise ValueError(f"Invalid model choice: {choice_number}")
                    if len(current_models) <= 1:
                        raise ValueError("Can't delete the last model")
                    deleted_model = current_models.pop(choice_number - 1)
                    defaults = current.agents.defaults
                    if deleted_model in {
                        defaults.model,
                        defaults.subagent_model,
                        defaults.assistant_model,
                        defaults.vision_model,
                        defaults.compaction_model,
                    }:
                        raise ValueError(f"Model `{deleted_model}` is assigned to an active slot")
                    deleted.append(deleted_model)
                    return {"agents.defaults.modelChoice": current_models}

                await self.configuration.mutate_runtime(remove)
                deleted_model = deleted[0]
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
                await self.configuration.update_runtime(
                    {"agents.defaults.reasoningEffort": effort},
                    on_applied=lambda current: self._provider_resolver.update_reasoning_effort(
                        current.agents.defaults.reasoning_effort
                    ),
                )
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
                paths = {
                    "agent": "agents.defaults.model",
                    "subagent": "agents.defaults.subagentModel",
                    "assistant": "agents.defaults.assistantModel",
                }
                def select(current: Any) -> Mapping[str, Any]:
                    if full_model not in current.agents.defaults.model_choice:
                        raise ValueError(f"Unknown model choice: {full_model}")
                    return {paths[subcmd]: full_model}

                await self.configuration.mutate_runtime(select)

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
            items = self.sessions.list_sessions(
                msg.channel,
                min_turns=3,
                limit=limit,
                chat_id=msg.chat_id,
            )
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
            s = self.sessions.get_session(msg.channel, arg, chat_id=msg.chat_id)
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
            s = self.sessions.get_session(msg.channel, arg, chat_id=msg.chat_id)
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
    def _working_result_preview(result: Any, max_chars: int = 8_000) -> Any:
        """Return a redacted bounded value suitable for a live Working event."""
        value: Any = result
        if isinstance(result, str):
            try:
                value = json.loads(result)
            except json.JSONDecodeError:
                value = result
        value = redact_value(value)
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            encoded = str(value)
        if len(encoded) <= max_chars:
            return value
        return {
            "truncated": True,
            "totalChars": len(encoded),
            "preview": encoded[:max_chars],
        }

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
        self._nowledge_working_memory_loaded.pop(session.key, None)
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
                title=USER_TEXT.intervention_invalid
                if action.error
                else USER_TEXT.intervention_no_pending,
                message=action.error or "",
            )
            resolved = None
        elif (
            action.action is not InterventionAction.REVOKE_SESSION
            and (pending := self.intervention.current_pending(conversation, principal_id))
            is not None
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

    async def _dispatch(
        self,
        msg: InboundMessage,
        gen: int = 0,
        pending_turn: _PendingTurn | None = None,
    ) -> None:
        """Track one consumed input across queue waits, execution and cancellation."""
        pending_turn = pending_turn or self._new_pending_turn(msg)
        try:
            await self._dispatch_inner(msg, gen, pending_turn)
            if not pending_turn.history_committed:
                self._persist_pending_failure(
                    pending_turn,
                    RuntimeError("Turn ended without a durable terminal record."),
                )
        except asyncio.CancelledError:
            persisted = True
            if not pending_turn.history_committed:
                if msg.session_key in self._stop_requested:
                    self._stash_pending_turn(msg.session_key, pending_turn)
                elif asyncio.current_task() not in self._superseded_tasks:
                    record = (
                        self.turns.get(turn_id)
                        if (turn_id := self._message_turn_id(msg))
                        else None
                    )
                    stopped_by_user = (
                        record is not None
                        and record.state is TurnState.CANCELLING
                        and record.detail in {"cancelled by user", "stopped by user"}
                    )
                    runtime_shutdown = (
                        record is not None
                        and record.state is TurnState.CANCELLING
                        and record.detail == "runtime shutdown"
                    )
                    persisted = await self._persist_pending_interruption_ordered(
                        pending_turn,
                        content=(
                            self.tips.turn_stopped
                            if stopped_by_user
                            else self.tips.turn_interrupted
                            if runtime_shutdown
                            else self.tips.turn_cancelled
                        ),
                        control_kind=(
                            "stop"
                            if stopped_by_user
                            else "runtime_shutdown"
                            if runtime_shutdown
                            else "interrupted"
                        ),
                    )
            if turn_id := self._message_turn_id(msg):
                record = self.turns.get(turn_id)
                if record is not None and record.state is not TurnState.CANCELLING:
                    if persisted:
                        self.turns.cancel(turn_id, "turn dispatch cancelled")
                    else:
                        self.turns.fail(turn_id, "turn interruption persistence failed")
            raise
        except Exception as exc:
            persisted = True
            if not pending_turn.history_committed:
                persisted = self._persist_pending_failure(pending_turn, exc)
            if turn_id := self._message_turn_id(msg):
                record = self.turns.get(turn_id)
                if record is not None and record.state is not TurnState.CANCELLING:
                    self.turns.fail(
                        turn_id,
                        "turn dispatch failed"
                        if persisted
                        else "turn failure persistence failed",
                    )
            raise
        else:
            if pending_turn.handed_off:
                return
            if turn_id := self._message_turn_id(msg):
                record = self.turns.get(turn_id)
                if record is not None and record.state not in {
                    TurnState.COMPLETED,
                    TurnState.CANCELLED,
                    TurnState.FAILED,
                }:
                    self.turns.fail(turn_id, "turn ended without a terminal state")
        finally:
            if gen and self._session_gen.get(msg.session_key) == gen:
                self._session_gen.pop(msg.session_key, None)

    async def _dispatch_inner(
        self,
        msg: InboundMessage,
        gen: int,
        pending_turn: _PendingTurn,
    ) -> None:
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
                    response = await self._process_message(msg, pending_turn=pending_turn)

                    # Discard if a newer message for the same session arrived
                    # during processing (hard interrupt / merge).
                    if gen and self._session_gen.get(msg.session_key, 0) != gen:
                        return

                    if response is not None:
                        self._snapshot_terminal_deferred(msg, response)
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
                    if turn_id := self._message_turn_id(msg):
                        self.turns.cancel(turn_id, "turn task cancelled")
                    logger.info("Task cancelled for session {}", msg.session_key)
                    raise
                except Exception as exc:
                    error_detail = self._public_turn_error(exc)
                    if isinstance(exc, _TurnProviderError):
                        logger.error(
                            "Provider failed for session {}: {}",
                            msg.session_key,
                            error_detail["message"],
                        )
                    else:
                        logger.exception("Error processing message for session {}", msg.session_key)
                    persisted = self._persist_pending_failure(pending_turn, exc)
                    turn_id = self._message_turn_id(msg)
                    if turn_id is not None:
                        self.turns.fail(
                            turn_id,
                            error_detail["message"]
                            if persisted
                            else "turn failure persistence failed",
                        )
                    failure_response = OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=self.tips.error,
                        event_id=msg.event_id,
                        correlation_id=msg.correlation_id,
                        request_id=msg.request_id,
                        turn_id=turn_id,
                        principal_id=msg.principal_id,
                        metadata={
                            **(msg.metadata or {}),
                            "_turn_failed": True,
                            "_turn_error": error_detail,
                        },
                    )
                    self._snapshot_terminal_deferred(msg, failure_response)
                    await self.bus.publish_outbound(failure_response)
                finally:
                    sk = msg.session_key
                    if not gen or self._session_gen.get(sk, 0) == gen:
                        self._progressed.pop(sk, None)
                        orphans = self._steer_buf.pop(sk, None)
                        if orphans:
                            requeueable: list[InboundMessage] = []
                            handoff_turn_ids: set[str] = set()
                            for orphan in orphans:
                                self._release_buffered_admission(orphan)
                                joined = bool(
                                    isinstance(orphan.metadata, dict)
                                    and orphan.metadata.get("_web_joined_turn")
                                )
                                if joined and not self.activate_web_steer(sk, orphan):
                                    self.release_web_steer(sk, orphan)
                                    self._persist_pending_interruption(
                                        _PendingTurn(
                                            message=orphan,
                                            session_key=None,
                                            transient=False,
                                        )
                                    )
                                    continue
                                if joined and (joined_turn_id := self._message_turn_id(orphan)):
                                    handoff_turn_ids.add(joined_turn_id)
                                if not joined:
                                    self.release_web_steer(sk, orphan)
                                requeueable.append(orphan)
                            if requeueable:
                                merged = requeueable[0]
                                for extra in requeueable[1:]:
                                    merged = self._merge_messages(merged, extra)
                                merged.metadata["_web_handoff_ready"] = True
                                try:
                                    await self.bus.publish_inbound(merged)
                                    if self._message_turn_id(msg) in handoff_turn_ids:
                                        pending_turn.handed_off = True
                                except asyncio.CancelledError:
                                    persisted = self._persist_pending_interruption(
                                        _PendingTurn(
                                            message=merged,
                                            session_key=None,
                                            transient=False,
                                        )
                                    )
                                    for cancelled_turn_id in handoff_turn_ids:
                                        record = self.turns.get(cancelled_turn_id)
                                        if record is None or record.state in {
                                            TurnState.COMPLETED,
                                            TurnState.CANCELLED,
                                            TurnState.FAILED,
                                            TurnState.CANCELLING,
                                        }:
                                            continue
                                        if persisted:
                                            self.turns.cancel(
                                                cancelled_turn_id,
                                                "deferred steer dispatch cancelled",
                                            )
                                        else:
                                            self.turns.fail(
                                                cancelled_turn_id,
                                                "deferred steer persistence failed",
                                            )
                                    raise
                                except Exception:
                                    logger.exception(
                                        "Failed to requeue deferred steer for {}",
                                        sk,
                                    )
                                    persisted = self._persist_pending_interruption(
                                        _PendingTurn(
                                            message=merged,
                                            session_key=None,
                                            transient=False,
                                        )
                                    )
                                    for failed_turn_id in {
                                        self._message_turn_id(orphan)
                                        for orphan in requeueable
                                        if self._message_turn_id(orphan) is not None
                                    }:
                                        record = self.turns.get(failed_turn_id)
                                        if record is None or record.state in {
                                            TurnState.COMPLETED,
                                            TurnState.CANCELLED,
                                            TurnState.FAILED,
                                            TurnState.CANCELLING,
                                        }:
                                            continue
                                        if persisted:
                                            self.turns.cancel(
                                                failed_turn_id,
                                                "deferred steer delivery failed",
                                            )
                                        else:
                                            self.turns.fail(
                                                failed_turn_id,
                                                "deferred steer persistence failed",
                                            )

    async def close_mcp(self) -> None:
        """Drain pending background archives, terminate ssh/proc sessions, close MCP."""
        durability_task = self._durability_retry_task
        durability_tasks = (
            (durability_task,)
            if durability_task is not None
            and durability_task is not asyncio.current_task()
            and not durability_task.done()
            else ()
        )
        for task in durability_tasks:
            task.cancel()
        if durability_tasks:
            await asyncio.gather(*durability_tasks, return_exceptions=True)
        self._durability_retry_task = None
        shutdown_turn_ids = [
            record.turn_id
            for record in self.turns.snapshot()
            if record.state
            not in {TurnState.COMPLETED, TurnState.CANCELLED, TurnState.FAILED}
        ]
        for turn_id in shutdown_turn_ids:
            self.turns.begin_cancel(turn_id, "runtime shutdown")
        active_tasks = tuple(
            dict.fromkeys(
                task
                for tasks in (*self._active_tasks.values(), *self._direct_tasks.values())
                for task in tasks
                if task is not asyncio.current_task() and not task.done()
            )
        )
        active_pending_turns = tuple(
            pending_turn
            for task in active_tasks
            if (pending_turn := self._task_pending_turns.get(task)) is not None
        )
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        for pending_turn in active_pending_turns:
            if not pending_turn.history_committed:
                self._persist_pending_interruption(
                    pending_turn,
                    content=self.tips.turn_interrupted,
                    control_kind="runtime_shutdown",
                )
        self._active_tasks.clear()
        self._direct_tasks.clear()
        self._turn_tasks.clear()
        self._task_pending_turns.clear()
        self._pending_buf.clear()
        self._exclusive_sessions.clear()
        stop_operations = tuple(
            task
            for task in self._stop_operations.values()
            if task is not asyncio.current_task() and not task.done()
        )
        if stop_operations:
            await asyncio.gather(
                *(asyncio.shield(task) for task in stop_operations),
                return_exceptions=True,
            )
        self._stop_operations.clear()
        cancel_operations = tuple(
            task
            for task in self._turn_cancel_operations.values()
            if task is not asyncio.current_task() and not task.done()
        )
        if cancel_operations:
            await asyncio.gather(
                *(asyncio.shield(task) for task in cancel_operations),
                return_exceptions=True,
            )
        self._turn_cancel_operations.clear()
        if self.intervention is not None:
            await self.intervention.close()
        self._stop_requested.clear()
        self._superseded_tasks.clear()
        abandoned_steer = [
            message for messages in self._steer_buf.values() for message in messages
        ]
        for message in abandoned_steer:
            self.release_web_steer(message.session_key, message)
            self._release_buffered_admission(message)
        self._steer_buf.clear()
        self._web_steer_reservations.clear()
        self._web_turn_attachment_usage.clear()
        deferred_messages = [
            message for messages in self._post_stop_buf.values() for message in messages
        ]
        for message in deferred_messages:
            self.release_web_steer(message.session_key, message)
            self._release_buffered_admission(message)
        self._post_stop_buf.clear()
        handoff_messages = self._extract_handoff_messages(lambda _message: True)
        queued_messages = self.bus.drain_inbound()
        for message in [
            *abandoned_steer,
            *deferred_messages,
            *handoff_messages,
            *queued_messages,
        ]:
            self._persist_pending_interruption(
                self._new_pending_turn(message),
                content=self.tips.turn_interrupted,
                control_kind="runtime_shutdown",
            )
        self._flush_stopped_turn_retries()
        for session_key, records in tuple(self._pending_durability.items()):
            retained: list[_PendingDurabilityRecord] = []
            for record in records:
                if not self._journal_pending_turn(record):
                    retained.append(record)
            if retained:
                self._pending_durability[session_key] = retained
            else:
                self._pending_durability.pop(session_key, None)

        unresolved_turn_ids = {
            stopped.turn_id
            for records in self._stopped_turns.values()
            for stopped in records
            if stopped.turn_id is not None
        }
        unresolved_turn_ids.update(
            turn_id
            for records in self._pending_durability.values()
            for pending_record in records
            if (turn_id := self._message_turn_id(pending_record.message)) is not None
        )
        for turn_id in shutdown_turn_ids:
            record = self.turns.get(turn_id)
            if record is None or record.state is not TurnState.CANCELLING:
                continue
            if turn_id in unresolved_turn_ids:
                self.turns.finalize_fail(
                    turn_id,
                    "runtime shutdown persistence failed",
                )
            else:
                self.turns.finalize_cancel(turn_id, "runtime shutdown")

        background_tasks = tuple(
            task
            for task in self._background_tasks
            if task is not asyncio.current_task() and not task.done()
        )
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        for pending in self._background_pending.values():
            closer = getattr(pending, "close", None)
            if callable(closer):
                closer()
        self._background_tasks.clear()
        self._background_scopes.clear()
        self._background_keys.clear()
        self._background_pending.clear()

        await self.mcp_host.close()
        await self.tool_host.close()
        await self.sessions.close()
        await self._vision_fallback.close()
        await self._provider_resolver.close()
        if self.nowledge_client is not None:
            await self.nowledge_client.close()
        if self._recent_log_sink_id is not None:
            logger.remove(self._recent_log_sink_id)
            self._recent_log_sink_id = None
        unresolved_persistence = sum(
            len(records) for records in self._stopped_turns.values()
        ) + sum(len(records) for records in self._pending_durability.values())
        if unresolved_persistence:
            raise RuntimeError(
                f"{unresolved_persistence} turn persistence owner(s) remain unresolved"
            )

    @staticmethod
    def _close_background_awaitable(awaitable: Awaitable[Any]) -> None:
        closer = getattr(awaitable, "close", None)
        if callable(closer):
            closer()

    def _schedule_background(
        self,
        coro: Awaitable[Any],
        *,
        storage_scope: str | None = None,
        kind: str | None = None,
    ) -> None:
        """Schedule bounded post-turn work with one pending update per scope and kind."""
        key = (storage_scope, kind) if storage_scope is not None and kind else None
        if key is not None:
            existing = self._background_keys.get(key)
            if existing is not None and not existing.done():
                previous = self._background_pending.pop(key, None)
                if previous is not None:
                    self._close_background_awaitable(previous)
                self._background_pending[key] = coro
                return
            if len(self._background_keys) >= self._MAX_BACKGROUND_TASKS:
                self._close_background_awaitable(coro)
                logger.warning(
                    "Background {} admission skipped at the global task limit",
                    kind,
                )
                return

            async def run_coalesced() -> None:
                current = coro
                while True:
                    try:
                        await current
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error("Background task failed: {}", exc)
                    next_awaitable = self._background_pending.pop(key, None)
                    if next_awaitable is None:
                        return
                    current = next_awaitable

            scheduled: Awaitable[Any] = run_coalesced()
        else:
            scheduled = coro
        task = asyncio.create_task(scheduled)
        self._background_tasks.append(task)
        if storage_scope is not None:
            self._background_scopes[task] = storage_scope
        if key is not None:
            self._background_keys[key] = task

        def _forget(done: asyncio.Task) -> None:
            try:
                self._background_tasks.remove(done)
            except ValueError:
                pass
            self._background_scopes.pop(done, None)
            if key is not None and self._background_keys.get(key) is done:
                self._background_keys.pop(key, None)
                pending = self._background_pending.pop(key, None)
                if pending is not None:
                    self._close_background_awaitable(pending)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logger.error("Background task failed: {}", error)

        task.add_done_callback(_forget)

    async def cancel_background_for_session(self, storage_scope: str) -> int:
        """Cancel and drain post-turn work owned by one deleted session."""
        tasks = [
            task
            for task, scope in tuple(self._background_scopes.items())
            if scope == storage_scope and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for key, pending in tuple(self._background_pending.items()):
            if key[0] != storage_scope:
                continue
            self._background_pending.pop(key, None)
            self._close_background_awaitable(pending)
        return len(tasks)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        for record in self.turns.snapshot():
            if record.state not in {
                TurnState.COMPLETED,
                TurnState.CANCELLED,
                TurnState.FAILED,
            }:
                self.turns.begin_cancel(record.turn_id, "runtime shutdown")
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
        for tasks in self._direct_tasks.values():
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
        pending_turn: _PendingTurn | None = None,
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
            turn_defaults = self._config.agents.defaults
            turn_model = turn_defaults.model
            turn_effort = turn_defaults.reasoning_effort
            turn_pulse = turn_defaults.pulse_enabled
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
                pulse=turn_pulse,
                ssh_sessions=self.ssh.context_block(),
                proc_sessions=self.procs.context_block(),
                subs=self.subagents.context_block(),
            )
            run_state = _TurnRunState(turn_messages=[messages[-1]])
            if pending_turn is not None:
                pending_turn.session = session
                pending_turn.run_state = run_state
            turn_id = str(msg.metadata.get("turn_id") or uuid.uuid4().hex)
            self.turns.start(
                turn_id,
                msg.session_key,
                principal_id,
                request_id=str(msg.metadata.get("request_id") or "") or None,
                conversation_id=str(msg.metadata.get("_web_session_id") or "") or None,
            )
            tool_context = ToolExecutionContext(
                turn_id=turn_id,
                session_key=msg.session_key,
                storage_scope=session.key,
                conversation=ConversationRef(channel, chat_id, msg.session_key),
                principal_id=principal_id,
                state_hook=lambda state: self.turns.transition(turn_id, state),
                message_id=msg.metadata.get("message_id"),
                session=session,
                model=turn_model,
                subagent_model=(
                    turn_defaults.subagent_model
                    or turn_defaults.assistant_model
                    or turn_model
                ),
                reasoning_effort=turn_effort,
                pulse_enabled=turn_pulse,
                user_input=msg.content,
            )
            try:
                final_content, _, _ = await self._run_agent_loop(
                    messages,
                    tool_context=tool_context,
                    run_state=run_state,
                )
            except ToolTurnAbortedError as exc:
                if msg.session_key in self._stop_requested:
                    self._stash_stopped_turn(
                        msg.session_key,
                        session,
                        run_state,
                        transient,
                        pending_turn,
                        turn_id,
                    )
                    if pending_turn is not None:
                        pending_turn.history_committed = True
                    self._forget_message_turn(tool_context.turn_id)
                    self.turns.cancel(turn_id, "turn stopped by user")
                    raise asyncio.CancelledError from exc
                self._forget_message_turn(tool_context.turn_id)
                error_detail = {
                    "code": "tool_turn_aborted",
                    "title": "Tool execution aborted",
                    "message": str(redact_value(exc.message))[:2_000],
                }
                self._persist_aborted_turn(
                    session,
                    run_state,
                    exc.message,
                    transient,
                    self._artifact_refs_from_message(msg),
                    turn_id=turn_id,
                    runtime_session_key=msg.session_key,
                    terminal_error=error_detail,
                )
                self._mark_turn_history_committed(msg, pending_turn)
                self.turns.fail(turn_id, "tool execution aborted")
                return OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=exc.message,
                    event_id=msg.event_id,
                    correlation_id=msg.correlation_id,
                    request_id=msg.request_id,
                    turn_id=turn_id,
                    principal_id=msg.principal_id,
                    metadata={
                        **(msg.metadata or {}),
                        "_turn_failed": True,
                        "_turn_error": error_detail,
                        **self._turn_timing_metadata(turn_id),
                    },
                )
            except asyncio.CancelledError:
                if msg.session_key in self._stop_requested:
                    self._stash_stopped_turn(
                        msg.session_key,
                        session,
                        run_state,
                        transient,
                        pending_turn,
                        turn_id,
                    )
                    if pending_turn is not None:
                        pending_turn.history_committed = True
                elif asyncio.current_task() not in self._superseded_tasks:
                    record = self.turns.get(turn_id)
                    stopped_by_user = (
                        record is not None
                        and record.state is TurnState.CANCELLING
                        and record.detail in {"cancelled by user", "stopped by user"}
                    )
                    runtime_shutdown = (
                        record is not None
                        and record.state is TurnState.CANCELLING
                        and record.detail == "runtime shutdown"
                    )
                    self._persist_interrupted_turn(
                        session,
                        run_state,
                        transient,
                        self._artifact_refs_from_message(msg),
                        turn_id,
                        runtime_session_key=msg.session_key,
                        content=(
                            self.tips.turn_stopped
                            if stopped_by_user
                            else self.tips.turn_interrupted
                            if runtime_shutdown
                            else self.tips.turn_cancelled
                        ),
                        control_kind=(
                            "stop"
                            if stopped_by_user
                            else "runtime_shutdown"
                            if runtime_shutdown
                            else "interrupted"
                        ),
                    )
                    if pending_turn is not None:
                        pending_turn.history_committed = True
                self._forget_message_turn(tool_context.turn_id)
                self.turns.cancel(turn_id, "turn task cancelled")
                raise
            except Exception as exc:
                self._forget_message_turn(tool_context.turn_id)
                error_detail = self._public_turn_error(exc)
                self._persist_aborted_turn(
                    session,
                    run_state,
                    self.tips.error,
                    transient,
                    self._artifact_refs_from_message(msg),
                    turn_id=turn_id,
                    runtime_session_key=msg.session_key,
                    terminal_error=error_detail,
                )
                self._mark_turn_history_committed(msg, pending_turn)
                self.turns.fail(
                    turn_id,
                    f"turn processing failed ({type(exc).__name__})",
                )
                raise
            finally:
                if self.intervention is not None:
                    await self.intervention.finish_turn(tool_context.turn_id)
            _old_msg_count_sys = len(session.messages)
            self._annotate_turn_entries(
                run_state.turn_messages,
                turn_id,
                status="completed",
            )
            try:
                self._persist_turn_entries(
                    session,
                    run_state.turn_messages,
                    artifact_refs=self._artifact_refs_from_message(msg),
                )
            except Exception:
                raise
            self.turns.complete(turn_id)
            self._mark_turn_history_committed(msg, pending_turn)
            if not transient:
                self._schedule_background(
                    self.memory_compactor.maybe_compact_by_tokens(session),
                    storage_scope=session.key,
                    kind="compaction",
                )
                if self.thread_manager:
                    _new_msgs_sys = session.messages[_old_msg_count_sys:]
                    self._schedule_background(
                        self.thread_manager.append_turn_and_distill(session, _new_msgs_sys),
                        storage_scope=session.key,
                        kind="nowledge",
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
                event_id=msg.event_id,
                correlation_id=msg.correlation_id,
                request_id=msg.request_id,
                turn_id=turn_id,
                principal_id=msg.principal_id,
                metadata={
                    **(msg.metadata or {}),
                    **self._turn_timing_metadata(turn_id),
                },
            )

        logger.info(
            "Processing message from {}:{} ({} chars, {} media)",
            msg.channel,
            msg.sender_id,
            len(msg.content),
            len(msg.media or []),
        )

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
        turn_defaults = self._config.agents.defaults
        turn_model = turn_defaults.model
        turn_effort = turn_defaults.reasoning_effort
        turn_pulse = turn_defaults.pulse_enabled
        initial_messages = self.context.build_messages(
            history=history,
            compacted_memory=session.compacted_memory,
            injected_memories=injected_memories,
            working_memory=working_memory,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            pulse=turn_pulse,
            ssh_sessions=self.ssh.context_block(),
            proc_sessions=self.procs.context_block(),
            subs=self.subagents.context_block(),
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            meta["_thinking"] = not tool_hint and not content.lstrip().startswith("↪")
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    event_id=msg.event_id,
                    correlation_id=msg.correlation_id,
                    request_id=msg.request_id,
                    turn_id=turn_id,
                    principal_id=msg.principal_id,
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
                    event_id=msg.event_id,
                    correlation_id=msg.correlation_id,
                    request_id=msg.request_id,
                    turn_id=turn_id,
                    principal_id=msg.principal_id,
                    metadata=meta,
                )
            )

        async def _bus_thinking(payload: dict[str, Any]) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_thinking"] = True
            meta["_thinking_payload"] = payload
            reasoning = payload.get("reasoningContent")
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=reasoning if isinstance(reasoning, str) else "",
                    event_id=msg.event_id,
                    correlation_id=msg.correlation_id,
                    request_id=msg.request_id,
                    turn_id=turn_id,
                    principal_id=msg.principal_id,
                    metadata=meta,
                )
            )

        run_state = _TurnRunState(turn_messages=[initial_messages[-1]])
        if pending_turn is not None:
            pending_turn.session = session
            pending_turn.run_state = run_state
        turn_id = str(msg.metadata.get("turn_id") or uuid.uuid4().hex)
        principal_id = msg.principal_id or msg.sender_id
        self.turns.start(
            turn_id,
            msg.session_key,
            principal_id,
            request_id=str(msg.metadata.get("request_id") or "") or None,
            conversation_id=str(msg.metadata.get("_web_session_id") or "") or None,
        )
        tool_context = ToolExecutionContext(
            turn_id=turn_id,
            session_key=msg.session_key,
            storage_scope=session.key,
            conversation=ConversationRef(msg.channel, msg.chat_id, msg.session_key),
            principal_id=principal_id,
            state_hook=lambda state: self.turns.transition(turn_id, state),
            message_id=msg.metadata.get("message_id"),
            session=session,
            model=turn_model,
            subagent_model=(
                turn_defaults.subagent_model
                or turn_defaults.assistant_model
                or turn_model
            ),
            reasoning_effort=turn_effort,
            pulse_enabled=turn_pulse,
            user_input=msg.content,
        )
        try:
            final_content, _, _ = await self._run_agent_loop(
                initial_messages,
                on_progress=on_progress or _bus_progress,
                on_thinking=(
                    _bus_thinking if msg.channel == "web" and not transient else None
                ),
                on_tool_event=None if transient else _bus_tool_event,
                session_key=None if transient else msg.session_key,
                tool_context=tool_context,
                run_state=run_state,
            )
        except ToolTurnAbortedError as exc:
            if msg.session_key in self._stop_requested:
                self._stash_stopped_turn(
                    msg.session_key,
                    session,
                    run_state,
                    transient,
                    pending_turn,
                    turn_id,
                )
                if pending_turn is not None:
                    pending_turn.history_committed = True
                self._forget_message_turn(tool_context.turn_id)
                self.turns.cancel(turn_id, "turn stopped by user")
                raise asyncio.CancelledError from exc
            self._forget_message_turn(tool_context.turn_id)
            error_detail = {
                "code": "tool_turn_aborted",
                "title": "Tool execution aborted",
                "message": str(redact_value(exc.message))[:2_000],
            }
            self._persist_aborted_turn(
                session,
                run_state,
                exc.message,
                transient,
                self._artifact_refs_from_message(msg),
                turn_id=turn_id,
                runtime_session_key=msg.session_key,
                terminal_error=error_detail,
            )
            self._mark_turn_history_committed(msg, pending_turn)
            self.turns.fail(turn_id, "tool execution aborted")
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=exc.message,
                event_id=msg.event_id,
                correlation_id=msg.correlation_id,
                request_id=msg.request_id,
                turn_id=turn_id,
                principal_id=msg.principal_id,
                metadata={
                    **(msg.metadata or {}),
                    "_turn_failed": True,
                    "_turn_error": error_detail,
                    **self._turn_timing_metadata(turn_id),
                },
            )
        except asyncio.CancelledError:
            if msg.session_key in self._stop_requested:
                self._stash_stopped_turn(
                    msg.session_key,
                    session,
                    run_state,
                    transient,
                    pending_turn,
                    turn_id,
                )
                if pending_turn is not None:
                    pending_turn.history_committed = True
            elif asyncio.current_task() not in self._superseded_tasks:
                record = self.turns.get(turn_id)
                stopped_by_user = (
                    record is not None
                    and record.state is TurnState.CANCELLING
                    and record.detail in {"cancelled by user", "stopped by user"}
                )
                runtime_shutdown = (
                    record is not None
                    and record.state is TurnState.CANCELLING
                    and record.detail == "runtime shutdown"
                )
                self._persist_interrupted_turn(
                    session,
                    run_state,
                    transient,
                    self._artifact_refs_from_message(msg),
                    turn_id,
                    runtime_session_key=msg.session_key,
                    content=(
                        self.tips.turn_stopped
                        if stopped_by_user
                        else self.tips.turn_interrupted
                        if runtime_shutdown
                        else self.tips.turn_cancelled
                    ),
                    control_kind=(
                        "stop"
                        if stopped_by_user
                        else "runtime_shutdown"
                        if runtime_shutdown
                        else "interrupted"
                    ),
                )
                if pending_turn is not None:
                    pending_turn.history_committed = True
            self._forget_message_turn(tool_context.turn_id)
            self.turns.cancel(turn_id, "turn task cancelled")
            raise
        except Exception as exc:
            self._forget_message_turn(tool_context.turn_id)
            error_detail = self._public_turn_error(exc)
            self._persist_aborted_turn(
                session,
                run_state,
                self.tips.error,
                transient,
                self._artifact_refs_from_message(msg),
                turn_id=turn_id,
                runtime_session_key=msg.session_key,
                terminal_error=error_detail,
            )
            self._mark_turn_history_committed(msg, pending_turn)
            self.turns.fail(
                turn_id,
                f"turn processing failed ({type(exc).__name__})",
            )
            raise
        finally:
            if self.intervention is not None:
                await self.intervention.finish_turn(tool_context.turn_id)

        _old_msg_count = len(session.messages)
        self._annotate_turn_entries(
            run_state.turn_messages,
            turn_id,
            status="completed",
        )
        try:
            self._persist_turn_entries(
                session,
                run_state.turn_messages,
                artifact_refs=self._artifact_refs_from_message(msg),
            )
        except Exception:
            raise
        self.turns.complete(turn_id)
        self._mark_turn_history_committed(msg, pending_turn)
        if not transient:
            self._schedule_background(
                self.memory_compactor.maybe_compact_by_tokens(session),
                storage_scope=session.key,
                kind="compaction",
            )
            if self.thread_manager:
                _new_msgs = session.messages[_old_msg_count:]
                self._schedule_background(
                    self.thread_manager.append_turn_and_distill(session, _new_msgs),
                    storage_scope=session.key,
                    kind="nowledge",
                )

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool):
            sent_in_turn = mt.sent_in_turn(tool_context.turn_id)
            mt.forget_turn(tool_context.turn_id)
            if sent_in_turn:
                return None

        if final_content is None:
            final_content = self.tips.no_response

        logger.info(
            "Response to {}:{} recorded ({} chars)",
            msg.channel,
            msg.sender_id,
            len(final_content),
        )
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            event_id=msg.event_id,
            correlation_id=msg.correlation_id,
            request_id=msg.request_id,
            turn_id=turn_id,
            principal_id=msg.principal_id,
            metadata={
                **(msg.metadata or {}),
                **self._turn_timing_metadata(turn_id),
            },
        )

    def _forget_message_turn(self, turn_id: str) -> None:
        """Release message delivery state for an aborted or failed turn."""
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.forget_turn(turn_id)

    def _mark_turn_history_committed(
        self,
        msg: InboundMessage,
        pending_turn: _PendingTurn | None,
    ) -> None:
        """Expose a committed turn to ingress before outbound delivery awaits."""
        if pending_turn is not None:
            pending_turn.history_committed = True
            if self._pending_buf.get(msg.session_key) is pending_turn.message:
                self._pending_buf.pop(msg.session_key, None)
        self._progressed[msg.session_key] = True

    def _stash_stopped_turn(
        self,
        session_key: str,
        session: Session,
        run_state: _TurnRunState,
        transient: bool,
        pending_turn: _PendingTurn | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Keep one cancelled turn until the stop command has its terminal text."""
        stopped = _StoppedTurn(
            session=session,
            run_state=run_state,
            transient=transient,
            order_key=self._pending_order_key(pending_turn),
            turn_id=turn_id,
            artifact_refs=(
                self._artifact_refs_from_message(pending_turn.message)
                if pending_turn is not None
                else ()
            ),
            runtime_session_key=session_key,
        )
        records = self._stopped_turns.setdefault(session_key, [])
        if not any(record.run_state is run_state for record in records):
            records.append(stopped)

    def _stopped_turn_from_pending(self, pending_turn: _PendingTurn) -> _StoppedTurn:
        """Build a minimal stopped turn for cancellation before provider execution."""
        if pending_turn.session is not None and pending_turn.run_state is not None:
            return _StoppedTurn(
                session=pending_turn.session,
                run_state=pending_turn.run_state,
                transient=pending_turn.transient,
                order_key=self._pending_order_key(pending_turn),
                turn_id=self._message_turn_id(pending_turn.message),
                artifact_refs=self._artifact_refs_from_message(pending_turn.message),
                recovery_id=self._message_recovery_id(pending_turn.message),
                runtime_session_key=pending_turn.message.session_key,
            )
        msg = pending_turn.message
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            session = self.sessions.get_or_create(channel, chat_id)
        elif pending_turn.session_key is not None:
            session = self.sessions.get_system_session(pending_turn.session_key)
        else:
            session = self.sessions.get_or_create(msg.channel, msg.chat_id)
            session.metadata["_last_principal_id"] = msg.principal_id or msg.sender_id

        content = self.context._build_user_content(msg.content, msg.media or None)
        run_state = _TurnRunState(turn_messages=[{"role": "user", "content": content}])
        return _StoppedTurn(
            session=session,
            run_state=run_state,
            transient=pending_turn.transient,
            order_key=self._pending_order_key(pending_turn),
            turn_id=self._message_turn_id(pending_turn.message),
            artifact_refs=self._artifact_refs_from_message(pending_turn.message),
            recovery_id=self._message_recovery_id(pending_turn.message),
            runtime_session_key=pending_turn.message.session_key,
        )

    @staticmethod
    def _pending_order_key(pending_turn: _PendingTurn | None) -> tuple[int, float]:
        if pending_turn is None:
            return (0, 0.0)
        try:
            timestamp = pending_turn.message.timestamp.timestamp()
        except (AttributeError, OSError, OverflowError, ValueError):
            timestamp = 0.0
        ordinal = pending_turn.ordinal or pending_turn.message.ingress_ordinal
        return (max(int(ordinal or 0), 0), timestamp)

    @staticmethod
    def _message_recovery_id(message: InboundMessage) -> str | None:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        recovery_id = str(metadata.get("_recovery_id") or "")
        return recovery_id if re.fullmatch(r"[0-9a-f]{32}", recovery_id) else None

    @staticmethod
    def _durability_session_key(stopped: _StoppedTurn) -> str:
        """Map a persisted session back to its runtime ordering scope."""
        if stopped.runtime_session_key:
            return stopped.runtime_session_key
        session = stopped.session
        if session.channel == "_system":
            return session.key
        return f"{session.channel}:{session.chat_id}"

    def _canonicalize_stopped_turns(
        self,
        stopped_records: list[_StoppedTurn],
    ) -> list[_StoppedTurn]:
        """Bind every stopped turn for one persisted session to one mutable snapshot."""
        canonical: dict[str, Session] = {}
        rebound: list[_StoppedTurn] = []
        for stopped in stopped_records:
            source = stopped.session
            session = canonical.get(source.key)
            if session is None:
                if source.channel == "_system":
                    session = self.sessions.get_system_session(source.id)
                else:
                    session = self.sessions.get_session(source.channel, source.id) or source
                canonical[source.key] = session
            if session is not source:
                source_metadata = dict(source.metadata)
                current_metadata = dict(session.metadata)
                source_sync = source_metadata.get("_nowledge_thread_sync")
                current_sync = current_metadata.get("_nowledge_thread_sync")
                merged_metadata = {**source_metadata, **current_metadata}
                if isinstance(source_sync, dict) or isinstance(current_sync, dict):
                    older = source_sync if isinstance(source_sync, dict) else {}
                    newer = current_sync if isinstance(current_sync, dict) else {}
                    merged_sync = {**older, **newer}
                    for field in (
                        "acked_source_index",
                        "last_distilled_source_index",
                    ):
                        merged_sync[field] = max(
                            int(older.get(field, 0) or 0),
                            int(newer.get(field, 0) or 0),
                        )
                    merged_metadata["_nowledge_thread_sync"] = merged_sync
                session.metadata = merged_metadata
            rebound.append(replace(stopped, session=session))
        return sorted(rebound, key=lambda stopped: stopped.order_key)

    def _stash_pending_turn(self, session_key: str, pending_turn: _PendingTurn) -> None:
        """Stash an input cancelled while waiting for a session or turn slot."""
        if pending_turn.history_committed:
            return
        stopped = self._stopped_turn_from_pending(pending_turn)
        self._stopped_turns.setdefault(session_key, []).append(stopped)
        pending_turn.history_committed = True

    def _wake_durability_retry(self, session_key: str) -> None:
        """Wake the live retry owner after a fallback takes durable ownership."""
        if getattr(self, "_running", False) and session_key not in self._stop_requested:
            self._ensure_session_durability_retry(session_key)

    def request_session_durability_retry(self, session_key: str) -> None:
        """Ask the lock-owning scheduler to retry retained session history."""
        self._wake_durability_retry(session_key)

    def _persist_pending_interruption(
        self,
        pending_turn: _PendingTurn,
        *,
        content: str | None = None,
        control_kind: str = "interrupted",
    ) -> bool:
        """Persist a queued input interrupted by deadline or runtime shutdown."""
        if pending_turn.history_committed:
            return True
        terminal_content = content or self.tips.turn_cancelled
        terminal_control = {"kind": control_kind}
        stopped: _StoppedTurn | None = None
        try:
            stopped = self._stopped_turn_from_pending(pending_turn)
            stopped.terminal_control = terminal_control
            self._persist_stopped_turn(
                stopped,
                terminal_content,
                schedule_background=False,
            )
        except Exception:
            self._persistence_error_count += 1
            self._retain_pending_interruption(
                pending_turn,
                stopped=stopped,
                content=terminal_content,
                control_kind=control_kind,
            )
            if stopped is not None:
                logger.exception(
                    "Failed to persist queued interrupted turn for {}",
                    stopped.session.key,
                )
            else:
                logger.exception(
                    "Failed to prepare queued interrupted turn for {}",
                    pending_turn.message.session_key,
                )
            return False
        pending_turn.history_committed = True
        return True

    def _retain_pending_interruption(
        self,
        pending_turn: _PendingTurn,
        *,
        stopped: _StoppedTurn | None = None,
        content: str | None = None,
        control_kind: str = "interrupted",
        terminal_error: dict[str, Any] | None = None,
        tool_error: str = "turn stopped before the tool result was recorded",
        tool_status: str = "cancelled",
    ) -> None:
        """Transfer an interrupted input to the live durability owner."""
        if pending_turn.history_committed:
            return
        if stopped is None:
            try:
                stopped = self._stopped_turn_from_pending(pending_turn)
            except Exception:
                stopped = None
        if stopped is not None:
            retained = replace(
                stopped,
                terminal_content=content or self.tips.turn_cancelled,
                terminal_error=terminal_error,
                terminal_control=(None if terminal_error is not None else {"kind": control_kind}),
                tool_error=tool_error,
                tool_status=tool_status,
            )
            durability_key = self._durability_session_key(stopped)
            records = self._stopped_turns.setdefault(durability_key, [])
            if not any(record.run_state is stopped.run_state for record in records):
                records.append(retained)
        else:
            durability_key = pending_turn.message.session_key
            pending_store = getattr(self, "_pending_durability", None)
            if pending_store is None:
                pending_store = {}
                self._pending_durability = pending_store
            records = pending_store.setdefault(durability_key, [])
            recovery_id = self._message_recovery_id(pending_turn.message)
            if not any(
                record.message is pending_turn.message
                or (recovery_id is not None and record.recovery_id == recovery_id)
                for record in records
            ):
                records.append(
                    _PendingDurabilityRecord(
                        message=pending_turn.message,
                        transient=pending_turn.transient,
                        order_key=self._pending_order_key(pending_turn),
                        terminal_content=content or self.tips.turn_cancelled,
                        terminal_error=terminal_error,
                        terminal_control=(
                            None
                            if terminal_error is not None
                            else {"kind": control_kind}
                        ),
                        tool_error=tool_error,
                        tool_status=tool_status,
                        recovery_id=recovery_id,
                    )
                )
        pending_turn.history_committed = True
        self._wake_durability_retry(durability_key)

    async def _persist_pending_interruption_ordered(
        self,
        pending_turn: _PendingTurn,
        *,
        content: str | None = None,
        control_kind: str = "interrupted",
    ) -> bool:
        """Serialize a cancelled queued turn after all earlier session work."""
        session_lock = self._session_locks.setdefault(
            pending_turn.message.session_key,
            asyncio.Lock(),
        )
        try:
            async with session_lock:
                return self._persist_pending_interruption(
                    pending_turn,
                    content=content,
                    control_kind=control_kind,
                )
        except asyncio.CancelledError:
            self._retain_pending_interruption(
                pending_turn,
                content=content,
                control_kind=control_kind,
            )
            raise

    @staticmethod
    def _public_turn_error(error: BaseException | None) -> dict[str, Any]:
        """Return a bounded, redacted error safe for session and Web projections."""
        if error is None:
            return {
                "code": "turn_failed",
                "title": "Turn failed",
                "message": "The turn ended before it produced a valid result.",
            }
        safe_message = str(redact_value(str(error))).strip()
        if not safe_message:
            safe_message = "The turn ended before it produced a valid result."
        safe_message = safe_message[:2_000]
        if isinstance(error, _TurnProviderError):
            return {
                "code": "provider_error",
                "title": "Provider request failed",
                "message": safe_message,
                "hint": "Review the provider response and retry when the cause is resolved.",
            }
        if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            return {
                "code": "timeout",
                "title": "Turn timed out",
                "message": safe_message,
                "hint": "Retry the turn or increase the relevant timeout.",
            }
        return {
            "code": "turn_failed",
            "title": "Turn failed",
            "message": safe_message,
            "errorType": type(error).__name__,
        }

    def _persist_pending_failure(
        self,
        pending_turn: _PendingTurn,
        error: BaseException | None = None,
    ) -> bool:
        """Persist a terminal error when execution fails outside the provider loop."""
        if pending_turn.history_committed:
            return True
        error_detail = self._public_turn_error(error)
        stopped: _StoppedTurn | None = None
        try:
            stopped = self._stopped_turn_from_pending(pending_turn)
            stopped.terminal_error = error_detail
            self._persist_stopped_turn(
                stopped,
                self.tips.error,
                schedule_background=False,
                tool_error="turn failed before the tool result was recorded",
                tool_status="error",
            )
        except Exception:
            self._persistence_error_count += 1
            if stopped is not None:
                retained = replace(
                    stopped,
                    terminal_content=self.tips.error,
                    terminal_error=error_detail,
                    tool_error="turn failed before the tool result was recorded",
                    tool_status="error",
                )
                durability_key = self._durability_session_key(stopped)
                records = self._stopped_turns.setdefault(durability_key, [])
                if not any(record.run_state is stopped.run_state for record in records):
                    records.append(retained)
                self._wake_durability_retry(durability_key)
                logger.exception("Failed to persist failed turn for {}", stopped.session.key)
            else:
                self._retain_pending_interruption(
                    pending_turn,
                    content=self.tips.error,
                    terminal_error=error_detail,
                    tool_error="turn failed before the tool result was recorded",
                    tool_status="error",
                )
                logger.exception(
                    "Failed to prepare failed turn for {}",
                    pending_turn.message.session_key,
                )
            pending_turn.history_committed = True
            return False
        pending_turn.history_committed = True
        return True

    def _persist_interrupted_turn(
        self,
        session: Session,
        run_state: _TurnRunState,
        transient: bool,
        artifact_refs: tuple[dict[str, Any], ...] = (),
        turn_id: str | None = None,
        runtime_session_key: str | None = None,
        content: str | None = None,
        control_kind: str = "interrupted",
    ) -> bool:
        """Persist a terminal record for cancellation outside explicit /stop."""
        stopped = _StoppedTurn(
            session=session,
            run_state=run_state,
            transient=transient,
            turn_id=turn_id,
            artifact_refs=artifact_refs,
            terminal_control={"kind": control_kind},
            runtime_session_key=runtime_session_key,
        )
        try:
            self._persist_stopped_turn(
                stopped,
                content or self.tips.turn_cancelled,
                schedule_background=False,
            )
        except Exception:
            self._persistence_error_count += 1
            durability_key = self._durability_session_key(stopped)
            records = self._stopped_turns.setdefault(durability_key, [])
            if not any(record.run_state is run_state for record in records):
                records.append(
                    replace(
                        stopped,
                        terminal_content=content or self.tips.turn_cancelled,
                        terminal_control={"kind": control_kind},
                    )
                )
            self._wake_durability_retry(durability_key)
            logger.exception("Failed to persist interrupted turn for {}", session.key)
            return False
        return True

    def _persist_aborted_turn(
        self,
        session: Session,
        run_state: _TurnRunState,
        content: str,
        transient: bool,
        artifact_refs: tuple[dict[str, Any], ...] = (),
        turn_id: str | None = None,
        runtime_session_key: str | None = None,
        terminal_error: dict[str, Any] | None = None,
    ) -> bool:
        """Persist a structured terminal record for an application-aborted turn."""
        stopped = _StoppedTurn(
            session=session,
            run_state=run_state,
            transient=transient,
            turn_id=turn_id,
            artifact_refs=artifact_refs,
            terminal_error=terminal_error,
            runtime_session_key=runtime_session_key,
        )
        try:
            self._persist_stopped_turn(
                stopped,
                content,
                tool_error="turn aborted before the tool result was recorded",
                tool_status="aborted",
            )
        except Exception:
            self._persistence_error_count += 1
            durability_key = self._durability_session_key(stopped)
            records = self._stopped_turns.setdefault(durability_key, [])
            if not any(record.run_state is run_state for record in records):
                records.append(
                    replace(
                        stopped,
                        terminal_content=content,
                        terminal_error=terminal_error,
                        tool_error="turn aborted before the tool result was recorded",
                        tool_status="aborted",
                    )
                )
            self._wake_durability_retry(durability_key)
            logger.exception("Failed to persist aborted turn for {}", session.key)
            return False
        return True

    @property
    def _stopped_recovery_dir(self):
        return self.runtime_files.root / "recovery" / "stopped_turns"

    def _write_stopped_recovery_payload(
        self,
        recovery_id: str,
        payload: Mapping[str, Any],
    ) -> bool:
        temp_path: Path | None = None
        fd: int | None = None
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            if len(encoded) > self.runtime_files.max_file_bytes:
                return False
            auxiliary_writer = getattr(
                self.runtime_files,
                "write_auxiliary_bytes",
                None,
            )
            if callable(auxiliary_writer):
                return bool(
                    auxiliary_writer(
                        f"recovery/stopped_turns/stopped_{recovery_id}.json",
                        encoded,
                        max_group_bytes=self.runtime_files.max_session_bytes,
                    )
                )
            directory = self._stopped_recovery_dir
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"stopped_{recovery_id}.json"
            existing_size = target.stat().st_size if target.exists() else 0
            total_size = sum(
                item.stat().st_size
                for item in directory.glob("stopped_[0-9a-f]*.json")
                if item.is_file()
            )
            if total_size - existing_size + len(encoded) > self.runtime_files.max_session_bytes:
                return False
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=directory
            )
            temp_path = target.parent / os.path.basename(temp_name)
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False
        return True

    def _quarantine_stopped_recovery(self, path: Path) -> None:
        """Move one invalid disposable recovery record out of the active quota."""
        try:
            quarantine = self._stopped_recovery_dir.parent / "quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            target = quarantine / path.name
            os.replace(path, target)
            retained = sorted(
                (item for item in quarantine.glob("stopped_[0-9a-f]*.json") if item.is_file()),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
            total = 0
            for index, item in enumerate(retained):
                size = item.stat().st_size
                total += size
                if index >= 63 or total > self.runtime_files.max_session_bytes:
                    item.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to quarantine stopped-turn recovery file {}", path)

    def _journal_stopped_turn(self, stopped: _StoppedTurn, content: str) -> bool:
        """Durably transfer one failed stopped-turn record across process shutdown."""
        recovery_id = stopped.recovery_id or uuid.uuid4().hex
        payload = {
            "version": 1,
            "kind": "stopped",
            "recoveryId": recovery_id,
            "session": {
                "channel": stopped.session.channel,
                "id": stopped.session.id,
                "chatId": stopped.session.chat_id,
            },
            "turnMessages": self._recovery_turn_messages(
                stopped.run_state.turn_messages
            ),
            "transient": stopped.transient,
            "orderKey": list(stopped.order_key),
            "turnId": stopped.turn_id,
            "artifactRefs": list(stopped.artifact_refs),
            "terminalContent": stopped.terminal_content or content,
            "terminalError": stopped.terminal_error,
            "terminalControl": stopped.terminal_control,
            "toolError": stopped.tool_error,
            "toolStatus": stopped.tool_status,
            "runtimeSessionKey": stopped.runtime_session_key,
        }
        return self._write_stopped_recovery_payload(recovery_id, payload)

    def _recovery_turn_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove provider-only bulk and private paths from recovery payloads."""
        normalized: list[dict[str, Any]] = []
        for message in messages:
            entry = dict(message)
            content = entry.get("content")
            if isinstance(content, str):
                if entry.get("role") == "tool" and len(content) > self._TOOL_RESULT_MAX_CHARS:
                    content = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                if entry.get("role") == "user":
                    content = re.sub(r"\[image:\s*[^\]]+\]", "[image]", content)
                entry["content"] = content
            elif isinstance(content, list):
                blocks: list[Any] = []
                for value in content:
                    if not isinstance(value, dict):
                        blocks.append(value)
                        continue
                    block = dict(value)
                    metadata = block.pop("_meta", None)
                    if (
                        block.get("type") == "image_url"
                        and str((block.get("image_url") or {}).get("url") or "").startswith(
                            "data:image/"
                        )
                    ):
                        blocks.append({"type": "text", "text": "[image]"})
                    elif isinstance(metadata, dict) and metadata.get("attachment_path"):
                        blocks.append({"type": "text", "text": "[file attachment]"})
                    else:
                        blocks.append(block)
                entry["content"] = blocks
            normalized.append(entry)
        return normalized

    def _journal_pending_turn(self, record: _PendingDurabilityRecord) -> bool:
        """Durably retain raw ingress when session materialization is unavailable."""
        recovery_id = record.recovery_id or uuid.uuid4().hex
        message = record.message
        payload = {
            "version": 1,
            "kind": "pending",
            "recoveryId": recovery_id,
            "message": {
                "channel": message.channel,
                "senderId": message.sender_id,
                "chatId": message.chat_id,
                "content": message.content,
                "timestamp": message.timestamp.isoformat(),
                "mediaCount": len(message.media),
                "sessionKeyOverride": message.session_key_override,
                "eventId": message.event_id,
                "correlationId": message.correlation_id,
                "priority": message.priority,
                "deadlineAt": (
                    message.deadline_at.isoformat() if message.deadline_at else None
                ),
                "requestId": message.request_id,
                "turnId": self._message_turn_id(message),
                "principalId": message.principal_id,
                "artifactRefs": list(self._artifact_refs_from_message(message)),
            },
            "transient": record.transient,
            "orderKey": list(record.order_key),
            "terminalContent": record.terminal_content,
            "terminalError": record.terminal_error,
            "terminalControl": record.terminal_control,
            "toolError": record.tool_error,
            "toolStatus": record.tool_status,
            "runtimeSessionKey": message.session_key,
        }
        return self._write_stopped_recovery_payload(recovery_id, payload)

    def _unlink_stopped_recovery(self, recovery_id: str | None) -> None:
        """Remove a recovery record only after its session write is durable."""
        if recovery_id is None:
            return
        (self._stopped_recovery_dir / f"stopped_{recovery_id}.json").unlink(
            missing_ok=True
        )

    def _recovered_pending_message(
        self,
        payload: Mapping[str, Any],
        raw_order: tuple[int, float],
        recovery_id: str,
    ) -> InboundMessage:
        """Rebuild a bounded raw ingress owner from a recovery record."""
        message_data = payload.get("message") or {}
        if not isinstance(message_data, Mapping):
            raise ValueError("pending recovery message is invalid")
        timestamp = datetime.fromisoformat(
            str(message_data.get("timestamp") or datetime.now().isoformat())
        )
        raw_deadline = message_data.get("deadlineAt")
        deadline_at = datetime.fromisoformat(str(raw_deadline)) if raw_deadline else None
        artifact_refs = [
            dict(item)
            for item in message_data.get("artifactRefs") or ()
            if isinstance(item, dict)
        ]
        recovered_content = str(message_data.get("content") or "")
        recovered_media_count = int(message_data.get("mediaCount") or 0)
        if recovered_media_count <= 0 and isinstance(message_data.get("media"), list):
            recovered_media_count = len(message_data["media"])
        if recovered_media_count > 0:
            recovered_content = (
                f"{recovered_content}\n\n[file attachments unavailable during recovery]"
            ).strip()
        return InboundMessage(
            channel=str(message_data.get("channel") or ""),
            sender_id=str(message_data.get("senderId") or ""),
            chat_id=str(message_data.get("chatId") or ""),
            content=recovered_content,
            timestamp=timestamp,
            media=[],
            metadata={
                "turn_id": message_data.get("turnId"),
                "_artifact_refs": artifact_refs,
                "_recovery_id": recovery_id,
            },
            session_key_override=(
                str(message_data["sessionKeyOverride"])
                if message_data.get("sessionKeyOverride")
                else None
            ),
            event_id=str(message_data.get("eventId") or "") or None,
            correlation_id=str(message_data.get("correlationId") or "") or None,
            priority=(
                int(message_data["priority"])
                if message_data.get("priority") is not None
                else None
            ),
            deadline_at=deadline_at,
            request_id=str(message_data.get("requestId") or "") or None,
            turn_id=str(message_data.get("turnId") or "") or None,
            principal_id=str(message_data.get("principalId") or "") or None,
            ingress_ordinal=max(int(raw_order[0]), 0),
        )

    def _recovered_pending_record(
        self,
        payload: Mapping[str, Any],
        raw_order: tuple[int, float],
        recovery_id: str,
    ) -> _PendingDurabilityRecord:
        """Rebuild a pending durability owner without losing terminal semantics."""
        terminal_error = payload.get("terminalError")
        terminal_control = payload.get("terminalControl")
        return _PendingDurabilityRecord(
            message=self._recovered_pending_message(payload, raw_order, recovery_id),
            transient=bool(payload.get("transient")),
            order_key=raw_order,
            terminal_content=str(
                payload.get("terminalContent") or self.tips.turn_interrupted
            ),
            terminal_error=(
                dict(terminal_error) if isinstance(terminal_error, Mapping) else None
            ),
            terminal_control=(
                dict(terminal_control)
                if isinstance(terminal_control, Mapping)
                else None
            ),
            tool_error=str(
                payload.get("toolError")
                or "turn stopped before the tool result was recorded"
            ),
            tool_status=str(payload.get("toolStatus") or "cancelled"),
            recovery_id=recovery_id,
        )

    def _retain_recovery_backlog(
        self,
        owner: str,
        *,
        stopped: _StoppedTurn | None = None,
        pending: _PendingDurabilityRecord | None = None,
    ) -> None:
        """Keep a failed startup replay under the live durability gate."""
        if stopped is not None:
            records = self._stopped_turns.setdefault(owner, [])
            if not any(record.recovery_id == stopped.recovery_id for record in records):
                records.append(stopped)
            return
        if pending is not None:
            records = self._pending_durability.setdefault(owner, [])
            if not any(record.recovery_id == pending.recovery_id for record in records):
                records.append(pending)

    def _recover_stopped_turn_journal(self) -> None:
        """Replay durable stopped-turn handoffs before accepting new input."""
        directory = self._stopped_recovery_dir
        if not directory.is_dir():
            return
        candidates: list[
            tuple[str, tuple[int, float], str, Path, dict[str, Any]]
        ] = []
        for path in sorted(directory.glob("stopped_[0-9a-f]*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("stopped-turn recovery payload is invalid")
                recovery_id = str(payload.get("recoveryId") or "")
                if not re.fullmatch(r"[0-9a-f]{32}", recovery_id):
                    raise ValueError("invalid stopped-turn recovery ID")
                raw_order = payload.get("orderKey") or (0, 0.0)
                order_key = (int(raw_order[0]), float(raw_order[1]))
                if payload.get("kind") == "pending":
                    message_data = payload.get("message") or {}
                    owner = str(message_data.get("sessionKeyOverride") or "") or (
                        f"{message_data.get('channel') or ''}:"
                        f"{message_data.get('chatId') or ''}"
                    )
                else:
                    session_data = payload.get("session") or {}
                    owner = str(payload.get("runtimeSessionKey") or "") or (
                        f"{session_data.get('channel') or ''}:"
                        f"{session_data.get('chatId') or ''}"
                    )
                candidates.append((owner, order_key, recovery_id, path, payload))
            except Exception:
                self._persistence_error_count += 1
                logger.exception("Failed to inspect stopped-turn recovery file {}", path)
                self._quarantine_stopped_recovery(path)

        blocked_owners: set[str] = set()
        for owner, raw_order, recovery_id, path, payload in sorted(
            candidates,
            key=lambda item: (item[0], item[1], item[2]),
        ):
            stopped: _StoppedTurn | None = None
            recovered_pending: _PendingDurabilityRecord | None = None
            try:
                session_data = payload.get("session") or {}
                channel = str(session_data.get("channel") or "")
                session_id = str(session_data.get("id") or "")
                if payload.get("kind") == "pending":
                    recovered_pending = self._recovered_pending_record(
                        payload,
                        raw_order,
                        recovery_id,
                    )
                    stopped = self._stopped_turn_from_pending(
                        _PendingTurn(
                            message=recovered_pending.message,
                            session_key=None,
                            transient=recovered_pending.transient,
                            ordinal=raw_order[0],
                        )
                    )
                    stopped = replace(
                        stopped,
                        order_key=recovered_pending.order_key,
                        recovery_id=recovered_pending.recovery_id,
                        terminal_content=recovered_pending.terminal_content,
                        terminal_error=recovered_pending.terminal_error,
                        terminal_control=recovered_pending.terminal_control,
                        tool_error=recovered_pending.tool_error,
                        tool_status=recovered_pending.tool_status,
                    )
                else:
                    if channel == "_system":
                        session = self.sessions.get_system_session(session_id)
                    else:
                        session = self.sessions.get_session(channel, session_id)
                    if session is None:
                        raise ValueError("stopped-turn recovery session is unavailable")
                    stopped = _StoppedTurn(
                        session=session,
                        run_state=_TurnRunState(
                            turn_messages=[
                                dict(item)
                                for item in payload.get("turnMessages") or ()
                                if isinstance(item, dict)
                            ]
                        ),
                        transient=bool(payload.get("transient")),
                        order_key=raw_order,
                        turn_id=str(payload.get("turnId") or "") or None,
                        artifact_refs=tuple(
                            dict(item)
                            for item in payload.get("artifactRefs") or ()
                            if isinstance(item, dict)
                        ),
                        recovery_id=recovery_id,
                        terminal_content=(
                            str(payload.get("terminalContent"))
                            if payload.get("terminalContent")
                            else None
                        ),
                        terminal_error=(
                            dict(payload["terminalError"])
                            if isinstance(payload.get("terminalError"), dict)
                            else None
                        ),
                        terminal_control=(
                            dict(payload["terminalControl"])
                            if isinstance(payload.get("terminalControl"), dict)
                            else None
                        ),
                        tool_error=str(
                            payload.get("toolError")
                            or "turn stopped before the tool result was recorded"
                        ),
                        tool_status=str(payload.get("toolStatus") or "cancelled"),
                        runtime_session_key=(
                            str(payload.get("runtimeSessionKey") or "") or None
                        ),
                    )
                if owner in blocked_owners:
                    self._retain_recovery_backlog(
                        owner,
                        stopped=stopped,
                        pending=recovered_pending,
                    )
                    continue
                self._persist_stopped_turn(
                    stopped,
                    str(payload.get("terminalContent") or self.tips.turn_interrupted),
                    schedule_background=False,
                    tool_error=stopped.tool_error,
                    tool_status=stopped.tool_status,
                )
                self._unlink_stopped_recovery(recovery_id)
            except Exception as exc:
                self._persistence_error_count += 1
                logger.exception("Failed to replay stopped-turn recovery file {}", path)
                if isinstance(exc, ValueError) and "session is unavailable" in str(exc):
                    self._quarantine_stopped_recovery(path)
                    continue
                if stopped is None and recovered_pending is None:
                    self._quarantine_stopped_recovery(path)
                    continue
                blocked_owners.add(owner)
                self._retain_recovery_backlog(
                    owner,
                    stopped=stopped,
                    pending=recovered_pending,
                )

    def _flush_stopped_turn_retries(self) -> None:
        """Retry memory-owned stopped turns and journal any remaining failures."""
        for session_key, records in tuple(self._stopped_turns.items()):
            try:
                canonical = self._canonicalize_stopped_turns(records)
            except Exception:
                self._persistence_error_count += 1
                logger.exception(
                    "Failed to prepare retained stopped turns for {}",
                    session_key,
                )
                canonical = list(records)
            retained: list[_StoppedTurn] = []
            for stopped in canonical:
                try:
                    self._persist_stopped_turn(
                        stopped,
                        stopped.terminal_content or self.tips.turn_interrupted,
                        schedule_background=False,
                        tool_error=stopped.tool_error,
                        tool_status=stopped.tool_status,
                    )
                except Exception:
                    self._persistence_error_count += 1
                    logger.exception(
                        "Failed to flush retained stopped turn for {}", session_key
                    )
                    if not self._journal_stopped_turn(
                        stopped,
                        stopped.terminal_content or self.tips.turn_interrupted,
                    ):
                        retained.append(stopped)
            if retained:
                self._stopped_turns[session_key] = retained
            else:
                self._stopped_turns.pop(session_key, None)

    def _persist_stopped_turn(
        self,
        stopped: _StoppedTurn,
        content: str,
        *,
        schedule_background: bool = True,
        tool_error: str = "turn stopped before the tool result was recorded",
        tool_status: str = "cancelled",
    ) -> None:
        """Persist a protocol-valid terminal record for an explicitly stopped turn."""
        recovery_marker_key = "_stopped_recovery_ids"
        recovery_markers = stopped.session.metadata.get(recovery_marker_key, [])
        if not isinstance(recovery_markers, list):
            recovery_markers = []
        if stopped.recovery_id and stopped.recovery_id in recovery_markers:
            return
        messages: list[dict[str, Any]] = []
        pending: dict[str, str] = {}
        source_messages = list(stopped.run_state.turn_messages)

        def append_cancelled_results() -> None:
            for call_id, tool_name in pending.items():
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": tool_name,
                        "content": json.dumps(
                            {
                                "ok": False,
                                "error": tool_error,
                                "status": tool_status,
                                "side_effects_may_have_occurred": True,
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            pending.clear()

        for message in source_messages:
            role = message.get("role")
            if pending and role in {"user", "assistant"}:
                append_cancelled_results()
            messages.append(message)
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict) or not call.get("id"):
                        continue
                    function = call.get("function") or {}
                    pending[str(call["id"])] = str(
                        function.get("name") or call.get("name") or "unknown"
                    )
            elif message.get("role") == "tool":
                pending.pop(str(message.get("tool_call_id") or ""), None)
        append_cancelled_results()
        last_message = messages[-1] if messages else None
        last_content = last_message.get("content") if last_message is not None else None
        if isinstance(last_content, str) and "<pulse" in last_content.lower():
            from nanocat.agent.pulse import strip_pulse

            last_content = strip_pulse(last_content)
        turn_completed = (
            last_message is not None
            and last_message.get("role") == "assistant"
            and not last_message.get("tool_calls")
            and bool(last_content)
        )
        if not turn_completed:
            messages = self.context.add_assistant_message(messages, content)
        if stopped.turn_id:
            self._annotate_turn_entries(
                messages,
                stopped.turn_id,
                status="cancelled" if tool_status == "cancelled" else "failed",
            )
        terminal = next(
            (
                message
                for message in reversed(messages)
                if message.get("role") == "assistant" and not message.get("tool_calls")
            ),
            None,
        )
        if terminal is not None:
            if stopped.terminal_error:
                terminal["turn_error"] = dict(stopped.terminal_error)
            if stopped.terminal_control:
                terminal["turn_control"] = dict(stopped.terminal_control)

        old_message_count = len(stopped.session.messages)
        marker_was_present = recovery_marker_key in stopped.session.metadata
        old_marker_value = stopped.session.metadata.get(recovery_marker_key)
        if stopped.recovery_id:
            stopped.session.metadata[recovery_marker_key] = [
                *recovery_markers[-511:],
                stopped.recovery_id,
            ]
        try:
            self._persist_turn_entries(
                stopped.session,
                messages,
                artifact_refs=stopped.artifact_refs,
            )
        except BaseException:
            if stopped.recovery_id:
                if marker_was_present:
                    stopped.session.metadata[recovery_marker_key] = old_marker_value
                else:
                    stopped.session.metadata.pop(recovery_marker_key, None)
            raise
        if stopped.transient or not schedule_background:
            return
        self._schedule_background(
            self.memory_compactor.maybe_compact_by_tokens(stopped.session),
            storage_scope=stopped.session.key,
            kind="compaction",
        )
        if self.thread_manager:
            new_messages = stopped.session.messages[old_message_count:]
            self._schedule_background(
                self.thread_manager.append_turn_and_distill(stopped.session, new_messages),
                storage_scope=stopped.session.key,
                kind="nowledge",
            )

    @staticmethod
    def _validate_turn_entries(entries: list[dict[str, Any]]) -> None:
        """Reject persistence fragments that would corrupt provider tool-call history."""
        Session.validate_turn_entries(entries)

    def _annotate_turn_entries(
        self,
        messages: list[dict[str, Any]],
        turn_id: str,
        *,
        status: str,
    ) -> None:
        """Persist stable turn identity and terminal timing on protocol entries."""
        turns = getattr(self, "turns", None)
        record = turns.get(turn_id) if turns is not None else None
        started_at = record.started_at if record is not None else datetime.now(timezone.utc)
        ended_at = datetime.now(timezone.utc)
        duration_ms = max(0, int((ended_at - started_at).total_seconds() * 1000))
        for message in messages:
            message.setdefault("turn_id", turn_id)
            message.setdefault("turn_started_at", started_at.isoformat())
        terminal = next(
            (
                message
                for message in reversed(messages)
                if message.get("role") == "assistant" and not message.get("tool_calls")
            ),
            None,
        )
        if terminal is not None:
            terminal["turn_ended_at"] = ended_at.isoformat()
            terminal["turn_duration_ms"] = duration_ms
            terminal["turn_status"] = status

    def _turn_timing_metadata(self, turn_id: str) -> dict[str, Any]:
        record = self.turns.get(turn_id)
        if record is None:
            return {}
        return {
            "_working_started_at": record.started_at.isoformat(),
            "_working_ended_at": (
                record.ended_at.isoformat() if record.ended_at is not None else None
            ),
            "_working_duration_ms": record.duration_ms,
        }

    def _persist_turn_entries(
        self,
        session: Session,
        messages: list[dict[str, Any]],
        *,
        artifact_refs: tuple[dict[str, Any], ...] = (),
    ) -> None:
        """Append and durably save a turn, rolling memory back on any save failure."""
        old_message_count = len(session.messages)
        old_revision = session.revision
        old_updated_at = session.updated_at
        try:
            persisted_messages = [dict(message) for message in messages]
            if artifact_refs:
                first_user = next(
                    (
                        message
                        for message in persisted_messages
                        if message.get("role") == "user"
                    ),
                    None,
                )
                if first_user is not None:
                    first_user["artifact_refs"] = [dict(item) for item in artifact_refs]
            self._save_turn(session, persisted_messages)
            self.sessions.save(session, expected_revision=old_revision)
        except BaseException:
            del session.messages[old_message_count:]
            session.revision = old_revision
            session.updated_at = old_updated_at
            raise

    def _save_turn(
        self,
        session: Session,
        messages: list[dict[str, Any]],
    ) -> None:
        """Atomically append one protocol-valid turn to the in-memory session."""
        from nanocat.agent.pulse import strip_pulse

        entries: list[dict[str, Any]] = []
        for m in messages:
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
                    entry["content"] = (
                        stripped if stripped.strip() else self._EMPTY_USER_PLACEHOLDER
                    )
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if (c.get("_meta") or {}).get("attachment_path"):
                            filtered.append(
                                {"type": "text", "text": "[file attachment]"}
                            )
                            continue
                        if (
                            c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and ContextBuilder.is_ephemeral_text_block(c["text"])
                        ):
                            continue
                        if c.get("type") == "image_url" and c.get("image_url", {}).get(
                            "url", ""
                        ).startswith("data:image/"):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    substantive = any(
                        c.get("type") != "text"
                        or not isinstance(c.get("text"), str)
                        or bool(c["text"].strip())
                        for c in filtered
                    )
                    entry["content"] = (
                        filtered if substantive else self._EMPTY_USER_PLACEHOLDER
                    )
                if not entry.get("content"):
                    entry["content"] = self._EMPTY_USER_PLACEHOLDER
            entry.setdefault("timestamp", datetime.now().isoformat())
            entries.append(entry)

        self._validate_turn_entries(entries)
        session.messages.extend(entries)
        session.revision += len(entries)
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
            session_key_override=session_key,
        )
        if MessageBus.is_command_candidate(content):
            if self._command_dispatcher is None:
                raise RuntimeError("command dispatcher is unavailable")
            response = await self._command_dispatcher.execute(msg, publish=False)
            return response.content if response is not None else ""
        if MessageBus.is_escaped_text(content):
            msg = replace(msg, content=MessageBus.normalize_escaped_text(content))
        pending_turn = _PendingTurn(
            message=msg,
            session_key=session_key,
            transient=transient,
            ordinal=next(self._ingress_ordinals),
        )

        async def _run() -> str:
            await self._wait_for_stop_operation(session_key)
            await self._wait_for_session_operation(session_key)
            session_lock = self._session_locks.setdefault(session_key, asyncio.Lock())
            async with session_lock:
                async with self._turn_slots:
                    await self._connect_mcp()
                    response = await self._process_message(
                        msg,
                        session_key=session_key,
                        on_progress=on_progress,
                        transient=transient,
                        pending_turn=pending_turn,
                    )
            return response.content if response else ""

        current_task = asyncio.current_task()
        if current_task is not None:
            self._direct_tasks.setdefault(session_key, set()).add(current_task)
        try:
            if deadline_at is None:
                return await _run()
            now = datetime.now(deadline_at.tzinfo) if deadline_at.tzinfo else datetime.now()
            remaining = (deadline_at - now).total_seconds()
            if remaining <= 0:
                raise TimeoutError("direct turn deadline expired")
            return await asyncio.wait_for(_run(), timeout=remaining)
        except TimeoutError:
            if not pending_turn.history_committed:
                self._persist_pending_failure(
                    pending_turn,
                    TimeoutError("direct turn deadline expired"),
                )
            raise
        except asyncio.CancelledError:
            if not pending_turn.history_committed:
                if msg.session_key in self._stop_requested:
                    self._stash_pending_turn(msg.session_key, pending_turn)
                elif asyncio.current_task() not in self._superseded_tasks:
                    self._persist_pending_interruption(pending_turn)
            raise
        except Exception as exc:
            if not pending_turn.history_committed:
                self._persist_pending_failure(pending_turn, exc)
            raise
        finally:
            if current_task is not None:
                tasks = self._direct_tasks.get(session_key)
                if tasks is not None:
                    tasks.discard(current_task)
                    if not tasks:
                        self._direct_tasks.pop(session_key, None)
