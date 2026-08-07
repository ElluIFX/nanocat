"""Deterministic shell-command analysis used by the runtime security policy.

The analyzer is intentionally independent from the LLM and from subprocess
execution.  It does not try to prove that an arbitrary script is safe; when a
command cannot be bounded, it returns ``REQUIRE_INTERVENTION`` instead of
silently allowing it.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum


class CommandRisk(StrEnum):
    """Highest risk found in a command."""

    SAFE = "safe"
    REQUIRE_INTERVENTION = "require_intervention"
    HARD_DENY = "hard_deny"


@dataclass(frozen=True, slots=True)
class CommandSegment:
    """One top-level shell segment."""

    text: str
    tokens: tuple[str, ...]
    operator: str = ""


@dataclass(frozen=True, slots=True)
class CommandAnalysis:
    """Pure analysis result suitable for policy decisions and audit logs."""

    command: str
    shell: str
    risk: CommandRisk
    reason: str
    segments: tuple[CommandSegment, ...] = ()
    hazards: tuple[str, ...] = ()
    parse_error: str | None = None

    @property
    def dynamic(self) -> bool:
        return any(
            hazard
            in {
                "command_substitution",
                "encoded_command",
                "dynamic_script",
                "unresolved_variable",
            }
            for hazard in self.hazards
        )


_POWER_SHELL = {
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
}
_CMD = {"cmd", "cmd.exe"}
_SAFE_EXECUTABLES = {
    "cat",
    "dir",
    "echo",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "pwd",
    "rg",
    "sed",
    "tail",
    "type",
    "where",
    "which",
}
_SAFE_GIT_SUBCOMMANDS = {"branch", "diff", "log", "ls-files", "show", "status"}
_SAFE_POWER_SHELL_VERBS = {
    "get",
    "measure",
    "select",
    "sort",
    "where",
    "format",
    "compare",
    "out",
    "write",
    "test",
}
_INTERVENTION_COMMANDS = {
    "chmod",
    "chown",
    "crontab",
    "kill",
    "killall",
    "ncat",
    "nc",
    "netcat",
    "npm",
    "pkill",
    "powershell",
    "pwsh",
    "reg",
    "schtasks",
    "sc",
    "socat",
    "ssh",
    "start-process",
    "stop-process",
    "stop-service",
    "systemctl",
    "taskkill",
    "wmic",
}
_HARD_DENY_COMMANDS = {
    "diskpart",
    "format",
    "mkfs",
    "mkswap",
    "shutdown",
    "reboot",
    "poweroff",
}
_DELETE_COMMANDS = {
    "del",
    "erase",
    "find",
    "rd",
    "remove-item",
    "ri",
    "rm",
    "rmdir",
    "shred",
    "unlink",
}


def analyze_command(command: str, *, shell: bool | None = None) -> CommandAnalysis:
    """Analyze a command without executing it."""
    text = command.strip()
    if not text:
        return CommandAnalysis(text, _platform_shell(), CommandRisk.SAFE, "empty command")

    shell_name = _detect_shell(text, shell)
    try:
        segments = _split_segments(text)
        parsed = tuple(
            CommandSegment(segment, tuple(_tokenize(segment)), operator)
            for segment, operator in segments
        )
    except ValueError as exc:
        return CommandAnalysis(
            text,
            shell_name,
            CommandRisk.REQUIRE_INTERVENTION,
            "command syntax could not be parsed",
            parse_error=str(exc),
            hazards=("parse_failure",),
        )

    hazards: set[str] = set()
    reasons: list[str] = []
    highest = CommandRisk.SAFE
    for segment in parsed:
        risk, reason, segment_hazards = _classify_segment(segment, shell_name)
        hazards.update(segment_hazards)
        if reason:
            reasons.append(reason)
        if _risk_rank(risk) > _risk_rank(highest):
            highest = risk

    if any(op for _, op in segments):
        hazards.add("compound_command")
    if re.search(r"(?<!`)(?:\$\([^\n)]*\)|`[^`\n]+`)", text):
        hazards.add("command_substitution")
        if highest is CommandRisk.SAFE:
            highest = CommandRisk.REQUIRE_INTERVENTION
            reasons.append("command substitution is dynamic")
    if re.search(r"(?i)(?:^|\s)-encodedcommand(?:\s|$)", text):
        hazards.add("encoded_command")
        highest = CommandRisk.REQUIRE_INTERVENTION
        reasons.append("encoded PowerShell command cannot be inspected safely")
    if re.search(r"(?i)(?:^|[;&|\s])(?:curl|wget)\b[^\n]*\|\s*(?:sh|bash|pwsh|powershell|python)\b", text):
        highest = max_risk(highest, CommandRisk.REQUIRE_INTERVENTION)
        hazards.add("download_and_execute")
        reasons.append("downloaded content is piped into an interpreter")
    if re.search(r"(?i)(?:\$env:[A-Za-z_][\w]*|\$[A-Za-z_][\w]*)", text):
        hazards.add("unresolved_variable")
        if highest is CommandRisk.SAFE:
            highest = CommandRisk.REQUIRE_INTERVENTION
            reasons.append("command target depends on an unresolved variable")

    return CommandAnalysis(
        text,
        shell_name,
        highest,
        "; ".join(dict.fromkeys(reasons)) or "command is explicitly classified as safe",
        parsed,
        tuple(sorted(hazards)),
    )


def _classify_segment(
    segment: CommandSegment, shell_name: str
) -> tuple[CommandRisk, str, set[str]]:
    if not segment.tokens:
        return CommandRisk.REQUIRE_INTERVENTION, "empty compound command segment", {"parse_failure"}

    tokens = [token.strip("\"'").lower() for token in segment.tokens]
    executable = os.path.basename(tokens[0].replace("\\", "/"))
    hazards: set[str] = set()

    if any(operator in segment.text for operator in (">", "<")):
        hazards.add("redirection")
        return CommandRisk.REQUIRE_INTERVENTION, "shell redirection changes external state", hazards

    if executable in _HARD_DENY_COMMANDS:
        return CommandRisk.HARD_DENY, f"host-destructive command: {executable}", hazards
    if executable in _DELETE_COMMANDS and (
        executable != "find" or any(token in {"-delete", "-delete=true"} for token in tokens)
    ):
        return CommandRisk.HARD_DENY, f"shell deletion is not an approved file operation: {executable}", hazards

    if executable in _POWER_SHELL:
        if "-encodedcommand" in tokens:
            hazards.add("encoded_command")
            return CommandRisk.REQUIRE_INTERVENTION, "encoded PowerShell command", hazards
        if "-command" in tokens:
            hazards.add("dynamic_script")
            return CommandRisk.REQUIRE_INTERVENTION, "nested PowerShell script requires approval", hazards
        return CommandRisk.REQUIRE_INTERVENTION, "PowerShell invocation requires approval", hazards

    if executable in _CMD:
        if any(token in {"/c", "/k"} for token in tokens):
            hazards.add("dynamic_script")
            return CommandRisk.REQUIRE_INTERVENTION, "nested CMD script requires approval", hazards
        return CommandRisk.REQUIRE_INTERVENTION, "CMD invocation requires approval", hazards

    if executable in {"iex", "invoke-expression"}:
        hazards.add("dynamic_script")
        return CommandRisk.HARD_DENY, "dynamic script evaluation is not permitted", hazards

    if executable in _INTERVENTION_COMMANDS:
        return CommandRisk.REQUIRE_INTERVENTION, f"sensitive command: {executable}", hazards

    if executable in {"python", "python3", "node", "perl", "ruby", "bash", "sh", "zsh"}:
        if any(token in {"-c", "-e", "--eval", "-command"} for token in tokens):
            hazards.add("dynamic_script")
            return CommandRisk.REQUIRE_INTERVENTION, "inline interpreter script requires approval", hazards
        return CommandRisk.REQUIRE_INTERVENTION, "interpreter execution requires approval", hazards

    if executable == "git" and len(tokens) > 1 and tokens[1] in _SAFE_GIT_SUBCOMMANDS:
        return CommandRisk.SAFE, "read-only git command", hazards
    if executable in _SAFE_EXECUTABLES:
        return CommandRisk.SAFE, "read-only command", hazards

    if shell_name == "powershell" and "-" in executable:
        verb = executable.split("-", 1)[0]
        if verb in _SAFE_POWER_SHELL_VERBS:
            return CommandRisk.SAFE, "read-only PowerShell cmdlet", hazards

    return CommandRisk.REQUIRE_INTERVENTION, f"unclassified command: {executable}", {"unknown_command"}


def _split_segments(command: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    start = 0
    quote: str | None = None
    escaped = False
    depth = 0
    index = 0
    pending_operator = ""
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            index += 1
            continue
        if quote is not None:
            index += 1
            continue
        if char in "({[":
            depth += 1
            index += 1
            continue
        if char in ")}]":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced closing shell delimiter")
            index += 1
            continue
        operator = ""
        if depth == 0:
            if command[index : index + 2] in {"&&", "||"}:
                operator = command[index : index + 2]
            elif char in ";|&\n":
                operator = char
        if operator:
            value = command[start:index].strip()
            if value:
                segments.append((value, pending_operator))
            pending_operator = operator
            index += len(operator)
            start = index
            continue
        index += 1
    if quote is not None or depth != 0:
        raise ValueError("unbalanced shell quote or delimiter")
    value = command[start:].strip()
    if value:
        segments.append((value, pending_operator))
    return segments


def _tokenize(segment: str) -> list[str]:
    lexer = shlex.shlex(segment, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _detect_shell(command: str, shell: bool | None) -> str:
    if shell is False:
        return "direct"
    first = command.split(None, 1)[0].strip("\"'").lower()
    executable = os.path.basename(first.replace("\\", "/"))
    if executable in _POWER_SHELL:
        return "powershell"
    if executable in _CMD:
        return "cmd"
    return _platform_shell()


def _platform_shell() -> str:
    return "powershell" if os.name == "nt" else "posix"


def _risk_rank(risk: CommandRisk) -> int:
    return {CommandRisk.SAFE: 0, CommandRisk.REQUIRE_INTERVENTION: 1, CommandRisk.HARD_DENY: 2}[risk]


def max_risk(left: CommandRisk, right: CommandRisk) -> CommandRisk:
    return left if _risk_rank(left) >= _risk_rank(right) else right
