"""SSH tool: long-lived interactive SSH sessions with a rendered terminal screen.

Wraps the system ``ssh`` binary as a backgrounded subprocess and bridges its
stdio. A background reader feeds raw bytes into a fixed-size pyte virtual screen,
so the agent reads a bounded 80x24 plain-text snapshot even for full-screen TUI
apps (vim, htop, tmux) instead of an unbounded stream of ANSI redraw escapes.

The session keeps the process alive across tool calls (unlike the one-shot
``exec`` tool), so the agent can connect once and drive many dependent commands.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import deque
from typing import Any

from loguru import logger

from nanocat.agent.tools.base import Tool
from nanocat.agent.tools.shell import ExecTool, _decode_output

# Default remote PTY geometry. `ssh -tt` over pipes (no local tty) allocates an
# 80x24 PTY; a session may request a larger screen, synced to the remote via stty.
_DEFAULT_COLS, _DEFAULT_ROWS = 80, 24
_MIN_COLS, _MAX_COLS = 20, 300
_MIN_ROWS, _MAX_ROWS = 5, 100

_OPEN_SETTLE = 1.2  # seconds to wait after connect before returning initial screen
_SEND_SETTLE = 0.4  # seconds to wait after input before returning updated screen

# Control chars + CSI/escape sequences, stripped from the line-oriented scrollback
# view (the pyte screen view keeps them rendered).
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][AB0-2]|\x1b[=>]|[\x00-\x08\x0b\x0c\x0e-\x1f]"
)

# Named keys / chords -> byte sequences sent to the remote PTY.
_KEY_MAP: dict[str, bytes] = {
    "enter": b"\r",
    "tab": b"\t",
    "escape": b"\x1b",
    "space": b" ",
    "backspace": b"\x7f",
    "delete": b"\x1b[3~",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "ctrl-a": b"\x01",
    "ctrl-c": b"\x03",
    "ctrl-d": b"\x04",
    "ctrl-e": b"\x05",
    "ctrl-l": b"\x0c",
    "ctrl-r": b"\x12",
    "ctrl-u": b"\x15",
    "ctrl-w": b"\x17",
    "ctrl-z": b"\x1a",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f12": b"\x1b[24~",
}
_KEY_NAMES = sorted(_KEY_MAP)


class SSHSession:
    """One live ssh subprocess + its rendered virtual screen and scrollback."""

    def __init__(
        self,
        session_id: str,
        label: str,
        proc: asyncio.subprocess.Process,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
    ):
        import pyte  # lazy: only needed once a session is actually opened

        self.id = session_id
        self.label = label
        self.proc = proc
        self.cols = cols
        self.rows = rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._scrollback: deque[str] = deque(maxlen=2000)
        self._sb_partial = ""  # carry for an incomplete trailing line
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
                try:
                    self._stream.feed(data)
                except Exception:
                    logger.exception("pyte feed failed for ssh session {}", self.id)
                self._append_scrollback(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ssh reader loop error for session {}", self.id)
        # Natural EOF (the ssh process exited): reap it so exit_code is real, not
        # None. Cancellation (terminate) re-raises above and skips this.
        self.alive = False
        try:
            self.exit_code = await asyncio.wait_for(self.proc.wait(), timeout=2.0)
        except Exception:
            self.exit_code = self.proc.returncode

    def _append_scrollback(self, data: bytes) -> None:
        text = _ANSI_RE.sub("", _decode_output(data))
        text = self._sb_partial + text
        lines = text.split("\n")
        self._sb_partial = lines.pop()
        for ln in lines:
            self._scrollback.append(ln.rstrip())

    def render_screen(self) -> str:
        # pyte pads every row to full width; strip per-line trailing spaces and
        # the blank rows at the top/bottom (terminal padding) to save tokens.
        # Interior blank rows are kept so TUI layout stays faithful.
        lines = [line.rstrip() for line in self._screen.display]
        start, end = 0, len(lines)
        while start < end and not lines[start]:
            start += 1
        while end > start and not lines[end - 1]:
            end -= 1
        return "\n".join(lines[start:end])

    def render_scrollback(self, max_lines: int = 200) -> str:
        lines = list(self._scrollback)
        if self._sb_partial:
            lines.append(self._sb_partial)
        return "\n".join(lines[-max_lines:]).rstrip()

    async def send_bytes(self, data: bytes) -> None:
        if not self.alive or self.proc.stdin is None or self.proc.stdin.is_closing():
            raise RuntimeError(f"session {self.id} is not alive (connection closed)")
        try:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self.alive = False
            raise RuntimeError(f"connection lost while sending: {e}") from e
        self.last_activity = time.monotonic()

    async def terminate(self) -> None:
        self.alive = False
        try:
            if self.proc.stdin is not None and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except Exception:
            pass
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

    def idle_seconds(self) -> int:
        return int(time.monotonic() - self.last_activity)


class SSHManager:
    """Owns all live ssh sessions, keyed by short uuid."""

    def __init__(self) -> None:
        self._sessions: dict[str, SSHSession] = {}

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
        session = SSHSession(sid, host.strip(), proc, cols, rows)
        session.start_reader()
        self._sessions[sid] = session
        await asyncio.sleep(_OPEN_SETTLE)

        # Sync the remote PTY to the requested geometry (default 80x24 needs none).
        if (cols, rows) != (_DEFAULT_COLS, _DEFAULT_ROWS) and session.alive:
            try:
                await session.send_bytes(f"stty rows {rows} cols {cols} 2>/dev/null\n".encode())
                await asyncio.sleep(0.25)
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

    async def send(
        self,
        session_id: str,
        *,
        text: str | None = None,
        keys: list[str] | None = None,
        enter: bool = False,
        immediate_return: bool = True,
    ) -> str:
        session = self._sessions.get(session_id)
        if session is None:
            return f"Error: no ssh session {session_id!r} (use ssh_list)."

        payload = b""
        if text:
            payload += text.encode("utf-8", errors="replace")
        for key in keys or []:
            mapped = _KEY_MAP.get(key.lower().strip())
            if mapped is None:
                return f"Error: unknown key {key!r}. Valid keys: {', '.join(_KEY_NAMES)}"
            payload += mapped
        if enter:
            payload += b"\r"
        if not payload:
            return "Error: nothing to send (provide text and/or keys, or enter=true)."

        try:
            await session.send_bytes(payload)
        except RuntimeError as e:
            return (
                f"Error: {e}. The connection dropped; use ssh_read to see the last "
                f"output, then ssh_close to clear it."
            )

        if immediate_return:
            await asyncio.sleep(_SEND_SETTLE)
            return f"[sent to {session_id}] screen:\n{session.render_screen()}"
        return f"[sent to {session_id}] (call ssh_read to view output)"

    def read(self, session_id: str, mode: str = "screen") -> str:
        session = self._sessions.get(session_id)
        if session is None:
            return f"Error: no ssh session {session_id!r} (use ssh_list)."
        body = session.render_scrollback() if mode == "scrollback" else session.render_screen()
        status = "" if session.alive else f" [ended, exit={session.exit_code}]"
        return f"[{session_id} {mode}]{status}\n{body}"

    async def close(self, session_id: str) -> str:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return f"Error: no ssh session {session_id!r}."
        await session.terminate()
        return f"Closed ssh session {session_id}."

    def list(self) -> str:
        if not self._sessions:
            return "No open ssh sessions."
        rows = []
        for sid, s in self._sessions.items():
            state = "alive" if s.alive else f"ended(exit={s.exit_code})"
            rows.append(f"{sid} · {s.label} · {state} · idle {s.idle_seconds()}s")
        return "\n".join(rows)

    def context_block(self) -> str | None:
        """One compact line per session for the per-turn CONTEXT block. Ended-but-
        unclosed sessions are shown too, so a dropped connection surfaces to the agent."""
        if not self._sessions:
            return None
        lines = []
        for sid, s in self._sessions.items():
            if s.alive:
                lines.append(f"{sid} · {s.label} · idle {s.idle_seconds()}s")
            else:
                lines.append(f"{sid} · {s.label} · ENDED(exit={s.exit_code}) — ssh_close to clear")
        return "\n".join(lines)

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for s in sessions:
            try:
                await s.terminate()
            except Exception:
                logger.exception("error closing ssh session {}", s.id)


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
                    "description": "Terminal width (default 80); raise for wide TUIs",
                    "minimum": _MIN_COLS,
                    "maximum": _MAX_COLS,
                },
                "rows": {
                    "type": "integer",
                    "description": "Terminal height (default 24)",
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
                    "items": {"type": "string", "enum": _KEY_NAMES},
                    "description": "Named keys/chords sent in order (TUI navigation/control)",
                },
                "enter": {"type": "boolean", "description": "Append Enter (default false)"},
                "immediate_return": {
                    "type": "boolean",
                    "description": "If true (default) wait briefly and return the updated "
                    "screen; if false return at once and use ssh_read",
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
        **kwargs: Any,
    ) -> str:
        return await self._mgr.send(
            session_id,
            text=text,
            keys=keys,
            enter=enter,
            immediate_return=immediate_return,
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
