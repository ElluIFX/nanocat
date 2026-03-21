"""File system tools: read, write, edit, list."""

import base64
import difflib
import json
import mimetypes
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.utils.helpers import detect_image_mime

_APPROVE_HINT = (
    "\nIf you believe this action is necessary, explain the reason to the user "
    "and ask them to use /approve to temporarily bypass this check."
)


def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
    safety_check: bool = True,
) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if safety_check and allowed_dir:
        all_dirs = [allowed_dir] + (extra_allowed_dirs or [])
        if not any(_is_under(resolved, d) for d in all_dirs):
            raise PermissionError(
                f"Path {path} is outside allowed directory {allowed_dir}" + _APPROVE_HINT
            )
    return resolved


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


class _FsTool(Tool):
    """Shared base for filesystem tools — common init and path resolution."""

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        extra_allowed_dirs: list[Path] | None = None,
        safety_check: bool = True,
    ):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._extra_allowed_dirs = extra_allowed_dirs
        self._safety_check = safety_check

    def _resolve(self, path: str) -> Path:
        return _resolve_path(
            path, self._workspace, self._allowed_dir, self._extra_allowed_dirs, self._safety_check
        )


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileTool(_FsTool):
    """Read file contents with optional line-based pagination."""

    _MAX_CHARS = 128_000
    _DEFAULT_LIMIT = 2000

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Returns numbered lines. "
            "Use offset and limit to paginate through large files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to read"},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed, default 1)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default 2000)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, path: str, offset: int = 1, limit: int | None = None, **kwargs: Any
    ) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"
            if not fp.is_file():
                return f"Error: Not a file: {path}"

            all_lines = fp.read_text(encoding="utf-8").splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if total == 0:
                return json.dumps({"path": str(fp), "total_lines": 0, "content": ""})
            if offset > total:
                return f"Error: offset {offset} is beyond end of file ({total} lines)"

            start = offset - 1
            end = min(start + (limit or self._DEFAULT_LIMIT), total)
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            content = "\n".join(numbered)

            if len(content) > self._MAX_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > self._MAX_CHARS:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                content = "\n".join(trimmed)

            return json.dumps(
                {
                    "path": str(fp),
                    "total_lines": total,
                    "showing": [offset, end],
                    "truncated": end < total,
                    "next_offset": end + 1 if end < total else None,
                    "content": content,
                },
                ensure_ascii=False,
            )
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# load_image
# ---------------------------------------------------------------------------


class LoadImageTool(_FsTool):
    """Load image content from file and include EXIF metadata."""

    _MAX_BYTES = 12 * 1024 * 1024  # 12MB

    @property
    def name(self) -> str:
        return "load_image"

    @property
    def description(self) -> str:
        return (
            "Load content of an image file into your context, including its EXIF metadata. "
            "Use compress=True to downscale and compress oversized images before loading."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The image file path to load"},
                "compress": {
                    "type": "boolean",
                    "description": (
                        "Downscale the image to JPEG <= 3840x3840. Only for oversized images."
                    ),
                    "default": False,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, path: str, compress: bool = False, **kwargs: Any
    ) -> str | list[dict[str, Any]]:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"
            if not fp.is_file():
                return f"Error: Not a file: {path}"

            raw = fp.read_bytes()
            if not raw:
                return f"Error: Empty file: {path}"

            if compress:
                raw, mime = self._compress_image(raw)
            else:
                if len(raw) > self._MAX_BYTES:
                    size_mb = len(raw) / (1024 * 1024)
                    return (
                        f"Error: File too large: {path} ({size_mb:.1f} MB). "
                        f"Maximum supported size is {self._MAX_BYTES // (1024 * 1024)} MB. "
                        f"Use load_image(path, compress=True) to read it as compressed image."
                    )
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(fp))[0]

            if not mime or not mime.startswith("image/"):
                return f"Error: Unsupported or non-image file: {path}"

            exif = self._extract_exif(fp)
            b64 = base64.b64encode(raw).decode("ascii")
            meta_text = json.dumps(exif, ensure_ascii=False, indent=2)

            return [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": f"Image EXIF:\n{meta_text}"},
            ]
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading image file: {e}"

    @staticmethod
    def _compress_image(raw: bytes, max_edge: int = 3840, quality: int = 95) -> tuple[bytes, str]:
        """Downscale so longest edge <= max_edge, then JPEG-compress."""
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        # Convert palette / RGBA to RGB for JPEG
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        longest = max(w, h)
        if longest > max_edge:
            scale = max_edge / longest
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg"

    @staticmethod
    def _extract_exif(path: Path) -> dict[str, Any]:
        exif_data: dict[str, Any] = {}
        try:
            from PIL import ExifTags, Image
        except Exception:
            return {"_note": "EXIF parser unavailable (install Pillow to enable EXIF parsing)."}

        try:
            with Image.open(path) as img:
                raw_exif = img.getexif()
                if not raw_exif:
                    return {}
                tag_map = ExifTags.TAGS
                for tag_id, value in raw_exif.items():
                    name = tag_map.get(tag_id, str(tag_id))
                    exif_data[name] = str(value)
        except Exception as e:
            return {"_note": f"Failed to parse EXIF: {e}"}
        return exif_data


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class WriteFileTool(_FsTool):
    """Write content to a file."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Parent directories will be created."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            fp = self._resolve(path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return json.dumps({"path": str(fp), "bytes_written": len(content.encode("utf-8"))})
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def _find_match(content: str, old_text: str) -> tuple[str | None, int]:
    """Locate old_text in content: exact first, then line-trimmed sliding window.

    Both inputs should use LF line endings (caller normalises CRLF).
    Returns (matched_fragment, count) or (None, 0).
    """
    if old_text in content:
        return old_text, content.count(old_text)

    old_lines = old_text.splitlines()
    if not old_lines:
        return None, 0
    stripped_old = [line.strip() for line in old_lines]
    content_lines = content.splitlines()

    candidates = []
    for i in range(len(content_lines) - len(stripped_old) + 1):
        window = content_lines[i : i + len(stripped_old)]
        if [line.strip() for line in window] == stripped_old:
            candidates.append("\n".join(window))

    if candidates:
        return candidates[0], len(candidates)
    return None, 0


class EditFileTool(_FsTool):
    """Edit a file by replacing text with fallback matching."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing old_text with new_text. "
            "Supports minor whitespace/line-ending differences. "
            "Set replace_all=true to replace every occurrence."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit"},
                "old_text": {
                    "type": "string",
                    "description": "Text to find and replace (omit when using line_start/line_end)",
                },
                "new_text": {"type": "string", "description": "Replacement text"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)",
                },
                "line_start": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-indexed start line for line-range replace",
                },
                "line_end": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-indexed end line (inclusive) for line-range replace",
                },
            },
            "required": ["path", "new_text"],
        }

    async def execute(
        self,
        path: str,
        new_text: str,
        old_text: str | None = None,
        replace_all: bool = False,
        line_start: int | None = None,
        line_end: int | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"

            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")

            # Line-range replace mode
            if line_start is not None or line_end is not None:
                if line_start is None or line_end is None:
                    return "Error: both line_start and line_end are required for line-range replace"
                lines = content.splitlines(keepends=True)
                total = len(lines)
                if line_start < 1 or line_end > total or line_start > line_end:
                    return f"Error: line range {line_start}-{line_end} out of bounds (file has {total} lines)"
                norm_new = new_text.replace("\r\n", "\n")
                if norm_new and not norm_new.endswith("\n"):
                    norm_new += "\n"
                new_lines = lines[: line_start - 1] + [norm_new] + lines[line_end:]
                new_content = "".join(new_lines)
                if uses_crlf:
                    new_content = new_content.replace("\n", "\r\n")
                fp.write_bytes(new_content.encode("utf-8"))
                return json.dumps({"path": str(fp), "lines_replaced": [line_start, line_end]})

            # Text-match replace mode
            if old_text is None:
                return "Error: old_text is required when not using line_start/line_end"
            match, count = _find_match(content, old_text.replace("\r\n", "\n"))
            if match is None:
                return self._not_found_msg(old_text, content, path)
            if count > 1 and not replace_all:
                return (
                    f"Warning: old_text appears {count} times. "
                    "Provide more context to make it unique, or set replace_all=true."
                )
            norm_new = new_text.replace("\r\n", "\n")
            replacements = count if replace_all else 1
            new_content = (
                content.replace(match, norm_new)
                if replace_all
                else content.replace(match, norm_new, 1)
            )
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")
            fp.write_bytes(new_content.encode("utf-8"))
            return json.dumps({"path": str(fp), "replacements": replacements})
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"

    @staticmethod
    def _not_found_msg(old_text: str, content: str, path: str) -> str:
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)

        best_ratio, best_start = 0.0, 0
        for i in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, i

        if best_ratio > 0.5:
            diff = "\n".join(
                difflib.unified_diff(
                    old_lines,
                    lines[best_start : best_start + window],
                    fromfile="old_text (provided)",
                    tofile=f"{path} (actual, line {best_start + 1})",
                    lineterm="",
                )
            )
            return f"Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
        return (
            f"Error: old_text not found in {path}. No similar text found. Verify the file content."
        )


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


class ListDirTool(_FsTool):
    """List directory contents with optional recursion."""

    _DEFAULT_MAX = 200
    _IGNORE_DIRS = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".coverage",
        "htmlcov",
    }

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. "
            "Set recursive=true to explore nested structure. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The directory path to list"},
                "recursive": {
                    "type": "boolean",
                    "description": "Recursively list all files (default false)",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum entries to return (default 200)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        recursive: bool = False,
        max_entries: int | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            dp = self._resolve(path)
            if not dp.exists():
                return f"Error: Directory not found: {path}"
            if not dp.is_dir():
                return f"Error: Not a directory: {path}"

            cap = max_entries or self._DEFAULT_MAX
            items: list[str] = []
            total = 0

            if recursive:
                for item in sorted(dp.rglob("*")):
                    if any(p in self._IGNORE_DIRS for p in item.parts):
                        continue
                    total += 1
                    if len(items) < cap:
                        rel = item.relative_to(dp)
                        items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                for item in sorted(dp.iterdir()):
                    if item.name in self._IGNORE_DIRS:
                        continue
                    total += 1
                    if len(items) < cap:
                        pfx = "📁 " if item.is_dir() else "📄 "
                        items.append(f"{pfx}{item.name}")

            if not items and total == 0:
                return json.dumps({"path": str(dp), "entries": [], "total": 0, "truncated": False})

            return json.dumps(
                {
                    "path": str(dp),
                    "entries": items,
                    "total": total,
                    "truncated": total > cap,
                }
            )
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"


# ---------------------------------------------------------------------------
# grep_file
# ---------------------------------------------------------------------------


class GrepFileTool(_FsTool):
    """Search file content with regex."""

    @property
    def name(self) -> str:
        return "grep_file"

    @property
    def description(self) -> str:
        return "Search a file with a regex pattern. Returns matching line numbers and content, with optional surrounding context."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to search"},
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Lines of context before/after each match (default 0)",
                },
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Max matches to return (default 50)",
                },
            },
            "required": ["path", "pattern"],
        }

    async def execute(
        self, path: str, pattern: str, context_lines: int = 0, max_matches: int = 50, **kwargs: Any
    ) -> str:
        import re as _re

        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"
            lines = fp.read_text(encoding="utf-8").splitlines()
            rx = _re.compile(pattern)
        except PermissionError as e:
            return f"Error: {e}"
        except _re.error as e:
            return f"Error: Invalid regex: {e}"
        except Exception as e:
            return f"Error: {e}"

        results: list[dict[str, Any]] = []
        seen: set[int] = set()
        match_count = 0

        for i, line in enumerate(lines):
            if rx.search(line):
                match_count += 1
                if match_count > max_matches:
                    break
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                for j in range(start, end):
                    if j not in seen:
                        seen.add(j)
                        results.append(
                            {
                                "line": j + 1,
                                "content": lines[j],
                                "match": j == i,
                            }
                        )

        truncated = match_count > max_matches
        return json.dumps(
            {
                "path": str(fp),
                "pattern": pattern,
                "matches": match_count if not truncated else f"{max_matches}+",
                "truncated": truncated,
                "results": results,
            },
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# insert_lines
# ---------------------------------------------------------------------------


class InsertLinesTool(_FsTool):
    """Insert text before or after a given line number."""

    @property
    def name(self) -> str:
        return "insert_lines"

    @property
    def description(self) -> str:
        return "Insert text before or after a specific line number in a file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-indexed line number to insert relative to",
                },
                "text": {
                    "type": "string",
                    "description": "Text to insert (newline appended if missing)",
                },
                "after": {
                    "type": "boolean",
                    "description": "Insert after the line (default true); false = insert before",
                },
            },
            "required": ["path", "line", "text"],
        }

    async def execute(
        self, path: str, line: int, text: str, after: bool = True, **kwargs: Any
    ) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"
            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            lines = raw.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True)
            total = len(lines)
            if line < 1 or line > total:
                return f"Error: line {line} out of bounds (file has {total} lines)"
            insert_text = text.replace("\r\n", "\n")
            if not insert_text.endswith("\n"):
                insert_text += "\n"
            idx = line if after else line - 1
            lines.insert(idx, insert_text)
            new_content = "".join(lines)
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")
            fp.write_bytes(new_content.encode("utf-8"))
            return json.dumps({"path": str(fp), "inserted_at": line, "after": after})
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error inserting lines: {e}"


# ---------------------------------------------------------------------------
# delete_lines
# ---------------------------------------------------------------------------


class DeleteLinesTool(_FsTool):
    """Delete a range of lines from a file."""

    @property
    def name(self) -> str:
        return "delete_lines"

    @property
    def description(self) -> str:
        return "Delete a range of lines from a file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "line_start": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-indexed first line to delete",
                },
                "line_end": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-indexed last line to delete (inclusive)",
                },
            },
            "required": ["path", "line_start", "line_end"],
        }

    async def execute(self, path: str, line_start: int, line_end: int, **kwargs: Any) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"
            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            lines = raw.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True)
            total = len(lines)
            if line_start < 1 or line_end > total or line_start > line_end:
                return f"Error: line range {line_start}-{line_end} out of bounds (file has {total} lines)"
            del lines[line_start - 1 : line_end]
            new_content = "".join(lines)
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")
            fp.write_bytes(new_content.encode("utf-8"))
            return json.dumps(
                {
                    "path": str(fp),
                    "deleted_lines": [line_start, line_end],
                    "lines_remaining": len(lines),
                }
            )
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error deleting lines: {e}"


# ---------------------------------------------------------------------------
# file_hex
# ---------------------------------------------------------------------------


class FileHexTool(_FsTool):
    """Read or write raw bytes at a given offset, with hex+ASCII display."""

    _DEFAULT_LENGTH = 256
    _MAX_LENGTH = 4096

    @property
    def name(self) -> str:
        return "file_hex"

    @property
    def description(self) -> str:
        return (
            "Read or write raw bytes from/to a file at a byte offset. "
            "Read mode returns a hex dump with ASCII side-by-side. "
            "Write mode accepts hex string and patches bytes in-place."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "mode": {
                    "type": "string",
                    "enum": ["read", "write"],
                    "description": "read or write (default read)",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Byte offset (default 0)",
                },
                "length": {
                    "type": "integer",
                    "minimum": 1,
                    "description": f"Bytes to read (default {self._DEFAULT_LENGTH}, max {self._MAX_LENGTH})",
                },
                "hex_data": {
                    "type": "string",
                    "description": "Hex string to write (write mode only, e.g. 'deadbeef')",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        mode: str = "read",
        offset: int = 0,
        length: int | None = None,
        hex_data: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            fp = self._resolve(path)
            if mode == "write":
                if not hex_data:
                    return "Error: hex_data is required for write mode"
                hex_data = hex_data.replace(" ", "").replace("\n", "")
                try:
                    data = bytes.fromhex(hex_data)
                except ValueError as e:
                    return f"Error: invalid hex_data: {e}"
                if not fp.exists():
                    return f"Error: File not found: {path}"
                raw = bytearray(fp.read_bytes())
                end = offset + len(data)
                if end > len(raw):
                    raw.extend(b"\x00" * (end - len(raw)))
                raw[offset:end] = data
                fp.write_bytes(bytes(raw))
                return json.dumps({"path": str(fp), "offset": offset, "bytes_written": len(data)})

            # read mode
            if not fp.exists():
                return f"Error: File not found: {path}"
            n = min(length or self._DEFAULT_LENGTH, self._MAX_LENGTH)
            raw = fp.read_bytes()
            file_size = len(raw)
            chunk = raw[offset : offset + n]
            actual = len(chunk)

            lines = []
            for i in range(0, actual, 16):
                row = chunk[i : i + 16]
                addr = f"{offset + i:08x}"
                hex_part = " ".join(f"{b:02x}" for b in row)
                hex_part = f"{hex_part:<47}"
                ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
                lines.append(f"{addr}  {hex_part}  {ascii_part}")

            return json.dumps(
                {
                    "path": str(fp),
                    "offset": offset,
                    "length": actual,
                    "file_size": file_size,
                    "truncated": (offset + actual) < file_size,
                    "next_offset": offset + actual if (offset + actual) < file_size else None,
                    "hex_dump": "\n".join(lines),
                },
                ensure_ascii=False,
            )
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error in file_hex: {e}"
