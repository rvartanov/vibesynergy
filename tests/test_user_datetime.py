from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from vibesynergy.core.user_datetime import (
    DISPLAY_TZ,
    format_user_date,
    format_user_datetime,
    format_user_long_date,
    format_user_month_year,
    format_user_short_weekday,
    format_user_time_from_utc,
)


def test_format_user_date_unpadded() -> None:
    assert format_user_date(date(2026, 12, 1)) == "1-12-2026"
    assert format_user_date(date(2026, 1, 9)) == "9-1-2026"


def test_format_user_datetime_amsterdam_winter() -> None:
    instant = datetime(2026, 12, 1, 12, 45, 55, tzinfo=UTC)
    assert format_user_datetime(instant, DISPLAY_TZ) == "1-12-2026 13:45"


def test_format_user_datetime_amsterdam_summer_dst() -> None:
    instant = datetime(2026, 7, 1, 10, 30, 0, tzinfo=UTC)
    assert format_user_datetime(instant, DISPLAY_TZ) == "1-7-2026 12:30"


def test_format_user_time_from_utc_has_no_seconds() -> None:
    instant = datetime(2026, 3, 29, 0, 30, 5, tzinfo=UTC)
    assert format_user_time_from_utc(instant, DISPLAY_TZ) == "01:30"


def test_format_js_user_display() -> None:
    import json
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("node is not installed")

    script = """
globalThis.document = { documentElement: {} };
const { readFile } = await import("node:fs/promises");
globalThis.fetch = async (url) => ({ json: async () => JSON.parse(await readFile(url, "utf8")) });
import { userDate, userDateTime, userTime, longDate } from "./js/format.js";
import { setLanguage } from "./js/i18n.js";
await setLanguage("en");
const winter = userDateTime("2026-12-01T12:45:55.000Z");
await setLanguage("nl");
const nlLong = longDate("2026-12-04");
console.log(JSON.stringify({
  winter, nlLong, day: userDate("2026-12-01"),
  time: userTime("2026-12-01T12:45:55.000Z"),
}));
"""
    static = Path(__file__).parents[1] / "src/vibesynergy/portal/static"
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=static,
        capture_output=True,
        text=True,
        check=True,
    )
    got = json.loads(out.stdout)
    assert got["day"] == "1-12-2026"
    assert got["winter"] == "1-12-2026 13:45"
    assert got["time"] == "13:45"
    assert got["nlLong"] == "vrijdag 4 december 2026"


def test_weekday_and_month_names_follow_language() -> None:
    day = date(2026, 12, 4)
    assert format_user_long_date(day, "en") == "Friday 4 December 2026"
    assert format_user_long_date(day, "nl") == "vrijdag 4 december 2026"
    assert format_user_short_weekday(day, "en") == "Fri"
    assert format_user_short_weekday(day, "nl") == "vr"
    assert format_user_month_year(date(2026, 12, 1), "en") == "Dec 2026"
    assert format_user_month_year(date(2026, 12, 1), "nl") == "dec 2026"
