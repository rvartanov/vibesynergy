"""Access-log redaction: request paths carry secrets (the Withings webhook key, the MCP
key) and query strings carry more (Strava's verify token, OAuth codes and state).

Rather than knowing each secret, the filter hides every query string and every path
segment that looks like a key (20+ URL-safe characters), so a secret added later is
covered too. Used through ``log_config()`` for uvicorn.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

_KEY_SEGMENT = re.compile(r"(?<=/)[A-Za-z0-9_-]{20,}(?=/|$)")
REDACTED = "[redacted]"


def redact_path(path: str) -> str:
    path, sep, _query = path.partition("?")
    return _KEY_SEGMENT.sub(REDACTED, path) + (f"?{REDACTED}" if sep else "")


class RedactSecrets(logging.Filter):
    """For uvicorn.access records: args = (client, method, path, http_version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_path(args[2]), *args[3:])
        return True


def log_config() -> dict[str, Any]:
    """Uvicorn's default logging configuration with the redaction filter on the access log."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_secrets"] = {"()": RedactSecrets}
    config["handlers"]["access"].setdefault("filters", []).append("redact_secrets")
    return config
