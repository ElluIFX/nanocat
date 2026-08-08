"""Conservative redaction helpers for logs and audit records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_REDACTED = "<redacted>"


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def redact_value(value: Any, *, key: object | None = None) -> Any:
    """Return a JSON-friendly redacted copy of a diagnostic value."""
    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(item_key): redact_value(item, key=item_key) for item_key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Redact a mapping into a detached mutable dictionary for serializers."""
    redacted = redact_value(value or {})
    return dict(redacted) if isinstance(redacted, Mapping) else {}
