"""Path containment and resolution utilities.

These helpers are deterministic guards used by both the policy layer and
legacy tool implementations. Authorization is decided by the application
executor; the helpers do not carry mutable approval state.
"""

from __future__ import annotations

import os
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
        return f"Path '{path}' is outside the workspace directory."
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
    home = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command)
    # POSIX-style "/abs/path" only applies off Windows. On Windows a leading
    # "/" token is a command flag (e.g. Everything's /ad, /a-d; cmd's /F), not a
    # path — matching it would resolve to "C:\ad" and wrongly trip containment.
    posix = (
        []
        if os.name == "nt"
        else re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
    )
    return win + posix + home
