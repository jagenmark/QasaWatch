"""Small, dependency-free helpers for keeping credentials out of diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "<redacted>"
_SECRET_KEYS = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|credential)", re.I
)
_URL_CREDENTIAL = re.compile(r"(https?://[^:/\s]+:)([^@/\s]+)(@)", re.I)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")


def redact_text(value: object, secrets: tuple[str, ...] = ()) -> str:
    """Redact common authorization forms and explicitly supplied secret values."""

    text = str(value)
    text = _URL_CREDENTIAL.sub(rf"\1{REDACTED}\3", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a recursively redacted copy suitable for logs and error details."""

    result: dict[str, Any] = {}
    for key, item in value.items():
        if _SECRET_KEYS.search(str(key)):
            result[str(key)] = REDACTED
        elif isinstance(item, Mapping):
            result[str(key)] = redact_mapping(item)
        elif isinstance(item, (list, tuple)):
            result[str(key)] = [
                redact_mapping(v) if isinstance(v, Mapping) else v for v in item
            ]
        else:
            result[str(key)] = item
    return result
