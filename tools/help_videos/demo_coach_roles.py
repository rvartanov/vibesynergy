"""Put an advisory AI coach's second opinion and a plan proposal in the demo, or take them
out (#118).

Only for the help-video demo home (``~/vs-help-home``), never for real data: the scenarios
``today``, ``coach`` and ``search-inbox`` seed before they record and clear afterwards, so
the advice card shows a second opinion that differs from the AI coach's advice and Waiting
for you shows a plan change to approve. The second opinion is held against the AI coach's
advice of ``demo_daily_advice.py``, which this helper seeds and clears with it (one call,
so a recording does not wait twice). Run with the demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \
        uv run python tools/help_videos/demo_coach_roles.py seed|clear
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import demo_daily_advice
from sqlalchemy import delete, insert

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log, plan_proposal

DEMO_HOME = Path.home() / "vs-help-home"
KEY = "demo-second-opinion"
AUTHOR = "client:demo-advisor"
NAME = "Tweede coach"


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def clear(rt: Runtime) -> None:
    demo_daily_advice.clear(rt)
    with rt.engine.begin() as conn:
        conn.execute(delete(coach_decision_log).where(coach_decision_log.c.idempotency_key == KEY))
        conn.execute(delete(plan_proposal).where(plan_proposal.c.author == AUTHOR))


def seed(rt: Runtime) -> None:
    clear(rt)
    demo_daily_advice.seed(rt)
    day = today_in(rt.settings.timezone)
    with rt.engine.begin() as conn:
        conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="coach",
                author=AUTHOR,
                entry_type="morning",
                idempotency_key=KEY,
                for_date=day,
                text="Ik zou vandaag geen duurloop doen: wandel een half uur en loop morgen.",
                meta={
                    "author_name": NAME,
                    "safety": {
                        "level": "flag",
                        "colour": "amber",
                        "reasons": ["Zware benen na de lange duurloop van gisteren"],
                    },
                    "harness": {
                        "version": 2,
                        "via": "mcp",
                        "role": "advisory",
                        "task": "morning",
                        "target": day.isoformat(),
                    },
                },
            )
        )
        tomorrow = day + timedelta(days=1)
        conn.execute(
            insert(plan_proposal).values(
                created_at=utc_now(),
                author=AUTHOR,
                author_name=NAME,
                role="advisory",
                tool="vibesynergy_plan_session_move",
                arguments={"session_id": "demo", "date": tomorrow.isoformat()},
                reason="Zware benen vandaag: een dag later loop je hem frisser.",
                summary={
                    "action": "move",
                    "name": "Duurloop Z2 60'",
                    "sport": "Run",
                    "from_date": day.isoformat(),
                    "date": tomorrow.isoformat(),
                },
                status="open",
            )
        )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in {"seed", "clear"}:
        sys.exit("usage: demo_coach_roles.py seed|clear")
    rt = Runtime.open(load_settings())
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()
    print(f"{action}: done")


if __name__ == "__main__":
    main()
