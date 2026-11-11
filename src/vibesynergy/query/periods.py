"""Periods as the MCP tools and portal accept them (docs/MCP.md §2.2)."""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from vibesynergy.core.timeutil import Window

EARLIEST = date(2000, 1, 1)
NAMED = (
    "today, yesterday, last_7d, last_14d, last_28d, last_90d, last_365d, week, this_week, "
    "last_week, last_<n>m, last_<n>d, next_<n>m, next_<n>d, this_month, last_month, "
    "this_year, ytd, all, YYYY-Www, YYYY-MM, YYYY, YYYY-MM-DD or YYYY-MM-DD..YYYY-MM-DD"
)


class PeriodError(ValueError):
    pass


def resolve(
    period: str | None,
    start: str | None = None,
    end: str | None = None,
    *,
    today: date,
    default: str = "last_7d",
) -> Window:
    """A Window from ``start``/``end`` or a named period (``default`` when neither)."""
    try:
        if start or end:
            first = date.fromisoformat(start) if start else EARLIEST
            last = date.fromisoformat(end) if end else today
            return Window(first, last)
        return _named((period or default).strip().lower(), today)
    except ValueError as exc:
        if isinstance(exc, PeriodError):
            raise
        raise PeriodError(f"{exc}. Periods: {NAMED}") from exc


def _subtract_months(day: date, months: int) -> date:
    month = day.month - months
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _add_months(day: date, months: int) -> date:
    month = day.month + months
    year = day.year
    while month > 12:
        month -= 12
        year += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _rolling_months(today: date, months: int) -> Window:
    start = _subtract_months(today, months) + timedelta(days=1)
    return Window(start, today)


def _forward_months(today: date, months: int) -> Window:
    end = _add_months(today, months) - timedelta(days=1)
    return Window(today, end)


def _named(p: str, today: date) -> Window:
    monday = today - timedelta(days=today.weekday())
    if p == "today":
        return Window(today, today)
    if p == "yesterday":
        return Window(today - timedelta(days=1), today - timedelta(days=1))
    if m := re.fullmatch(r"last_(\d+)d", p):
        return Window.last_days(int(m.group(1)), today)
    if m := re.fullmatch(r"next_(\d+)d", p):
        n = int(m.group(1))
        return Window(today, today + timedelta(days=n - 1))
    if p == "week":
        return Window(monday, monday + timedelta(days=6))
    if p == "this_week":
        return Window(monday, today)
    if p == "last_week":
        return Window(monday - timedelta(days=7), monday - timedelta(days=1))
    if m := re.fullmatch(r"last_(\d+)m", p):
        return _rolling_months(today, int(m.group(1)))
    if m := re.fullmatch(r"next_(\d+)m", p):
        return _forward_months(today, int(m.group(1)))
    if p == "this_month":
        return Window(today.replace(day=1), today)
    if p == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return Window(last.replace(day=1), last)
    if p in ("this_year", "ytd"):
        return Window(today.replace(month=1, day=1), today)
    if p == "all":
        return Window(EARLIEST, today)
    if m := re.fullmatch(r"(\d{4})-w(\d{1,2})", p):
        first = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        return Window(first, first + timedelta(days=6))
    if re.fullmatch(r"\d{4}-\d{2}", p):
        first = date.fromisoformat(f"{p}-01")
        return next(Window(first, first).months())
    if re.fullmatch(r"\d{4}", p):
        return Window(date(int(p), 1, 1), date(int(p), 12, 31))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?", p):
        return Window.parse(p)
    raise PeriodError(f"unknown period {p!r}. Periods: {NAMED}")
