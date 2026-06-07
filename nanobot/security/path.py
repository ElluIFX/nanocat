"""Path containment and resolution utilities.

All filesystem and exec tools use these checks so that the bypass
context var consistently controls path restrictions.
"""

from __future__ import annotations

import re
from pathlib import Path


def is_under(path: Path, directory: Path) -> bool:
    """Return True when *path* is inside (or equal to) *directory*."""
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def resolve_path(
    path: str,
    workspace: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve a user-supplied path string to an absolute Path.

    Relative paths are resolved against *workspace* (preferred) or *cwd*.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        base = workspace or cwd or Path.cwd()
        p = base / p
    return p.resolve()


def check_path_containment(
    path: str | Path,
    workspace: Path,
    cwd: Path | None = None,
) -> str | None:
    """Return an error string if *path* escapes *workspace*, else None."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        base = workspace or cwd or Path.cwd()
        p = base / p
    p = p.resolve()

    if not p.is_relative_to(workspace.resolve()):
        return (
            f"Path '{path}' is outside the workspace directory. "
            f"Use /approve to temporarily bypass this check."
        )
    return None


def extract_path_args(command: str) -> list[str]:
    """Extract non-flag token arguments from a shell command as potential path targets.

    Skips the command verb and option flags (-x, --flag, /F).
    For key=value tokens (e.g. dd's of=...), extracts the value part.
    """
    tokens = re.split(r"\s+", command.strip())
    result: list[str] = []
    for i, tok in enumerate(tokens):
        if i == 0:
            continue
        clean = tok.strip("\"'")
        if not clean:
            continue
        if re.match(r"^-{1,2}[a-zA-Z]", clean) or re.match(r"^/[a-zA-Z]{1,2}$", clean):
            continue
        if "=" in clean:
            _, _, val = clean.partition("=")
            if val:
                result.append(val)
        else:
            result.append(clean)
    return result


def extract_absolute_paths(command: str) -> list[str]:
    """Extract absolute path-like strings from a command."""
    win = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)
    posix = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
    home = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command)
    return win + posix + home
