"""Render structured application data as channel-neutral Markdown."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "session_token",
        "token",
    }
)
_OMIT_KEYS = frozenset({"embedding", "embedding_vector", "raw_response", "raw_json"})
_MAX_ITEMS = 20
_MAX_DEPTH = 4
_DEFAULT_MAX_CHARS = 16_000


def _normalize_key(key: Any) -> str:
    normalized = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", str(key).strip())
    return normalized.replace("-", "_").replace(" ", "_").lower()


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SENSITIVE_KEYS or normalized.endswith("_api_key")


def _is_omitted_key(key: Any) -> bool:
    return _normalize_key(key) in _OMIT_KEYS


def _label(key: Any) -> str:
    text = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", str(key).replace("_", " ").replace("-", " "))
    return text.strip().capitalize() or "Value"


def _escape_inline(value: str) -> str:
    text = " ".join(value.split())
    for marker in ("\\", "`", "*", "_", "[", "]", "<", ">", "|"):
        text = text.replace(marker, f"\\{marker}")
    return text


def _scalar(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        if "\n" in value or "\r" in value:
            body = value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
            return f"~~~text\n{body}\n~~~"
        return _escape_inline(value)
    return _escape_inline(str(value))


def _is_scalar_value(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _render_value(value: Any, *, level: int) -> str:
    if _is_scalar_value(value):
        return _scalar(value)
    if level > _MAX_DEPTH:
        return "_Nested value omitted._"
    if isinstance(value, Mapping):
        return _render_mapping(value, level=level)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        if not items:
            return "—"
        if all(_is_scalar_value(item) for item in items):
            visible = items[:_MAX_ITEMS]
            lines = [f"- {_scalar(item)}" for item in visible]
            if len(items) > _MAX_ITEMS:
                lines.append(f"- _… and {len(items) - _MAX_ITEMS} more items._")
            return "\n".join(lines)

        blocks: list[str] = []
        for index, item in enumerate(items[:_MAX_ITEMS], start=1):
            heading = "#" * min(6, level + 3)
            blocks.append(f"{heading} Item {index}\n\n{_render_value(item, level=level + 1)}")
        if len(items) > _MAX_ITEMS:
            blocks.append(f"_… and {len(items) - _MAX_ITEMS} more items._")
        return "\n\n".join(blocks)
    return _scalar(value)


def _render_mapping(value: Mapping[Any, Any], *, level: int) -> str:
    scalar_lines: list[str] = []
    nested_blocks: list[str] = []
    for key, item in value.items():
        if _is_omitted_key(key):
            continue
        if _is_sensitive_key(key):
            item = "redacted"
        if isinstance(item, str) and ("\n" in item or "\r" in item):
            scalar_lines.append(f"- **{_label(key)}**:\n\n{_scalar(item)}")
            continue
        if _is_scalar_value(item):
            scalar_lines.append(f"- **{_label(key)}**: {_scalar(item)}")
            continue
        if level >= _MAX_DEPTH:
            scalar_lines.append(f"- **{_label(key)}**: _Nested value omitted._")
            continue
        heading = "#" * min(6, level + 3)
        rendered_item = _render_value(item, level=level + 1)
        if not rendered_item:
            continue
        nested_blocks.append(
            f"{heading} {_label(key)}\n\n{rendered_item}"
        )
    return "\n".join(scalar_lines) + ("\n\n" if scalar_lines and nested_blocks else "") + "\n\n".join(nested_blocks)


def render_structured(data: Any, *, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """Render JSON-like data into readable Markdown without exposing secrets."""
    rendered = _render_value(data, level=0).strip()
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 38].rstrip() + "\n\n_… output truncated by NanoCat._"
