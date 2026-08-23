"""Runtime-owned command ingress and execution lane."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from loguru import logger

from nanocat.application.command_feedback import CommandFeedback
from nanocat.application.text_catalog import USER_TEXT
from nanocat.bus.events import InboundMessage, OutboundMessage
from nanocat.bus.queue import BusClosedError, MessageBus
from nanocat.core.commands import CommandErrorCode, CommandResult


class CommandDispatcher:
    """Execute slash commands independently from the LLM turn pipeline."""

    _FAST_COMMANDS = frozenset(
        {"approve", "deny", "reject", "help", "logs", "whoami", "model", "stop", "restart"}
    )

    def __init__(self, agent: Any, bus: MessageBus):
        self._agent = agent
        self._bus = bus
        runtime = getattr(getattr(agent, "_config", None), "runtime", None)
        max_turns = max(1, int(getattr(runtime, "max_concurrent_turns", 1)))
        self._max_active = max(8, max_turns * 4)
        self._task: asyncio.Task[Any] | None = None
        self._active: set[asyncio.Task[Any]] = set()
        self._active_sessions: dict[str, int] = {}
        self._task_sessions: dict[asyncio.Task[Any], str] = {}
        self._closing = False

    async def start(self) -> asyncio.Task[Any]:
        """Start the command consumer once."""
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self.run(), name="nanocat.command-dispatcher")
        return self._task

    def apply_concurrency_limit(self, max_concurrent_turns: int) -> None:
        """Apply command admission capacity for subsequently accepted commands."""
        self._max_active = max(8, max(1, max_concurrent_turns) * 4)

    async def close(self) -> None:
        """Stop command ingress and reclaim all command tasks."""
        self._closing = True
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        active = tuple(self._active)
        for item in active:
            if not item.done():
                item.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active.clear()
        self._active_sessions.clear()
        self._task_sessions.clear()

    async def run(self) -> None:
        """Consume commands until the bus closes or the runtime stops."""
        logger.info("Command dispatcher started")
        while not self._closing:
            try:
                msg = await self._bus.consume_command()
            except asyncio.CancelledError:
                raise
            except BusClosedError:
                logger.info("Command dispatcher stopped: message bus closed")
                return
            except Exception:
                logger.exception("Command dispatcher failed to consume a command")
                continue

            if self._command_name(msg) in {"stop", "restart"}:
                self._schedule(msg)
                continue
            if self._is_fast_command(msg):
                await self._execute_tracked(msg)
                continue
            if len(self._active) >= self._max_active:
                await self._publish(self._busy_response(msg))
                continue
            self._schedule(msg)

    def _schedule(self, msg: InboundMessage) -> None:
        self._increment_session(msg.session_key)

        async def run_tracked() -> None:
            task = asyncio.current_task()
            if task is not None:
                self._task_sessions[task] = msg.session_key
            try:
                await self.execute(msg)
            finally:
                if task is not None:
                    self._task_sessions.pop(task, None)
                self._decrement_session(msg.session_key)

        task = asyncio.create_task(
            run_tracked(),
            name=f"nanocat.command.{self._command_name(msg)}",
        )
        self._active.add(task)
        task.add_done_callback(self._forget_task)

    async def _execute_tracked(self, msg: InboundMessage) -> None:
        task = asyncio.current_task()
        self._increment_session(msg.session_key)
        if task is not None:
            self._task_sessions[task] = msg.session_key
        try:
            await self.execute(msg)
        finally:
            if task is not None:
                self._task_sessions.pop(task, None)
            self._decrement_session(msg.session_key)

    def _increment_session(self, session_key: str) -> None:
        self._active_sessions[session_key] = self._active_sessions.get(session_key, 0) + 1

    def _decrement_session(self, session_key: str) -> None:
        count = self._active_sessions.get(session_key, 0)
        if count <= 1:
            self._active_sessions.pop(session_key, None)
        else:
            self._active_sessions[session_key] = count - 1

    def is_session_busy(self, session_key: str) -> bool:
        """Return whether another command currently owns this session route."""
        count = self._active_sessions.get(session_key, 0)
        current = asyncio.current_task()
        if current is not None and self._task_sessions.get(current) == session_key:
            count -= 1
        return count > 0

    async def execute(
        self,
        msg: InboundMessage,
        *,
        publish: bool = True,
    ) -> OutboundMessage | None:
        """Execute one command and optionally publish its channel response."""
        try:
            response = await self._execute(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Command failed ({} chars)", len(msg.content))
            response = self._result_response(
                msg,
                CommandResult(
                    ok=False,
                    code=CommandErrorCode.COMMAND_FAILED,
                    title="Command failed",
                    message=str(exc) or "The command could not be completed.",
                ),
            )
        if publish and response is not None:
            await self._publish(response)
        return response

    async def _execute(self, msg: InboundMessage) -> OutboundMessage | None:
        service = self._agent.command_service
        intervention_response = await service.dispatch_intervention(msg)
        if intervention_response is not None:
            return self._control_response(msg, intervention_response)

        inspection = self._agent.command_router.inspect(msg.content)
        if inspection.result is not None:
            return self._result_response(
                msg,
                inspection.result,
            )
        spec = inspection.spec
        if spec is None or inspection.parsed is None:
            return self._result_response(
                msg,
                CommandResult(
                    ok=False,
                    code=CommandErrorCode.INVALID_ARGUMENT,
                    title="Invalid command",
                    message="The command could not be parsed.",
                    usage="/help",
                ),
            )

        command_name = spec.name
        if command_name == "stop":
            return self._control_response(msg, await self._agent._handle_stop(msg))
        if command_name == "restart":
            stopped = await self._agent._handle_stop(msg)
            if stopped.metadata.get("persistence_failed") is True:
                return self._control_response(msg, stopped)
            return self._control_response(msg, await self._agent._handle_restart(msg))

        idle_required = self._requires_idle(inspection)
        reserved = False
        if idle_required:
            reserved = self._agent.try_reserve_session_operation(msg.session_key)
            if not reserved:
                return self._idle_response(msg)

        try:
            session = (
                None
                if command_name in {"help", "logs", "model"}
                else self._agent.sessions.get_or_create(msg.channel, msg.chat_id)
            )
            outcome = await service.dispatch(msg, session)
            if outcome.response is not None:
                return self._control_response(msg, outcome.response)
            if outcome.handled:
                return self._result_response(
                    msg,
                    CommandResult(ok=True, message="Command completed."),
                )
            return self._result_response(
                msg,
                CommandResult(
                    ok=False,
                    code=CommandErrorCode.COMMAND_FAILED,
                    title="Command not handled",
                    message=f"`/{command_name}` was not handled by this runtime.",
                    usage="/help",
                ),
            )
        finally:
            if reserved:
                self._agent.release_session_operation(msg.session_key)

    @staticmethod
    def _command_name(msg: InboundMessage) -> str:
        token = (msg.content or "").lstrip().split(maxsplit=1)[0]
        return token[1:].split("@", 1)[0].lower() if token.startswith("/") else "unknown"

    def _is_fast_command(self, msg: InboundMessage) -> bool:
        name = self._command_name(msg)
        if name in self._FAST_COMMANDS:
            return True
        return name == "compact" and msg.content.strip().lower().split()[1:] == ["status"]

    @staticmethod
    def _requires_idle(inspection: Any) -> bool:
        spec = inspection.spec
        if spec is None:
            return False
        subcommand = inspection.parsed.subcommand if inspection.parsed else None
        if spec.idle_only and subcommand not in spec.idle_exempt_subcommands:
            return True
        return spec.name == "session" and subcommand in {"switch", "rename", "delete"}

    def _control_response(self, msg: InboundMessage, response: OutboundMessage) -> OutboundMessage:
        metadata = dict(response.metadata or {})
        metadata.setdefault("_control", True)
        metadata.setdefault("_command", self._command_name(msg))
        return replace(
            response,
            channel=response.channel or msg.channel,
            chat_id=response.chat_id or msg.chat_id,
            metadata=metadata,
            event_id=response.event_id or msg.event_id,
            correlation_id=response.correlation_id or msg.correlation_id,
            request_id=response.request_id or msg.request_id,
            principal_id=response.principal_id or msg.principal_id,
        )

    def _result_response(self, msg: InboundMessage, result: CommandResult) -> OutboundMessage:
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=CommandFeedback.render(result),
            event_id=msg.event_id,
            correlation_id=msg.correlation_id,
            request_id=msg.request_id,
            principal_id=msg.principal_id,
            metadata={"_control": True, "_command": self._command_name(msg)},
        )

    def _idle_response(self, msg: InboundMessage) -> OutboundMessage:
        return self._result_response(
            msg,
            CommandResult(
                ok=False,
                code=CommandErrorCode.INVALID_STATE,
                title="Command unavailable",
                message=USER_TEXT.command_idle_only,
            ),
        )

    def _busy_response(self, msg: InboundMessage) -> OutboundMessage:
        return self._result_response(
            msg,
            CommandResult(
                ok=False,
                code=CommandErrorCode.INVALID_STATE,
                title="Command lane busy",
                message=USER_TEXT.command_lane_busy,
            ),
        )

    async def _publish(self, response: OutboundMessage) -> None:
        await self._bus.publish_outbound(self._control_response_from_response(response))

    @staticmethod
    def _control_response_from_response(response: OutboundMessage) -> OutboundMessage:
        metadata = dict(response.metadata or {})
        metadata["_control"] = True
        return replace(response, metadata=metadata)

    def _forget_task(self, task: asyncio.Task[Any]) -> None:
        self._active.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error("Command task failed: {}", exception)
