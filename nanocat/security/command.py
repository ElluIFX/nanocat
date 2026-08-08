"""Pure command-target helpers used by the security policy."""

from __future__ import annotations

import re

_URL_RE = re.compile(r"https?://[^\s\"'<>|;]+", re.IGNORECASE)


def extract_command_urls(command: str) -> tuple[str, ...]:
    """Extract explicit HTTP(S) URLs without judging their safety."""
    return tuple(_URL_RE.findall(command))
