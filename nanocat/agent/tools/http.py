"""Structured HTTP request tool — method/headers/json → JSON {status, headers,
body}, without the curl-via-exec quoting pain.

Beyond one-shot requests it supports:
- in-memory cookie sessions (`session`/`session_id`) — a persistent httpx client
  kept in memory only, never written to disk;
- multipart file upload (`files`: field -> local path);
- fake-stream (`stream`): return a temp-file path immediately while the response
  body streams into it in the background (pair with proc/read_file to watch it).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from typing import Any

import httpx

from nanocat.agent.tools.base import Tool, exc_message

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
_RESP_HEADER_KEYS = ("content-type", "content-length", "location", "server", "set-cookie")


class HttpSessionManager:
    """In-memory HTTP sessions (persistent cookie jars) + active fake-stream tasks.

    Nothing is persisted to disk; sessions and streams are dropped on shutdown.
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
    ):
        self._mgr = manager
        self._proxy = proxy

    @property
    def name(self) -> str:
        return "http_request"

    @property
    def description(self) -> str:
        return (
            "Make an HTTP request (method/headers/json) → JSON {status, headers, body}. "
            "For APIs/webhooks, not article text (use web_fetch). Options: session=true (+ "
            "reuse via session_id) keeps cookies across calls; files={field:path} uploads; "
            "stream=true returns a temp-file path immediately and writes the body into it."
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
                    "description": "Return a temp-file path at once and stream the body into it "
                    "in the background (read the file to watch it fill)",
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
        fd, path = tempfile.mkstemp(suffix=".stream", prefix="nanocat_http_")
        os.close(fd)
        if use_session:
            client, sid = self._mgr.session(session_id)
            owns = False
        else:
            client, sid, owns = self._mgr.new_client(), None, True

        stream_id = uuid.uuid4().hex[:8]
        task = asyncio.create_task(
            self._stream_to_file(client, owns, method, url, headers, json_body, body, timeout, path)
        )
        self._mgr.track_stream(stream_id, task)
        result = {
            "ok": True,
            "streaming": True,
            "stream_id": stream_id,
            "streaming_to": path,
            "status": "downloading",
            "hint": "read this file as it fills; it stops growing when the download completes",
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
        path: str,
    ) -> None:
        content = body if json_body is None else None
        try:
            async with client.stream(
                method.upper(),
                url,
                headers=headers,
                json=json_body,
                content=content,
                timeout=timeout,
            ) as resp:
                with open(path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                with open(path, "ab") as f:
                    f.write(f"\n[stream error: {e}]".encode())
            except Exception:
                pass
        finally:
            if owns:
                try:
                    await client.aclose()
                except Exception:
                    pass
