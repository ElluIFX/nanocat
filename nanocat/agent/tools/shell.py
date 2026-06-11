"""Shell execution tool."""

from __future__ import annotations

import asyncio
import json
import locale
import os
import platform
import shutil
import signal
import sys
import time
from typing import Any

from loguru import logger

from nanocat.agent.tools.base import Tool


def _decode_output(data: bytes) -> str:
    """Decode subprocess output to str.  UTF-8 first; fall back to system encoding."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        system_enc = locale.getpreferredencoding(False) or "utf-8"
        return data.decode(system_enc, errors="replace")


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        workspace_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        path_append: str = "",
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.workspace_dir = workspace_dir or working_dir
        self.path_append = path_append

        # Wire extra security patterns into the central command guard.
        from nanocat.security.command import set_allow_always, set_extra_deny

        if deny_patterns:
            set_extra_deny(deny_patterns)
        if allow_patterns:
            set_allow_always(allow_patterns)

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600

    @property
    def description(self) -> str:
        system = platform.system()
        working_path = self.working_dir or os.getcwd()
        runtime = (
            f"{'macOS' if system == 'Darwin' else system} "
            f"{platform.machine()}, Python {platform.python_version()}"
        )

        if system == "Windows":
            gnu_available = all(shutil.which(x) for x in ["grep", "sed", "awk"])
            gnu_note = (
                "GNU tools (grep/sed/awk) available; fall back to Windows-native on failure."
                if gnu_available
                else "GNU tools not available; use Windows-native commands or file tools."
            )
            platform_policy = f"Windows: enable UTF-8 if output is garbled. {gnu_note}"
        else:
            platform_policy = "POSIX: prefer UTF-8 and standard shell tools."

        return (
            f"Execute a shell command and return its output.\n"
            f"Runtime: {runtime} | CWD: {working_path}\n"
            f"{platform_policy}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the command",
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
        from nanocat.security import safety_bypass
        from nanocat.security.command import guard_command

        cwd = working_dir or self.working_dir or os.getcwd()

        if not safety_bypass.get():
            error = guard_command(
                command,
                cwd=cwd,
                workspace=self.workspace_dir or cwd,
                on_blocked=self._on_blocked,
            )
            if error:
                return error

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")

        try:
            t_start = time.monotonic()
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                self._kill_tree(process)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {effective_timeout} seconds"

            stdout_text = _decode_output(stdout) if stdout else ""
            stderr_text = _decode_output(stderr) if stderr else ""
            elapsed = round(time.monotonic() - t_start, 3)

            return json.dumps(
                {
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "returncode": process.returncode,
                    "elapsed_s": elapsed,
                },
                ensure_ascii=False,
            )

        except Exception as e:
            return json.dumps(
                {"stdout": "", "stderr": str(e), "returncode": -1}, ensure_ascii=False
            )

    @staticmethod
    def _kill_tree(process: asyncio.subprocess.Process) -> None:
        """Kill *process* and all its children (cross-platform best-effort)."""
        try:
            if sys.platform == "win32":
                subprocess = __import__("subprocess")
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=10,
                )
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    process.kill()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _on_blocked(
        self, command: str, category: str, shell_type: str, reason: str
    ) -> bool:
        """Hook called when a command is about to be blocked.

        Return True to temporarily allow the command.
        """
        logger.warning(
            "[SHELL BLOCKED] category={} shell={} reason={} | command: {!r}",
            category,
            shell_type,
            reason,
            command,
        )
        return False
