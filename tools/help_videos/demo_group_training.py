"""Put a RunSynergy group training and a "Training geven" session in the demo, or take them out.

Only for the help-video demo home (``~/vs-help-home``), never for real data: the scenario
``group-training`` seeds before it records and clears afterwards, so other videos keep the
demo without a plan. Run with the demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \
        uv run python tools/help_videos/demo_group_training.py seed|clear
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, inspect

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import plan_model
from vibesynergy.runtime import Runtime
from vibesynergy.sources.runsynergy import map_trainings
from vibesynergy.store.schema import metadata, plan, planned_item, raw_item

PLAN = "demo-groepstraining"
DEMO_HOME = Path.home() / "vs-help-home"

DATA = {
    "v": 1,
    "club": "Loopgroep Demo",
    "club_id": 1,
    "groep": "Dinsdaggroep",
    "rol": "coach",
    "soort": "groep",
    "start": "19:00",
    "afgelast": False,
    "titel": "Wisselduurloop DL2/DL3",
    "type": "interval",
    "onderdelen": [
        {"deel": "warming_up", "min": 12, "tekst": "Inlopen DL1"},
        {"deel": "techniek", "min": 10, "tekst": "Loopscholing: knieheffen, A-loop"},
        {"deel": "kern", "min": 42, "tekst": "7x 4' DL2 / 2' DL3, zonder stilstaan"},
        {"deel": "cooling_down", "min": 15, "tekst": "Uitlopen DL1 en rekken"},
    ],
}


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def _ics(day) -> str:
    data = json.dumps({**DATA, "datum": day.isoformat()}, ensure_ascii=False)
    escaped = data.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    stamp = day.strftime("%Y%m%d")
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//RunSynergy demo//NL",
            "BEGIN:VEVENT",
            f"UID:demo-{stamp}@runsynergy.example",
            "DTSTAMP:20261116T120000Z",
            f"DTSTART;TZID=Europe/Amsterdam:{stamp}T190000",
            "SUMMARY:Loopgroep Demo: Wisselduurloop DL2/DL3",
            f"X-RUNSYNERGY-DATA:{escaped}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        names = set(inspect(conn).get_table_names())
        for table in metadata.sorted_tables:
            if table.name in names and "plan_id" in table.c and table.name != "planned_item":
                conn.execute(delete(table).where(table.c.plan_id == PLAN))
        conn.execute(delete(plan).where(plan.c.id == PLAN))
        conn.execute(delete(planned_item).where(planned_item.c.source == "runsynergy"))
        conn.execute(delete(raw_item).where(raw_item.c.source == "runsynergy"))


def seed(rt: Runtime) -> None:
    clear(rt)
    day = today_in(rt.settings.timezone)
    plan_model.create_plan(
        rt.engine,
        name=PLAN,
        start=day - timedelta(days=day.weekday()),
        end=day + timedelta(days=60),
        reason="help video",
        author="demo",
    )
    plan_model.set_session(
        rt.engine,
        session_id=None,
        day=day,
        name="Training geven Loopgroep Demo",
        sport="Run",
        description=(
            "Training geven, zelf rustig mee.\n\nWarmup\n- 15m Z1 HR\n\nMain set\n- 55m Z1 HR"
        ),
        reason="help video",
        author="demo",
        slot="evening",
    )
    window = f"{day - timedelta(days=14)}..{day + timedelta(days=366)}"
    stored = rt.raw.put(
        source="runsynergy",
        dataset="trainings",
        item_key="calendar-link",
        payload=json.dumps(
            {"ics": _ics(day), "timezone": rt.settings.timezone, "window": window}
        ).encode(),
        content_type="application/json",
    )
    with rt.engine.begin() as conn:
        write_records(
            conn,
            list(map_trainings(rt.raw.load(stored.id))),
            source="runsynergy",
            raw_item_id=stored.id,
        )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in {"seed", "clear"}:
        sys.exit("usage: demo_group_training.py seed|clear")
    rt = Runtime.open(load_settings())
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()
    print(f"{action}: done")


if __name__ == "__main__":
    main()
