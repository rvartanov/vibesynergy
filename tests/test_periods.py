"""Named periods (portal picker and MCP contract)."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from vibesynergy.core.timeutil import Window
from vibesynergy.query import periods

ROOT = Path(__file__).resolve().parents[1]


def test_last_months_rolling_and_clamp() -> None:
    today = date(2026, 3, 31)
    assert periods.resolve("last_1m", today=today) == Window(date(2026, 3, 1), today)
    mid = date(2026, 3, 15)
    assert periods.resolve("last_1m", today=mid) == Window(date(2026, 2, 16), mid)
    leap = date(2024, 3, 31)
    assert periods.resolve("last_1m", today=leap) == Window(date(2024, 3, 1), leap)
    assert periods.resolve("last_3m", today=today).start == date(2026, 1, 1)
    assert periods.resolve("last_6m", today=today).start == date(2025, 10, 1)
    assert periods.resolve("last_12m", today=today) == Window(date(2025, 4, 1), today)
    oct = date(2026, 10, 3)
    assert periods.resolve("last_12m", today=oct) == Window(date(2025, 10, 4), oct)


def test_forward_presets() -> None:
    today = date(2026, 10, 3)
    assert periods.resolve("next_7d", today=today) == Window(today, date(2026, 10, 9))
    assert periods.resolve("next_14d", today=today) == Window(today, date(2026, 10, 16))
    assert periods.resolve("next_1m", today=today) == Window(today, date(2026, 11, 2))
    assert periods.resolve("next_3m", today=today) == Window(today, date(2027, 1, 2))


def test_week_full_monday_sunday() -> None:
    sunday = date(2026, 10, 4)
    monday = date(2026, 9, 28)
    assert periods.resolve("week", today=sunday) == Window(monday, sunday)
    assert periods.resolve("week", today=monday) == Window(monday, date(2026, 10, 4))
    assert periods.resolve("week", today=date(2026, 1, 1)) == Window(
        date(2025, 12, 29), date(2026, 1, 4)
    )


def test_this_week_unchanged_monday_through_today() -> None:
    today = date(2026, 9, 24)
    assert periods.resolve("this_week", today=today) == Window(date(2026, 9, 21), today)


def test_last_14d() -> None:
    today = date(2026, 10, 3)
    assert periods.resolve("last_14d", today=today) == Window(date(2026, 9, 20), today)


def test_js_python_period_parity() -> None:
    if not shutil.which("node"):
        pytest.skip("node not available")
    today = date(2026, 10, 3)
    names = [
        "today",
        "yesterday",
        "last_7d",
        "last_14d",
        "last_1m",
        "last_3m",
        "last_6m",
        "last_12m",
        "next_7d",
        "next_14d",
        "next_1m",
        "next_3m",
        "week",
        "this_week",
        "last_week",
        "ytd",
        "all",
        "2024",
    ]
    cases = []
    for name in names:
        span = periods.resolve(name, today=today)
        cases.append(
            {
                "period": name,
                "expect_start": str(span.start),
                "expect_end": str(span.end),
            }
        )
    script = ROOT / "tools" / "period_parity.mjs"
    subprocess.run(
        ["node", str(script), str(today), json.dumps(cases)],
        check=True,
        cwd=ROOT,
    )
