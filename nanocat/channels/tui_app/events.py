"""Display-event helpers shared by the TUI channel and its Textual app."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanocat.bus.events import OutboundMessage

_INTERVENTION_FIELD_RE = re.compile(
    r"^(Capability|Tool|Parameters|Operation|Expires):\s*(.*)$", re.MULTILINE
)
_SENSITIVE_DISPLAY_RE = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|token|api[-_ ]?key)\s*[:=]\s*[^\s,;]+"
)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def parse_subagent_result(text: str) -> dict[str, Any] | None:
    """Return the parsed announce dict if *text* is a subagent result JSON, else None."""
    s = text.lstrip()
    if not s.startswith("{") or "subagent_id" not in s:
        return None
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) and "subagent_id" in obj and "result" in obj else None


def flatten_content(content: Any) -> str:
    """Flatten a history message's content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


def grab_clipboard_images() -> list[str]:
    """Return file paths for any image currently on the OS clipboard.

    A raw bitmap (e.g. from a screenshot tool) is written as PNG into the TUI
    media dir; copied image *files* (from a file manager) are used in place.
    Returns [] when the clipboard holds no image (plain text, empty, …).
    """
    try:
        from PIL import Image, ImageGrab

        from nanocat.config.paths import get_media_dir
    except Exception:
        return []
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return []
    if isinstance(data, Image.Image):
        out = get_media_dir("tui") / f"paste_{uuid.uuid4().hex[:8]}.png"
        try:
            data.save(out, "PNG")
        except Exception:
            return []
        return [str(out)]
    if isinstance(data, list):
        return [
            str(p)
            for f in data
            if (p := Path(str(f))).is_file() and p.suffix.lower() in _IMAGE_EXTS
        ]
    return []


def summarize_args(args: Any, limit: int = 64) -> str:
    """One-line preview of tool-call arguments for the chat pane."""
    if not isinstance(args, dict) or not args:
        return ""
    if len(args) == 1:
        value = next(iter(args.values()))
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    else:
        text = json.dumps(args, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def redact_intervention_text(value: Any, limit: int = 240) -> str:
    """Keep scheduler-provided approval facts safe for terminal rendering."""
    text = str(value or "").strip()
    text = _SENSITIVE_DISPLAY_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text[: limit - 1] + "…" if len(text) > limit else text


def intervention_payload(msg: OutboundMessage) -> dict[str, Any] | None:
    """Normalize scheduler-owned intervention metadata for the TUI bridge."""
    metadata = msg.metadata or {}
    request_id = str(metadata.get("request_id") or "").strip()
    mode = str(metadata.get("_intervention_mode") or "").strip().lower()
    if mode not in {"auto", "yolo"}:
        mode = ""
    if not request_id and not mode:
        return None
    if metadata.get("_intervention_update"):
        return {
            "request_id": request_id,
            "state": str(metadata.get("intervention_state") or "updated"),
            "detail": redact_intervention_text(msg.content),
            "mode": mode,
        }

    fields = dict(_INTERVENTION_FIELD_RE.findall(msg.content or ""))
    raw_expiry = str(metadata.get("expires_at") or "").strip()
    expiry_text = raw_expiry or str(fields.get("Expires") or "").strip()
    expires_at: datetime | None = None
    if expiry_text:
        try:
            expires_at = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = expires_at.astimezone(timezone.utc)
        except ValueError:
            try:
                expires_at = datetime.strptime(expiry_text, "%Y-%m-%d %H:%M:%S UTC").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                expires_at = None
    return {
        "request_id": request_id,
        "capability": redact_intervention_text(
            metadata.get("capability") or fields.get("Capability")
        ),
        "operation": redact_intervention_text(
            metadata.get("operation") or fields.get("Operation")
        ),
        "tool_name": redact_intervention_text(metadata.get("tool_name") or fields.get("Tool")),
        "tool_params": redact_intervention_text(
            metadata.get("tool_params") or fields.get("Parameters"),
            limit=2_000,
        ),
        "approval_flow": str(metadata.get("approval_flow") or "manual"),
        "review_decision": str(metadata.get("review_decision") or ""),
        "review_reason": redact_intervention_text(metadata.get("review_reason")),
        "allowed_actions": tuple(str(a) for a in (metadata.get("allowed_actions") or ())),
        "expires": redact_intervention_text(fields.get("Expires") or raw_expiry),
        "expires_at": expires_at,
        "state": "pending",
        "mode": mode,
    }
