"""The automatic race or test mark (#84).

An activity on the day of a planned test session or a race of the same sport gets the
purpose ``test`` or ``race`` by itself. A planned test is a session named with test, FTP or
CSS that is not skipped: the same rule the Test evaluation task of the AI harness starts
from (``coach_runner.schedule.tests_done``). A race is one of VibeSynergy's own races
(``races.rows``).

- Of several activities of the test's sport that day, the longest is the test. A race marks
  each activity of its sports (a triathlon its swim, ride and run).
- A session you give as a trainer (``coached_by_me``) is never marked.
- The mark is never set over the athlete's own purpose, and an undone mark never comes back
  (``session_purpose.set_auto_mark``).

After a derive (``Runtime.derive``) the last ``LOOKBACK_DAYS`` are looked at, so an activity
that arrives a day late is still marked. A new mark computes its thresholds at once
(``threshold_tests.propose``): proposals the athlete approves.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.query import session_purpose
from vibesynergy.query.plan import family
from vibesynergy.store.schema import activity_unified

log = logging.getLogger(__name__)

TEST_NAME = re.compile(r"test|\bftp\b|\bcss\b", re.IGNORECASE)  # "Looptest", "FTP-test"
LOOKBACK_DAYS = 3
# The activity families a race of each sport covers (``races.SPORTS``).
RACE_FAMILIES = {
    "run": {"run"},
    "trail": {"run"},
    "ride": {"ride"},
    "swim": {"swim"},
    "triathlon": {"swim", "ride", "run", "multisport"},
    "duathlon": {"ride", "run", "multisport"},
}


def _family(sport: str | None) -> str | None:
    from vibesynergy.query import multisport

    if multisport.is_multisport(sport):
        return "multisport"
    return family(sport)


def test_sessions(engine: Engine, day: date) -> list[dict[str, Any]]:
    """The day's planned test sessions: named test, FTP or CSS, and not skipped."""
    from vibesynergy.query import plan_model

    sessions = plan_model.view(engine, day, day).get("sessions") or []
    return [
        s
        for s in sessions
        if TEST_NAME.search(s.get("name") or "") and (s.get("status") or "planned") != "skipped"
    ]


def _activities(engine: Engine, day: date) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(
                    activity_unified.c.group_id,
                    activity_unified.c.sport,
                    activity_unified.c.purpose,
                    activity_unified.c.name,
                    activity_unified.c.elapsed_s,
                    activity_unified.c.moving_s,
                ).where(activity_unified.c.local_date == day)
            ).mappings()
        ]


def _coached(a: dict[str, Any]) -> bool:
    """A session you give as a trainer: chosen, or by its title."""
    return a["purpose"] == session_purpose.COACHED or (
        session_purpose.derive(a["sport"], a.get("name")) == session_purpose.COACHED
    )


def tests_done(engine: Engine, day: date) -> list[tuple[dict[str, Any], list[str]]]:
    """Each planned test of the day with the activities of its sport that day."""
    sessions = test_sessions(engine, day)
    if not sessions:
        return []
    done = _activities(engine, day)
    out = []
    for session in sessions:
        ids = [
            a["group_id"]
            for a in done
            if family(a["sport"]) == family(session.get("sport")) and not _coached(a)
        ]
        if ids:
            out.append((session, ids))
    return out


def candidates(engine: Engine, day: date) -> list[dict[str, Any]]:
    """What the rule marks on ``day``: ``{"activity_id", "purpose", "reason"}`` each."""
    from vibesynergy.query import races

    done = [a for a in _activities(engine, day) if not _coached(a)]
    out: dict[str, dict[str, Any]] = {}
    for race in races.rows(engine, day, since=day, until=day):
        families = RACE_FAMILIES.get((race.get("sport") or "").lower(), set())
        for a in done:
            if _family(a["sport"]) in families:
                out[a["group_id"]] = {
                    "activity_id": a["group_id"],
                    "purpose": "race",
                    "reason": f"race {race['race_id']}",
                }
    for session, ids in tests_done(engine, day):
        mine = [a for a in done if a["group_id"] in ids and a["group_id"] not in out]
        if not mine:
            continue
        longest = max(mine, key=lambda a: float(a["elapsed_s"] or a["moving_s"] or 0))
        out[longest["group_id"]] = {
            "activity_id": longest["group_id"],
            "purpose": "test",
            "reason": f"planned test {session['session_id']}",
        }
    return list(out.values())


def run(engine: Engine, parquet, today: date, *, days: int = LOOKBACK_DAYS) -> dict[str, Any]:
    """Mark the last ``days`` (today included) and compute the new marks' thresholds."""
    from vibesynergy.query import threshold_tests

    marked = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        for item in candidates(engine, day):
            if session_purpose.set_auto_mark(
                engine, item["activity_id"], item["purpose"], reason=item["reason"]
            ):
                marked.append(item)
    if marked:
        session_purpose.backfill(engine)
    proposals = []
    for item in marked:
        try:
            result = threshold_tests.propose(engine, parquet, item["activity_id"])
        except Exception:  # a calculation never stops the marking
            log.exception("threshold calculation for %s failed", item["activity_id"])
            continue
        proposals.extend(result.get("proposed") or [])
    return {"marked": marked, "proposals": proposals}


def after_derive(rt) -> None:
    """Called by ``Runtime.derive``; a failure here never stops a sync."""
    from vibesynergy.core.timeutil import today_in

    try:
        run(rt.engine, rt.parquet, today_in(rt.settings.timezone))
    except Exception:
        log.exception("the automatic race and test mark failed")
