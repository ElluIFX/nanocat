"""Structured HTTP request tool — method/headers/json → JSON {status, headers,
body}, without the curl-via-exec quoting pain.

Beyond one-shot requests it supports:
- in-memory cookie sessions (`session`/`session_id`) — a persistent httpx client
  kept in memory only, never written to disk;
- multipart file upload (`files`: field -> local path);
- fake-stream (`stream`): return a workspace runtime-file path immediately while
  the response body streams into it in the background (pair with read_file).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextvars import ContextVar
from typing import Any

import httpx

from nanocat.agent.tools.base import Tool, exc_message

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
_RESP_HEADER_KEYS = ("content-type", "content-length", "location", "server", "set-cookie")


class HttpSessionManager:
    """In-memory HTTP sessions (persistent cookie jars) + active fake-stream tasks.

    Cookie sessions stay in memory. Active stream tasks are cancelled on shutdown;
    completed runtime files remain owned by the injected runtime file store.
    """

    def __init__(self, proxy: str | None = None) -> None:
        self._proxy = proxy
        self._sessions: dict[str, httpx.AsyncClient] = {}
        self._streams: dict[str, asyncio.Task] = {}

    def session(self, session_id: str | None) -> tuple[httpx.AsyncClient, str]:
        """Return (client, id) for *session_id*, creating one (new id if None)."""
        if session_id and session_id in self._sessions:
            return self._sessions[session_id], session_id
        sid = session_id or uuid.uuid4().hex[:8]
        self._sessions[sid] = httpx.AsyncClient(follow_redirects=True, proxy=self._proxy)
        return self._sessions[sid], sid

    def new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(follow_redirects=True, proxy=self._proxy)

    def track_stream(self, stream_id: str, task: asyncio.Task) -> None:
        self._streams[stream_id] = task
        task.add_done_callback(lambda _t: self._streams.pop(stream_id, None))

    async def close_all(self) -> None:
        for task in list(self._streams.values()):
            task.cancel()
        for task in list(self._streams.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._streams.clear()
        for client in list(self._sessions.values()):
            try:
                await client.aclose()
            except Exception:
                pass
        self._sessions.clear()


class HttpRequestTool(Tool):
    def __init__(
        self,
        manager: HttpSessionManager,
        proxy: str | None = None,
        runtime_file_store: Any | None = None,
    ):
        self._mgr = manager
        self._proxy = proxy
        self._runtime_file_store = runtime_file_store
        self._storage_scope: ContextVar[str | None] = ContextVar(
            "http_request_storage_scope", default=None
        )

    def set_storage_scope(self, storage_scope: str) -> None:
        """Bind the runtime-file scope for the current tool execution context."""
        self._storage_scope.set(storage_scope)

    @property
    def name(self) -> str:
        return "http_request"

    @property
    def description(self) -> str:
        return (
            "Make an HTTP request (method/headers/json) → JSON {status, headers, body}. "
            "For APIs/webhooks, not article text (use web_fetch). Options: session=true (+ "
            "reuse via session_id) keeps cookies across calls; files={field:path} uploads; "
            "stream=true returns a workspace runtime-file path immediately and writes the "
            "body into it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": _METHODS, "default": "GET"},
                "headers": {"type": "object", "description": "Request headers"},
                "json_body": {"type": "object", "description": "JSON body (sets Content-Type)"},
                "body": {
                    "type": "string",
                    "description": "Raw text body (when not using json_body)",
                },
                "timeout": {"type": "number", "default": 30, "description": "Seconds"},
                "session": {
                    "type": "boolean",
                    "description": "Keep cookies in an in-memory session; the result returns a "
                    "session_id to pass back on later calls",
                },
                "session_id": {"type": "string", "description": "Reuse an existing session"},
                "files": {
                    "type": "object",
                    "description": "Multipart upload: {field_name: local_file_path}",
                },
                "stream": {
                    "type": "boolean",
                    "description": "Return a workspace runtime-file path at once and stream "
                    "the body into it in the background",
                },
            },
            "required": ["url"],
        }

    async def execute(
        self,
        url: str,
        method: str = "GET",
        headers: dict | None = None,
        json_body: dict | None = None,
        body: str | None = None,
        timeout: float = 30.0,
        session: bool = False,
        session_id: str | None = None,
        files: dict | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> str:
        use_session = session or bool(session_id)

        if stream:
            return self._start_stream(
                url, method, headers, json_body, body, timeout, use_session, session_id
            )

        # Build the body kwargs (multipart files take precedence, then json_body, then raw).
        handles: list = []
        req: dict[str, Any] = {"headers": headers, "timeout": timeout}
        if files:
            files_arg = {}
            for field, path in files.items():
                try:
                    fh = open(path, "rb")
                except Exception as e:
                    for h in handles:
                        h.close()
                    return json.dumps(
                        {"ok": False, "error": f"cannot open file {path!r}", "detail": exc_message(e)}
                    )
                handles.append(fh)
                files_arg[field] = (os.path.basename(path), fh)
            req["files"] = files_arg
        elif json_body is not None:
            req["json"] = json_body
        elif body is not None:
            req["content"] = body

        sid = None
        try:
            if use_session:
                client, sid = self._mgr.session(session_id)
                resp = await client.request(method.upper(), url, **req)
            else:
                async with self._mgr.new_client() as client:
                    resp = await client.request(method.upper(), url, **req)
        except Exception as e:
            return json.dumps(
                {"ok": False, "error": "request failed", "detail": exc_message(e)}
            )
        finally:
            for h in handles:
                h.close()

        text = resp.text

        result: dict[str, Any] = {
            "ok": True,
            "status": resp.status_code,
            "url": str(resp.url),
            "headers": {k: resp.headers.get(k) for k in _RESP_HEADER_KEYS if k in resp.headers},
            "body": text,
            "elapsed_s": round(resp.elapsed.total_seconds(), 3),
        }
        if sid:
            result["session_id"] = sid
        return json.dumps(result, ensure_ascii=False)

    def _start_stream(
        self,
        url: str,
        method: str,
        headers: dict | None,
        json_body: dict | None,
        body: str | None,
        timeout: float,
        use_session: bool,
        session_id: str | None,
    ) -> str:
        if self._runtime_file_store is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "HTTP streaming storage is unavailable",
                    "hint": "Retry without stream=true.",
                }
            )
        storage_scope = self._storage_scope.get()
        if not storage_scope:
            return json.dumps(
                {
                    "ok": False,
                    "error": "HTTP streaming has no runtime storage scope",
                    "hint": "Retry without stream=true.",
                }
            )
        try:
            allocation = self._runtime_file_store.allocate(
                storage_scope,
                "http",
                suffix=".stream",
            )
        except Exception as e:
            return json.dumps(
                {
                    "ok": False,
                    "error": "cannot allocate HTTP stream file",
                    "detail": exc_message(e),
                },
                ensure_ascii=False,
            )

        try:
            if use_session:
                client, sid = self._mgr.session(session_id)
                owns = False
            else:
                client, sid, owns = self._mgr.new_client(), None, True
        except Exception as e:
            self._runtime_file_store.discard(allocation)
            return json.dumps(
                {
                    "ok": False,
                    "error": "cannot initialize HTTP stream client",
                    "detail": exc_message(e),
                },
                ensure_ascii=False,
            )

        stream_id = uuid.uuid4().hex[:8]
        max_bytes = max(0, int(self._runtime_file_store.max_file_bytes))
        task = asyncio.create_task(
            self._stream_to_file(
                client,
                owns,
                method,
                url,
                headers,
                json_body,
                body,
                timeout,
                allocation,
                max_bytes,
                stream_id,
            )
        )
        self._mgr.track_stream(stream_id, task)
        result = {
            "ok": True,
            "streaming": True,
            "stream_id": stream_id,
            "path": allocation.relative_path,
            "max_bytes": max_bytes,
            "status": "downloading",
            "hint": "Use read_file on this path; it stops growing when the download completes",
        }
        if sid:
            result["session_id"] = sid
        return json.dumps(result, ensure_ascii=False)

    async def _stream_to_file(
        self,
        client: httpx.AsyncClient,
        owns: bool,
        method: str,
        url: str,
        headers: dict | None,
        json_body: dict | None,
        body: str | None,
        timeout: float,
        allocation: Any,
        max_bytes: int,
        stream_id: str,
    ) -> None:
        content = body if json_body is None else None
        completed = False
        try:
            async with client.stream(
                method.upper(),
                url,
                headers=headers,
                json=json_body,
                content=content,
                timeout=timeout,
            ) as resp:
                with open(allocation.absolute_path, "wb") as f:
                    written = 0
                    async for chunk in resp.aiter_bytes():
                        remaining = max_bytes - written
                        if remaining <= 0:
                            self._write_truncated_marker(f, max_bytes)
                            break
                        f.write(chunk[:remaining])
                        written += min(len(chunk), remaining)
                        if len(chunk) > remaining:
                            self._write_truncated_marker(f, max_bytes)
                            break
            completed = True
        except asyncio.CancelledError:
            self._runtime_file_store.discard(allocation)
            raise
        except Exception as e:
            try:
                self._append_error_marker(allocation.absolute_path, e, max_bytes)
                completed = True
            except Exception:
                self._runtime_file_store.discard(allocation)
        finally:
            if completed:
                try:
                    self._runtime_file_store.finalize(
                        allocation,
                        source_name=self.name,
                        source_id=stream_id,
                    )
                except Exception:
                    self._runtime_file_store.discard(allocation)
            if owns:
                try:
                    await client.aclose()
                except Exception:
                    pass

    @staticmethod
    def _write_truncated_marker(handle: Any, max_bytes: int) -> None:
        marker = b"\n[stream truncated at runtime file limit]\n"
        if max_bytes < len(marker):
            return
        handle.seek(max_bytes - len(marker))
        handle.write(marker)

    @staticmethod
    def _append_error_marker(path: Any, error: Exception, max_bytes: int) -> None:
        marker = f"\n[stream error: {exc_message(error)}]".encode("utf-8")
        with open(path, "r+b") as handle:
            handle.seek(0, os.SEEK_END)
            current = handle.tell()
            if current + len(marker) <= max_bytes:
                handle.write(marker)
            elif max_bytes >= len(marker):
                handle.seek(max_bytes - len(marker))
                handle.write(marker)
