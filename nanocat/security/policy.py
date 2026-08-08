"""Structured runtime security decisions for tool execution.

This module deliberately contains no user-facing messaging.  A policy either
allows a call, rejects it as a hard policy violation, or asks the application
layer to obtain a runtime-scoped intervention decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nanocat.security.command_analyzer import CommandRisk, analyze_command
from nanocat.security.network import validate_url_target
from nanocat.security.path import extract_absolute_paths, resolve_path


class SecurityDecisionKind(StrEnum):
    """Outcome of a security preflight."""

    ALLOW = "allow"
    HARD_DENY = "hard_deny"
    REQUIRE_INTERVENTION = "require_intervention"


@dataclass(frozen=True, slots=True)
class SecurityDecision:
    """Structured result consumed by ``ToolExecutor``."""

    kind: SecurityDecisionKind
    capability: str
    summary: str
    reason: str = ""
    call_fingerprint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _fingerprint(tool_name: str, params: dict[str, Any], capability: str) -> str:
    payload = json.dumps(
        {"tool": tool_name, "capability": capability, "params": params},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_text(value: Any, limit: int = 240) -> str:
    text = str(value).strip()
    text = re.sub(
        r"(?i)(authorization|cookie|password|passwd|secret|token|api[-_ ]?key)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return text[:limit] + ("..." if len(text) > limit else "")


def _safe_command_summary(command: str) -> str:
    """Keep approval summaries to the executable and a small argument prefix."""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    visible = " ".join(_safe_text(part, 80) for part in parts[:3])
    return visible + (" ..." if len(parts) > 3 else "")


def _safe_url_summary(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "unknown-host"
    return f"{parsed.scheme or 'unknown'}://{host}"


class SecurityPolicy:
    """Evaluate built-in tool calls before they reach tool implementations.

    This is the single model-facing security decision point.  Command, path,
    and network analysis is deterministic and returns typed outcomes; legacy
    tool-local guards remain only as a defense for direct non-model calls.
    """

    _PATH_TOOLS = frozenset(
        {
            "read_file",
            "write_file",
            "edit_file",
            "list_dir",
            "grep_file",
            "insert_lines",
            "delete_lines",
            "file_hex",
            "load_image",
            "parse_image",
        }
    )
    _PATH_MUTATION_TOOLS = frozenset(
        {"write_file", "edit_file", "insert_lines", "delete_lines", "delete"}
    )
    _SAFE_TOOLS = frozenset(
        {
            "ask",
            "context_lookup",
            "file_hex",
            "grep_file",
            "list_dir",
            "load_image",
            "memory_add",
            "memory_delete",
            "memory_get",
            "memory_search",
            "memory_thread_get",
            "memory_thread_search",
            "memory_update",
            "parse_image",
            "proc_list",
            "proc_read",
            "read_file",
            "read_working_memory",
            "screenshot",
            "ssh_list",
            "ssh_read",
            "wait",
            "web_fetch",
            "web_search",
        }
    )

    def __init__(
        self,
        config: Any,
        workspace: Path,
        *,
        extra_allowed_dirs: tuple[Path, ...] = (),
    ):
        self._config = config
        self._workspace = workspace.resolve()
        self._extra_allowed_dirs = tuple(path.resolve() for path in extra_allowed_dirs)

    def evaluate(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> SecurityDecision:
        capability = f"tool.{tool_name}"

        # This is an explicit administrator-level bypass.  It intentionally
        # precedes every domain policy, including hard-deny rules.
        if not self._config.tools.global_safty_check:
            return self._allow(tool_name, params, capability, reason="global safety check disabled")

        if tool_name in {"exec", "proc_start", "proc_send", "proc_stop"}:
            if not self._config.tools.cmd.safety_check:
                return self._allow(tool_name, params, capability, reason="command safety check disabled")
        if tool_name in self._PATH_TOOLS or tool_name == "delete":
            if not self._config.tools.filesystem.safety_check:
                return self._allow(tool_name, params, capability, reason="filesystem safety check disabled")
        if tool_name in {"http_request", "web_fetch", "web_search"}:
            if not self._config.tools.web.safety_check:
                return self._allow(tool_name, params, capability, reason="web safety check disabled")

        if tool_name == "exec":
            decision = self._evaluate_command(tool_name, params, capability)
        elif tool_name == "proc_start":
            decision = self._evaluate_process_side_effect(tool_name, params, capability)
        elif tool_name in self._PATH_TOOLS or tool_name == "delete":
            decision = self._evaluate_paths(tool_name, params, capability)
        elif tool_name in {"http_request", "web_fetch"}:
            decision = self._evaluate_url(tool_name, params, capability)
        elif tool_name == "todo":
            decision = (
                self._require(
                    "message.send",
                    "Send a todo update through the current channel",
                    "todo notification is an external side effect",
                    tool_name,
                    params,
                )
                if params.get("notify")
                else self._allow(tool_name, params, capability)
            )
        elif tool_name == "message":
            path_decision = self._evaluate_declared_paths(tool_name, params, capability)
            decision = path_decision or self._require(
                "message.send",
                "Send a message through a configured channel",
                "outbound delivery is an external side effect",
                tool_name,
                params,
            )
        elif tool_name == "cron":
            action = str(params.get("action", "")).lower()
            decision = (
                self._allow(tool_name, params, capability)
                if action == "list"
                else self._require(
                    f"cron.{action or 'mutate'}",
                    f"Change scheduled jobs ({action or 'unknown action'})",
                    "scheduled jobs persist beyond the current turn",
                    tool_name,
                    params,
                )
            )
        elif tool_name in {"proc_send", "proc_stop"}:
            decision = self._require(
                f"process.{tool_name.removeprefix('proc_')}",
                f"Mutate a local process with {tool_name}",
                "long-lived process state requires explicit user approval",
                tool_name,
                params,
            )
        elif tool_name in {"ssh_open", "ssh_send", "ssh_close"}:
            decision = self._require(
                f"ssh.{tool_name.removeprefix('ssh_')}",
                f"Allow remote SSH operation {tool_name}",
                "remote access or session mutation requires explicit user approval",
                tool_name,
                params,
            )
        elif tool_name.startswith("mcp_"):
            decision = self._require(
                f"mcp.{tool_name}",
                f"Allow external MCP operation {tool_name}",
                "MCP calls cross the runtime capability boundary",
                tool_name,
                params,
            )
        elif tool_name in self._SAFE_TOOLS:
            decision = self._allow(tool_name, params, capability)
        else:
            decision = self._require(
                capability,
                f"Execute unclassified capability {tool_name}",
                "unclassified tool side effects require explicit user approval",
                tool_name,
                params,
            )

        return decision

    def _evaluate_command(
        self,
        tool_name: str,
        params: dict[str, Any],
        capability: str,
    ) -> SecurityDecision:
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            return self._require(
                capability,
                f"Provide a command to {tool_name}",
                "command is missing or empty",
                tool_name,
                params,
            )
        cwd = str(params.get("working_dir") or params.get("cwd") or self._workspace)
        cmd_config = self._config.tools.cmd
        lower = command.casefold()
        if cmd_config.allow_regex and not any(re.search(pattern, lower) for pattern in cmd_config.allow_regex):
            return self._decision(
                SecurityDecisionKind.HARD_DENY,
                capability,
                f"Blocked {tool_name} command: {_safe_command_summary(command)}",
                "command is not in the configured allow-list",
                tool_name,
                params,
            )
        if any(re.search(pattern, lower) for pattern in cmd_config.deny_regex):
            return self._decision(
                SecurityDecisionKind.HARD_DENY,
                capability,
                f"Blocked {tool_name} command: {_safe_command_summary(command)}",
                "configured command deny rule matched",
                tool_name,
                params,
            )

        analysis = analyze_command(command, shell=params.get("shell"))
        if cmd_config.restrict_to_workspace:
            for raw_path in extract_absolute_paths(command):
                try:
                    target = Path(raw_path).expanduser().resolve()
                except (OSError, RuntimeError):
                    return self._require(
                        capability,
                        f"Inspect command target: {_safe_command_summary(command)}",
                        "command path could not be resolved safely",
                        tool_name,
                        params,
                    )
                if not target.is_relative_to(Path(cwd).resolve()):
                    return self._decision(
                        SecurityDecisionKind.HARD_DENY,
                        capability,
                        f"Blocked {tool_name} access outside the working directory",
                        "command path containment rejected the target",
                        tool_name,
                        params,
                    )

        decision_kind = {
            CommandRisk.SAFE: SecurityDecisionKind.ALLOW,
            CommandRisk.REQUIRE_INTERVENTION: SecurityDecisionKind.REQUIRE_INTERVENTION,
            CommandRisk.HARD_DENY: SecurityDecisionKind.HARD_DENY,
        }[analysis.risk]
        summary = f"Execute {tool_name}: {_safe_command_summary(command)}"
        return self._decision(
            decision_kind,
            capability,
            summary,
            analysis.reason,
            tool_name,
            params,
            shell=analysis.shell,
            hazards=analysis.hazards,
            segments=tuple(segment.text for segment in analysis.segments),
            parse_error=analysis.parse_error,
        )

    def _evaluate_paths(
        self,
        tool_name: str,
        params: dict[str, Any],
        capability: str,
    ) -> SecurityDecision:
        paths = self._path_values(tool_name, params)
        for raw_path in paths:
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            decision = self._check_path(tool_name, raw_path, capability, params)
            if decision is not None:
                return decision
        if tool_name in self._PATH_MUTATION_TOOLS:
            return self._require(
                f"filesystem.{tool_name}",
                f"Modify filesystem targets with {tool_name}",
                "filesystem mutations require explicit user approval",
                tool_name,
                params,
            )
        if tool_name == "delete" and params.get("permanent"):
            fs_config = self._config.tools.filesystem
            if fs_config.safety_check:
                return self._require(
                    "filesystem.delete_permanent",
                    "Permanently delete the requested filesystem targets",
                    "permanent deletion is not recoverable",
                    tool_name,
                    params,
                )
        return self._allow(tool_name, params, capability)

    def _evaluate_process_side_effect(
        self,
        tool_name: str,
        params: dict[str, Any],
        capability: str,
    ) -> SecurityDecision:
        decision = self._evaluate_command(tool_name, params, capability)
        if decision.kind is not SecurityDecisionKind.ALLOW:
            return decision
        return self._require(
            "process.start",
            "Start a long-lived local process",
            "persistent process state requires explicit user approval",
            tool_name,
            params,
        )

    def _check_path(
        self,
        tool_name: str,
        raw_path: str,
        capability: str,
        params: dict[str, Any],
        *,
        force_containment: bool = False,
    ) -> SecurityDecision | None:
        fs_config = self._config.tools.filesystem
        if not fs_config.safety_check:
            return None
        resolved = resolve_path(raw_path, workspace=self._workspace)
        target = str(resolved).replace("\\", "/").lower()
        if fs_config.allow_regex and not any(re.search(rx, target) for rx in fs_config.allow_regex):
            reason = "filesystem allow-list rejected the target"
        elif any(re.search(rx, target) for rx in fs_config.deny_regex):
            reason = "filesystem deny-list rejected the target"
        elif (force_containment or fs_config.restrict_to_workspace) and not any(
            resolved.is_relative_to(boundary)
            for boundary in (self._workspace, *self._extra_allowed_dirs)
        ):
            reason = "filesystem workspace containment rejected the target"
        else:
            return None
        return self._decision(
            SecurityDecisionKind.HARD_DENY,
            capability,
            f"Blocked {tool_name} access to {_safe_text(raw_path)}",
            reason,
            tool_name,
            params,
        )

    def _evaluate_url(
        self,
        tool_name: str,
        params: dict[str, Any],
        capability: str,
    ) -> SecurityDecision:
        path_decision = self._evaluate_declared_paths(tool_name, params, capability)
        if path_decision is not None:
            return path_decision
        url = params.get("url")
        if not isinstance(url, str) or not url.strip():
            return self._allow(tool_name, params, capability)
        web_config = self._config.tools.web
        method = str(params.get("method", "GET")).upper()
        if not web_config.safety_check:
            if tool_name == "http_request" and method not in {"GET", "HEAD", "OPTIONS"}:
                return self._require(
                    "network.http_write",
                    f"Send an HTTP {method} request to {_safe_url_summary(url)}",
                    "HTTP mutation requires explicit user approval",
                    tool_name,
                    params,
                )
            return self._allow(tool_name, params, capability)
        ok, reason = validate_url_target(url)
        if not ok:
            return self._decision(
                SecurityDecisionKind.HARD_DENY,
                "network.private_target",
                f"Blocked private or internal URL {_safe_url_summary(url)}",
                reason,
                tool_name,
                params,
            )
        if tool_name == "http_request" and method not in {"GET", "HEAD", "OPTIONS"}:
            return self._require(
                "network.http_write",
                f"Send an HTTP {method} request to {_safe_url_summary(url)}",
                "HTTP mutation requires explicit user approval",
                tool_name,
                params,
            )
        return self._allow(tool_name, params, capability)

    def _path_values(self, tool_name: str, params: dict[str, Any]) -> list[Any]:
        if tool_name == "delete":
            values = params.get("paths", [])
            values = [values] if isinstance(values, str) else values
            return [*values, params.get("path")]
        keys = ("path", "directory", "file", "source", "target")
        return [params.get(key) for key in keys]

    def _evaluate_declared_paths(
        self,
        tool_name: str,
        params: dict[str, Any],
        capability: str,
    ) -> SecurityDecision | None:
        """Validate local paths carried as message attachments or uploads."""
        if tool_name == "message":
            values = params.get("attachments", [])
        elif tool_name == "http_request":
            files = params.get("files", {})
            values = list(files.values()) if isinstance(files, dict) else files
        else:
            return None
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple)):
            return None
        for raw_path in values:
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            decision = self._check_path(
                tool_name,
                raw_path,
                capability,
                params,
                force_containment=True,
            )
            if decision is not None:
                return decision
        return None

    def _require(
        self,
        capability: str,
        summary: str,
        reason: str,
        tool_name: str,
        params: dict[str, Any],
    ) -> SecurityDecision:
        return self._decision(
            SecurityDecisionKind.REQUIRE_INTERVENTION,
            capability,
            summary,
            reason,
            tool_name,
            params,
        )

    def _allow(
        self,
        tool_name: str,
        params: dict[str, Any],
        capability: str,
        *,
        reason: str = "",
    ) -> SecurityDecision:
        return self._decision(
            SecurityDecisionKind.ALLOW,
            capability,
            f"Execute {tool_name}",
            reason,
            tool_name,
            params,
        )

    @staticmethod
    def _decision(
        kind: SecurityDecisionKind,
        capability: str,
        summary: str,
        reason: str,
        tool_name: str,
        params: dict[str, Any],
        **metadata: Any,
    ) -> SecurityDecision:
        metadata = {"tool_name": tool_name, **metadata}
        return SecurityDecision(
            kind=kind,
            capability=capability,
            summary=summary,
            reason=reason,
            call_fingerprint=_fingerprint(tool_name, params, capability),
            metadata=metadata,
        )
