"""Shared terminal-session core for the ssh and proc tools.

A `TerminalSession` wraps an asyncio subprocess whose stdout is fed into a pyte
virtual screen (+ a line-oriented scrollback) and whose stdin accepts text and
named-key chords. `TerminalManager` owns a set of sessions and provides the
generic send/read/close/list/context_block/close_all plumbing; subclasses only
implement how a session is spawned (`SSHManager.open`, `ProcManager.start`) and
two small description hooks.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque

from loguru import logger

from nanocat.agent.tools.shell import ExecTool, _decode_output

# Default virtual-screen geometry and clamps.
_DEFAULT_COLS, _DEFAULT_ROWS = 80, 24
_MIN_COLS, _MAX_COLS = 20, 300
_MIN_ROWS, _MAX_ROWS = 5, 100

# Timing: after sending input we settle for a base delay (overridable via `wait`),
# then keep draining only while output is still actively streaming.
_SEND_SETTLE = 0.5
_SEND_QUIET = 0.25
_SEND_STREAM_CEIL = 8.0
_OPEN_SETTLE = 1.2
_OPEN_QUIET = 0.3
_OPEN_STREAM_CEIL = 8.0
_WAIT_CEILING = 120.0

# Control chars + CSI/escape sequences, stripped from the line-oriented scrollback.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][AB0-2]|\x1b[=>]|[\x00-\x08\x0b\x0c\x0e-\x1f]"
)

# Special (non-printable) named keys -> byte sequences. Modifier chords such as
# "ctrl-b" or "alt-x" are computed by _key_to_bytes, so they need not be listed.
_NAMED_KEYS: dict[str, bytes] = {
    "enter": b"\r",
    "return": b"\r",
    "tab": b"\t",
    "escape": b"\x1b",
    "esc": b"\x1b",
    "space": b" ",
    "backspace": b"\x7f",
    "delete": b"\x1b[3~",
    "del": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
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
_NAMED_KEY_LIST = sorted(_NAMED_KEYS)

# Modifier aliases -> canonical flag (c=ctrl, a=alt, s=shift).
_MODIFIERS = {
    "ctrl": "c",
    "control": "c",
    "ctl": "c",
    "c": "c",
    "alt": "a",
    "meta": "a",
    "option": "a",
    "opt": "a",
    "m": "a",
    "shift": "s",
    "s": "s",
}
# xterm CSI encodings for modified navigation keys.
_ARROW_FINAL = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
_TILDE_PARAM = {"insert": "2", "delete": "3", "del": "3", "pageup": "5", "pagedown": "6"}


def _key_to_bytes(name: str) -> bytes | None:
    """Resolve a key/chord name to the bytes a terminal sends, or None if invalid.

    Accepts named keys (enter, up, f5, ...), single characters, and arbitrary
    modifier chords joined by '-' or '+': 'ctrl-b', 'alt-x', 'ctrl-alt-del',
    'shift-tab'. Modifiers: ctrl/control, alt/meta/option, shift.
    """
    raw = name.strip()
    if not raw:
        return None
    *mod_parts, base = re.split(r"[-+]", raw)
    flags = set()
    for m in mod_parts:
        flag = _MODIFIERS.get(m.lower())
        if flag is None:
            return None
        flags.add(flag)
    if not base:
        return None
    ctrl, alt, shift = "c" in flags, "a" in flags, "s" in flags
    base_l = base.lower()

    if base_l in _NAMED_KEYS:
        if not flags:
            return _NAMED_KEYS[base_l]
        if base_l == "tab" and shift and not ctrl and not alt:
            return b"\x1b[Z"  # back-tab
        mod = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
        if base_l in _ARROW_FINAL:
            return f"\x1b[1;{mod}{_ARROW_FINAL[base_l]}".encode()
        if base_l in _TILDE_PARAM:
            return f"\x1b[{_TILDE_PARAM[base_l]};{mod}~".encode()
        # Modifier not expressible for this key (e.g. ctrl-enter); best effort.
        return (b"\x1b" if alt else b"") + _NAMED_KEYS[base_l]

    if len(base) == 1:
        ch = base.upper() if (shift and base.isalpha()) else base
        if ctrl and ord(ch) < 128:
            data = bytes([ord(ch.upper()) & 0x1F])
        else:
            data = ch.encode("utf-8", "replace")
        return (b"\x1b" if alt else b"") + data

    return None


class TerminalSession:
    """One live subprocess + its rendered pyte screen and line scrollback."""

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
                    logger.exception("pyte feed failed for session {}", self.id)
                self._append_scrollback(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reader loop error for session {}", self.id)
        # Natural EOF (the process exited): reap it so exit_code is real, not None.
        # Cancellation (terminate) re-raises above and skips this.
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
        # pyte pads every row to full width; strip per-line trailing spaces and the
        # blank rows at the top/bottom (terminal padding) to save tokens. Interior
        # Blank rows are kept so full-screen terminal layouts stay faithful.
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
            raise RuntimeError(f"session {self.id} is not alive (process exited)")
        try:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self.alive = False
            raise RuntimeError(f"lost the process while sending: {e}") from e
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

    def uptime(self) -> int:
        return int(time.monotonic() - self.created_at)

    async def drain_until_idle(self, quiet: float, ceiling: float) -> None:
        """Keep waiting while output is still actively streaming (last activity
        within *quiet* seconds), capped at *ceiling*. Lets a streaming command
        finish before the screen is read, without penalising silent ones."""
        deadline = time.monotonic() + ceiling
        while (
            self.alive
            and time.monotonic() - self.last_activity < quiet
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)


class TerminalManager:
    """Owns a set of TerminalSessions, keyed by short uuid. Subclasses spawn the
    sessions (open/start) and override the description hooks."""

    kind = "session"  # used in user-facing messages, e.g. "ssh"/"proc"
    clear_cmd = "session_close"  # tool name suggested to clear an ended session
    # Byte that `enter` sends. A PTY (ssh) translates CR->LF, so CR is correct
    # there; over a raw local pipe (proc) there's no tty translation, so a line
    # program needs an actual newline.
    enter_byte = b"\r"

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}

    # --- subclass hooks --------------------------------------------------
    def _time_desc(self, s: TerminalSession) -> str:
        return f"idle {s.idle_seconds()}s"

    def _describe(self, sid: str, s: TerminalSession) -> str:
        return f"{sid} · {s.label[:50]} · {self._time_desc(s)}"

    def _describe_item(self, s: TerminalSession) -> dict:
        return {"label": s.label}

    # --- generic session operations -------------------------------------
    async def send(
        self,
        session_id: str,
        *,
        text: str | None = None,
        keys: list[str] | None = None,
        enter: bool = False,
        immediate_return: bool = True,
        wait: float | None = None,
    ) -> str:
        session = self._sessions.get(session_id)
        if session is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"no {self.kind} session {session_id!r}",
                    "hint": f"use {self.kind}_list",
                },
                ensure_ascii=False,
            )

        payload = b""
        if text:
            payload += text.encode("utf-8", errors="replace")
        for key in keys or []:
            mapped = _key_to_bytes(key)
            if mapped is None:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"unknown key {key!r}",
                        "hint": f"named keys: {', '.join(_NAMED_KEY_LIST)}; chords: ctrl-b, alt-x, ctrl-alt-del",
                    },
                    ensure_ascii=False,
                )
            payload += mapped
        if enter:
            payload += self.enter_byte
        if not payload:
            return json.dumps({"ok": False, "error": "nothing to send"}, ensure_ascii=False)

        try:
            await session.send_bytes(payload)
        except RuntimeError as e:
            return json.dumps(
                {
                    "ok": False,
                    "error": str(e),
                    "hint": f"use {self.kind}_read to see the last output, then {self.clear_cmd}",
                },
                ensure_ascii=False,
            )

        if immediate_return:
            base = _SEND_SETTLE if wait is None else max(0.0, min(wait, _WAIT_CEILING))
            await asyncio.sleep(base)
            await session.drain_until_idle(_SEND_QUIET, _SEND_STREAM_CEIL)
            return json.dumps(
                {
                    "ok": True,
                    "sent_to": session_id,
                    "screen": session.render_screen(),
                },
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, "sent_to": session_id}, ensure_ascii=False)

    def read(self, session_id: str, mode: str = "screen") -> str:
        session = self._sessions.get(session_id)
        if session is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"no {self.kind} session {session_id!r}",
                    "hint": f"use {self.kind}_list",
                },
                ensure_ascii=False,
            )
        body = session.render_scrollback() if mode == "scrollback" else session.render_screen()
        return json.dumps(
            {
                "ok": True,
                "session_id": session_id,
                "mode": mode,
                "alive": session.alive,
                "exit_code": session.exit_code,
                "content": body,
            },
            ensure_ascii=False,
        )

    async def close(self, session_id: str) -> str:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return json.dumps(
                {"ok": False, "error": f"no {self.kind} session {session_id!r}"},
                ensure_ascii=False,
            )
        await session.terminate()
        return json.dumps({"ok": True, "closed": session_id}, ensure_ascii=False)

    def list(self) -> str:
        items = []
        for sid, s in self._sessions.items():
            items.append(
                {
                    "id": sid,
                    "alive": s.alive,
                    "exit_code": s.exit_code,
                }
                | self._describe_item(s)
            )
        return json.dumps({"ok": True, "sessions": items}, ensure_ascii=False)

    def context_block(self) -> str | None:
        """One compact line per session for the per-turn CONTEXT block. Ended-but-
        unclosed sessions are shown too, so a drop surfaces to the agent."""
        if not self._sessions:
            return None
        lines = []
        for sid, s in self._sessions.items():
            if s.alive:
                lines.append(self._describe(sid, s))
            else:
                lines.append(
                    f"{sid} · {s.label[:50]} · ENDED(exit={s.exit_code}) — "
                    f"{self.clear_cmd} to clear"
                )
        return "\n".join(lines)

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for s in sessions:
            try:
                await s.terminate()
            except Exception:
                logger.exception("error closing {} session {}", self.kind, s.id)
