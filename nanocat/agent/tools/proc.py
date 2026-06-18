"""Background local process tool.

Unlike the one-shot ``exec`` tool (which blocks and dies when the command
returns), this keeps a process alive across tool calls: start it, read its
streaming output later, stop it. For dev servers, ``tail -f``, watchers,
training runs — the local sibling of the SSH session tool, minus the PTY.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from typing import Any

from loguru import logger

from nanocat.agent.tools.base import Tool
from nanocat.agent.tools.shell import ExecTool, _decode_output

_START_SETTLE = 0.5  # seconds to wait after launch before returning initial output
_SCROLLBACK = 4000  # retained output lines per process


class Proc:
    """One live background process + its bounded line-oriented output buffer."""

    def __init__(self, proc_id: str, command: str, proc: asyncio.subprocess.Process):
        self.id = proc_id
        self.command = command
        self.proc = proc
        self._scrollback: deque[str] = deque(maxlen=_SCROLLBACK)
        self._partial = ""
        self.created_at = time.monotonic()
        self.last_activity = self.created_at
        self.alive = True
        self.exit_code: int | None = None
        self._reader: asyncio.Task | None = None

    def start_reader(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                data = await self.proc.stdout.read(4096)
                if not data:
                    break
                self.last_activity = time.monotonic()
                self._append(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("proc reader error for {}", self.id)
        self.alive = False
        try:
            self.exit_code = await asyncio.wait_for(self.proc.wait(), timeout=2.0)
        except Exception:
            self.exit_code = self.proc.returncode

    def _append(self, data: bytes) -> None:
        text = self._partial + _decode_output(data)
        lines = text.split("\n")
        self._partial = lines.pop()
        for ln in lines:
            self._scrollback.append(ln.rstrip("\r"))

    def render(self, max_lines: int = 200) -> str:
        lines = list(self._scrollback)
        if self._partial:
            lines.append(self._partial)
        return "\n".join(lines[-max_lines:]).rstrip()

    async def terminate(self) -> None:
        self.alive = False
        ExecTool._kill_tree(self.proc)
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5.0)
        except Exception:
            pass

    def uptime(self) -> int:
        return int(time.monotonic() - self.created_at)


class ProcManager:
    """Owns all background processes, keyed by short uuid."""

    def __init__(self) -> None:
        self._procs: dict[str, Proc] = {}

    async def start(self, command: str, cwd: str | None) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        except Exception as e:
            return f"Error: failed to start process: {e}"

        pid = uuid.uuid4().hex[:8]
        p = Proc(pid, command, proc)
        p.start_reader()
        self._procs[pid] = p
        await asyncio.sleep(_START_SETTLE)

        if not p.alive:
            out = p.render()
            self._procs.pop(pid, None)
            await p.terminate()
            return json.dumps(
                {
                    "started": False,
                    "message": "process exited immediately",
                    "exit_code": p.exit_code,
                    "output": out,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"started": True, "id": pid, "command": command, "output": p.render(50)},
            ensure_ascii=False,
        )

    def read(self, proc_id: str, max_lines: int = 200) -> str:
        p = self._procs.get(proc_id)
        if p is None:
            return f"Error: no process {proc_id!r} (use proc_list)."
        return json.dumps(
            {
                "id": proc_id,
                "alive": p.alive,
                "exit_code": p.exit_code,
                "output": p.render(max_lines),
            },
            ensure_ascii=False,
        )

    async def stop(self, proc_id: str) -> str:
        p = self._procs.pop(proc_id, None)
        if p is None:
            return f"Error: no process {proc_id!r}."
        await p.terminate()
        return json.dumps(
            {"stopped": True, "id": proc_id, "exit_code": p.exit_code}, ensure_ascii=False
        )

    def list(self) -> str:
        if not self._procs:
            return "No background processes."
        rows = [
            {
                "id": pid,
                "command": p.command[:80],
                "alive": p.alive,
                "exit_code": p.exit_code,
                "uptime_s": p.uptime(),
            }
            for pid, p in self._procs.items()
        ]
        return json.dumps(rows, ensure_ascii=False)

    def context_block(self) -> str | None:
        """One compact line per running process for the per-turn CONTEXT block."""
        live = [(pid, p) for pid, p in self._procs.items() if p.alive]
        if not live:
            return None
        return "\n".join(f"{pid} · {p.command[:50]} · up {p.uptime()}s" for pid, p in live)

    async def close_all(self) -> None:
        procs = list(self._procs.values())
        self._procs.clear()
        for p in procs:
            try:
                await p.terminate()
            except Exception:
                logger.exception("error stopping proc {}", p.id)


class ProcStartTool(Tool):
    def __init__(self, manager: ProcManager, working_dir: str):
        self._mgr = manager
        self._working_dir = working_dir

    @property
    def name(self) -> str:
        return "proc_start"

    @property
    def description(self) -> str:
        return (
            "Start a long-lived background process and return its id. Unlike exec "
            "(one-shot, blocks until the command exits), this keeps the process running "
            "so you can read its streaming output with proc_read and stop it with "
            "proc_stop — use for dev servers, tail -f, watchers, training runs."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run in the background",
                },
                "cwd": {"type": "string", "description": "Working directory (default: workspace)"},
            },
            "required": ["command"],
        }

    async def execute(self, command: str, cwd: str | None = None, **kwargs: Any) -> str:
        from nanocat.security import safety_bypass
        from nanocat.security.command import guard_command

        run_cwd = cwd or self._working_dir
        if not safety_bypass.get():
            error = guard_command(command, cwd=run_cwd, workspace=self._working_dir)
            if error:
                return error
        return await self._mgr.start(command, run_cwd)


class ProcReadTool(Tool):
    def __init__(self, manager: ProcManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "proc_read"

    @property
    def description(self) -> str:
        return "Read recent output from a background process."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proc_id": {"type": "string"},
                "max_lines": {
                    "type": "integer",
                    "description": "Tail this many lines (default 200)",
                },
            },
            "required": ["proc_id"],
        }

    async def execute(self, proc_id: str, max_lines: int = 200, **kwargs: Any) -> str:
        return self._mgr.read(proc_id, max_lines=max_lines)


class ProcStopTool(Tool):
    def __init__(self, manager: ProcManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "proc_stop"

    @property
    def description(self) -> str:
        return "Stop a background process and terminate it (and its children)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"proc_id": {"type": "string"}},
            "required": ["proc_id"],
        }

    async def execute(self, proc_id: str, **kwargs: Any) -> str:
        return await self._mgr.stop(proc_id)


class ProcListTool(Tool):
    def __init__(self, manager: ProcManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "proc_list"

    @property
    def description(self) -> str:
        return "List background processes (id, command, alive, uptime)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return self._mgr.list()
