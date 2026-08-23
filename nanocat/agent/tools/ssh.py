"""SSH tool: long-lived interactive SSH sessions with a rendered terminal screen.

Wraps the system ``ssh`` binary as a backgrounded subprocess and bridges its
stdio. `ssh -tt` gives the remote a PTY (over pipes), so the shared
`TerminalSession` (pyte virtual screen + scrollback + key sending) drives even
full-screen terminal apps (vim/htop/tmux). The session stays alive across tool calls,
unlike the one-shot ``exec`` tool.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from nanocat.agent.tools.base import Tool, exc_message, tool_err, tool_ok
from nanocat.agent.tools.shell import ExecTool
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

    def __init__(self, workspace: Path | None = None) -> None:
        super().__init__()
        self._workspace = workspace.resolve() if workspace else None

    def _resolve_local_path(self, path: str) -> Path:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute() and self._workspace is not None:
            resolved = self._workspace / resolved
        return resolved.resolve()

    def _resolve_identity(self, identity: str | None) -> str | None:
        if not identity:
            return None
        return str(self._resolve_local_path(identity))

    @staticmethod
    def _validate_host(host: str) -> str:
        normalized_host = host.strip()
        if not normalized_host:
            raise ValueError("host is required")
        if normalized_host.startswith("-") or any(
            char in normalized_host for char in ("\x00", "\r", "\n", " ", "\t")
        ):
            raise ValueError("host contains unsupported characters")
        return normalized_host

    @classmethod
    def _validate_remote(cls, host: str, remote_path: str) -> tuple[str, str] | str:
        try:
            normalized_host = cls._validate_host(host)
        except ValueError as exc:
            return str(exc)
        unsafe_path_chars = "\x00\r\n\t `\"';&|$><(){}[]*?!\\"
        if not remote_path or any(char in remote_path for char in unsafe_path_chars):
            return "remote_path contains unsupported shell-sensitive characters"
        return normalized_host, remote_path

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader | None,
        limit: int = 8192,
    ) -> bytes:
        if stream is None:
            return b""
        result = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = limit - len(result)
            if remaining > 0:
                result.extend(chunk[:remaining])
        return bytes(result)

    async def _run_scp(self, argv: list[str], timeout: float) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=sys.platform != "win32",
            )
        except FileNotFoundError as exc:
            raise RuntimeError("`scp` binary not found on PATH") from exc
        except Exception as exc:
            raise RuntimeError(f"failed to launch scp: {exc_message(exc)}") from exc

        stderr_task = asyncio.create_task(self._read_limited(proc.stderr))
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            await self._settle_scp(proc, stderr_task)
            raise
        except asyncio.CancelledError:
            await self._settle_scp(proc, stderr_task)
            raise
        stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
        return proc.returncode or 0, stderr

    @staticmethod
    async def _settle_scp(
        proc: asyncio.subprocess.Process,
        stderr_task: asyncio.Task[bytes],
    ) -> None:
        if proc.returncode is None:
            kill_task = asyncio.create_task(asyncio.to_thread(ExecTool._kill_tree, proc))
            while not kill_task.done():
                try:
                    await asyncio.shield(kill_task)
                except asyncio.CancelledError:
                    continue
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            pass
        if not stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=2.0)
            except TimeoutError:
                stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)

    def _scp_argv(
        self,
        *,
        port: int | None,
        identity: str | None,
    ) -> list[str]:
        argv = [
            "scp",
            "-q",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        if port is not None:
            argv += ["-P", str(port)]
        resolved_identity = self._resolve_identity(identity)
        if resolved_identity:
            argv += ["-i", resolved_identity]
        return argv

    async def upload_file(
        self,
        local_path: str,
        host: str,
        remote_path: str,
        *,
        port: int | None = None,
        identity: str | None = None,
        timeout: float = 120.0,
    ) -> str:
        remote = self._validate_remote(host, remote_path)
        if isinstance(remote, str):
            return tool_err(remote)
        normalized_host, normalized_remote_path = remote
        source = self._resolve_local_path(local_path)
        if not source.exists():
            return tool_err(f"File not found: {local_path}")
        if not source.is_file():
            return tool_err(f"Not a file: {local_path}")
        source_size = source.stat().st_size

        argv = self._scp_argv(port=port, identity=identity)
        argv.extend([str(source), f"{normalized_host}:{normalized_remote_path}"])
        try:
            returncode, detail = await self._run_scp(argv, timeout)
        except TimeoutError:
            return tool_err(
                "SSH upload timed out",
                hint="verify the remote host and retry with a larger timeout",
                partial_remote_target=True,
            )
        except RuntimeError as exc:
            return tool_err(exc_message(exc), partial_remote_target=False)
        if returncode != 0:
            return tool_err(
                "SSH upload failed",
                hint="verify SSH authentication and the remote destination",
                detail=detail or None,
                returncode=returncode,
                partial_remote_target=True,
            )
        return tool_ok(
            direction="upload",
            local_path=str(source),
            host=normalized_host,
            remote_path=normalized_remote_path,
            bytes=source_size,
        )

    async def download_file(
        self,
        host: str,
        remote_path: str,
        local_path: str,
        *,
        port: int | None = None,
        identity: str | None = None,
        timeout: float = 120.0,
    ) -> str:
        remote = self._validate_remote(host, remote_path)
        if isinstance(remote, str):
            return tool_err(remote)
        normalized_host, normalized_remote_path = remote
        target = self._resolve_local_path(local_path)
        if target.exists() and target.is_dir():
            return tool_err(f"Local destination is a directory: {local_path}")

        temp_path: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".nanocat-download",
                dir=target.parent,
            )
            temp_path = Path(temp_name)
            os.close(fd)
            argv = self._scp_argv(port=port, identity=identity)
            argv.extend([f"{normalized_host}:{normalized_remote_path}", str(temp_path)])
            returncode, detail = await self._run_scp(argv, timeout)
            if returncode != 0:
                return tool_err(
                    "SSH download failed",
                    hint="verify SSH authentication and the remote source",
                    detail=detail or None,
                    returncode=returncode,
                )
            if not temp_path.is_file():
                return tool_err("SSH download produced no local file")
            size = temp_path.stat().st_size
            os.replace(temp_path, target)
            temp_path = None
            return tool_ok(
                direction="download",
                host=normalized_host,
                remote_path=normalized_remote_path,
                local_path=str(target),
                bytes=size,
            )
        except TimeoutError:
            return tool_err(
                "SSH download timed out",
                hint="verify the remote host and retry with a larger timeout",
            )
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as exc:
            return tool_err(f"SSH download failed: {exc_message(exc)}")
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    async def open(
        self,
        host: str,
        *,
        port: int | None = None,
        identity: str | None = None,
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
    ) -> str:
        import json

        try:
            normalized_host = self._validate_host(host)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
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
        resolved_identity = self._resolve_identity(identity)
        if resolved_identity:
            if not Path(resolved_identity).is_file():
                return json.dumps(
                    {"ok": False, "error": f"Identity file not found: {identity}"},
                    ensure_ascii=False,
                )
            argv += ["-i", resolved_identity]
        argv.append(normalized_host)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return json.dumps({"ok": False, "error": "`ssh` binary not found on PATH"})
        except Exception as e:
            return json.dumps({"ok": False, "error": f"failed to launch ssh: {e}"})

        sid = uuid.uuid4().hex[:8]
        session = TerminalSession(sid, normalized_host, proc, cols, rows)
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
            return json.dumps(
                {
                    "ok": False,
                    "session_id": sid,
                    "exit_code": session.exit_code,
                    "output": detail or "",
                },
                ensure_ascii=False,
            )

        screen = session.render_screen()
        return json.dumps(
            {
                "ok": True,
                "session_id": sid,
                "host": normalized_host,
                "cols": cols,
                "rows": rows,
                "screen": screen,
                "connecting": not bool(screen),
            },
            ensure_ascii=False,
        )

    def _describe_item(self, s: TerminalSession) -> dict:
        return {"host": s.label[:50], "idle_s": s.idle_seconds()}


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
            "and full-screen terminal apps (vim/htop/tmux): launch one, then "
            "ssh_read(mode=screen) "
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
                "cols": {
                    "type": "integer",
                    "default": _DEFAULT_COLS,
                    "description": "Terminal width; raise for wide terminal apps",
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
        cols: int = _DEFAULT_COLS,
        rows: int = _DEFAULT_ROWS,
        **kwargs: Any,
    ) -> str:
        return await self._mgr.open(
            host,
            port=port,
            identity=identity,
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
                    "description": "screen = current terminal screen render; "
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


class SSHUploadTool(Tool):
    def __init__(self, manager: SSHManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "ssh_upload"

    @property
    def description(self) -> str:
        return (
            "Upload one local file to a remote host with scp. Relative local paths resolve "
            "against the workspace. Uses SSH config, key, or agent authentication and does "
            "not prompt for passwords."
        )

    @property
    def capabilities(self) -> tuple[str, ...]:
        return ("ssh.upload", "filesystem.read")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "local_path": {"type": "string", "description": "Local source file"},
                "host": {"type": "string", "description": "ssh-config alias or user@host"},
                "remote_path": {
                    "type": "string",
                    "description": "Remote destination path without shell-sensitive characters",
                },
                "port": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 65535,
                    "description": "SSH port, if non-default",
                },
                "identity": {"type": "string", "description": "Private key path"},
                "timeout": {
                    "type": "number",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 1800,
                    "description": "Transfer deadline in seconds",
                },
            },
            "required": ["local_path", "host", "remote_path"],
        }

    async def execute(
        self,
        local_path: str,
        host: str,
        remote_path: str,
        port: int | None = None,
        identity: str | None = None,
        timeout: float = 120.0,
        **kwargs: Any,
    ) -> str:
        return await self._mgr.upload_file(
            local_path,
            host,
            remote_path,
            port=port,
            identity=identity,
            timeout=timeout,
        )


class SSHDownloadTool(Tool):
    def __init__(self, manager: SSHManager):
        self._mgr = manager

    @property
    def name(self) -> str:
        return "ssh_download"

    @property
    def description(self) -> str:
        return (
            "Download one remote file with scp. Relative local paths resolve against the "
            "workspace. The completed file atomically replaces the local destination. Uses "
            "SSH config, key, or agent authentication and does not prompt for passwords."
        )

    @property
    def capabilities(self) -> tuple[str, ...]:
        return ("ssh.download", "filesystem.write")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "ssh-config alias or user@host"},
                "remote_path": {
                    "type": "string",
                    "description": "Remote source file without shell-sensitive characters",
                },
                "local_path": {"type": "string", "description": "Local destination file"},
                "port": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 65535,
                    "description": "SSH port, if non-default",
                },
                "identity": {"type": "string", "description": "Private key path"},
                "timeout": {
                    "type": "number",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 1800,
                    "description": "Transfer deadline in seconds",
                },
            },
            "required": ["host", "remote_path", "local_path"],
        }

    async def execute(
        self,
        host: str,
        remote_path: str,
        local_path: str,
        port: int | None = None,
        identity: str | None = None,
        timeout: float = 120.0,
        **kwargs: Any,
    ) -> str:
        return await self._mgr.download_file(
            host,
            remote_path,
            local_path,
            port=port,
            identity=identity,
            timeout=timeout,
        )
