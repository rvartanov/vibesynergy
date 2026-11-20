"""Put a badly recorded night in the demo for today, or take it out (#100).

The demo source has no Garmin sleep payloads, so the sleep check has nothing to look at.
``seed`` stores a synthetic Garmin night ending this morning in the raw history (no REM,
hours of light sleep without a change, HRV only for the first part of the night, the heart
rate measured all night), so the start page shows "Sleep may be badly recorded" with
"Yes, incomplete" / "No, it's right", and the sleep page lists the night. ``clear`` removes
the night, its stored check and the choice made on it.

Only for the help-video demo home (``~/vs-help-home``), never for real data. Run with the
demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \
        uv run python tools/help_videos/demo_sleep_check.py seed|clear
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, update

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import checkin, raw_item, sleep_check

DEMO_HOME = Path.home() / "vs-help-home"


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _gmt(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat()


def clear(rt: Runtime) -> None:
    day = today_in(rt.settings.timezone)
    keys = [day.isoformat(), (day - timedelta(days=1)).isoformat()]
    with rt.engine.begin() as conn:
        conn.execute(
            delete(raw_item).where(
                raw_item.c.source == "garmin",
                raw_item.c.dataset.in_(("sleep", "heart_rate")),
                raw_item.c.item_key.in_(keys),
                raw_item.c.request["demo"].as_boolean().is_(True),
            )
        )
        conn.execute(delete(sleep_check).where(sleep_check.c.date == day))
        conn.execute(update(checkin).where(checkin.c.date == day).values(sleep_incomplete=None))


def seed(rt: Runtime) -> None:
    clear(rt)
    zone = ZoneInfo(rt.settings.timezone)
    day = today_in(rt.settings.timezone)
    start = datetime.combine(day - timedelta(days=1), time(22, 40), tzinfo=zone)
    end = datetime.combine(day, time(6, 55), tzinfo=zone)
    hrv_end = datetime.combine(day, time(0, 50), tzinfo=zone)
    change = datetime.combine(day, time(0, 30), tzinfo=zone)
    asleep = int((end - start).total_seconds()) - 600

    def every(a: datetime, b: datetime, minutes: int):
        at = a
        while at <= b:
            yield at
            at += timedelta(minutes=minutes)

    payload = {
        "dailySleepDTO": {
            "calendarDate": day.isoformat(),
            "sleepStartTimestampGMT": _ms(start),
            "sleepEndTimestampGMT": _ms(end),
            "sleepTimeSeconds": asleep,
            "deepSleepSeconds": 3600,
            "lightSleepSeconds": asleep - 3600,
            "remSleepSeconds": 0,
            "awakeSleepSeconds": 600,
            "deviceRemCapable": True,
            "avgHeartRate": 52,
        },
        "sleepHeartRate": [{"startGMT": _ms(t), "value": 52} for t in every(start, end, 2)],
        "hrvData": [{"startGMT": _ms(t), "value": 48.0} for t in every(start, hrv_end, 5)],
        "sleepLevels": [
            {"startGMT": _gmt(start), "endGMT": _gmt(change), "activityLevel": 0.0},
            {"startGMT": _gmt(change), "endGMT": _gmt(end), "activityLevel": 1.0},
        ],
    }
    morning = datetime.combine(day, time(0, 0), tzinfo=zone)
    heart = {
        (day - timedelta(days=1)).isoformat(): [
            [_ms(t), 60 if t < start else 52]
            for t in every(morning - timedelta(hours=6), morning, 2)
        ],
        day.isoformat(): [
            [_ms(t), 52 if t < end else 70] for t in every(morning, end + timedelta(hours=2), 2)
        ],
    }
    for key, rows in heart.items():
        rt.raw.put(
            source="garmin",
            dataset="heart_rate",
            item_key=key,
            payload=json.dumps({"heartRateValues": rows}).encode(),
            content_type="application/json",
            request={"demo": True},
        )
    rt.raw.put(
        source="garmin",
        dataset="sleep",
        item_key=day.isoformat(),
        payload=json.dumps(payload).encode(),
        content_type="application/json",
        request={"demo": True},
    )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in {"seed", "clear"}:
        sys.exit("usage: demo_sleep_check.py seed|clear")
    rt = Runtime.open(load_settings())
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()
    print(f"{action}: done")


if __name__ == "__main__":
    main()
