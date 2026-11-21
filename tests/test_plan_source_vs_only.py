"""VibeSynergy is the only source of the plan (#111).

Change every kind of intervals.icu event (a planned workout, one added there, a week note, a
weekly target, a race, a season) and nothing changes in the advice, the AI coach's context,
the dashboard, ``vibesynergy_today``, ``vibesynergy_week`` or ``vibesynergy_plan``.
intervals.icu only holds a copy that it passes on to other services.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import insert, select, update
from test_mcp_today import call
from test_plan_intervals import MONDAY, SATURDAY, _seed

from vibesynergy.coach_runner import context
from vibesynergy.core.timeutil import Window
from vibesynergy.query import dashboard, plan, training_plan, week_progress
from vibesynergy.store.schema import icu_event

BLOCKS = ("today", "plan_horizon", "plan_next_week", "week", "races", "plan_match")
_VOLATILE = ("_at", "queried", "generated", "age_s", "elapsed")


def _scrub(value: Any) -> Any:
    """Drop clock values (when a read ran), keep everything a reader derives."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not any(m in str(k) for m in _VOLATILE)}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _snapshot(runtime, monkeypatch, day: date) -> str:
    monkeypatch.setattr("vibesynergy.mcp.server.today_in", lambda tz: day)
    board = dashboard.dashboard(
        runtime.engine,
        day,
        timezone=runtime.settings.timezone,
        athlete=runtime.settings.athlete,
        sources=[],
    )
    out: dict[str, Any] = {
        "dashboard": board,
        "verdict": dashboard.rules_verdict(runtime, day),
        "plan": plan.plan(runtime.engine, Window(SATURDAY, date(2027, 7, 31)), day),
        "races": plan.calendar_races(runtime.engine, day),
        "race_phase": plan.race_phase(runtime.engine, day),
        "week_progress": week_progress.week_progress(runtime.engine, MONDAY, day),
        "training_plan": training_plan.overview(runtime.engine, day),
        "context": {
            name: context.BLOCKS[name].build(runtime, day, {"task": "morning"}) for name in BLOCKS
        },
    }
    for tool, args in (
        ("vibesynergy_today", {}),
        ("vibesynergy_week", {}),
        ("vibesynergy_plan", {"period": "this_week"}),
        ("vibesynergy_plan_model", {}),
        ("vibesynergy_coach_state", {}),
    ):
        out[tool] = call(runtime, tool, **args)["data"]
    return json.dumps(_scrub(out), sort_keys=True, default=str, ensure_ascii=False)


def _change_intervals(runtime) -> None:
    """What another app or a hand edit could do to intervals.icu's calendar."""
    later = datetime(2026, 11, 2, 6, tzinfo=UTC)
    with runtime.engine.begin() as conn:
        # A plan session's copy: other day, name, steps and time.
        conn.execute(
            update(icu_event)
            .where(icu_event.c.id == "3")
            .values(
                date=date(2026, 11, 4),
                name="Drempel 4x8'",
                description="- 4x8m Z4 HR",
                raw={"moving_time": 5400, "icu_training_load": 140},
                updated=later,
            )
        )
        # The week note, a weekly target and the races.
        conn.execute(
            update(icu_event)
            .where(icu_event.c.id == "5")
            .values(description="Alles anders.", name="Wk 9 · Piek", updated=later)
        )
        conn.execute(
            update(icu_event)
            .where(icu_event.c.id == "6")
            .values(targets={"time_target": 36000}, updated=later)
        )
        conn.execute(
            update(icu_event).where(icu_event.c.id == "8").values(category="RACE_C", updated=later)
        )
        template = dict(
            conn.execute(select(icu_event).where(icu_event.c.id == "1")).mappings().one()
        )
        added = [
            # Garmin Coach style workouts added in intervals.icu, with and without the plan's tag.
            {"id": "90", "name": "Basis 45'", "external_id": None, "date": MONDAY},
            {"id": "91", "name": "Drempel", "external_id": "lake27:2026-11-03:run"},
            {"id": "92", "category": "RACE_A", "name": "Other race", "date": date(2026, 12, 6)},
            {"id": "93", "category": "SEASON_START", "name": "Other season"},
            {"id": "94", "category": "NOTE", "name": "Wk 2", "external_id": "lake27:week:02"},
        ]
        for row in added:
            conn.execute(
                insert(icu_event).values(
                    {
                        **template,
                        "category": "WORKOUT",
                        "date": date(2026, 11, 3),
                        "type": "Run",
                        "updated": later,
                        **row,
                    }
                )
            )


def test_an_intervals_icu_change_changes_nothing(runtime, monkeypatch) -> None:
    from vibesynergy.query import plan_model, races

    _seed(runtime)
    days = (SATURDAY, MONDAY)
    before = {day: _snapshot(runtime, monkeypatch, day) for day in days}
    assert "Lange rit Z2 1u45" in before[MONDAY] and "Rustig instappen." in before[MONDAY]
    assert "Example triathlon 2027" in before[MONDAY] and "WINTER TRAIL" in before[MONDAY]
    _change_intervals(runtime)
    for day in days:
        after = _snapshot(runtime, monkeypatch, day)
        assert after == before[day]
        assert "Drempel 4x8'" not in after and "Basis 45'" not in after
        assert "Alles anders." not in after and "Other race" not in after

    # The same reads do follow a change in VibeSynergy's own plan and races.
    plan_model.set_week(
        runtime.engine, week=1, note="Een nieuwe week.", reason="test", author="test"
    )
    (winter,) = [r for r in races.rows(runtime.engine, MONDAY) if r["title"] == "WINTER TRAIL"]
    races.set_priority(runtime.engine, winter["race_id"], "B")
    changed = _snapshot(runtime, monkeypatch, MONDAY)
    assert changed != before[MONDAY] and "Een nieuwe week." in changed


def test_garmin_coach_workouts_are_not_in_the_plan_and_no_advice_input(runtime) -> None:
    """Garmin calendar items the plan has not got stay "not in plan"."""
    from vibesynergy.model import PlannedItem
    from vibesynergy.normalize.normaliser import write_records

    _seed(runtime)
    before = plan.plan(runtime.engine, Window(MONDAY, MONDAY), MONDAY)
    stored = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="coach",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem("coach-basis", MONDAY, "plan_workout", "Basis", sport="running"),
                PlannedItem("coach-drempel", MONDAY, "plan_workout", "Drempel", sport="running"),
            ],
            source="garmin",
            raw_item_id=stored.id,
        )
    after = plan.plan(runtime.engine, Window(MONDAY, MONDAY), MONDAY)
    # Garmin Coach's workouts are not shown anywhere in the plan (#153).
    assert "not_in_plan" not in after
    assert not {"coach-basis", "coach-drempel"} & {i["source_id"] for i in after["items"]}
    assert "Drempel" not in json.dumps(after, default=str)
    assert [i["title"] for i in after["today"]] == [i["title"] for i in before["today"]]
    assert after["adherence"]["planned"] == before["adherence"]["planned"]
    verdict = dashboard.rules_verdict(runtime, MONDAY)
    assert "Drempel" not in json.dumps(verdict, default=str)
