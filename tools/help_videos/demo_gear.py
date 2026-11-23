"""Garmin's gear list for the "workout-gear" help video, and taking the video's gear out again
(#114).

The demo has no Strava, so no bikes or shoes of its own. ``seed`` puts a small synthetic
Garmin gear list in the demo (as the Garmin sync would collect it), so the Gear page can
suggest a match. ``clear`` takes out what the video adds through the portal: the gear of your
own, its mapping, what was linked to workouts and that history, so other videos and the next
recording start from the same demo.

Only for the help-video demo home (``~/vs-help-home``), never for real data. Run with the
demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \\
        uv run python tools/help_videos/demo_gear.py seed
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sqlalchemy import delete, or_

from vibesynergy.core.config import load_settings
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import (
    activity_gear,
    activity_gear_extra,
    gear_change,
    gear_item,
    gear_map,
)

DEMO_HOME = Path.home() / "vs-help-home"
GARMIN_GEAR = [
    {
        "uuid": "0a1b2c3d4e5f60718293a4b5c6d7e8f9",
        "displayName": "Racefiets",
        "gearTypeName": "Bike",
        "gearStatusName": "active",
    },
    {
        "uuid": "1b2c3d4e5f60718293a4b5c6d7e8f90a",
        "displayName": "Hardloopschoenen",
        "gearTypeName": "Shoes",
        "gearStatusName": "active",
    },
]


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def seed(rt: Runtime) -> None:
    rt.raw.put(
        source="garmin",
        dataset="gear",
        item_key="current",
        payload=json.dumps({"gear": GARMIN_GEAR, "stats": {}}).encode(),
        content_type="application/json",
    )


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        for table in (gear_item, gear_map, activity_gear_extra, gear_change):
            conn.execute(delete(table))
        conn.execute(
            delete(activity_gear).where(
                or_(activity_gear.c.ref.like("vs:%"), activity_gear.c.ref.is_(None))
            )
        )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ("seed", "clear"):
        sys.exit("usage: demo_gear.py seed|clear")
    rt = Runtime.open(load_settings(None))
    try:
        clear(rt)
        if action == "seed":
            seed(rt)
    finally:
        rt.close()


if __name__ == "__main__":
    main()
