"""Put a plan change and a copy to intervals.icu in the demo, or take them out (#141).

The demo has no plan of its own and no intervals.icu account, so neither the Coach page nor
Settings → Connections → intervals.icu shows what #141 changed. ``seed`` adds:

* a real change to the plan by the AI coach: a "Plan change" on the Coach page with its
  reason and, under Details, what changed;
* an old copy message from before #141 ("plan mirror: ..."), which the Coach page hides;
* a copy run of three changes in the audit log, so the intervals.icu card shows its line.

``clear`` removes exactly those rows. Only for the help-video demo home
(``~/vs-help-home``), never for real data. Run with the demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \
        uv run python tools/help_videos/demo_plan_copy.py seed|clear
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, insert

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.query import plan_model
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import audit_log, coach_decision_log

DEMO_HOME = Path.home() / "vs-help-home"
MARK = "demo-plan-copy"  # in meta.demo / detail.demo: what ``clear`` removes


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            delete(coach_decision_log).where(
                func.json_extract(coach_decision_log.c.meta, "$.demo") == MARK
            )
        )
        conn.execute(
            delete(audit_log).where(func.json_extract(audit_log.c.detail, "$.demo") == MARK)
        )


def seed(rt: Runtime) -> None:
    clear(rt)
    day = today_in(rt.settings.timezone)
    now = utc_now()
    later = day + timedelta(days=2)
    with rt.engine.begin() as conn:
        # Before #141: a copy shown as a plan change. It stays hidden on the Coach page.
        conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=now - timedelta(hours=2),
                kind="note",
                author=plan_model.MIRROR_AUTHOR,
                entry_type="plan_change",
                for_date=day,
                text="plan mirror: demo:week:01",
                meta={"action": "icu.update", "demo": MARK},
            )
        )
        conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=now - timedelta(minutes=30),
                kind="note",
                author="client:coach-runner",
                entry_type="plan_change",
                for_date=later,
                session_id="demo:run",
                plan_version_id="12",
                text="De duurloop schuift twee dagen op: eerst herstellen na de zware rit.",
                meta={
                    "action": "plan.session_move",
                    "version": 12,
                    "plan": {
                        "action": "session_move",
                        "name": "Duurloop Z2 50'",
                        "sport": "Run",
                        "date": later.isoformat(),
                        "from_date": day.isoformat(),
                    },
                    "demo": MARK,
                },
            )
        )
        run = (now - timedelta(minutes=25)).isoformat(timespec="microseconds")
        for n, (action, name) in enumerate(
            (
                ("icu.update", "Duurloop Z2 50'"),
                ("icu.update", "Wk 1 · Basis · ~6u"),
                ("icu.create", "Run target"),
            )
        ):
            conn.execute(
                insert(audit_log).values(
                    at=now - timedelta(minutes=25) + timedelta(seconds=n),
                    actor="plan",
                    action=action,
                    detail={
                        "reason": f"plan mirror: {name}",
                        "before": None,
                        "after": {"name": name},
                        "action": action,
                        "revertable": False,
                        "mirror_run": run,
                        "demo": MARK,
                    },
                    author=plan_model.MIRROR_AUTHOR,
                    scope="plan.write",
                    target="demo",
                    outcome="ok",
                )
            )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in {"seed", "clear"}:
        sys.exit("usage: demo_plan_copy.py seed|clear")
    rt = Runtime.open(load_settings())
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()
    print(f"{action}: done")


if __name__ == "__main__":
    main()
