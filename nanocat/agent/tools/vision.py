"""ParseImageTool and shared image‑parsing logic.

Used when the main model lacks native vision (e.g. DeepSeek).  Loads a local
image, down‑scales to a ~4K pixel budget, and asks a vision‑capable model for
a structured analysis.
"""

from __future__ import annotations

import base64
import io
from datetime import datetime
from typing import Any

# Soft import — Pillow is an optional dependency.
try:
    from PIL import Image

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    Image = None  # type: ignore[assignment]

from nanocat.agent.tools.base import Tool, tool_err, tool_ok
from nanocat.utils.helpers import detect_image_mime

# Max total pixels before down‑scaling (~4K resolution budget).
_MAX_PIXELS = 3840 * 2160

_PARSE_PROMPT = """You are a visual analysis assistant. Analyze the provided image and return a structured report in the following format. Be precise about positions; prefer concrete descriptions over vague ones.

## Transparency rule
For every region or element you describe, explicitly note the level of detail you provided. Use one of these tags at the end of each description:
- **[full]** — every readable detail in this region is captured
- **[partial]** — representative sampling; some minor details omitted
- **[summary]** — only high-level structure sketched; many details omitted

When the caller provides a focus hint, prioritize the requested area with **[full]** coverage.

## Output Format

### Summary
A concise 1-3 sentence description of what this image contains.

### Structure
Describe the layout and visual structure:
- Key regions or elements and their positions (e.g. "top-left", "center", "bottom third") followed by the transparency tag
- Their shapes or visual characteristics (e.g. "rectangular card", "circular avatar", "text block", "bar chart", "code editor panel")
- Visual hierarchy, grouping, or spatial relationships between elements

### Content
Extract and present all readable text content. Preserve logical structure:
- Use markdown tables for tabular data
- Use bullet/numbered lists for listed items
- Use code blocks (with language tag when inferable) for code snippets
- Use blockquotes for quoted passages
- For forms or structured documents, describe field labels and values
- For diagrams/charts, describe what they represent rather than attempting ASCII art

If the image contains no meaningful text (e.g. a photograph, abstract art), describe the visual content instead.  Never invent text that is not visibly present.

### Notes (optional)
Any additional observations: visual anomalies, quality issues, inferred context, or anything else notable.  Also note here which major regions received only **[summary]** coverage so the caller can decide whether to request a focused re‑read.
"""


def _downscale_image(img: Image.Image, raw: bytes) -> tuple[Image.Image, str]:
    """Down‑scale *img* if total pixels exceed _MAX_PIXELS.  Returns (scaled_img, mime)."""
    original_w, original_h = img.size
    total = original_w * original_h
    if total <= _MAX_PIXELS:
        return img, detect_image_mime(raw) or "image/png"

    scale = (_MAX_PIXELS / total) ** 0.5
    new_w = max(1, int(original_w * scale))
    new_h = max(1, int(original_h * scale))
    try:
        resample = Image.Resampling.LANCZOS  # Pillow >= 10
    except AttributeError:
        resample = Image.LANCZOS  # type: ignore[attr-defined]
    return img.resize((new_w, new_h), resample), detect_image_mime(raw) or "image/png"


def _encode_data_url(img: Image.Image, raw: bytes) -> str:
    """Encode *img* as a base64 data URL, preserving the original format."""
    fmt = img.format or "PNG"
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    mime = detect_image_mime(raw) or "image/png"
    if fmt == "JPEG":
        mime = "image/jpeg"
    elif fmt == "GIF":
        mime = "image/gif"
    elif fmt == "WEBP":
        mime = "image/webp"
    return f"data:{mime};base64,{b64}"


async def parse_image_via_model(
    path: str,
    workspace: str | None = None,
    focus: str | None = None,
) -> str:
    """Load, scale, and parse an image through the configured vision model.

    When *focus* is given, it is injected as an additional instruction to
    concentrate on that specific area/element/aspect of the image.
    """
    from pathlib import Path as _Path

    from nanocat.config.loader import get_runtime_config
    from nanocat.providers.manager import get_provider

    if not _PIL_AVAILABLE:
        return tool_err(
            "Pillow is not installed. Install it with 'pip install Pillow' to use image parsing."
        )

    img_path = _Path(path).expanduser()
    if not img_path.is_absolute() and workspace:
        img_path = _Path(workspace) / path
    img_path = img_path.resolve()

    if not img_path.is_file():
        return tool_err(f"file not found: {path}")

    try:
        raw = img_path.read_bytes()
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        return tool_err(f"cannot open image: {e}")

    original_w, original_h = img.size
    img, _mime = _downscale_image(img, raw)
    data_url = _encode_data_url(img, raw)

    cfg = get_runtime_config().agents.defaults
    vision_model = cfg.vision_model or cfg.assistant_model or cfg.model
    if "deepseek" in vision_model.lower():
        for m in cfg.model_choice:
            if "deepseek" not in m.lower():
                vision_model = m
                break
    provider = get_provider(vision_model)

    size_note = f"({img.size[0]}x{img.size[1]})"
    if (original_w * original_h) > _MAX_PIXELS:
        size_note += f", down-scaled from {original_w}x{original_h}"

    user_text = f"Analyze this image {size_note}."
    if focus:
        user_text += f"\n\nFocus requirement: {focus}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PARSE_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    try:
        response = await provider.chat_with_retry(
            messages=messages,
            model=vision_model,
            max_tokens=4096,
            temperature=0.3,
        )
        if response.content:
            return tool_ok(content=response.content.strip())
        return tool_err("vision model returned empty response.")
    except Exception as e:
        return tool_err(f"Error calling vision model: {e}")


class ParseImageTool(Tool):
    """Explicit tool for parsing images through a vision-capable model.

    The LLM can call this directly when it knows it lacks vision capability.
    Use it independently or in conjunction with the adaptive load_image tool.
    """

    def __init__(self, workspace: str | None = None):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "parse_image"

    @property
    def description(self) -> str:
        return (
            "If you do not have vision capabilities, use this tool to parse and "
            "understand an image when a user provides one or when you need to "
            "read an image file. Returns a structured analysis: summary, layout "
            "description, and extracted text/markdown content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path to the image file.",
                },
                "focus": {
                    "type": "string",
                    "description": (
                        "Leave empty on first read. On subsequent reads, specify what "
                        "region, element, or aspect of the image to analyze in greater "
                        "detail, and your desired return format if needed."
                    ),
                },
            },
            "required": ["path"],
        }

    async def execute(self, **kwargs: Any) -> str:
        path: str = kwargs.get("path", "")
        if not path:
            return tool_err("'path' parameter is required.")
        focus: str | None = kwargs.get("focus") or None
        return await parse_image_via_model(path, self._workspace, focus=focus)


class ScreenshotTool(Tool):
    """Capture the screen to an image file the agent can then view via
    load_image / parse_image. Uses Pillow's ImageGrab (Windows/macOS; on Linux
    it needs a running X server / scrot)."""

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return (
            "Capture the screen to a PNG and return its path; then use load_image or "
            "parse_image to view it. Optionally capture a [x1,y1,x2,y2] region. "
            "Works on Windows/macOS (Linux needs an X grabber)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional [x1, y1, x2, y2] box; omit for the full screen",
                },
            },
            "required": [],
        }

    async def execute(self, region: list[int] | None = None, **kwargs: Any) -> str:
        try:
            from PIL import ImageGrab
        except Exception as e:
            return tool_err(f"Pillow ImageGrab unavailable: {e}")

        from nanocat.config.paths import get_media_dir

        bbox = (region[0], region[1], region[2], region[3]) if region and len(region) == 4 else None
        try:
            try:
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                img = ImageGrab.grab(bbox=bbox)  # older Pillow / non-Windows
        except Exception as e:
            return tool_err(f"screenshot failed: {e}")

        path = get_media_dir("screenshot") / f"{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        try:
            img.save(path)
        except Exception as e:
            return tool_err(f"failed to save screenshot: {e}")

        return tool_ok(
            saved_to=str(path),
            width=img.width,
            height=img.height,
            hint="use load_image(path) or parse_image(path) to view it",
        )
