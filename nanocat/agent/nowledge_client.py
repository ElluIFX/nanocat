"""Typed, lifecycle-owned client for the Nowledge Mem REST API."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx
from loguru import logger


class NowledgeRequestError(RuntimeError):
    """Classified failure returned by the Nowledge API boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class NowledgeClient:
    """Shared async HTTP client with bounded retries and structured failures."""

    _RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:14242",
        api_key: str | None = None,
        *,
        space_id: str | None = None,
        request_timeout: float = 15.0,
    ):
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._space_id = space_id
        self._request_timeout = request_timeout
        self._http: httpx.AsyncClient | None = None
        self._closed = False
        self._health_until = 0.0
        self._health_ok = False

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _client(self) -> httpx.AsyncClient:
        if self._closed:
            raise NowledgeRequestError("Nowledge client is closed", code="closed")
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers=self._headers(),
                timeout=httpx.Timeout(self._request_timeout),
            )
        return self._http

    async def close(self) -> None:
        """Close the shared connection pool; safe to call repeatedly."""
        self._closed = True
        client, self._http = self._http, None
        if client is not None:
            await client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        retryable: bool = False,
    ) -> Any:
        attempts = 2 if retryable else 1
        last_error: NowledgeRequestError | None = None
        for attempt in range(attempts):
            try:
                client = await self._client()
                response = await client.request(
                    method,
                    f"{self._api_url}/{path.lstrip('/')}",
                    json=dict(json) if json is not None else None,
                    params=dict(params) if params is not None else None,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = NowledgeRequestError(
                    "Nowledge request timed out",
                    code="timeout",
                    retryable=True,
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.1)
                    continue
                raise last_error from exc
            except httpx.RequestError as exc:
                last_error = NowledgeRequestError(
                    "Nowledge service is unreachable",
                    code="unavailable",
                    retryable=True,
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.1)
                    continue
                raise last_error from exc

            if response.status_code >= 400:
                retry_status = response.status_code in self._RETRYABLE_STATUS
                if response.status_code == 404:
                    error_code = "not_found"
                elif response.status_code >= 500:
                    error_code = "server"
                else:
                    error_code = "api_error"
                last_error = NowledgeRequestError(
                    f"Nowledge API returned HTTP {response.status_code}",
                    code=error_code,
                    status_code=response.status_code,
                    retryable=retry_status,
                )
                if retryable and retry_status and attempt + 1 < attempts:
                    await asyncio.sleep(0.1)
                    continue
                raise last_error

            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise NowledgeRequestError(
                    "Nowledge API returned invalid JSON",
                    code="invalid_response",
                ) from exc

        raise last_error or NowledgeRequestError("Nowledge request failed", code="internal")

    async def is_available(self) -> bool:
        """Check health with a short-lived cache to avoid probing every turn."""
        loop_time = asyncio.get_running_loop().time()
        if loop_time < self._health_until:
            return self._health_ok
        try:
            await self._request("GET", "/health", timeout=2.0, retryable=True)
        except NowledgeRequestError as exc:
            logger.debug("Nowledge health check failed: {}", exc.code)
            self._health_ok = False
        else:
            self._health_ok = True
        self._health_until = loop_time + 5.0
        return self._health_ok

    async def search_memories(
        self,
        query: str,
        limit: int = 10,
        *,
        mode: str | None = None,
        include_entities: bool | None = None,
        filter_labels: list[str] | None = None,
        metadata_filters: Mapping[str, Any] | list[str] | None = None,
        space_id: str | None = None,
        unit_type: str | None = None,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 100))}
        optional = {
            "mode": mode,
            "include_entities": include_entities,
            "filter_labels": filter_labels,
            "space_id": space_id or self._space_id,
            "unit_type": unit_type,
            **filters,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if metadata_filters:
            if isinstance(metadata_filters, Mapping):
                payload["metadata_filters"] = [
                    f"{key}={value}" for key, value in metadata_filters.items()
                ]
            else:
                payload["metadata_filters"] = metadata_filters
        result = await self._request("POST", "/memories/search", json=payload, retryable=True)
        return result if isinstance(result, list) else []

    async def get_memory(self, memory_id: str, *, space_id: str | None = None) -> dict[str, Any]:
        params = {"space_id": space_id or self._space_id} if space_id or self._space_id else None
        result = await self._request(
            "GET", f"/memories/{memory_id}", params=params, retryable=True
        )
        return result if isinstance(result, dict) else {}

    async def create_memory(self, content: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "content": content,
            "space_id": fields.pop("space_id", None) or self._space_id,
            **{key: value for key, value in fields.items() if value is not None},
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = await self._request("POST", "/memories", json=payload)
        return result if isinstance(result, dict) else {}

    async def update_memory(
        self, memory_id: str, *, space_id: str | None = None, **fields: Any
    ) -> dict[str, Any]:
        payload = {key: value for key, value in fields.items() if value is not None}
        if space_id or self._space_id:
            payload["space_id"] = space_id or self._space_id
        result = await self._request("PATCH", f"/memories/{memory_id}", json=payload)
        return result if isinstance(result, dict) else {}

    async def delete_memory(
        self,
        memory_id: str,
        cascade_delete: bool = True,
        *,
        space_id: str | None = None,
    ) -> bool:
        params = {
            "cascade_delete": cascade_delete,
            "space_id": space_id or self._space_id,
        }
        await self._request(
            "DELETE",
            f"/memories/{memory_id}",
            params={key: value for key, value in params.items() if value is not None},
        )
        return True

    async def supersede_memory(
        self,
        memory_id: str,
        newer_memory_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        payload = {"newer_memory_id": newer_memory_id, "reason": reason, "space_id": self._space_id}
        payload = {key: value for key, value in payload.items() if value is not None}
        result = await self._request("POST", f"/memories/{memory_id}/supersede", json=payload)
        return result if isinstance(result, dict) else {}

    async def deprecate_memory(
        self,
        memory_id: str,
        *,
        reason: str | None = None,
        replacement_memory_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "reason": reason,
            "replacement_memory_id": replacement_memory_id,
            "space_id": self._space_id,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = await self._request("POST", f"/memories/{memory_id}/deprecate", json=payload)
        return result if isinstance(result, dict) else {}

    async def get_working_memory(
        self, *, date: str | None = None, space_id: str | None = None
    ) -> str:
        params = {
            key: value
            for key, value in {"date": date, "space_id": space_id or self._space_id}.items()
            if value
        }
        result = await self._request("GET", "/agent/working-memory", params=params, retryable=True)
        if isinstance(result, dict):
            return str(result.get("content") or result.get("markdown") or "")
        return ""

    async def create_thread(
        self,
        thread_id: str,
        title: str,
        messages: list[dict[str, Any]],
        *,
        source: str = "nanocat",
        space_id: str | None = None,
        project: str | None = None,
        workspace: str | None = None,
        tool_version: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        payload = {
            "thread_id": thread_id,
            "title": title,
            "messages": messages,
            "source": source,
            "space_id": space_id or self._space_id,
            "project": project,
            "workspace": workspace,
            "tool_version": tool_version,
            "metadata": metadata,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = await self._request("POST", "/threads", json=payload)
        if not isinstance(result, dict):
            return None
        thread = result.get("thread") or {}
        return thread.get("thread_id") or thread.get("id")

    async def append_messages(
        self,
        thread_id: str,
        messages: list[dict[str, Any]],
        *,
        idempotency_key: str,
        space_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": messages,
            "deduplicate": True,
            "idempotency_key": idempotency_key,
        }
        if space_id or self._space_id:
            payload["space_id"] = space_id or self._space_id
        result = await self._request(
            "POST",
            f"/threads/{thread_id}/append",
            json=payload,
            retryable=True,
        )
        return result if isinstance(result, dict) else {}

    async def search_threads(
        self,
        query: str,
        *,
        mode: str = "full",
        limit: int = 20,
        source: str | None = None,
        space_id: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "query": query,
            "mode": mode,
            "limit": max(1, min(limit, 500)),
            "source": source,
            "space_id": space_id or self._space_id,
        }
        result = await self._request(
            "GET",
            "/threads/search",
            params={key: value for key, value in params.items() if value is not None},
            retryable=True,
        )
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"threads": result}
        return {}

    async def get_thread(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        space_id: str | None = None,
    ) -> dict[str, Any]:
        params = {"limit": limit, "offset": offset, "space_id": space_id or self._space_id}
        result = await self._request(
            "GET",
            f"/threads/{thread_id}",
            params={key: value for key, value in params.items() if value is not None},
            retryable=True,
        )
        return result if isinstance(result, dict) else {}

    async def delete_thread(self, thread_id: str, *, cascade_delete_memories: bool = False) -> dict[str, Any]:
        result = await self._request(
            "DELETE",
            f"/threads/{thread_id}",
            params={
                "cascade_delete_memories": cascade_delete_memories,
                "space_id": self._space_id,
            },
        )
        return result if isinstance(result, dict) else {}

    async def triage(self, thread_content: str, *, preferred_language: str = "zh") -> dict[str, Any]:
        result = await self._request(
            "POST",
            "/memories/distill/triage",
            json={"thread_content": thread_content[:50_000], "preferred_language": preferred_language},
        )
        return result if isinstance(result, dict) else {}

    async def preview_distill(self, thread_id: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "thread_id": thread_id,
            "preferred_language": "zh",
            "space_id": self._space_id,
            **fields,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = await self._request("POST", "/memories/distill/preview", json=payload)
        return result if isinstance(result, dict) else {}

    async def distill(self, thread_id: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "thread_id": thread_id,
            "preferred_language": "zh",
            "space_id": self._space_id,
            **fields,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = await self._request("POST", "/memories/distill", json=payload)
        return result if isinstance(result, dict) else {}

    async def processing_status(self, *, include: bool = True) -> dict[str, Any]:
        result = await self._request(
            "GET",
            "/agent/knowledge-processing/status",
            params={"include": include},
            retryable=True,
        )
        return result if isinstance(result, dict) else {}

    async def agent_status(self) -> dict[str, Any]:
        result = await self._request("GET", "/agent/status", retryable=True)
        return result if isinstance(result, dict) else {}

    async def list_spaces(self) -> dict[str, Any]:
        """Return the Nowledge Space roster and product-level settings."""
        result = await self._request("GET", "/spaces", retryable=True)
        return result if isinstance(result, dict) else {}

    async def trigger(self, task: str) -> dict[str, Any]:
        allowed = {
            "memory-compaction": "/agent/trigger/memory-compaction",
            "kg-extraction": "/agent/trigger/kg-extraction",
            "daily-briefing": "/agent/trigger/daily-briefing",
            "crystallization": "/agent/trigger/crystallization",
            "insights": "/agent/trigger/insights",
        }
        path = allowed.get(task)
        if path is None:
            raise NowledgeRequestError("Unsupported Nowledge trigger", code="validation")
        result = await self._request("POST", path, json={})
        return result if isinstance(result, dict) else {}
