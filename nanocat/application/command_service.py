"""Application command dispatch independent from the LLM turn pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from nanocat.application.command_handlers import RuntimeCommandHandlers
from nanocat.application.command_router import CommandRouter
from nanocat.application.intervention import parse_intervention_action
from nanocat.bus.events import InboundMessage, OutboundMessage
from nanocat.core.commands import CommandClassification


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Result of handling one command before normal turn construction."""

    handled: bool = False
    response: OutboundMessage | None = None
    message: InboundMessage | None = None


@dataclass(frozen=True, slots=True)
class CommandCallbacks:
    """Legacy behavior callbacks exposed as narrow application ports."""

    new_session: Callable[[InboundMessage, Any], Awaitable[str | None] | str | None]
    logs: Callable[[InboundMessage], Awaitable[OutboundMessage] | OutboundMessage]
    compact: Callable[
        [InboundMessage, Any, str | None],
        Awaitable[OutboundMessage] | OutboundMessage,
    ]
    whoami: Callable[[InboundMessage, Any], Awaitable[OutboundMessage]]
    model: Callable[[InboundMessage], Awaitable[OutboundMessage]]
    session: Callable[[InboundMessage, Any], Awaitable[OutboundMessage]]
    intervention_response: Callable[[InboundMessage], Awaitable[OutboundMessage]] | None = None


class CommandService:
    """Execute control-plane commands without entering provider execution."""

    def __init__(
        self,
        router: CommandRouter,
        runtime_handlers: RuntimeCommandHandlers,
        callbacks: CommandCallbacks,
    ):
        self.router = router
        self.runtime_handlers = runtime_handlers
        self.callbacks = callbacks

    async def dispatch_intervention(self, msg: InboundMessage) -> OutboundMessage | None:
        """Resolve an explicit intervention response before command or LLM routing."""
        if parse_intervention_action(msg.content) is None:
            return None
        if self.callbacks.intervention_response is None:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Intervention response handler is unavailable.",
                metadata={"_control": True, "_intervention": True},
            )
        return await self.callbacks.intervention_response(msg)

    async def dispatch(
        self,
        msg: InboundMessage,
        session: Any,
    ) -> CommandOutcome:
        """Handle a command or return an unchanged message for normal turns."""
        inspection = self.router.inspect(msg.content)
        if inspection.classified.kind is CommandClassification.ESCAPED_TEXT:
            return CommandOutcome(message=replace(msg, content=inspection.classified.text))
        if not inspection.is_command:
            return CommandOutcome(message=msg)

        if inspection.result is not None:
            return CommandOutcome(
                handled=True,
                response=OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.router.feedback(inspection.result),
                    metadata={"_control": True},
                ),
            )

        command_name = inspection.spec.name if inspection.spec is not None else None
        if command_name is None:
            return CommandOutcome(message=msg)

        if command_name == "logs":
            result = self.callbacks.logs(msg)
            if hasattr(result, "__await__"):
                result = await result
            return CommandOutcome(handled=True, response=result)

        _raw_command, separator, command_tail = msg.content.strip().partition(" ")
        if command_name in {"model", "session"}:
            msg = replace(
                msg,
                content=f"/{command_name}{(' ' + command_tail) if separator else ''}",
            )

        if command_name in {"cron", "memory"}:
            result = await self.runtime_handlers.execute(
                inspection,
                principal_id=msg.principal_id or msg.sender_id,
                session=session,
            )
            return CommandOutcome(
                handled=True,
                response=OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.router.feedback(result),
                    metadata={"_control": True},
                ),
            )

        if command_name == "new":
            result = self.callbacks.new_session(msg, session)
            if hasattr(result, "__await__"):
                result = await result
            return CommandOutcome(
                handled=True,
                response=OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=str(result or "New session started."),
                ),
            )

        if command_name == "help":
            query = command_tail.strip() or None
            return CommandOutcome(
                handled=True,
                response=OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=self.router.command_list(query),
                ),
            )

        if command_name == "compact":
            subcommand = inspection.parsed.subcommand if inspection.parsed else None
            result = self.callbacks.compact(msg, session, subcommand)
            if hasattr(result, "__await__"):
                result = await result
            return CommandOutcome(handled=True, response=result)

        if command_name == "whoami":
            return CommandOutcome(handled=True, response=await self.callbacks.whoami(msg, session))

        if command_name == "model":
            return CommandOutcome(handled=True, response=await self.callbacks.model(msg))

        if command_name == "session":
            return CommandOutcome(handled=True, response=await self.callbacks.session(msg, session))

        return CommandOutcome(message=msg)
