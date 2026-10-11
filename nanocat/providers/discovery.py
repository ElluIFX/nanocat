from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from nanocat.application.text_catalog import USER_TEXT
from nanocat.config.schema import Config
from nanocat.providers.registry import PROVIDERS, find_by_name

_DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "custom": "http://localhost:8000/v1",
}


class ModelDiscoveryError(ValueError):
    """A safe, user-visible discovery failure without upstream response contents."""


async def discover_models(config: Config, channel: str | None = None) -> tuple[str, list[str]]:
    """Fetch the selected provider's live catalog with bounded I/O and pagination."""
    name = (channel or config.get_provider_name() or "").strip().lower().replace("-", "_")
    spec = find_by_name(name)
    if spec is None:
        raise ModelDiscoveryError(
            USER_TEXT.model_discover_unknown.format(providers=", ".join(s.name for s in PROVIDERS))
        )
    if spec.is_oauth:
        raise ModelDiscoveryError(USER_TEXT.model_discover_oauth.format(provider=name))
    provider = getattr(config.providers, name)
    if not provider.api_key and not spec.is_local and name != "custom":
        raise ModelDiscoveryError(USER_TEXT.model_discover_key.format(provider=name))
    base = provider.api_base or spec.default_api_base or _DEFAULT_BASES.get(name)
    if name == "kimi":
        base = config.providers.kimi.resolved_api_base()
    if not base:
        raise ModelDiscoveryError(USER_TEXT.model_discover_base.format(provider=name))
    base = base.rstrip("/")
    headers = httpx.Headers(provider.extra_headers or {})
    params: dict[str, str | int] = {}
    if name == "anthropic":
        headers.setdefault("x-api-key", provider.api_key)
        headers.setdefault("anthropic-version", "2023-06-01")
        params["limit"] = 100
    elif name == "gemini":
        headers.setdefault("x-goog-api-key", provider.api_key)
        params["pageSize"] = 1000
    elif provider.api_key:
        headers.setdefault("Authorization", f"Bearer {provider.api_key}")
    if name == "ollama" and not base.endswith("/v1"):
        base += "/v1"
    url = base + "/models"
    models: set[str] = set()
    cursors: set[str] = set()
    try:
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(timeout=15, follow_redirects=False) as client,
        ):
            for _ in range(20):
                async with client.stream("GET", url, headers=headers, params=params) as response:
                    if response.status_code >= 300:
                        raise ModelDiscoveryError(
                            USER_TEXT.model_discover_http.format(
                                provider=name, status=response.status_code
                            )
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 4 * 1024 * 1024:
                            raise ModelDiscoveryError(USER_TEXT.model_discover_size)
                payload = json.loads(content)
                if not isinstance(payload, dict):
                    raise ModelDiscoveryError(USER_TEXT.model_discover_invalid)
                items = payload.get("models" if name == "gemini" else "data")
                if not isinstance(items, list):
                    raise ModelDiscoveryError(USER_TEXT.model_discover_invalid)
                for item in items:
                    if not isinstance(item, dict):
                        raise ModelDiscoveryError(USER_TEXT.model_discover_invalid)
                    model_id = item.get("name" if name == "gemini" else "id")
                    if not isinstance(model_id, str) or not model_id.strip():
                        raise ModelDiscoveryError(USER_TEXT.model_discover_invalid)
                    if name == "gemini":
                        model_id = model_id.removeprefix("models/")
                    models.add(model_id)
                cursor: Any = payload.get("nextPageToken") if name == "gemini" else None
                if payload.get("has_more"):
                    cursor = payload.get("last_id")
                    if not cursor:
                        raise ModelDiscoveryError(USER_TEXT.model_discover_invalid)
                if not cursor:
                    return name, sorted(models)
                if not isinstance(cursor, str) or cursor in cursors:
                    raise ModelDiscoveryError(USER_TEXT.model_discover_invalid)
                cursors.add(cursor)
                params["pageToken" if name == "gemini" else "after_id"] = cursor
            raise ModelDiscoveryError(USER_TEXT.model_discover_pages)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ModelDiscoveryError(USER_TEXT.model_discover_timeout) from exc
    except httpx.HTTPError as exc:
        raise ModelDiscoveryError(USER_TEXT.model_discover_connection) from exc
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ModelDiscoveryError):
            raise
        raise ModelDiscoveryError(USER_TEXT.model_discover_invalid) from exc
