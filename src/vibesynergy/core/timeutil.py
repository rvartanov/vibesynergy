"""Time helpers. Timestamps are timezone-aware UTC; calendar dates are the user's local dates."""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def today_in(timezone: str) -> date:
    return datetime.now(ZoneInfo(timezone)).date()


def add_months(day: date, months: int) -> date:
    """The same calendar day ``months`` ahead, clipped to the end of a shorter month."""
    if months < 0:
        raise ValueError("months must be zero or more")
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class Window:
    """An inclusive range of calendar dates."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window end {self.end} is before start {self.start}")

    @classmethod
    def last_days(cls, days: int, today: date) -> Window:
        return cls(today - timedelta(days=days - 1), today)

    @classmethod
    def parse(cls, text: str) -> Window:
        """Parse ``YYYY-MM-DD`` or ``YYYY-MM-DD..YYYY-MM-DD``."""
        start, _, end = text.partition("..")
        return cls(date.fromisoformat(start), date.fromisoformat(end or start))

    def days(self) -> Iterator[date]:
        for offset in range((self.end - self.start).days + 1):
            yield self.start + timedelta(days=offset)

    def months(self) -> Iterator[Window]:
        """Calendar months overlapping this window, each as a full-month Window."""
        current = self.start.replace(day=1)
        while current <= self.end:
            last = current.replace(day=calendar.monthrange(current.year, current.month)[1])
            yield Window(current, last)
            current = last + timedelta(days=1)

    def __len__(self) -> int:
        return (self.end - self.start).days + 1

    def __str__(self) -> str:
        return str(self.start) if self.start == self.end else f"{self.start}..{self.end}"
