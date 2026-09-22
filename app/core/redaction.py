"""Strip credential-shaped fragments from strings before they are logged."""

from __future__ import annotations

import re

_SECRET_RE = re.compile(r"(?i)(password|api[_-]?key|authorization)\s*[=:]\s*\S+")
_URL_USERINFO_RE = re.compile(r"://[^@\s/]+@")


def redact_secrets(text: str) -> str:
    """Remove credential-shaped fragments before a string is written to a log."""
    cleaned = _URL_USERINFO_RE.sub("://***@", text)
    return _SECRET_RE.sub(r"\1=***", cleaned)
