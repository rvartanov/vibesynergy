"""Remove the races and events the "races" help video adds to the demo (#91).

The video adds a race and a training camp through the portal form. ``clear`` takes them out
of the demo again (rows, their history, feed messages and their copies' links), so other
videos and the next recording start from the same demo.

Only for the help-video demo home (``~/vs-help-home``), never for real data. Run with the
demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \\
        uv run python tools/help_videos/demo_races.py clear
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import delete, func

from vibesynergy.core.config import load_settings
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import audit_log, coach_decision_log, plan_link, race

DEMO_HOME = Path.home() / "vs-help-home"
NAMES = ("Zevenheuvelenloop", "Trainingskamp Mallorca")


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        ids = [r.id for r in conn.execute(race.select().where(race.c.name.in_(NAMES)))]
        for ident in ids:
            conn.execute(delete(audit_log).where(audit_log.c.target == f"race:{ident}"))
            conn.execute(  # the "Plan change" messages on the coach feed
                delete(coach_decision_log).where(
                    func.json_extract(coach_decision_log.c.meta, "$.plan.race_id") == ident
                )
            )
            conn.execute(delete(plan_link).where(plan_link.c.session_id == f"race:{ident}"))
        conn.execute(delete(race).where(race.c.id.in_(ids)))


def main() -> None:
    _guard()
    if (sys.argv[1] if len(sys.argv) > 1 else "") != "clear":
        sys.exit("usage: demo_races.py clear")
    rt = Runtime.open(load_settings(None))
    try:
        clear(rt)
    finally:
        rt.close()


if __name__ == "__main__":
    main()
