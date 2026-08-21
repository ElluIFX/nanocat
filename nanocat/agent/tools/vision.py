"""Screen capture tool."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nanocat.agent.tools.base import Tool, tool_err, tool_ok


class ScreenshotTool(Tool):
    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return (
            "Capture the screen to a PNG and return its path; then use load_image "
            "to view it. Optionally capture a [x1,y1,x2,y2] region. "
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
        except Exception as exc:
            return tool_err(f"Pillow ImageGrab unavailable: {exc}")

        from nanocat.config.paths import get_media_dir

        bbox = (
            (region[0], region[1], region[2], region[3])
            if region and len(region) == 4
            else None
        )
        try:
            try:
                image = ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                image = ImageGrab.grab(bbox=bbox)
        except Exception as exc:
            return tool_err(f"screenshot failed: {exc}")

        path = get_media_dir("screenshot") / f"{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        try:
            image.save(path)
        except Exception as exc:
            return tool_err(f"failed to save screenshot: {exc}")

        return tool_ok(
            saved_to=str(path),
            width=image.width,
            height=image.height,
            hint="use load_image(path) to view it",
        )
