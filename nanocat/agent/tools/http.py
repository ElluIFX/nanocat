"""Structured HTTP request tool — make an HTTP call with method/headers/json and
get back status + headers + body as JSON, without the curl-via-exec quoting
pain. SSRF-guarded like the web tools (reuses security/network.py)."""

from __future__ import annotations

from typing import Any

import httpx

from nanocat.agent.tools.base import Tool

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
_MAX_BODY = 100_000  # cap; oversized bodies also hit the global tool-result truncation
_RESP_HEADER_KEYS = ("content-type", "content-length", "location", "server")


class HttpRequestTool(Tool):
    def __init__(self, proxy: str | None = None, safety_check: bool = True):
        self._proxy = proxy
        self._safety_check = safety_check

    @property
    def name(self) -> str:
        return "http_request"

    @property
    def description(self) -> str:
        return (
            "Make an HTTP request (method/headers/json body) and return status, headers "
            "and body as JSON. Use for APIs and webhooks instead of curl. To read an "
            "article's main text, prefer web_fetch."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": _METHODS, "description": "default GET"},
                "headers": {"type": "object", "description": "Request headers"},
                "json": {"type": "object", "description": "JSON body (sets Content-Type)"},
                "body": {"type": "string", "description": "Raw text body (when not using json)"},
                "timeout": {"type": "number", "description": "Seconds (default 30)"},
            },
            "required": ["url"],
        }

    async def execute(
        self,
        url: str,
        method: str = "GET",
        headers: dict | None = None,
        json: dict | None = None,
        body: str | None = None,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> str:
        import json as _json

        from nanocat.security.network import validate_resolved_url, validate_url_target

        if self._safety_check:
            ok, err = validate_url_target(url)
            if not ok:
                return f"Error: blocked URL: {err}"

        try:
            async with httpx.AsyncClient(follow_redirects=True, proxy=self._proxy) as client:
                resp = await client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    json=json,
                    content=body if json is None else None,
                    timeout=timeout,
                )
        except Exception as e:
            return f"Error: request failed: {e}"

        if self._safety_check:
            ok, err = validate_resolved_url(str(resp.url))
            if not ok:
                return f"Error: redirect blocked: {err}"

        text = resp.text
        truncated = len(text) > _MAX_BODY
        if truncated:
            text = text[:_MAX_BODY]

        return _json.dumps(
            {
                "status": resp.status_code,
                "url": str(resp.url),
                "headers": {k: resp.headers.get(k) for k in _RESP_HEADER_KEYS if k in resp.headers},
                "body": text,
                "body_truncated": truncated,
                "elapsed_s": round(resp.elapsed.total_seconds(), 3),
            },
            ensure_ascii=False,
        )
