"""Runtime-scoped fallback for models that reject image input."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse

import httpx
from loguru import logger
from PIL import Image

from nanocat.providers.base import LLMProvider, LLMResponse
from nanocat.utils.helpers import detect_image_mime

_MAX_PIXELS = 3840 * 2160
_MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
_VISION_REJECTION_MARKERS = (
    "image input is not supported",
    "image inputs are not supported",
    "vision is not supported",
    "model does not support image input",
    "does not support image input",
    "multimodal input is not supported",
    "multimodal inputs are not supported",
)
_VISION_PROMPT = """You are a visual analysis assistant. Analyze the provided image and return a structured report in the following format. Be precise about positions; prefer concrete descriptions over vague ones.

## Transparency rule
For every region or element you describe, explicitly note the level of detail you provided. Use one of these tags at the end of each description:
- **[full]** — every readable detail in this region is captured
- **[partial]** — representative sampling; some minor details omitted
- **[summary]** — only high-level structure sketched; many details omitted

## Output Format

### Summary
A concise 1-3 sentence description of what this image contains.

### Structure
Describe the layout and visual structure:
- Key regions or elements and their positions followed by the transparency tag
- Their shapes or visual characteristics
- Visual hierarchy, grouping, or spatial relationships between elements

### Content
Extract and present all readable text content. Preserve logical structure:
- Use markdown tables for tabular data
- Use bullet or numbered lists for listed items
- Use code blocks with a language tag when inferable for code snippets
- Use blockquotes for quoted passages
- For forms or structured documents, describe field labels and values
- For diagrams or charts, describe what they represent

If the image contains no meaningful text, describe the visual content instead. Never invent text that is not visibly present.

### Notes (optional)
Add relevant observations about visual anomalies, quality, inferred context, or regions that received only summary coverage.
"""


class VisionFallbackError(RuntimeError):
    """A vision fallback request could not be prepared or completed."""


class VisionImageExtractionError(VisionFallbackError):
    """An image block could not be loaded or normalized."""


class VisionModelError(VisionFallbackError):
    """The configured fallback model could not describe an image."""


class VisionFallbackService:
    """Retry image requests through textual descriptions after explicit rejection."""

    def __init__(
        self,
        provider_resolver: Any,
        config: Any,
        *,
        workspace: Path,
        cache_size: int = 128,
    ):
        if cache_size <= 0:
            raise ValueError("vision fallback cache_size must be positive")
        self._provider_resolver = provider_resolver
        self._config = config
        self._workspace = workspace.resolve()
        self._cache_size = cache_size
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        self._closed = False

    async def close(self) -> None:
        """Close the runtime-owned HTTP client."""
        if self._closed:
            return
        self._closed = True
        await self._http.aclose()

    async def chat_with_fallback(
        self,
        provider: LLMProvider,
        *,
        messages: list[dict[str, Any]],
        model: str,
        **chat_kwargs: Any,
    ) -> LLMResponse:
        """Send one request and retry once with image descriptions when required."""
        response = await provider.chat_with_retry(
            messages=messages,
            model=model,
            **chat_kwargs,
        )
        if not self._contains_images(messages) or not self._is_vision_rejection(
            provider,
            response,
        ):
            return response

        fallback_model = self._fallback_model()
        if not fallback_model or fallback_model.strip().casefold() == model.strip().casefold():
            return self._fallback_error(
                "Vision fallback model unavailable",
                response,
            )

        try:
            rewritten = await self._replace_images(messages, fallback_model)
        except asyncio.CancelledError:
            raise
        except VisionImageExtractionError as exc:
            return self._fallback_error(f"Vision fallback image extraction failed: {exc}", response)
        except VisionModelError as exc:
            return self._fallback_error(f"Vision fallback model failed: {exc}", response)

        logger.info(
            "Model {} rejected image input; retrying with descriptions from {}",
            model,
            fallback_model,
        )
        return await provider.chat_with_retry(
            messages=rewritten,
            model=model,
            **chat_kwargs,
        )

    def _fallback_model(self) -> str | None:
        defaults = self._config.agents.defaults
        return defaults.vision_model or defaults.assistant_model

    @staticmethod
    def _contains_images(messages: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(block, dict) and block.get("type") == "image_url"
            for message in messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
        )

    @staticmethod
    def _is_vision_rejection(provider: LLMProvider, response: LLMResponse) -> bool:
        if response.finish_reason != "error" or provider.is_transient_error(response.content):
            return False
        error = (response.content or "").casefold()
        if any(marker in error for marker in _VISION_REJECTION_MARKERS):
            return True
        return "unknown variant" in error and "image_url" in error and "expected" in error and "text" in error

    @staticmethod
    def _fallback_error(message: str, original: LLMResponse) -> LLMResponse:
        detail = " ".join((original.content or "unknown provider error").split())[:500]
        return LLMResponse(
            content=f"{message}. Current model rejected image input. Original provider error: {detail}",
            finish_reason="error",
        )

    async def _replace_images(
        self,
        messages: list[dict[str, Any]],
        fallback_model: str,
    ) -> list[dict[str, Any]]:
        rewritten: list[dict[str, Any]] = []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                rewritten.append(dict(message))
                continue
            blocks: list[Any] = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "image_url":
                    blocks.append(block)
                    continue
                description = await self._describe_image(block, fallback_model)
                blocks.append(
                    {
                        "type": "text",
                        "text": f"[Image description]\n{description}",
                    }
                )
            rewritten.append({**message, "content": blocks})
        return rewritten

    async def _describe_image(self, block: dict[str, Any], fallback_model: str) -> str:
        raw = await self._read_image(block)
        cache_key = hashlib.sha256(raw).hexdigest()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached

            data_url, size_note = self._prepare_image(raw)
            provider = self._provider_resolver.resolve(fallback_model)
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": _VISION_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"Analyze this image {size_note}."},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    },
                ],
                model=fallback_model,
                reasoning_effort=None,
            )
            if response.finish_reason == "error":
                detail = " ".join((response.content or "unknown provider error").split())[:500]
                raise VisionModelError(detail)
            description = (response.content or "").strip()
            if not description:
                raise VisionModelError("fallback model returned an empty description")
            self._cache[cache_key] = description
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return description

    async def _read_image(self, block: dict[str, Any]) -> bytes:
        image_url = block.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url.startswith("data:"):
            return self._decode_data_url(url)
        if isinstance(url, str) and urlparse(url).scheme in {"http", "https"}:
            return await self._download_image(url)

        meta_path = (block.get("_meta") or {}).get("path")
        candidate = meta_path or url
        if not isinstance(candidate, str) or not candidate:
            raise VisionImageExtractionError("image block has no readable source")
        if candidate.startswith("file://"):
            candidate = urlparse(candidate).path
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            path = self._workspace / path
        try:
            if not path.is_file():
                raise VisionImageExtractionError(f"image file not found: {candidate}")
            return path.read_bytes()
        except OSError as exc:
            raise VisionImageExtractionError(f"cannot read image file: {exc}") from exc

    @staticmethod
    def _decode_data_url(url: str) -> bytes:
        try:
            header, payload = url.split(",", 1)
            if not header.casefold().startswith("data:image/"):
                raise VisionImageExtractionError("data URL is not an image")
            raw = base64.b64decode(payload, validate=True) if ";base64" in header.casefold() else unquote_to_bytes(payload)
        except (ValueError, UnicodeError) as exc:
            raise VisionImageExtractionError("invalid image data URL") from exc
        if len(raw) > _MAX_DOWNLOAD_BYTES:
            raise VisionImageExtractionError("image exceeds the fallback size limit")
        return raw

    async def _download_image(self, url: str) -> bytes:
        try:
            async with self._http.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_DOWNLOAD_BYTES:
                        raise VisionImageExtractionError(
                            "downloaded image exceeds the fallback size limit"
                        )
                    chunks.append(chunk)
        except VisionImageExtractionError:
            raise
        except httpx.HTTPError as exc:
            raise VisionImageExtractionError(f"cannot download image: {exc}") from exc
        return b"".join(chunks)

    @staticmethod
    def _prepare_image(raw: bytes) -> tuple[str, str]:
        try:
            image = Image.open(io.BytesIO(raw))
            image.load()
        except Exception as exc:
            raise VisionImageExtractionError(f"cannot open image: {exc}") from exc

        original_width, original_height = image.size
        if original_width * original_height > _MAX_PIXELS:
            scale = (_MAX_PIXELS / (original_width * original_height)) ** 0.5
            size = (max(1, int(original_width * scale)), max(1, int(original_height * scale)))
            image = image.resize(size, Image.Resampling.LANCZOS)

        image_format = image.format or "PNG"
        if image_format not in {"PNG", "JPEG", "GIF", "WEBP"}:
            image_format = "PNG"
        buffer = io.BytesIO()
        image.save(buffer, format=image_format)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        mime = detect_image_mime(buffer.getvalue()) or "image/png"
        size_note = f"({image.width}x{image.height})"
        if image.size != (original_width, original_height):
            size_note += f", down-scaled from {original_width}x{original_height}"
        return f"data:{mime};base64,{encoded}", size_note
