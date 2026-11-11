"""Per-request HTTP timeouts for source sync and OAuth clients."""

from __future__ import annotations

from vibesynergy.core.config import Settings

DEFAULT_HTTP_TIMEOUT_SECONDS = 60.0
_CONNECT_CAP_SECONDS = 15.0


def http_timeout_seconds(settings: Settings, source_id: str | None = None) -> float:
    """Connect+read budget in seconds (``[sources.<id>]`` overrides ``[schedule]``)."""
    if source_id:
        raw = settings.source_config(source_id).get("http_timeout_seconds")
        if raw is not None:
            return _coerce(raw)
    raw = settings.schedule.get("http_timeout_seconds")
    if raw is not None:
        return _coerce(raw)
    return DEFAULT_HTTP_TIMEOUT_SECONDS


def requests_timeout_pair(total_seconds: float) -> tuple[float, float]:
    """``(connect, read)`` tuple for ``requests`` with a short connect cap."""
    total = max(1.0, float(total_seconds))
    connect = min(_CONNECT_CAP_SECONDS, total)
    return (connect, total)


def _coerce(raw: object) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_HTTP_TIMEOUT_SECONDS
    return max(1.0, value)
