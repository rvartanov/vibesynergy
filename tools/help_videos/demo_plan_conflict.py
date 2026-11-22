"""Let another app overwrite the demo's plan in intervals.icu, or take that out again (#110).

``seed`` notes three put-backs of two sessions, an hour apart, by an app connected to
intervals.icu, so VibeSynergy has stopped putting that mirror back: Waiting for you →
Notices shows the line with "put my plan back". ``clear`` removes those rows and the hold.

Only for the help-video demo home (``~/vs-help-home``), never for real data. Run with the
demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \\
        uv run python tools/help_videos/demo_plan_conflict.py seed|clear
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import plan_conflict
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import mirror_hold, mirror_putback, plan_session

DEMO_HOME = Path.home() / "vs-help-home"
RUN = "demo-plan-conflict"
FALLBACK = ["Duurloop Z2 45'", "Intervallen 5×3'"]  # noqa: RUF001 - the workout's real name


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        conn.execute(delete(mirror_putback).where(mirror_putback.c.run.like(f"{RUN}%")))
        conn.execute(delete(mirror_hold).where(mirror_hold.c.mirror == plan_conflict.INTERVALS))


def seed(rt: Runtime) -> None:
    clear(rt)
    with rt.engine.connect() as conn:
        found = conn.execute(
            select(plan_session.c.id, plan_session.c.name)
            .where(plan_session.c.status == "planned")
            .order_by(plan_session.c.date)
            .limit(2)
        ).all()
    sessions = [(r.id, r.name) for r in found] or [(f"demo:{n}", n) for n in FALLBACK]
    now = utc_now()
    rules = plan_conflict.limits(rt.settings)
    for n in range(rules.stop_after):
        at = now - timedelta(hours=rules.stop_after - n)
        plan_conflict.record(
            rt.engine,
            plan_conflict.INTERVALS,
            [
                {"item": ident, "name": name, "changed_at": at, "writer": "icu-app:4242"}
                for ident, name in sessions
            ],
            run=f"{RUN}-{n}",
            rules=rules,
            now=at,
        )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in {"seed", "clear"}:
        sys.exit("usage: demo_plan_conflict.py seed|clear")
    rt = Runtime.open(load_settings())
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()


if __name__ == "__main__":
    main()
