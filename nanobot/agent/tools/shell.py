"""Shell execution tool."""

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool

logger = logging.getLogger(__name__)

_UNIX = "unix"
_CMD = "cmd"
_POWERSHELL = "powershell"
_ANY = "any"
_SYSTEM = "system"
_FILE = "file"

# (pattern, shell_type, danger_category)
# Patterns are matched against the lowercased command string.
_DEFAULT_DENY_RULES: list[tuple[str, str, str]] = [
    # --- FILE danger: Unix shell ---
    (r"\brm\b.*-[a-zA-Z]*[rf]", _UNIX, _FILE),
    (r"\bdd\b.*\bof=", _UNIX, _FILE),
    (r">\s*/dev/sd[a-z]", _UNIX, _FILE),
    # --- FILE danger: CMD ---
    (r"\bdel\b.*(?:/[fq])", _CMD, _FILE),
    (r"\brmdir\b.*(?:/s)", _CMD, _FILE),
    # --- FILE danger: PowerShell ---
    (r"\bremove-item\b.*-(?:recurse|force|r)\b", _POWERSHELL, _FILE),
    (r"\bri\b.*-(?:recurse|force|r)\b", _POWERSHELL, _FILE),
    # --- SYSTEM danger: Unix shell ---
    (r":\(\)\s*\{.*?\};\s*:", _UNIX, _SYSTEM),
    (r"\b(?:mkfs|fdisk)\b", _UNIX, _SYSTEM),
    # --- SYSTEM danger: CMD ---
    (r"(?:^|[;&|]\s*)format\s+[a-zA-Z]:", _CMD, _SYSTEM),
    # --- SYSTEM danger: Any ---
    (r"\bdiskpart\b", _ANY, _SYSTEM),
    (r"\b(?:shutdown|reboot|poweroff|halt)\b", _ANY, _SYSTEM),
]


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        workspace_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.workspace_dir = workspace_dir or working_dir
        self._deny_rules: list[tuple[str, str, str]] = list(_DEFAULT_DENY_RULES)
        self._extra_deny_patterns: list[str] = deny_patterns or []
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600)."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                encoding="utf-8",
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {effective_timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Head + tail truncation to preserve both start and end of output
            max_len = self._MAX_OUTPUT
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

    def _on_blocked(self, command: str, category: str, shell_type: str, reason: str) -> bool:
        """
        Hook called when a command is about to be blocked.
        Return True to temporarily allow the command.
        Override or replace this method to implement custom allow logic.
        """
        logger.warning(
            "[SHELL BLOCKED] category=%s shell=%s reason=%s | command: %r",
            category,
            shell_type,
            reason,
            command,
        )
        return False

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()
        workspace = Path(self.workspace_dir or cwd).resolve()

        for pattern, shell_type, category in self._deny_rules:
            if not re.search(pattern, lower, re.DOTALL):
                continue

            allowed = False
            reason = f"{'file' if category == _FILE else 'system'}-dangerous command [{shell_type}]"

            if category == _FILE:
                allowed = self._file_danger_in_workspace(cmd, cwd, workspace)

            if not allowed and self._on_blocked(cmd, category, shell_type, reason):
                allowed = True

            if not allowed:
                return f"Error: Command blocked by safety guard ({reason})"

        for pattern in self._extra_deny_patterns:
            if re.search(pattern, lower):
                reason = "dangerous pattern [custom]"
                if not self._on_blocked(cmd, _SYSTEM, _ANY, reason):
                    return f"Error: Command blocked by safety guard ({reason})"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        from nanobot.security.network import contains_internal_url

        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()
            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    def _file_danger_in_workspace(self, command: str, cwd: str, workspace: Path) -> bool:
        """
        Return True if every extracted path argument in a file-dangerous command
        resolves to a path within the workspace directory.
        Returns False if any path is outside the workspace or resolution fails.
        """
        paths = self._extract_path_args(command)
        if not paths:
            return False

        cwd_path = Path(cwd).resolve()
        for raw in paths:
            try:
                expanded = os.path.expandvars(raw.strip("\"'"))
                p = Path(expanded).expanduser()
                if not p.is_absolute():
                    p = cwd_path / p
                p = p.resolve()
            except Exception:
                return False

            if p != workspace and workspace not in p.parents:
                return False

        return True

    @staticmethod
    def _extract_path_args(command: str) -> list[str]:
        """
        Extract non-flag token arguments from a shell command as potential path targets.
        Skips the command verb and option flags (-x, --flag, /F).
        For key=value tokens (e.g. dd's of=...), extracts the value part.
        """
        tokens = re.split(r"\s+", command.strip())
        result = []
        for i, tok in enumerate(tokens):
            if i == 0:
                continue
            clean = tok.strip("\"'")
            if not clean:
                continue
            if re.match(r"^-{1,2}[a-zA-Z]", clean) or re.match(r"^/[a-zA-Z]{1,2}$", clean):
                continue
            if "=" in clean:
                _, _, val = clean.partition("=")
                if val:
                    result.append(val)
            else:
                result.append(clean)
        return result

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command)
        return win_paths + posix_paths + home_paths
