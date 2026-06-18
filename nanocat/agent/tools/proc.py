"""Background local process tool — the local sibling of the SSH session tool.

Unlike the one-shot ``exec`` tool, a process started here stays alive across tool
calls: send it keys/text with ``proc_send``, read its rendered screen / output
with ``proc_read``, and stop it with ``proc_stop``. Built on the shared
`TerminalSession` (pyte virtual screen + scrollback + key chords).

It runs the child over pipes (no PTY), which is fine for dev servers, REPLs,
tail -f, watchers and training runs. A local program sees a pipe, not a tty, so
full-screen TUIs (vim/htop) won't truly fullscreen — use the ssh tool for those.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from nanocat.agent.tools.base import Tool
from nanocat.agent.tools.terminal import (
    _DEFAULT_COLS,
    _DEFAULT_ROWS,
    _MAX_COLS,
    _MAX_ROWS,
    _MIN_COLS,
    _MIN_ROWS,
    _OPEN_QUIET,
    _OPEN_SETTLE,
    _OPEN_STREAM_CEIL,
    TerminalManager,
    TerminalSession,
)


class ProcManager(TerminalManager):
    """Owns all background local processes; spawns them via a shell."""

    kind = "proc"
    clear_cmd = "proc_stop"
    enter_byte = b"\n"  # local pipe has no tty to translate CR->LF

    async def start(
        self,
        command: str,
        cwd: str | None = None,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
    ) -> str:
        cols = max(_MIN_COLS, min(_MAX_COLS, cols))
        rows = max(_MIN_ROWS, min(_MAX_ROWS, rows))
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,  # PIPE so proc_send can write to it
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        except Exception as e:
            return f"Error: failed to start process: {e}"

        sid = uuid.uuid4().hex[:8]
        session = TerminalSession(sid, command, proc, cols, rows)
        session.start_reader()
        self._sessions[sid] = session
        await asyncio.sleep(_OPEN_SETTLE)
        await session.drain_until_idle(_OPEN_QUIET, _OPEN_STREAM_CEIL)

        if not session.alive:
            detail = session.render_scrollback() or session.render_screen()
            self._sessions.pop(sid, None)
            await session.terminate()
            return (
                f"Error: process exited immediately (exit={session.exit_code}). "
                f"Output:\n{detail or '(no output)'}"
            )

        screen = session.render_screen() or session.render_scrollback()
        return f"Started proc {sid} ({command[:60]}). Output:\n{screen or '(no output yet)'}"

    def _time_desc(self, s: TerminalSession) -> str:
        return f"up {s.uptime()}s"


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
            "Start a long-lived local process and return its id. Unlike exec (one-shot), "
            "it keeps running so you can send input with proc_send, read its rendered "
            "screen/output with proc_read, and stop it with proc_stop — for dev servers, "
            "REPLs, tail -f, watchers, training runs. (Runs over a pipe, not a real tty, "
            "so full-screen TUIs like vim won't fullscreen; use ssh for those.)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "cwd": {"type": "string", "description": "Working directory (default: workspace)"},
                "cols": {
                    "type": "integer",
                    "default": _DEFAULT_COLS,
                    "description": "Screen width",
                    "minimum": _MIN_COLS,
                    "maximum": _MAX_COLS,
                },
                "rows": {
                    "type": "integer",
                    "default": _DEFAULT_ROWS,
                    "description": "Screen height",
                    "minimum": _MIN_ROWS,
                    "maximum": _MAX_ROWS,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        cwd: str | None = None,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
        **kwargs: Any,
    ) -> str:
        from nanocat.security import safety_bypass
        from nanocat.security.command import guard_command

        run_cwd = cwd or self._working_dir
        if not safety_bypass.get():
            error = guard_command(command, cwd=run_cwd, workspace=self._working_dir)
            if error:
                return error
        return await self._mgr.start(command, run_cwd, cols, rows)


class ProcSendTool(Tool):
    def __init__(self, manager: ProcManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "proc_send"

    @property
    def description(self) -> str:
        return "Send input to a local process: literal text and/or named keys; enter submits."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proc_id": {"type": "string"},
                "text": {"type": "string", "description": "Literal text to type"},
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keys/chords sent in order. Named keys (enter, tab, esc, "
                    "up/down/left/right, home, end, pageup, pagedown, delete, f1-f12), single "
                    "characters, or modifier chords with ctrl/alt/shift, e.g. 'ctrl-c', "
                    "'ctrl-d', 'alt-x', 'ctrl-alt-del'.",
                },
                "enter": {"type": "boolean", "default": False, "description": "Append Enter"},
                "immediate_return": {
                    "type": "boolean",
                    "default": True,
                    "description": "Wait until output settles and return the updated screen; "
                    "if false return at once and use proc_read",
                },
                "wait": {
                    "type": "number",
                    "description": "Seconds to wait for output before returning; raise for "
                    "slow commands",
                },
            },
            "required": ["proc_id"],
        }

    async def execute(
        self,
        proc_id: str,
        text: str | None = None,
        keys: list[str] | None = None,
        enter: bool = False,
        immediate_return: bool = True,
        wait: float | None = None,
        **kwargs: Any,
    ) -> str:
        return await self._mgr.send(
            proc_id,
            text=text,
            keys=keys,
            enter=enter,
            immediate_return=immediate_return,
            wait=wait,
        )


class ProcReadTool(Tool):
    def __init__(self, manager: ProcManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "proc_read"

    @property
    def description(self) -> str:
        return "Read a local process's output."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proc_id": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["screen", "scrollback"],
                    "default": "screen",
                    "description": "screen = current rendered screen; "
                    "scrollback = recent line history (ANSI stripped, good for logs)",
                },
            },
            "required": ["proc_id"],
        }

    async def execute(self, proc_id: str, mode: str = "screen", **kwargs: Any) -> str:
        return self._mgr.read(proc_id, mode=mode)


class ProcStopTool(Tool):
    def __init__(self, manager: ProcManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "proc_stop"

    @property
    def description(self) -> str:
        return "Stop a local process and terminate it (and its children)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"proc_id": {"type": "string"}},
            "required": ["proc_id"],
        }

    async def execute(self, proc_id: str, **kwargs: Any) -> str:
        return await self._mgr.close(proc_id)


class ProcListTool(Tool):
    def __init__(self, manager: ProcManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "proc_list"

    @property
    def description(self) -> str:
        return "List background processes (id, command, state, uptime)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return self._mgr.list()
