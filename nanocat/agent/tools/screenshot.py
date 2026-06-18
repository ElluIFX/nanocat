"""Screenshot tool: capture the screen to an image file the agent can then view
via load_image / parse_image. Uses Pillow's ImageGrab (Windows/macOS; on Linux
it needs a running X server / scrot)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from nanocat.agent.tools.base import Tool


class ScreenshotTool(Tool):
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
            return f"Error: Pillow ImageGrab unavailable: {e}"

        from nanocat.config.paths import get_media_dir

        bbox = (region[0], region[1], region[2], region[3]) if region and len(region) == 4 else None
        try:
            try:
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                img = ImageGrab.grab(bbox=bbox)  # older Pillow / non-Windows
        except Exception as e:
            return f"Error: screenshot failed: {e}"

        path = get_media_dir("screenshot") / f"{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        try:
            img.save(path)
        except Exception as e:
            return f"Error: failed to save screenshot: {e}"

        return json.dumps(
            {
                "saved_to": str(path),
                "width": img.width,
                "height": img.height,
                "hint": "use load_image(path) or parse_image(path) to view it",
            },
            ensure_ascii=False,
        )
