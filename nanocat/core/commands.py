"""Stable command contracts for the control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from nanocat.core.messages import ConversationRef, Principal


class CommandClassification(StrEnum):
    """Classification returned before ordinary LLM routing."""

    COMMAND = "command"
    ESCAPED_TEXT = "escaped_text"
    ORDINARY_TEXT = "ordinary_text"


class CommandExecutionPolicy(StrEnum):
    """Interaction policy for a registered command."""

    IMMEDIATE = "immediate"
    CANCEL_TURN = "cancel_turn"
    QUEUE = "queue"
    NEW_TURN = "new_turn"
    RUNTIME_CONTROL = "runtime_control"
    INTERVENTION_RESPONSE = "intervention_response"
    IDLE_ONLY = "idle_only"


class CommandErrorCode(StrEnum):
    """Stable user-visible command error codes."""

    UNKNOWN_COMMAND = "unknown_command"
    UNKNOWN_SUBCOMMAND = "unknown_subcommand"
    MISSING_ARGUMENT = "missing_argument"
    INVALID_ARGUMENT = "invalid_argument"
    CONFLICTING_ARGUMENT = "conflicting_argument"
    PERMISSION_DENIED = "permission_denied"
    COMMAND_DISABLED = "command_disabled"
    INVALID_STATE = "invalid_state"
    INTERVENTION_NOT_FOUND = "intervention_not_found"
    INTERVENTION_EXPIRED = "intervention_expired"
    COMMAND_FAILED = "command_failed"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """Side-effect-free parsed command syntax."""

    raw_text: str
    name: str
    canonical_name: str
    subcommand: str | None = None
    positional_args: tuple[str, ...] = ()
    options: Mapping[str, str | bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Single source of truth for command identity and behavior metadata."""

    name: str
    aliases: tuple[str, ...] = ()
    group: str = "general"
    summary: str = ""
    usage: str = ""
    examples: tuple[str, ...] = ()
    subcommands: tuple[str, ...] = ()
    requires_subcommand: bool = False
    permission: str = "user"
    execution_policy: CommandExecutionPolicy = CommandExecutionPolicy.IMMEDIATE
    handler: Any | None = None
    enabled: bool = True
    legacy_passthrough: bool = False
    accepts_arguments: bool = True
    idle_only: bool = False
    idle_exempt_subcommands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip().lstrip("/").lower())
        object.__setattr__(
            self,
            "aliases",
            tuple(alias.strip().lstrip("/").lower() for alias in self.aliases),
        )
        object.__setattr__(
            self,
            "subcommands",
            tuple(item.strip().lower() for item in self.subcommands),
        )
        object.__setattr__(
            self,
            "idle_exempt_subcommands",
            tuple(item.strip().lower() for item in self.idle_exempt_subcommands),
        )


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Explicit command handler context; no implicit AgentLoop state."""

    principal: Principal | None
    conversation: ConversationRef
    session: Any | None = None
    turn_id: str | None = None
    runtime_context: Any | None = None
    intervention_broker: Any | None = None
    cancellation_token: Any | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured command result rendered by the channel adapter later."""

    ok: bool
    code: str = "ok"
    title: str = ""
    message: str = ""
    usage: str | None = None
    examples: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)
    persist_history: bool = False
    execution_effect: str = "no-op"
    audit: Mapping[str, Any] = field(default_factory=dict)
