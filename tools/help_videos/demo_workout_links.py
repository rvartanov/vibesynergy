"""Put a small plan of VibeSynergy's own in the demo, or take it out again (#181).

The demo has no plan, so no workout the portal names can open to its steps or link to its
activity. ``seed`` adds a plan with three sessions: today's run (the demo's run of today
makes it done, so it links to that activity), tomorrow's easy ride and a long run the day
after (both open to their steps). The advice seeded by ``demo_daily_advice`` names today's
run and tomorrow's ride; ``demo_plan_copy``'s plan change names the long run.

``clear`` removes exactly that plan. Only for the help-video demo home (``~/vs-help-home``),
never for real data. Run with the demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \
        uv run python tools/help_videos/demo_workout_links.py seed|clear
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, inspect

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.query import plan_model
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log, metadata, plan

PLAN = "demo-workout-links"
REASON = "help video: demo-workout-links"  # marks the plan's own change messages
DEMO_HOME = Path.home() / "vs-help-home"

# (days from today, name, sport, workout text); the names match the demo's advice.
SESSIONS = (
    (0, "Duurloop Z2 45'", "Run", "Rustig, geen tempo.\n\nMain set\n- 45m Z2 HR"),
    (
        1,
        "Rustige rit Z2 1u",
        "Ride",
        "Rustig rondrijden.\n\nWarmup\n- 10m Z1 HR\n\nMain set\n- 45m Z2 HR"
        "\n\nCooldown\n- 5m Z1 HR",
    ),
    (2, "Duurloop Z2 50'", "Run", "Warmup\n- 10m Z1 HR\n\nMain set\n- 40m Z2 HR"),
)


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        names = set(inspect(conn).get_table_names())
        for table in metadata.sorted_tables:
            if table.name in names and "plan_id" in table.c and table.name != "planned_item":
                conn.execute(delete(table).where(table.c.plan_id == PLAN))
        conn.execute(delete(plan).where(plan.c.id == PLAN))
    clear_messages(rt)


def seed(rt: Runtime) -> None:
    clear(rt)
    day = today_in(rt.settings.timezone)
    plan_model.create_plan(
        rt.engine,
        name=PLAN,
        start=day - timedelta(days=day.weekday()),
        end=day + timedelta(days=60),
        reason=REASON,
        author="demo",
    )
    for shift, name, sport, text in SESSIONS:
        plan_model.set_session(
            rt.engine,
            session_id=None,
            day=day + timedelta(days=shift),
            name=name,
            sport=sport,
            description=text,
            reason=REASON,
            author="demo",
        )
    clear_messages(rt)


def clear_messages(rt: Runtime) -> None:
    """Making the plan writes a plan change per session; the videos do not show those."""
    with rt.engine.begin() as conn:
        conn.execute(
            delete(coach_decision_log).where(
                coach_decision_log.c.entry_type == "plan_change",
                coach_decision_log.c.text == REASON,
            )
        )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in {"seed", "clear"}:
        sys.exit("usage: demo_workout_links.py seed|clear")
    rt = Runtime.open(load_settings())
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()
    print(f"{action}: done")


if __name__ == "__main__":
    main()
