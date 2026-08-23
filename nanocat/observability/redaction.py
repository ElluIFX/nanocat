"""Conservative redaction helpers for logs and audit records."""

from __future__ import annotations

import re
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
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|password|passwd|secret|token|api[-_ ]?key|credential)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)(["\'](?:authorization|cookie|password|passwd|secret|token|access_token|'
    r'refresh_token|api[-_ ]?key|credential)["\']\s*:\s*)'
    r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^,}\s]+)'
)
_JSON_SECRET_CONTAINER_RE = re.compile(
    r'(?i)(["\'](?:headers|extra_headers|extraHeaders|env)["\']\s*:\s*)\{[^{}]*\}'
)
_URL_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:authorization|password|secret|token|access_token|refresh_token|"
    r"api[-_]?key|credential)=)[^&#\s]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_CREDENTIAL_RE = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://[^\s/:@]+):[^\s/@]+@"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)


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
    if isinstance(value, str):
        value = _PRIVATE_KEY_RE.sub(_REDACTED, value)
        value = _BEARER_RE.sub(f"Bearer {_REDACTED}", value)
        value = _URL_CREDENTIAL_RE.sub(r"\1:<redacted>@", value)
        value = _URL_QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
        value = _JSON_SECRET_CONTAINER_RE.sub(
            lambda match: f"{match.group(1)}{{\"redacted\":true}}",
            value,
        )
        value = _JSON_SECRET_RE.sub(lambda match: f"{match.group(1)}\"{_REDACTED}\"", value)
        return _INLINE_SECRET_RE.sub(lambda match: f"{match.group(1)}={_REDACTED}", value)
    return value


def redact_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Redact a mapping into a detached mutable dictionary for serializers."""
    redacted = redact_value(value or {})
    return dict(redacted) if isinstance(redacted, Mapping) else {}
