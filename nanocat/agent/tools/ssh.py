"""SSH tool: long-lived interactive SSH sessions with a rendered terminal screen.

Wraps the system ``ssh`` binary as a backgrounded subprocess and bridges its
stdio. `ssh -tt` gives the remote a PTY (over pipes), so the shared
`TerminalSession` (pyte virtual screen + scrollback + key sending) drives even
full-screen TUI apps (vim/htop/tmux). The session stays alive across tool calls,
unlike the one-shot ``exec`` tool.
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


class SSHManager(TerminalManager):
    """Owns all live ssh sessions; spawns them via the system ssh binary."""

    kind = "ssh"
    clear_cmd = "ssh_close"

    async def open(
        self,
        host: str,
        *,
        port: int | None = None,
        identity: str | None = None,
        extra_args: list[str] | None = None,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
    ) -> str:
        if not host or not host.strip():
            return "Error: host is required."
        cols = max(_MIN_COLS, min(_MAX_COLS, cols))
        rows = max(_MIN_ROWS, min(_MAX_ROWS, rows))
        argv = [
            "ssh",
            "-tt",  # force remote PTY even without a local tty
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "BatchMode=no",
            "-o",
            "ConnectTimeout=10",
        ]
        if port:
            argv += ["-p", str(port)]
        if identity:
            argv += ["-i", identity]
        if extra_args:
            argv += list(extra_args)
        argv.append(host.strip())

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return "Error: `ssh` binary not found on PATH."
        except Exception as e:
            return f"Error: failed to launch ssh: {e}"

        sid = uuid.uuid4().hex[:8]
        session = TerminalSession(sid, host.strip(), proc, cols, rows)
        session.start_reader()
        self._sessions[sid] = session
        await asyncio.sleep(_OPEN_SETTLE)
        await session.drain_until_idle(_OPEN_QUIET, _OPEN_STREAM_CEIL)

        # Sync the remote PTY to the requested geometry (default 80x24 needs none).
        if (cols, rows) != (_DEFAULT_COLS, _DEFAULT_ROWS) and session.alive:
            try:
                await session.send_bytes(f"stty rows {rows} cols {cols} 2>/dev/null\n".encode())
                await session.drain_until_idle(_OPEN_QUIET, 2.0)
            except RuntimeError:
                pass

        if not session.alive:
            detail = session.render_scrollback() or session.render_screen()
            self._sessions.pop(sid, None)
            await session.terminate()
            return (
                f"Error: connection to {host} failed (exit={session.exit_code}). "
                f"Output:\n{detail or '(no output)'}"
            )

        screen = session.render_screen()
        if not screen and not session.render_scrollback():
            return (
                f"Opened ssh session {sid} ({host}, {cols}x{rows}) — connecting, no output "
                f"yet. Call ssh_read shortly; if it stays empty the host may be unreachable."
            )
        return f"Opened ssh session {sid} ({host}, {cols}x{rows}). Initial screen:\n{screen}"


class SSHOpenTool(Tool):
    def __init__(self, manager: SSHManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "ssh_open"

    @property
    def description(self) -> str:
        return (
            "Open a persistent SSH session (live PTY) and return its id for follow-up "
            "ssh_send/ssh_read. Prefer this over running `ssh` through exec — exec "
            "is one-shot and cannot hold an interactive connection. Drives shell commands "
            "and full-screen TUI apps (vim/htop/tmux): launch one, then ssh_read(mode=screen) "
            "for the rendered screen and ssh_send keys to navigate. Uses ~/.ssh/config and "
            "key/agent auth; a password/passphrase prompt shows on screen — answer with ssh_send."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "ssh-config alias or user@host"},
                "port": {"type": "integer", "description": "Port (-p), if non-default"},
                "identity": {"type": "string", "description": "Private key path (-i)"},
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra raw ssh args, e.g. ['-J', 'jumphost']",
                },
                "cols": {
                    "type": "integer",
                    "default": _DEFAULT_COLS,
                    "description": "Terminal width; raise for wide TUIs",
                    "minimum": _MIN_COLS,
                    "maximum": _MAX_COLS,
                },
                "rows": {
                    "type": "integer",
                    "default": _DEFAULT_ROWS,
                    "description": "Terminal height",
                    "minimum": _MIN_ROWS,
                    "maximum": _MAX_ROWS,
                },
            },
            "required": ["host"],
        }

    async def execute(
        self,
        host: str,
        port: int | None = None,
        identity: str | None = None,
        extra_args: list[str] | None = None,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
        **kwargs: Any,
    ) -> str:
        return await self._mgr.open(
            host,
            port=port,
            identity=identity,
            extra_args=extra_args,
            cols=cols,
            rows=rows,
        )


class SSHSendTool(Tool):
    def __init__(self, manager: SSHManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "ssh_send"

    @property
    def description(self) -> str:
        return "Send input to an SSH session: literal text and/or named keys; enter submits."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "text": {"type": "string", "description": "Literal text to type"},
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keys/chords sent in order. Named keys (enter, tab, esc, "
                    "up/down/left/right, home, end, pageup, pagedown, delete, f1-f12), single "
                    "characters, or modifier chords with ctrl/alt/shift, e.g. 'ctrl-b' "
                    "(tmux prefix), 'ctrl-c', 'alt-x', 'ctrl-alt-del'.",
                },
                "enter": {"type": "boolean", "default": False, "description": "Append Enter"},
                "immediate_return": {
                    "type": "boolean",
                    "default": True,
                    "description": "Wait until output settles and return the updated screen; "
                    "if false return at once and use ssh_read",
                },
                "wait": {
                    "type": "number",
                    "description": "Seconds to wait for output before returning; raise for slow "
                    "commands like builds or installs",
                },
            },
            "required": ["session_id"],
        }

    async def execute(
        self,
        session_id: str,
        text: str | None = None,
        keys: list[str] | None = None,
        enter: bool = False,
        immediate_return: bool = True,
        wait: float | None = None,
        **kwargs: Any,
    ) -> str:
        return await self._mgr.send(
            session_id,
            text=text,
            keys=keys,
            enter=enter,
            immediate_return=immediate_return,
            wait=wait,
        )


class SSHReadTool(Tool):
    def __init__(self, manager: SSHManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "ssh_read"

    @property
    def description(self) -> str:
        return "Read an SSH session's output."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["screen", "scrollback"],
                    "default": "screen",
                    "description": "screen = current terminal screen render (for TUIs); "
                    "scrollback = recent line history (ANSI stripped)",
                },
            },
            "required": ["session_id"],
        }

    async def execute(self, session_id: str, mode: str = "screen", **kwargs: Any) -> str:
        return self._mgr.read(session_id, mode=mode)


class SSHCloseTool(Tool):
    def __init__(self, manager: SSHManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "ssh_close"

    @property
    def description(self) -> str:
        return "Close an SSH session and terminate its process."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        }

    async def execute(self, session_id: str, **kwargs: Any) -> str:
        return await self._mgr.close(session_id)


class SSHListTool(Tool):
    def __init__(self, manager: SSHManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "ssh_list"

    @property
    def description(self) -> str:
        return "List open SSH sessions (id, host, state, idle)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return self._mgr.list()
