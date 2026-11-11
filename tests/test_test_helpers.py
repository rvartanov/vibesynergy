"""The browser tests' fixed "today": a zone where it is about noon, at any UTC hour."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tests_helpers import midday_timezone


def test_midday_timezone_is_about_noon_at_every_hour() -> None:
    for hour in range(24):
        now = datetime(2026, 10, 30, hour, 59, tzinfo=UTC)
        local = now.astimezone(ZoneInfo(midday_timezone(now)))
        assert local.hour == 12, (hour, midday_timezone(now))
