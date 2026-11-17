"""Put an AI coach's morning advice in the demo for today, or take it out (#89).

Only for the help-video demo home (``~/vs-help-home``), never for real data: the scenario
``today`` seeds before it records and clears afterwards, so the advice card shows what an
advice from the AI coach looks like (its colour, reasons and the Details with the rules
beside it). Run with the demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \
        uv run python tools/help_videos/demo_daily_advice.py seed|clear
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import delete, insert

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.query import daily_advice
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log

DEMO_HOME = Path.home() / "vs-help-home"
KEY = "demo-daily-advice"


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        conn.execute(delete(coach_decision_log).where(coach_decision_log.c.idempotency_key == KEY))


def seed(rt: Runtime) -> None:
    clear(rt)
    day = today_in(rt.settings.timezone)
    rules = daily_advice.current(rt, day, trigger=False).get("rules") or {}
    safety = {
        "level": "ease",
        "colour": "amber",
        "reasons": [
            "Je check-in zegt zware benen, je HRV is normaal",
            "Gisteren een lange duurloop",
        ],
        # The demo's rules may be stricter; a coach that is milder says why (the contract).
        "override_reason": "Je HRV en rusthartslag zijn normaal; rustig duurwerk kan vandaag.",
    }
    meta = {
        "safety": safety,
        "author_name": "AI coach (API)",
        "harness": {
            "version": 2,
            "via": "runner",
            "task": "morning",
            "rules_floor": {
                "level": rules.get("safety_level") or "ok",
                "rules": rules.get("rules") or [],
            },
        },
    }
    with rt.engine.begin() as conn:
        conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="coach",
                author="client:coach-runner",
                entry_type="morning",
                idempotency_key=KEY,
                for_date=day,
                text="Doe de duurloop vandaag rustig: 45 minuten in zone 2, geen tempo.",
                meta=meta,
            )
        )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in {"seed", "clear"}:
        sys.exit("usage: demo_daily_advice.py seed|clear")
    rt = Runtime.open(load_settings())
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()
    print(f"{action}: done")


if __name__ == "__main__":
    main()
