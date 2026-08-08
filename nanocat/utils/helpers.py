"""Utility functions for NanoCat."""

import json
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import tiktoken


def detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes, ignoring file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_time_str(timezone: bool = True) -> str:
    """Human-readable current time with weekday and timezone, e.g. '2026-03-15 22:30 (Saturday) (CST)'."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S (%A)")
    if timezone:
        tz = time.strftime("%Z") or "UTC"
        return f"{now} ({tz})"
    return now


def split_message(content: str, max_len: int = 2000) -> list[str]:
    """
    Split content into chunks within max_len, preferring line breaks.

    Args:
        content: The text content to split.
        max_len: Maximum length per chunk (default 2000 for Discord compatibility).

    Returns:
        List of message chunks, each within max_len.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        # Try to break at newline first, then space, then hard break
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def build_assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a provider-safe assistant message with optional reasoning fields."""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    return msg


def _normalize_for_token_estimate(value: Any) -> Any:
    """Keep provider-visible structure while bounding non-text media cost."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"timestamp", "_meta"}:
                continue
            if key == "image_url" and isinstance(item, dict):
                result[key] = {"url": "[image omitted]"}
                result["_estimated_image_tokens"] = 1024
                continue
            result[key] = _normalize_for_token_estimate(item)
        return result
    if isinstance(value, list):
        return [_normalize_for_token_estimate(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _token_estimate_payload(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> str:
    payload = {
        "messages": _normalize_for_token_estimate(messages),
        "tools": _normalize_for_token_estimate(tools or []),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def estimate_prompt_tokens_fast(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Return a bounded O(n) estimate without tokenizer or network calls."""
    payload = _token_estimate_payload(messages, tools)
    image_tokens = sum(
        1024
        for message in messages
        for block in (message.get("content") or [],)
        if isinstance(block, list)
        for item in block
        if isinstance(item, dict) and item.get("type") == "image_url"
    )
    return max(1, (len(payload) + 3) // 4 + image_tokens)


@lru_cache(maxsize=1)
def _cl100k_encoding():
    return tiktoken.get_encoding("cl100k_base")


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens with tiktoken."""
    try:
        enc = _cl100k_encoding()
        return len(enc.encode(_token_estimate_payload(messages, tools)))
    except Exception:
        return estimate_prompt_tokens_fast(messages, tools)


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Estimate prompt tokens via provider counter first, then tiktoken fallback."""
    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        try:
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                return int(tokens), str(source or "provider_counter")
        except Exception:
            pass

    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        return int(estimated), "tiktoken"
    return 0, "none"


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files

    try:
        tpl = pkg_files("nanocat") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    def _copytree(src, dest: Path):
        dest.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            target = dest / child.name
            if child.is_dir():
                _copytree(child, target)
            else:
                target.write_bytes(child.read_bytes())

    for item in tpl.iterdir():
        if item.name.endswith(".md") and not item.name.startswith("."):
            _write(item, workspace / item.name)
    _write(tpl / "MEMORY.md", workspace / "MEMORY.md")

    skills_dst = workspace / "skills"
    skills_dst.mkdir(exist_ok=True)
    skills_src = tpl / "skills"
    if skills_src.is_dir():
        for skill in skills_src.iterdir():
            if skill.is_dir() and not (skills_dst / skill.name).exists():
                _copytree(skill, skills_dst / skill.name)
                added.append(str((skills_dst / skill.name).relative_to(workspace)))

    if added and not silent:
        for name in added:
            print(f"Created {name}")
    return added
