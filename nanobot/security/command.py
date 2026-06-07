"""Shell command safety rules — deny patterns and enforcement.

All patterns are matched against the lowercased command string.
Categories: "file" (destructive file ops), "system" (OS-level danger),
"network" (exploitation tools), "policy" (disallowed by convention).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from nanobot.security.path import (
    extract_absolute_paths,
    extract_path_args,
)

_UNIX = "unix"
_CMD = "cmd"
_POWERSHELL = "powershell"
_ANY = "any"
_FILE = "file"
_SYSTEM = "system"
_NETWORK = "network"
_POLICY = "policy"

# (pattern, shell_type, category)
_DENY_RULES: list[tuple[str, str, str]] = [
    # --- FILE: Unix ---
    (r"\brm\b.*-[a-zA-Z]*[rf]", _UNIX, _FILE),
    (r"\bdd\b.*\bof=", _UNIX, _FILE),
    (r">\s*/dev/sd[a-z]", _UNIX, _FILE),
    # --- FILE: CMD ---
    (r"\bdel\b.*(?:/[fq])", _CMD, _FILE),
    (r"\brmdir\b.*(?:/s)", _CMD, _FILE),
    # --- FILE: PowerShell ---
    (r"\bremove-item\b.*-(?:recurse|force|r)\b", _POWERSHELL, _FILE),
    (r"\bri\b.*-(?:recurse|force|r)\b", _POWERSHELL, _FILE),
    # --- SYSTEM: Unix ---
    (r":\(\)\s*\{.*?\};\s*:", _UNIX, _SYSTEM),  # fork bomb
    (r"\b(?:mkfs|fdisk|mkswap|mkpart)\b", _UNIX, _SYSTEM),
    (r"\b(?:dd\b.*\bof=/dev/)", _UNIX, _SYSTEM),
    # --- SYSTEM: CMD ---
    (r"(?:^|[;&|]\s*)format\s+[a-zA-Z]:", _CMD, _SYSTEM),
    # --- SYSTEM: Any ---
    (r"\bdiskpart\b", _ANY, _SYSTEM),
    (r"\b(?:shutdown|reboot|poweroff|halt|init\s*[06])\b", _ANY, _SYSTEM),
    (r"\b(?:systemctl\s+(?:stop|disable|mask)|sc\s+(?:stop|delete))\b", _ANY, _SYSTEM),
    # --- NETWORK: Any ---
    (r"\b(?:ncat|socat|netcat)\b", _ANY, _NETWORK),
    (r"\b(?:telnet|rsh|rlogin)\b", _ANY, _NETWORK),
    (r"\b(?:nc\s+-[lL]|nc\s+-e\s)", _ANY, _NETWORK),
    # Pipe-to-shell execution via curl/wget
    (r"(?:curl|wget)\s+.*\s*\|\s*(?:sh|bash|zsh|dash|python|perl|ruby|lua)\b", _ANY, _NETWORK),
    # --- POLICY: CMD ---
    (r"\bdir\b", _CMD, _POLICY),
    # --- POLICY: PowerShell (dangerous by convention) ---
    (r"\binvoke-expression\b", _POWERSHELL, _POLICY),
    (r"\biex\b\s", _POWERSHELL, _POLICY),
    # --- SYSTEM: registry / persistence ---
    (r"\breg\s+(?:add|delete|import)\b", _CMD, _SYSTEM),
    (r"\bschtasks\b.*(?:/create|/delete)", _CMD, _SYSTEM),
    (r"\bcrontab\b", _UNIX, _SYSTEM),
    # --- SYSTEM: sudo with dangerous subcommands ---
    (r"\bsudo\b.*\b(?:rm\s+-[a-zA-Z]*[rf]|mkfs|fdisk|dd\b.*\bof=|chmod\s+[0-7]*7)", _UNIX, _SYSTEM),
    # --- POLICY: env var dumping ---
    (r"\b(?:set|env|printenv|export)\s*$", _ANY, _POLICY),
    (r"\b(?:set|printenv)\s*\|", _ANY, _POLICY),
    (r"\b(?:netstat\b|tasklist\b|ps\s+(?:aux|ef)|ss\s+-[tulp])", _ANY, _POLICY),
]

# Patterns that are always blocked regardless of category.
_UNCONDITIONAL: list[str] = []

# Patterns that unconditionally allow a command (checked before deny rules).
_ALLOW_ALWAYS: list[str] = []


def set_unconditional_deny(patterns: list[str]) -> None:
    """Set additional patterns that are blocked unconditionally."""
    _UNCONDITIONAL.clear()
    _UNCONDITIONAL.extend(patterns)


def set_allow_always(patterns: list[str]) -> None:
    """Set patterns that allow a command before any deny check."""
    _ALLOW_ALWAYS.clear()
    _ALLOW_ALWAYS.extend(patterns)


def set_extra_deny(patterns: list[str]) -> None:
    """Add extra deny patterns to the default set."""
    for p in patterns:
        if (p, _ANY, _SYSTEM) not in _DENY_RULES:
            _DENY_RULES.append((p, _ANY, _SYSTEM))


def guard_command(
    command: str,
    cwd: str,
    workspace: str,
    *,
    restrict_to_workspace: bool = False,
    on_blocked: Callable[[str, str, str, str], bool] | None = None,
) -> str | None:
    """Check *command* against all safety rules.

    Returns an error string if blocked, None if allowed.
    *on_blocked* is called before blocking and can return True to override.
    """
    cmd = command.strip()
    lower = cmd.lower()
    workspace_path = Path(workspace).resolve()
    cwd_path = Path(cwd).resolve()

    # Allow-list check (if configured, ALLOW_ALWAYS acts as strict allow-list)
    if _ALLOW_ALWAYS:
        if not any(re.search(p, lower) for p in _ALLOW_ALWAYS):
            return "Error: Command blocked by safety guard (not in allow-list)"

    # Unconditional deny
    for p in _UNCONDITIONAL:
        if re.search(p, lower):
            reason = "dangerous pattern [custom]"
            if on_blocked and on_blocked(cmd, _SYSTEM, _ANY, reason):
                break
            return _block_msg(reason)

    for pattern, shell_type, category in _DENY_RULES:
        if not re.search(pattern, lower, re.DOTALL):
            continue

        if category == _FILE:
            reason = f"file-dangerous command [{shell_type}]"
        elif category == _NETWORK:
            reason = f"network-exploit command [{shell_type}]"
        elif category == _POLICY:
            reason = f"disallowed command [{shell_type}]"
        else:
            reason = f"system-dangerous command [{shell_type}]"

        # File-dangerous commands are allowed if all paths are in workspace.
        if category == _FILE:
            if _file_danger_in_workspace(cmd, cwd, workspace_path):
                continue

        if on_blocked and on_blocked(cmd, category, shell_type, reason):
            continue

        return _block_msg(reason)

    # SSRF check
    from nanobot.security.network import contains_internal_url

    if contains_internal_url(cmd):
        return _block_msg("internal/private URL detected")

    # Path containment
    if restrict_to_workspace:
        if "..\\" in cmd or "../" in cmd:
            return _block_msg("path traversal detected")

        for raw in extract_absolute_paths(cmd):
            try:
                expanded = Path(os.path.expandvars(raw.strip())).expanduser().resolve()
            except Exception:
                continue
            if expanded.is_absolute() and not expanded.is_relative_to(cwd_path):
                return _block_msg("path outside working dir")

    return None


def _file_danger_in_workspace(command: str, cwd: str, workspace: Path) -> bool:
    """Return True if every extracted path in the command is inside workspace."""
    paths = extract_path_args(command)
    if not paths:
        return False

    cwd_path = Path(cwd).resolve()
    for raw in paths:
        try:
            expanded = os.path.expandvars(raw.strip("\"'"))
            p = Path(expanded).expanduser()
            if not p.is_absolute():
                p = cwd_path / p
            p = p.resolve()
        except Exception:
            return False
        if not p.is_relative_to(workspace):
            return False
    return True


def _block_msg(reason: str) -> str:
    return (
        f"Error: Command blocked by safety guard ({reason})"
        "\nIf you believe this action is necessary, explain the reason to the user "
        "and ask them to use /approve to temporarily bypass this check."
    )
