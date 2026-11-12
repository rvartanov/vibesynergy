"""VibeSynergy leads the plan: the readers use its own model, without intervals.icu. Garmin
copies fold into its sessions, the rest is not in the plan, weeks and targets come from
the model, and a session can be moved and skipped from the calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from vibesynergy.core.timeutil import Window
from vibesynergy.derive import unify
from vibesynergy.model import Activity, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import horizon, plan, plan_model, planning, schedule, training_plan
from vibesynergy.store.schema import icu_event, plan_link

MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
WEDNESDAY = date(2026, 11, 4)
RIDE = "Warmup\n- 10m 50-60%\n\nMain set\n- 40m 65-75%\n\nCooldown\n- 10m 50-60%"


def _session(runtime, day: date, name: str, sport: str, description: str) -> str:
    return plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=day,
        name=name,
        sport=sport,
        description=description,
        reason="plan",
        author="Claude",
    )["session_id"]


def _seed(runtime) -> dict[str, str]:
    """A plan made in VibeSynergy: one week with a note and a target, three sessions, and
    a Garmin calendar with a copy of the ride and a workout from elsewhere."""
    plan_model.create_plan(
        runtime.engine,
        name="Lake 27",
        start=MONDAY,
        end=date(2026, 11, 29),
        reason="new season",
        author="athlete",
    )
    plan_model.set_week(
        runtime.engine, week=1, note="Rustig instappen.", reason="plan", author="Claude"
    )
    plan_model.set_target(
        runtime.engine, week=1, sport="ride", time_s=11_700, reason="plan", author="Claude"
    )
    ids = {
        "ride": _session(runtime, MONDAY, "Duurrit Z2 60'", "Ride", RIDE),
        "run": _session(runtime, TUESDAY, "Duurloop Z2 40'", "Run", "- 40m Z2 HR"),
        "swim": _session(runtime, WEDNESDAY, "Zwemmen techniek", "Swim", "4x\n- 100mtr Z2 Pace"),
    }
    stored = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="2026-11",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                # Before the plan starts Garmin is still the plan.
                PlannedItem("workout:old", date(2026, 10, 29), "workout", "Old", sport="running"),
                PlannedItem("workout:copy", MONDAY, "workout", "Duurrit Z2 60'", sport="cycling"),
                PlannedItem("workout:tag", TUESDAY, "workout", "Run 40 (lake-27)", sport="running"),
                PlannedItem("workout:foreign", MONDAY, "workout", "Threshold", sport="cycling"),
                Activity(
                    "ride-mon",
                    datetime(2026, 11, 2, 9, tzinfo=UTC),
                    MONDAY,
                    "cycling",
                    name="Rit",
                    moving_s=3500.0,
                ),
            ],
            source="garmin",
            raw_item_id=stored.id,
        )
    unify.rebuild(runtime.engine)
    return ids


def _sessions(out: dict) -> list[dict]:
    return [i for i in out["items"] if i["kind"] in plan.TRAINING_KINDS]


def test_the_calendar_is_the_own_plan_without_any_intervals_event(runtime) -> None:
    ids = _seed(runtime)
    with runtime.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(icu_event)).scalar() == 0
        assert conn.execute(select(func.count()).select_from(plan_link)).scalar() == 0
    out = plan.plan(runtime.engine, Window(date(2026, 10, 29), WEDNESDAY), TUESDAY)
    assert out["plan_source"] == "vibesynergy" and out["plan_start"] == "2026-11-02"
    old, ride, run, swim = _sessions(out)
    assert (old["source"], old["plan_source"]) == ("garmin", "garmin")
    # A session without a copy in intervals.icu is shown, with what the model knows.
    assert (ride["source"], ride["source_id"], ride["session_id"]) == (
        "vibesynergy",
        ids["ride"],
        ids["ride"],
    )
    assert (ride["plan_source"], ride["ownership"], ride["in_plan"]) == (
        "vibesynergy",
        "vibesynergy_plan",
        True,
    )
    assert (ride["sport"], ride["extra"]["icu_type"], ride["extra"]["duration_s"]) == (
        "cycling",
        "Ride",
        3600,
    )
    assert "icu_id" not in ride["extra"] and "- 40m 65-75%" in ride["description"]
    assert (swim["sport"], swim["extra"]["distance_m"], swim["extra"]["duration_s"]) == (
        "swimming",
        400,
        600,
    )
    # Planned against done works on the model's sessions.
    assert (ride["status"], ride["done_by"][0]["id"]) == ("done", "garmin:ride-mon")
    assert (run["status"], swim["status"]) == ("today", "upcoming")
    # The Garmin workout from before the plan was missed; the foreign one never counts.
    assert out["adherence"] == {
        "planned": 2,
        "done": 1,
        "changed": 0,
        "missed": 1,
        "not_in_plan": 1,
    }
    assert [i["session_id"] for i in out["today"]] == [ids["run"]]


def test_a_garmin_copy_folds_in_and_a_foreign_workout_is_not_in_the_plan(runtime) -> None:
    _seed(runtime)
    out = plan.plan(runtime.engine, Window(MONDAY, WEDNESDAY), MONDAY)
    ride, run, _swim = _sessions(out)
    assert [(c["source_id"], c["match"]) for c in ride["garmin_copies"]] == [
        ("workout:copy", "name")
    ]
    # The plan's id is its tag in a Garmin title.
    assert [(c["source_id"], c["match"]) for c in run["garmin_copies"]] == [
        ("workout:tag", "plan_tag")
    ]
    (foreign,) = out["not_in_plan"]
    assert (foreign["source_id"], foreign["in_plan"], foreign["status"]) == (
        "workout:foreign",
        False,
        "not_in_plan",
    )
    # The horizon guard checks the model's sessions: the swim has no copy on Garmin.
    guard = horizon.status(runtime.engine, MONDAY, 3)
    assert guard["has_plan"] is True and guard["checked"] == 3
    assert guard["missing"] == [{"date": "2026-11-04", "title": "Zwemmen techniek"}]
    assert guard["fill"] == {"planned_through": "2026-11-04", "behind": False}
    assert [i["title"] for i in guard["not_in_plan"]] == ["Threshold"]


def test_a_skipped_session_is_not_planned(runtime) -> None:
    ids = _seed(runtime)
    plan_model.skip_session(runtime.engine, session_id=ids["run"], reason="ziek", author="Claude")
    out = plan.plan(runtime.engine, Window(MONDAY, WEDNESDAY), WEDNESDAY)
    assert [i["session_id"] for i in _sessions(out)] == [ids["ride"], ids["swim"]]
    assert out["adherence"]["planned"] == 1 and out["adherence"]["missed"] == 0
    # Its Garmin copy has no session to fold into any more.
    assert sorted(i["source_id"] for i in out["not_in_plan"]) == ["workout:foreign", "workout:tag"]
    # 60' ride and the swim's 10': the skipped run's 40' are gone.
    assert plan.planned_moving_s(runtime.engine, Window(MONDAY, WEDNESDAY)) == 4200


def test_a_session_with_a_copy_carries_the_intervals_event_id(runtime) -> None:
    # Garmin delivery and the activity pairing are still keyed by the intervals.icu event.
    from test_plan_model import EVENTS, _put

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    day = date(2026, 11, 17)
    before = plan.plan(runtime.engine, Window(day, day), day)
    plan_model.set_source(runtime.engine, "vibesynergy")
    after = plan.plan(runtime.engine, Window(day, day), day)
    (old,), (new,) = _sessions(before), _sessions(after)
    assert (old["source"], before["plan_source"]) == ("intervals_icu", "intervals_icu")
    assert (new["source"], after["plan_source"]) == ("vibesynergy", "vibesynergy")
    assert new["session_id"] == "lake27:2026-11-17:run-test" and "session_id" not in old
    assert new["extra"]["icu_id"] == old["extra"]["icu_id"] == "3"
    for key in ("title", "sport", "date", "status"):
        assert new[key] == old[key]
    assert new["extra"]["duration_s"] == old["extra"]["duration_s"] == 2520
    assert after["weeks"][0]["note"]["text"] == before["weeks"][0]["note"]["text"]
    assert [(t["family"], t["time_s"]) for t in after["weeks"][0]["targets"]] == [("run", 16200)]


def test_weeks_and_targets_come_from_the_model(runtime) -> None:
    _seed(runtime)
    out = plan.plan(runtime.engine, Window(MONDAY, WEDNESDAY), TUESDAY)
    (week,) = out["weeks"]
    assert (week["start"], week["end"], week["time_s"]) == ("2026-11-02", "2026-11-08", 11_700)
    assert week["note"] == {"id": "lake-27:week:01", "title": "Wk 1", "text": "Rustig instappen."}
    (goal,) = week["targets"]
    assert (goal["sport"], goal["family"], goal["time_s"], goal["done_s"]) == (
        "cycling",
        "ride",
        11_700,
        3500,
    )
    overview = training_plan.overview(runtime.engine, TUESDAY)
    assert overview["plan_source"] == "vibesynergy"
    (row,) = overview["weeks"]
    assert (row["number"], row["text"], row["when"]) == (1, "Rustig instappen.", "current")
    assert (row["time_s"], row["done_s"]) == (11_700, 3500)
    assert [i["id"] for i in overview["upcoming"]["items"]] == [
        "vibesynergy:lake-27:2026-11-03:run",
        "vibesynergy:lake-27:2026-11-04:swim",
    ]


def test_the_projection_uses_the_steps_of_the_model(runtime, monkeypatch) -> None:
    from vibesynergy.query import planned_load, workout_model

    _seed(runtime)
    seen: list[list] = []

    def load(steps, fam, inputs, **kwargs):
        seen.append(steps)
        return None

    def no_parse(_text):
        raise AssertionError("the model has the steps: no text is parsed")

    monkeypatch.setattr(planned_load, "planned_load", load)
    monkeypatch.setattr(workout_model, "parse_intervals", no_parse)
    overview = {"races": [], "weeks": [{"end": "2026-11-08"}]}
    fitness = {"rows": [["2026-11-02", 10.0, 20.0, 20.0]], "current": {}}
    planning.projection(runtime.engine, None, TUESDAY, overview=overview, fitness=fitness)
    assert [[s.get("duration_s") or s.get("times") for s in steps] for steps in seen] == [
        [2400],
        [4],
    ]


def test_the_calendar_moves_and_skips_a_session_without_an_intervals_copy(runtime) -> None:
    ids = _seed(runtime)
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    write = {"x-vibesynergy": "portal", "origin": "http://localhost"}
    span = {"start": "2026-11-02", "end": "2026-11-08"}

    def planned() -> dict[str, list[str]]:
        data = api.get("/api/schedule", params=span).json()["data"]
        assert data["editable"] is True
        return {d["date"]: [p["session_id"] for p in d["planned"]] for d in data["days"]}

    assert planned()["2026-11-03"] == [ids["run"]]
    moved = api.post(
        "/api/plan/session/move",
        json={"session_id": ids["run"], "date": "2026-11-05"},
        headers=write,
    )
    assert moved.status_code == 200
    days = planned()
    assert days["2026-11-03"] == [] and days["2026-11-05"] == [ids["run"]]
    skipped = api.post("/api/plan/session/skip", json={"session_id": ids["run"]}, headers=write)
    assert skipped.status_code == 200
    assert planned()["2026-11-05"] == []
    # The day's plan on the start page says where the plan lives.
    day = api.get("/api/today", params={"date": "2026-11-02"}).json()["data"]
    assert day["plan_source"] == "vibesynergy"
    assert [i["source_id"] for i in day["not_in_plan"]] == ["workout:foreign"]
    week = schedule.calendar(runtime.engine, MONDAY, date(2026, 11, 8), TUESDAY)
    assert week["weeks"][0]["note"] == "Rustig instappen."
    assert week["week_planned"]["2026-11-02"]["ride"] == {
        "time_s": 3600,
        "distance_m": 0,
        "target_s": 11_700,
    }


def test_a_plan_change_dates_from_the_session_not_from_the_intervals_copy(runtime) -> None:
    # The horizon guard gives Garmin delivery an hour after a plan change.
    from test_plan_model import EVENTS, _put

    from vibesynergy.store.schema import plan_version

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    plan_model.set_source(runtime.engine, "vibesynergy")
    moved = plan_model.move_session(
        runtime.engine,
        session_id="lake27:2026-11-17:run-test",
        day=date(2026, 11, 18),
        reason="druk",
        author="Claude",
    )
    with runtime.engine.connect() as conn:
        at = conn.execute(
            select(plan_version.c.created_at).where(plan_version.c.n == moved["version"])
        ).scalar()
    assert plan_model.changed_at(runtime.engine, ["3", "404"]) == {"3": at}
