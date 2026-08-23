"""Structured runtime security decisions for tool execution.

This module deliberately contains no user-facing messaging.  A policy either
allows a call, rejects it as a hard policy violation, or asks the application
layer to obtain a runtime-scoped intervention decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from nanocat.security.command import CommandRisk, analyze_command, extract_command_urls
from nanocat.security.network import is_local_url
from nanocat.security.path import extract_absolute_paths, is_under, resolve_path


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


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|token|api[-_ ]?key|credential|identity)",
)
_SENSITIVE_INLINE_RE = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|token|api[-_ ]?key|credential)"
    r"\s*[:=]\s*[^\n,;'\"]+",
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://[^/\s:@]+):[^@\s]+@")


def _safe_text(value: Any, limit: int = 240) -> str:
    text = _redact_string(str(value).strip())
    return text[:limit] + ("..." if len(text) > limit else "")


def _redact_string(value: str) -> str:
    text = _SENSITIVE_INLINE_RE.sub(r"\1=[REDACTED]", value)
    return _URL_CREDENTIAL_RE.sub(r"\1:[REDACTED]@", text)


def _redact_value(value: Any, key: str | None = None) -> Any:
    if key and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_value(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _safe_params(params: dict[str, Any], limit: int = 4000) -> str:
    text = json.dumps(_redact_value(params), ensure_ascii=False, separators=(",", ":"), default=str)
    return text[:limit] + ("..." if len(text) > limit else "")


def _tool_match_text(tool_name: str, params: dict[str, Any]) -> str:
    payload = json.dumps(params, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{tool_name}({payload})"


def _tool_display(tool_name: str, params: dict[str, Any]) -> str:
    return f"{tool_name}({_safe_params(params)})"


def _compile_rules(patterns: list[str], field_name: str) -> tuple[re.Pattern[str], ...]:
    try:
        return tuple(re.compile(pattern) for pattern in patterns if pattern)
    except re.error as exc:
        raise ValueError(f"invalid {field_name} pattern: {exc}") from exc


class SecurityPolicy:
    """Evaluate built-in tool calls before they reach tool implementations.

    This is the single model-facing security decision point.  Command, path,
    and network analysis is deterministic and returns typed outcomes; legacy
    tool implementations contain no policy bypass.
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
        }
    )
    _SAFE_TOOLS = frozenset(
        {
            "ask",
            "wait",
            "message",
            "cron",
            "todo",
            "proc_list",
            "proc_read",
            "read_working_memory",
            "screenshot",
            "ssh_list",
            "ssh_read",
            "web_search",
            "memory_*",
            "subagent_*",
        }
    )

    def _is_safe_tool(self, tool_name: str) -> bool:
        policy = self._config.tools.policy
        return any(
            fnmatchcase(tool_name, pattern)
            for pattern in (*self._SAFE_TOOLS, *policy.safty_safe_tool)
            if pattern
        )

    def __init__(
        self,
        config: Any,
        workspace: Path,
    ):
        self._config = config
        self._workspace = workspace.resolve()
        policy = config.tools.policy
        if policy.safty_check:
            self._allow_rules = _compile_rules(policy.safty_allow_regex, "safty_allow_regex")
            self._deny_rules = _compile_rules(policy.safty_deny_regex, "safty_deny_regex")
        else:
            self._allow_rules = ()
            self._deny_rules = ()

    def evaluate(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> SecurityDecision:
        capability = f"tool.{tool_name}"

        policy = self._config.tools.policy
        if not policy.safty_check:
            return self._allow(tool_name, params, capability, reason="safety policy disabled")

        if self._is_safe_tool(tool_name):
            return self._allow(tool_name, params, capability, reason="safe tool matched")

        rule_decision = self._evaluate_call_rules(tool_name, params, capability)
        if rule_decision is not None:
            return rule_decision

        if tool_name == "exec":
            decision = self._evaluate_command(tool_name, params, capability)
        elif tool_name == "proc_start":
            decision = self._evaluate_process_side_effect(tool_name, params, capability)
        elif tool_name in self._PATH_TOOLS or tool_name == "delete":
            decision = self._evaluate_paths(tool_name, params, capability)
        elif tool_name in {"http_request", "web_fetch"}:
            decision = self._evaluate_url(tool_name, params, capability)
        elif tool_name in {"proc_send", "proc_stop"}:
            decision = self._require(
                f"process.{tool_name.removeprefix('proc_')}",
                f"Mutate a local process with {tool_name}",
                "long-lived process state requires explicit user approval",
                tool_name,
                params,
            )
        elif tool_name in {"ssh_open", "ssh_send", "ssh_close"}:
            identity = params.get("identity")
            if tool_name == "ssh_open" and isinstance(identity, str) and identity.strip():
                path_decision = self._check_path(
                    tool_name,
                    identity,
                    "ssh.identity",
                    params,
                )
                if path_decision is not None:
                    return path_decision
            decision = self._require(
                f"ssh.{tool_name.removeprefix('ssh_')}",
                f"Allow remote SSH operation {tool_name}",
                "remote access or session mutation requires explicit user approval",
                tool_name,
                params,
            )
        elif tool_name in {"ssh_upload", "ssh_download"}:
            for path_field, path_capability in (
                ("local_path", f"ssh.{tool_name.removeprefix('ssh_')}"),
                ("identity", "ssh.identity"),
            ):
                local_path = params.get(path_field)
                if not isinstance(local_path, str) or not local_path.strip():
                    continue
                path_decision = self._check_path(
                    tool_name,
                    local_path,
                    path_capability,
                    params,
                )
                if path_decision is not None:
                    return path_decision
            decision = self._require(
                f"ssh.{tool_name.removeprefix('ssh_')}",
                f"Allow SSH file transfer {tool_name}",
                "remote file transfer requires explicit user approval",
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
        else:
            decision = self._require(
                capability,
                f"Execute unclassified capability {tool_name}",
                "unclassified tool side effects require explicit user approval",
                tool_name,
                params,
            )

        return decision

    def _evaluate_call_rules(
        self,
        tool_name: str,
        params: dict[str, Any],
        capability: str,
    ) -> SecurityDecision | None:
        target = _tool_match_text(tool_name, params)
        if any(rule.search(target) for rule in self._deny_rules):
            return self._decision(
                SecurityDecisionKind.HARD_DENY,
                capability,
                "blocked by safty_deny_regex",
                "a configured deny pattern matched the tool call",
                tool_name,
                params,
            )
        if any(rule.search(target) for rule in self._allow_rules):
            return self._allow(
                tool_name,
                params,
                capability,
                reason="matched safty_allow_regex",
            )
        return None

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
        analysis = analyze_command(command, shell=params.get("shell"))
        policy = self._config.tools.policy
        if policy.restrict_path_to_workspace:
            for raw_path in extract_absolute_paths(command):
                decision = self._check_path(tool_name, raw_path, capability, params)
                if decision is not None:
                    return decision
            cwd = params.get("working_dir") or params.get("cwd")
            if isinstance(cwd, str):
                decision = self._check_path(tool_name, cwd, capability, params)
                if decision is not None:
                    return decision
        if policy.restrict_url_outside_local:
            for url in extract_command_urls(command):
                if is_local_url(url):
                    return self._decision(
                        SecurityDecisionKind.HARD_DENY,
                        "network.private_target",
                        f"Blocked local URL {_safe_text(url)}",
                        "agent work scope is restricted to public network targets",
                        tool_name,
                        params,
                    )

        decision_kind = {
            CommandRisk.SAFE: SecurityDecisionKind.ALLOW,
            CommandRisk.REQUIRE_INTERVENTION: SecurityDecisionKind.REQUIRE_INTERVENTION,
            CommandRisk.HARD_DENY: SecurityDecisionKind.HARD_DENY,
        }[analysis.risk]
        summary = f"Execute {tool_name}: {_safe_text(command)}"
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
        if tool_name == "delete":
            return self._require(
                f"filesystem.{tool_name}",
                "Delete filesystem targets",
                "deletion is destructive even inside the workspace",
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
    ) -> SecurityDecision | None:
        if not self._config.tools.policy.restrict_path_to_workspace:
            return None
        try:
            resolved = resolve_path(raw_path, workspace=self._workspace)
        except (OSError, RuntimeError, ValueError):
            return None
        if is_under(resolved, self._workspace):
            return None
        return self._decision(
            SecurityDecisionKind.HARD_DENY,
            capability,
            f"Blocked {tool_name} access to {_safe_text(raw_path)}",
            "agent work scope is restricted to the configured workspace",
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
        method = str(params.get("method", "GET")).upper()
        if self._config.tools.policy.restrict_url_outside_local and is_local_url(url):
            return self._decision(
                SecurityDecisionKind.HARD_DENY,
                "network.private_target",
                f"Blocked local URL {_safe_text(url)}",
                "agent work scope is restricted to public network targets",
                tool_name,
                params,
            )
        if tool_name == "http_request" and method not in {"GET", "HEAD", "OPTIONS"}:
            return self._require(
                "network.http_write",
                f"Send an HTTP {method} request to {_safe_text(url)}",
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
        metadata = {
            "tool_name": tool_name,
            "tool_params": _safe_params(params),
            "tool_display": _tool_display(tool_name, params),
            **metadata,
        }
        return SecurityDecision(
            kind=kind,
            capability=capability,
            summary=summary,
            reason=reason,
            call_fingerprint=_fingerprint(tool_name, params, capability),
            metadata=metadata,
        )
