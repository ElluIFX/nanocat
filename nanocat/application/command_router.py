"""Command registry and pre-LLM control-plane inspection."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import Iterable

from nanocat.application.command_feedback import CommandFeedback
from nanocat.application.command_parser import (
    ClassifiedText,
    CommandParseError,
    CommandParser,
)
from nanocat.core.commands import (
    CommandClassification,
    CommandErrorCode,
    CommandExecutionPolicy,
    CommandResult,
    CommandSpec,
    ParsedCommand,
)


@dataclass(frozen=True, slots=True)
class CommandInspection:
    """Result of classifying and resolving one inbound text."""

    classified: ClassifiedText
    parsed: ParsedCommand | None = None
    spec: CommandSpec | None = None
    result: CommandResult | None = None

    @property
    def is_command(self) -> bool:
        return self.classified.kind is CommandClassification.COMMAND


class CommandRegistry:
    """Runtime-local command registry with alias collision checks."""

    def __init__(self, specs: Iterable[CommandSpec] = ()):
        self._specs: dict[str, CommandSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: CommandSpec) -> None:
        names = (spec.name, *spec.aliases)
        for name in names:
            if name in self._specs and self._specs[name] is not spec:
                raise ValueError(f"command name collision: {name}")
        for name in names:
            self._specs[name] = spec

    def get(self, name: str) -> CommandSpec | None:
        return self._specs.get(name.strip().lstrip("/").lower())

    def names(self) -> tuple[str, ...]:
        return tuple(sorted({spec.name for spec in self._specs.values()}))

    def specs(self) -> tuple[CommandSpec, ...]:
        """Return unique command specifications in stable display order."""
        return tuple(sorted(set(self._specs.values()), key=lambda spec: (spec.group, spec.name)))


class CommandRouter:
    """Perform command classification and deterministic pre-LLM rejection."""

    def __init__(self, registry: CommandRegistry, parser: CommandParser | None = None):
        self.registry = registry
        self.parser = parser or CommandParser()

    @classmethod
    def legacy_compatibility(cls) -> "CommandRouter":
        """Build the first registry while legacy handlers are still active."""
        legacy_names = (
            "logs",
            "help",
            "commands",
            "new",
            "stop",
            "restart",
            "model",
            "context",
            "whoami",
            "compact",
            "session",
        )
        no_argument_commands = frozenset(
            {"new", "stop", "restart", "context", "whoami", "compact"}
        )
        metadata = {
            "logs": ("Show recent runtime logs", "/logs [N]"),
            "help": ("Show command help", "/help [command|group]"),
            "commands": ("List command groups and usage", "/commands [command|group]"),
            "new": ("Start a new conversation", "/new"),
            "stop": ("Stop the current task", "/stop"),
            "restart": ("Restart the runtime", "/restart"),
            "model": (
                "View or switch models and reasoning effort",
                "/model [add|delete|agent|subagent|assistant|effort]",
            ),
            "context": ("Show context and token usage", "/context"),
            "whoami": ("Show the current principal and session", "/whoami"),
            "compact": ("Compact the current conversation", "/compact"),
            "session": ("Inspect or switch sessions", "/session [list|use|delete]"),
        }
        specs = []
        for name in legacy_names:
            summary, usage = metadata[name]
            specs.append(
                CommandSpec(
                    name=name,
                    group="legacy",
                    aliases=(
                        ("ctx",) if name == "context" else ("sid",) if name == "session" else ()
                    ),
                    summary=summary,
                    usage=usage,
                    execution_policy=(
                        CommandExecutionPolicy.CANCEL_TURN
                        if name == "stop"
                        else CommandExecutionPolicy.RUNTIME_CONTROL
                        if name == "restart"
                        else CommandExecutionPolicy.IMMEDIATE
                    ),
                    legacy_passthrough=True,
                    accepts_arguments=name not in no_argument_commands,
                )
            )
        specs.extend(
            [
                CommandSpec(
                    name="approve",
                    group="security",
                    summary="Approve the current sensitive operation",
                    usage="/approve once|turn",
                    execution_policy=CommandExecutionPolicy.INTERVENTION_RESPONSE,
                ),
                CommandSpec(
                    name="deny",
                    aliases=("reject",),
                    group="security",
                    summary="Reject the current sensitive operation",
                    usage="/deny",
                    execution_policy=CommandExecutionPolicy.INTERVENTION_RESPONSE,
                ),
                CommandSpec(
                    name="cron",
                    group="schedule",
                    summary="Manage scheduled jobs",
                    usage="/cron list|show|add|remove|run|enable|disable",
                    subcommands=("list", "show", "add", "remove", "run", "enable", "disable"),
                    requires_subcommand=True,
                    execution_policy=CommandExecutionPolicy.QUEUE,
                    legacy_passthrough=True,
                ),
                CommandSpec(
                    name="memory",
                    group="memory",
                    summary="Manage user-visible memory",
                    usage="/memory search|show|add|update|delete",
                    subcommands=("search", "show", "add", "update", "delete"),
                    requires_subcommand=True,
                    execution_policy=CommandExecutionPolicy.QUEUE,
                    legacy_passthrough=True,
                ),
            ]
        )
        return cls(CommandRegistry(specs))

    def inspect(self, text: str) -> CommandInspection:
        classified = self.parser.classify(text)
        if classified.kind is not CommandClassification.COMMAND:
            return CommandInspection(classified=classified)
        try:
            parsed = self.parser.parse(text)
        except CommandParseError as exc:
            return CommandInspection(
                classified=classified,
                result=CommandResult(
                    ok=False,
                    code=CommandErrorCode.INVALID_ARGUMENT,
                    title="Invalid command syntax",
                    message=str(exc),
                    usage="/help",
                    suggestions=("/help",),
                ),
            )

        spec = self.registry.get(parsed.canonical_name)
        if spec is None:
            suggestions = tuple(
                f"/{name}"
                for name in get_close_matches(parsed.canonical_name, self.registry.names(), n=3)
            )
            return CommandInspection(
                classified=classified,
                parsed=parsed,
                result=CommandResult(
                    ok=False,
                    code=CommandErrorCode.UNKNOWN_COMMAND,
                    title="Unknown command",
                    message=f"`/{parsed.canonical_name}` is not a registered command.",
                    usage="/help",
                    suggestions=suggestions or ("/help",),
                ),
            )
        if not spec.enabled:
            return CommandInspection(
                classified=classified,
                parsed=parsed,
                spec=spec,
                result=CommandResult(
                    ok=False,
                    code=CommandErrorCode.COMMAND_DISABLED,
                    title="Command disabled",
                    message=f"`/{spec.name}` is currently disabled.",
                    usage=spec.usage or "/help",
                ),
            )
        if not spec.accepts_arguments and (parsed.subcommand or parsed.options):
            return CommandInspection(
                classified=classified,
                parsed=parsed,
                spec=spec,
                result=CommandResult(
                    ok=False,
                    code=CommandErrorCode.INVALID_ARGUMENT,
                    title="Unexpected command arguments",
                    message=f"`/{spec.name}` does not accept arguments.",
                    usage=spec.usage or f"/{spec.name}",
                ),
            )
        if spec.requires_subcommand and parsed.subcommand is None:
            return CommandInspection(
                classified=classified,
                parsed=parsed,
                spec=spec,
                result=CommandResult(
                    ok=False,
                    code=CommandErrorCode.MISSING_ARGUMENT,
                    title="Missing command subcommand",
                    message=f"`/{spec.name}` requires a subcommand.",
                    usage=spec.usage or "/help",
                ),
            )
        if spec.subcommands and parsed.subcommand not in spec.subcommands:
            suggestions = tuple(f"/{spec.name} {name}" for name in spec.subcommands[:3])
            return CommandInspection(
                classified=classified,
                parsed=parsed,
                spec=spec,
                result=CommandResult(
                    ok=False,
                    code=CommandErrorCode.UNKNOWN_SUBCOMMAND,
                    title="Unknown command subcommand",
                    message=(
                        f"`{parsed.subcommand or ''}` is not a valid subcommand for `/{spec.name}`."
                    ),
                    usage=spec.usage or "/help",
                    suggestions=suggestions,
                ),
            )
        if not spec.legacy_passthrough and spec.handler is None:
            return CommandInspection(
                classified=classified,
                parsed=parsed,
                spec=spec,
                result=CommandResult(
                    ok=False,
                    code=CommandErrorCode.COMMAND_FAILED,
                    title="Command handler unavailable",
                    message=f"`/{spec.name}` is registered but not yet available in this runtime.",
                    usage=spec.usage or "/help",
                ),
            )
        return CommandInspection(classified=classified, parsed=parsed, spec=spec)

    def is_standalone(self, text: str) -> bool:
        """Return whether command metadata says the message must not interrupt a turn."""
        inspection = self.inspect(text)
        spec = inspection.spec
        if spec is None or inspection.classified.kind is not CommandClassification.COMMAND:
            return False
        return spec.execution_policy is not CommandExecutionPolicy.NEW_TURN

    @staticmethod
    def feedback(result: CommandResult) -> str:
        """Render a command result for the current legacy channel boundary."""
        return CommandFeedback.render(result)

    def command_list(self, query: str | None = None) -> str:
        """Render deterministic grouped help, optionally scoped to a command/group."""
        specs = list(self.registry.specs())
        if query:
            normalized = query.strip().lstrip("/").lower()
            matching = [spec for spec in specs if spec.name == normalized]
            if matching:
                specs = matching
            else:
                specs = [spec for spec in specs if spec.group == normalized]
            if not specs:
                return f"No command or group named `/{normalized}`.\n\n{self.command_list()}"
        groups: dict[str, list[CommandSpec]] = {}
        for spec in specs:
            groups.setdefault(spec.group, []).append(spec)
        lines = ["## Available commands"]
        for group, specs in groups.items():
            lines.append(f"### {group.title()}")
            for spec in specs:
                if spec.name == "approve":
                    lines.append(
                        "- `/approve once|turn`: Approve the current sensitive operation"
                    )
                    lines.append(
                        "- `/approve forever|cancel`: Enable or revoke session-wide YOLO approval"
                    )
                    continue
                aliases = (
                    f" ({', '.join('`/' + alias + '`' for alias in spec.aliases)})"
                    if spec.aliases
                    else ""
                )
                usage = f" — `{spec.usage}`" if spec.usage else ""
                lines.append(f"- `/{spec.name}`{aliases}: {spec.summary}{usage}")
        return "\n\n".join(lines)
