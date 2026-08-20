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

from nanocat.agent.tools.base import Tool, exc_message


def _decode_output(data: bytes) -> str:
    """Decode subprocess output to str.  UTF-8 first; fall back to system encoding."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        system_enc = locale.getpreferredencoding(False) or "utf-8"
        return data.decode(system_enc, errors="replace")


def build_command_env(
    extra_env: dict[str, str] | None = None, path_append: list[str] | None = None
) -> dict[str, str]:
    """Build the child environment shared by the exec and proc tools."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    if path_append:
        env["PATH"] = env.get("PATH", "") + os.pathsep + os.pathsep.join(path_append)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


class ExecTool(Tool):
    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        path_append: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.path_append = path_append or []
        self.extra_env = env or {}

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
                    "default": 60,
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
                "shell": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "subprocess shell=. True: via system shell "
                        "(pipes/redirects/builtins). False: direct exec of argv-split command."
                    ),
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        shell: bool = True,
        **kwargs: Any,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        env = build_command_env(self.extra_env, self.path_append)

        try:
            t_start = time.monotonic()
            if shell:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=sys.platform != "win32",
                )
            else:
                import shlex

                argv = shlex.split(command)
                if not argv:
                    return json.dumps(
                        {"ok": False, "stdout": "", "stderr": "empty command", "returncode": -1},
                        ensure_ascii=False,
                    )
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=sys.platform != "win32",
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
                return json.dumps(
                    {
                        "ok": False,
                        "stdout": "",
                        "stderr": f"Command timed out after {effective_timeout} seconds",
                        "returncode": -1,
                    },
                    ensure_ascii=False,
                )

            stdout_text = _decode_output(stdout) if stdout else ""
            stderr_text = _decode_output(stderr) if stderr else ""
            elapsed = round(time.monotonic() - t_start, 3)

            return json.dumps(
                {
                    "ok": process.returncode == 0,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "returncode": process.returncode,
                    "elapsed_s": elapsed,
                },
                ensure_ascii=False,
            )

        except Exception as e:
            return json.dumps(
                {"ok": False, "stdout": "", "stderr": exc_message(e), "returncode": -1},
                ensure_ascii=False,
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
                    child_pgid = os.getpgid(process.pid)
                    if child_pgid != os.getpgid(0):
                        os.killpg(child_pgid, signal.SIGKILL)
                    else:
                        process.kill()
                except (ProcessLookupError, OSError):
                    process.kill()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
