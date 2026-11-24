"""Put an AI coach's morning advice in the demo for today, or take it out (#89, #90).

Two versions: the 07:00 advice, and the update after the check-in that replaced it, so the
start page shows "Bijgewerkt …, na je check-in" with "Veranderd omdat: …" under "Why this
advice" and the Coach page its earlier version, marked replaced (#115, #147). And the
evening run's advice for tomorrow, so the start page shows the card "Advies voor morgen -
..." under today's advice (#138).

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
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, insert

from vibesynergy import alerts
from vibesynergy.core.config import load_settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.query import daily_advice
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log

DEMO_HOME = Path.home() / "vs-help-home"
KEY = "demo-daily-advice"
EARLIER = "demo-daily-advice-0700"
TOMORROW = "demo-daily-advice-evening"


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            delete(coach_decision_log).where(
                coach_decision_log.c.idempotency_key.in_((KEY, EARLIER, TOMORROW))
            )
        )
    # The advice notices that ``seed`` sent (today and tomorrow) go with them.
    store = SecretStore(rt.settings.secrets_dir, alerts.SECRETS)
    day = today_in(rt.settings.timezone)
    keys = {f"advice:{d.isoformat()}" for d in (day, day + timedelta(days=1))}
    store.set(
        alerts.ADVICE_STATE,
        [e for e in store.get(alerts.ADVICE_STATE) or [] if e.get("key") not in keys],
    )


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
            "version": 3,
            "via": "runner",
            "task": "morning",
            "rules_floor": {
                "level": rules.get("safety_level") or "ok",
                "rules": rules.get("rules") or [],
            },
        },
    }
    first = {
        **meta,
        "safety": {"level": "ok", "colour": "green", "reasons": ["Je HRV en slaap zijn normaal"]},
        "harness": {**meta["harness"], "target": day.isoformat(), "trigger": "schedule"},
    }
    meta["harness"].update(target=day.isoformat(), trigger="event", changes=["checkin"])
    # The sessions it names (#181): with demo_workout_links' plan they link and open.
    meta["sessions"] = [
        {"title": "Duurloop Z2 45'", "decision": "adjust", "note": "45 minuten, geen tempo"}
    ]
    with rt.engine.begin() as conn:
        earlier = conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now() - timedelta(hours=1),
                kind="coach",
                author="client:coach-runner",
                entry_type="morning",
                idempotency_key=EARLIER,
                for_date=day,
                text="Je bent goed hersteld: de duurloop van 60 minuten kan zoals gepland.",
                meta=first,
            )
        ).inserted_primary_key[0]
        # The update builds on the 07:00 version and says why it changed (#115).
        meta["changed_because"] = {
            "previous": earlier,
            "reason": "Je check-in meldt zware benen, dus de duurloop wordt korter en rustiger.",
        }
        current = conn.execute(
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
        ).inserted_primary_key[0]
    # The notice for the current version, as the AI coach's run sends it (#115).
    daily_advice.announce(rt, current)
    # The evening run's advice for tomorrow (#90): the start page's card for tomorrow and a
    # notice named after tomorrow (#138).
    with rt.engine.begin() as conn:
        evening = conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="coach",
                author="client:coach-runner",
                entry_type="evening",
                idempotency_key=TOMORROW,
                for_date=day,
                text="Morgen de rustige rit van 1 uur in zone 2; je herstel is nog niet "
                "helemaal terug.",
                meta={
                    "author_name": "AI coach (API)",
                    "safety": {
                        "level": "ease",
                        "colour": "amber",
                        "reasons": ["Je rusthartslag is twee dagen hoger"],
                    },
                    "sessions": [{"title": "Rustige rit Z2 1u", "decision": "keep"}],
                    "harness": {
                        "version": 3,
                        "via": "runner",
                        "task": "evening",
                        "target": (day + timedelta(days=1)).isoformat(),
                        "trigger": "schedule",
                    },
                },
            )
        ).inserted_primary_key[0]
    daily_advice.announce(rt, evening)


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
