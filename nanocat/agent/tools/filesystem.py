"""File system tools: read, write, edit, list."""

import base64
import difflib
import glob
import json
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any

from nanocat.agent.tools.base import Tool, tool_err, tool_ok
from nanocat.utils.helpers import detect_image_mime

_err = tool_err  # local alias to keep error call sites short


def _resolve_path(
    path: str,
    workspace: Path | None = None,
) -> Path:
    """Resolve a path against the workspace without applying security policy."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    return p.resolve()


class _FsTool(Tool):
    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    def _resolve(self, path: str) -> Path:
        return _resolve_path(path, self._workspace)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileTool(_FsTool):
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
                    "default": 1,
                    "description": "Line number to start reading from (1-indexed)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "default": 2000,
                    "description": "Maximum number of lines to read",
                    "minimum": 1,
                },
                "encoding": {
                    "type": "string",
                    "default": "utf-8",
                    "description": "Text encoding (python-style)",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
        encoding: str = "utf-8",
        **kwargs: Any,
    ) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return _err(f"File not found: {path}")
            if not fp.is_file():
                return _err(f"Not a file: {path}")

            all_lines = fp.read_text(encoding=encoding).splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if total == 0:
                return tool_ok(total_lines=0, content="")
            if offset > total:
                return _err(f"offset {offset} is beyond end of file ({total} lines)")

            start = offset - 1
            end = min(start + (limit or self._DEFAULT_LIMIT), total)
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            content = "\n".join(numbered)

            return tool_ok(
                total_lines=total,
                showing=[offset, end],
                content=content,
            )
        except PermissionError as e:
            return _err(str(e))
        except (UnicodeDecodeError, LookupError) as e:
            return _err(
                f"cannot decode {path} as {encoding!r}: {e}",
                "Try another encoding (e.g. encoding='gbk' for Simplified Chinese, "
                "'big5', or 'latin-1').",
            )
        except Exception as e:
            return _err(f"Error reading file: {e}")


# ---------------------------------------------------------------------------
# load_image
# ---------------------------------------------------------------------------


class LoadImageTool(_FsTool):
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
        fp = self._resolve(path)
        if not fp.exists():
            return _err(f"File not found: {path}")
        if not fp.is_file():
            return _err(f"Not a file: {path}")

        try:
            raw = fp.read_bytes()
            if not raw:
                return _err(f"Empty file: {path}")

            if compress:
                raw, mime = self._compress_image(raw)
            else:
                if len(raw) > self._MAX_BYTES:
                    size_mb = len(raw) / (1024 * 1024)
                    return _err(
                        f"File too large: {path} ({size_mb:.1f} MB > "
                        f"{self._MAX_BYTES // (1024 * 1024)} MB).",
                        "Retry with compress=true.",
                    )
                mime = detect_image_mime(raw) or mimetypes.guess_type(str(fp))[0]

            if not mime or not mime.startswith("image/"):
                return _err(f"Unsupported or non-image file: {path}")

            exif = self._extract_exif(fp)
            b64 = base64.b64encode(raw).decode("ascii")
            meta_text = json.dumps(exif, ensure_ascii=False, indent=2)

            return [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": f"Image EXIF:\n{meta_text}"},
            ]
        except PermissionError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Error reading image file: {e}")

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
            img = img.resize((new_w, new_h), Image.LANCZOS)  # pyright: ignore[reportAttributeAccessIssue]

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
            return tool_ok(bytes_written=len(content.encode("utf-8")))
        except PermissionError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Error writing file: {e}")


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


def _py_syntax_error(text: str) -> str | None:
    """Return a short description if *text* is not valid Python, else None."""
    import ast

    try:
        ast.parse(text)
        return None
    except SyntaxError as e:
        loc = f"line {e.lineno}" + (f":{e.offset}" if e.offset else "")
        return f"{loc}: {e.msg}"


class EditFileTool(_FsTool):
    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing old_text with new_text (text-match), or a line range "
            "(line_start/line_end). Supports minor whitespace/line-ending differences. "
            "Line numbers shift after any edit: in line-range mode also pass old_text so the "
            "tool verifies those lines still match and rejects the edit (asking you to re-read) "
            "if they don't. For .py files, an edit that would introduce a syntax error into a "
            "previously-valid file is rejected. Set replace_all=true to replace every occurrence."
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
                    "default": False,
                    "description": "Replace all occurrences",
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
                return _err(f"File not found: {path}")

            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")

            # Line-range replace mode
            if line_start is not None or line_end is not None:
                if line_start is None or line_end is None:
                    return _err("both line_start and line_end are required for line-range replace")
                lines = content.splitlines(keepends=True)
                total = len(lines)
                if line_start < 1 or line_end > total or line_start > line_end:
                    return _err(
                        f"line range {line_start}-{line_end} out of bounds (file has {total} lines).",
                        "Line numbers shift after any prior edit — re-read the file, or pass old_text "
                        "to match by content instead of line numbers.",
                    )
                # Guard against stale line numbers: if the caller states what those lines
                # currently hold (old_text), verify it before overwriting blindly.
                if old_text is not None:
                    target = "".join(lines[line_start - 1 : line_end])
                    if target.strip() != old_text.replace("\r\n", "\n").strip():
                        return _err(
                            f"lines {line_start}-{line_end} no longer match old_text; "
                            "the file changed since it was read.",
                            "Re-read the file to get current line numbers and content, then retry.",
                        )
                norm_new = new_text.replace("\r\n", "\n")
                if norm_new and not norm_new.endswith("\n"):
                    norm_new += "\n"
                lf_content = "".join(lines[: line_start - 1] + [norm_new] + lines[line_end:])
                new_content = lf_content.replace("\n", "\r\n") if uses_crlf else lf_content
                fp.write_bytes(new_content.encode("utf-8"))
                new_total = len(lf_content.splitlines())
                return tool_ok(
                    lines_replaced=[line_start, line_end],
                    new_total_lines=new_total,
                    line_delta=new_total - total,
                    py_syntax_error=_py_syntax_error(lf_content) if fp.suffix == ".py" else None,
                )

            # Text-match replace mode
            if old_text is None:
                return _err("old_text is required when not using line_start/line_end")
            match, count = _find_match(content, old_text.replace("\r\n", "\n"))
            if match is None:
                return self._not_found_msg(old_text, content, path)
            if count > 1 and not replace_all:
                return _err(
                    f"old_text appears {count} times.",
                    "Provide more context to make it unique, or set replace_all=true.",
                )
            norm_new = new_text.replace("\r\n", "\n")
            replacements = count if replace_all else 1
            lf_content = (
                content.replace(match, norm_new)
                if replace_all
                else content.replace(match, norm_new, 1)
            )
            new_content = lf_content.replace("\n", "\r\n") if uses_crlf else lf_content
            fp.write_bytes(new_content.encode("utf-8"))
            return tool_ok(
                replacements=replacements,
                new_total_lines=len(lf_content.splitlines()),
                py_syntax_error=_py_syntax_error(lf_content) if fp.suffix == ".py" else None,
            )
        except PermissionError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Error editing file: {e}")

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
            return _err(
                f"old_text not found in {path}.",
                f"Best match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}",
            )
        return _err(
            f"old_text not found in {path}. No similar text found. Verify the file content."
        )


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


class ListDirTool(_FsTool):
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
                    "default": False,
                    "description": "Recursively list all files",
                },
                "max_entries": {
                    "type": "integer",
                    "default": 200,
                    "description": "Maximum entries to return",
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
                return _err(f"Directory not found: {path}")
            if not dp.is_dir():
                return _err(f"Not a directory: {path}")

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
                return tool_ok(entries=[], total=0)

            return tool_ok(entries=items, total=total, showing=len(items))
        except PermissionError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Error listing directory: {e}")


# ---------------------------------------------------------------------------
# grep_file
# ---------------------------------------------------------------------------


class GrepFileTool(_FsTool):
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
                    "default": 0,
                    "description": "Lines of context before/after each match",
                },
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 50,
                    "description": "Max matches to return",
                },
                "encoding": {
                    "type": "string",
                    "default": "utf-8",
                    "description": "Text encoding (python-style)",
                },
            },
            "required": ["path", "pattern"],
        }

    async def execute(
        self,
        path: str,
        pattern: str,
        context_lines: int = 0,
        max_matches: int = 50,
        encoding: str = "utf-8",
        **kwargs: Any,
    ) -> str:
        import re as _re

        try:
            fp = self._resolve(path)
            if not fp.exists():
                return _err(f"File not found: {path}")
            lines = fp.read_text(encoding=encoding).splitlines()
            rx = _re.compile(pattern)
        except PermissionError as e:
            return _err(str(e))
        except _re.error as e:
            return _err(f"Invalid regex: {e}")
        except (UnicodeDecodeError, LookupError) as e:
            return _err(
                f"cannot decode {path} as {encoding!r}: {e}",
                "Try another encoding (e.g. encoding='gbk', 'big5', or 'latin-1').",
            )
        except Exception as e:
            return _err(str(e))

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

        return tool_ok(
            matches=match_count if match_count <= max_matches else f"{max_matches}+",
            results=results,
        )


# ---------------------------------------------------------------------------
# insert_lines
# ---------------------------------------------------------------------------


class InsertLinesTool(_FsTool):
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
                    "default": True,
                    "description": "Insert after the line; false = insert before",
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
                return _err(f"File not found: {path}")
            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            lines = raw.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True)
            total = len(lines)
            if line < 1 or line > total:
                return _err(f"line {line} out of bounds (file has {total} lines)")
            insert_text = text.replace("\r\n", "\n")
            if not insert_text.endswith("\n"):
                insert_text += "\n"
            idx = line if after else line - 1
            lines.insert(idx, insert_text)
            new_content = "".join(lines)
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")
            fp.write_bytes(new_content.encode("utf-8"))
            return tool_ok(inserted_at=line, after=after)
        except PermissionError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Error inserting lines: {e}")


# ---------------------------------------------------------------------------
# delete_lines
# ---------------------------------------------------------------------------


class DeleteLinesTool(_FsTool):
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
                return _err(f"File not found: {path}")
            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            lines = raw.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True)
            total = len(lines)
            if line_start < 1 or line_end > total or line_start > line_end:
                return _err(
                    f"line range {line_start}-{line_end} out of bounds (file has {total} lines)"
                )
            del lines[line_start - 1 : line_end]
            new_content = "".join(lines)
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")
            fp.write_bytes(new_content.encode("utf-8"))
            return tool_ok(
                deleted_lines=[line_start, line_end],
                lines_remaining=len(lines),
            )
        except PermissionError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Error deleting lines: {e}")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class DeleteTool(_FsTool):
    def __init__(
        self,
        workspace: Path | None = None,
        force_to_trash: bool = True,
    ):
        super().__init__(workspace)
        self._force_to_trash = force_to_trash

    @property
    def name(self) -> str:
        return "delete"

    @property
    def description(self) -> str:
        base = (
            "Delete files and/or directories, sending them to the system recycle bin "
            "(recoverable). Use this instead of rm/del/Remove-Item in exec. Each entry in "
            "`paths` is a literal path or a glob pattern (*, ?, [...], ** for recursive). "
            "Matches are processed in path order."
        )
        if not self._force_to_trash:
            base += " Set permanent=true to delete irreversibly (requires approval)."
        return base

    def _resolve_delete_path(self, path: str) -> Path:
        """Make a path absolute without resolving its final symlink target."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and self._workspace:
            candidate = self._workspace / candidate
        return Path(os.path.abspath(candidate))

    @property
    def parameters(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files/directories to delete; each is a literal path or glob pattern",
            },
            "recursive": {
                "type": "boolean",
                "description": "Required to permanently delete a non-empty directory",
            },
        }
        # The permanent option is only exposed when trash is not forced.
        if not self._force_to_trash:
            props["permanent"] = {
                "type": "boolean",
                "description": (
                    "Delete irreversibly, bypassing the recycle bin. This operation is "
                    "gated by the runtime security policy."
                ),
            }
        return {"type": "object", "properties": props, "required": ["paths"]}

    def _expand(self, entry: str) -> tuple[list[Path], dict[str, Any] | None]:
        """Resolve a literal path or expand a glob."""
        if any(c in entry for c in "*?["):
            pattern = str(self._resolve_delete_path(entry))
            matched: list[Path] = []
            for hit in glob.glob(pattern, recursive=True):
                matched.append(self._resolve_delete_path(hit))
            if not matched:
                return [], {"error": f"No paths matched pattern: {entry}"}
            return matched, None

        fp = self._resolve_delete_path(entry)
        if not fp.exists() and not fp.is_symlink():
            return [], {"error": f"Path not found: {entry}"}
        return [fp], None

    def _delete_one(self, fp: Path, to_trash: bool, recursive: bool) -> dict[str, Any]:
        if not fp.exists() and not fp.is_symlink():
            return {"ok": False, "error": f"Path not found: {fp}"}
        is_dir = fp.is_dir() and not fp.is_symlink()
        type_ = "dir" if is_dir else "file"

        if to_trash:
            try:
                from send2trash import send2trash as _send2trash

                _send2trash(str(fp))
            except ImportError:
                return {
                    "ok": False,
                    "error": "Recycle-bin backend unavailable (send2trash not installed).",
                    "hint": "Call delete with permanent=true, or install send2trash.",
                }
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"Failed to move to recycle bin: {e}",
                    "hint": "Target may be locked, in use, or on a volume without a recycle "
                    "bin. Retry with permanent=true to delete irreversibly.",
                }
            return {"ok": True, "type": type_}

        if is_dir:
            try:
                non_empty = any(fp.iterdir())
            except OSError:
                non_empty = True
            if non_empty and not recursive:
                return {
                    "ok": False,
                    "error": f"Directory not empty: {fp}",
                    "hint": "Set recursive=true to permanently delete a non-empty directory.",
                }
            try:
                shutil.rmtree(fp)
            except OSError as e:
                return {
                    "ok": False,
                    "error": f"Failed to delete directory: {e.strerror or e}",
                    "hint": "A file inside may be locked or in use.",
                }
        else:
            try:
                fp.unlink()
            except OSError as e:
                return {
                    "ok": False,
                    "error": f"Failed to delete file: {e.strerror or e}",
                    "hint": "The file may be locked or in use by another process.",
                }
        return {"ok": True, "type": type_}

    async def execute(
        self,
        paths: list[str] | str | None = None,
        permanent: bool = False,
        recursive: bool = False,
        **kwargs: Any,
    ) -> str:
        if isinstance(paths, str):
            entries = [paths]
        elif isinstance(paths, list):
            entries = [str(p) for p in paths]
        else:
            entries = []
        # Tolerate a singular `path` argument from the model.
        if isinstance(kwargs.get("path"), str):
            entries.append(kwargs["path"])
        entries = [e for e in entries if e and e.strip()]
        if not entries:
            return _err("No paths provided.")

        # Resolve the deletion mode. Trash is forced, or permanent needs approval.
        if self._force_to_trash:
            permanent = False

        candidates: list[Path] = []
        failed: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for entry in entries:
            matched, issue = self._expand(entry)
            if issue:
                failed.append(issue)
            for m in matched:
                if m not in seen:
                    seen.add(m)
                    candidates.append(m)

        # Deepest paths first so a directory's children are removed before the directory.
        candidates.sort(key=lambda p: len(p.parts), reverse=True)

        to_trash = not permanent
        deleted: list[dict[str, Any]] = []
        for fp in candidates:
            res = self._delete_one(fp, to_trash, recursive)
            if res.get("ok"):
                deleted.append({"path": str(fp), "type": res["type"]})
            else:
                entry = {"path": str(fp), "error": res["error"]}
                if res.get("hint"):
                    entry["hint"] = res["hint"]
                failed.append(entry)

        return json.dumps(
            {"ok": not failed, "deleted": deleted, "failed": failed},
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# file_hex
# ---------------------------------------------------------------------------


class FileHexTool(_FsTool):
    _DEFAULT_LENGTH = 256

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
                    "default": "read",
                    "description": "read or write",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Byte offset",
                },
                "length": {
                    "type": "integer",
                    "minimum": 1,
                    "default": self._DEFAULT_LENGTH,
                    "description": "Bytes to read",
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
                    return _err("hex_data is required for write mode")
                hex_data = hex_data.replace(" ", "").replace("\n", "")
                try:
                    data = bytes.fromhex(hex_data)
                except ValueError as e:
                    return _err(f"invalid hex_data: {e}")
                if not fp.exists():
                    return _err(f"File not found: {path}")
                raw = bytearray(fp.read_bytes())
                end = offset + len(data)
                if end > len(raw):
                    raw.extend(b"\x00" * (end - len(raw)))
                raw[offset:end] = data
                fp.write_bytes(bytes(raw))
                return tool_ok(offset=offset, bytes_written=len(data))

            # read mode
            if not fp.exists():
                return _err(f"File not found: {path}")
            n = length or self._DEFAULT_LENGTH
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

            return tool_ok(
                offset=offset,
                length=actual,
                file_size=file_size,
                hex_dump="\n".join(lines),
            )
        except PermissionError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Error in file_hex: {e}")
