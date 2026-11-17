"""Strength and pilates reach Garmin with exercise steps."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from vibesynergy.model import PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import garmin_strength as gs
from vibesynergy.query import horizon, icu_plan
from vibesynergy.sources.intervals_icu.mappers import map_events
from vibesynergy.store.schema import garmin_strength_delivery, planned_item

TODAY = date(2026, 11, 2)
DAY = date(2026, 11, 3)
BLOCK = """Anatomische adaptatie: licht gewicht, techniek eerst.
1. Goblet squat

[garmin]
warmup time=5m "Warming-up: 5' rustig fietsen"
SQUAT/GOBLET_SQUAT sets=2 reps=15 rest=75s "Goblet squat"
DEAD_BUG sets=2 reps=10 rest=45s "Dead bug"
PLANK/SIDE_PLANK sets=3 time=30s side=both rest=30s "Zijplank"

- 40m free"""
EVENT = {
    "id": 3001,
    "category": "WORKOUT",
    "type": "WeightTraining",
    "start_date_local": "2026-11-03T00:00:00",
    "name": "Kracht AA 40'",
    "description": BLOCK,
    "updated": "2026-10-30T08:15:00.000Z",
}


def test_block_becomes_garmin_steps_with_timed_holds_and_both_sides() -> None:
    block = gs.parse_block(BLOCK)
    assert block is not None
    workout = gs.build_workout("Kracht AA 40'", BLOCK, block)
    assert workout["sportType"]["sportTypeKey"] == "strength_training"
    assert workout["description"] == "Anatomische adaptatie: licht gewicht, techniek eerst."
    steps = workout["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[0]["endConditionValue"] == 300
    # Herstel until the lap button after the warm-up and between stations.
    lap = steps[1]
    assert lap["stepType"]["stepTypeKey"] == "recovery"
    assert lap["endCondition"]["conditionTypeKey"] == "lap.button" and "description" not in lap
    kinds = [
        s["type"] if s["type"] == "RepeatGroupDTO" else s["stepType"]["stepTypeKey"] for s in steps
    ]
    # Each exercise is one repeat; before a new station Garmin skips its last rest, and one
    # Herstel until lap names the minimum (#57).
    assert kinds == [
        "warmup", "recovery", "RepeatGroupDTO", "recovery", "RepeatGroupDTO", "recovery",
        "RepeatGroupDTO",
    ]  # fmt: skip
    squat = steps[2]
    assert squat["numberOfIterations"] == 2 and squat["skipLastRestStep"] is True
    work, rest = squat["workoutSteps"]
    assert (work["category"], work["exerciseName"]) == ("SQUAT", "GOBLET_SQUAT")
    assert work["endCondition"]["conditionTypeKey"] == "reps" and work["endConditionValue"] == 15
    assert rest["endConditionValue"] == 75
    assert steps[3]["endCondition"]["conditionTypeKey"] == "lap.button"
    assert steps[3]["description"] == "Herstel minstens 1:15 min, dan Lap"
    assert steps[5]["description"] == "Herstel minstens 45s, dan Lap"
    # A bare name resolves to its one category.
    assert steps[4]["workoutSteps"][0]["category"] == "HIP_STABILITY"
    # The last exercise: three sets, the last rest skipped because the workout ends.
    plank = steps[6]
    assert plank["numberOfIterations"] == 3 and plank["skipLastRestStep"] is True
    left, right, plank_rest = plank["workoutSteps"]
    assert left["endCondition"]["conditionTypeKey"] == "time" and left["endConditionValue"] == 30
    assert (left["description"], right["description"]) == ("Zijplank, links", "Zijplank, rechts")
    assert plank_rest["endConditionValue"] == 30
    orders = [steps[0]["stepOrder"], squat["stepOrder"], work["stepOrder"], rest["stepOrder"]]
    assert orders == [1, 3, 4, 5]


def test_one_station_keeps_its_rests_and_a_station_change_has_one_pause() -> None:
    text = """Kracht.

[garmin]
SQUAT/BARBELL_BACK_SQUAT sets=1 reps=5 rest=90s weight=40kg "Opwarmset"
SQUAT/BARBELL_BACK_SQUAT sets=3 reps=6 rest=180s weight=65kg "Back squat"
ROW/INVERTED_ROW sets=1 reps=8 "Inverted row"
"""
    block = gs.parse_block(text)
    assert block is not None
    steps = gs.build_workout("Kracht", text, block)["workoutSegments"][0]["workoutSteps"]
    kinds = [
        s["type"] if s["type"] == "RepeatGroupDTO" else s["stepType"]["stepTypeKey"] for s in steps
    ]
    # Warm-up set and its rest (same station), the work sets with their last rest skipped,
    # one pause until lap, and the last exercise without rest.
    assert kinds == ["interval", "recovery", "RepeatGroupDTO", "recovery", "interval"]
    assert steps[1]["endConditionValue"] == 90
    assert steps[2]["numberOfIterations"] == 3 and steps[2]["skipLastRestStep"] is True
    assert steps[3]["description"] == "Herstel minstens 3 min, dan Lap"
    assert "endConditionValue" not in steps[3]
    assert "skipLastRestStep" not in gs._repeat(2, [])


@pytest.mark.parametrize("line", ['pilates "Pilatesles"', 'pilates time=60m "Pilatesles"'])
def test_pilates_is_an_open_garmin_pilates_session(line: str) -> None:
    block = gs.parse_block(f"Pilatesles.\n\n[garmin]\n{line}")
    assert block is not None
    workout = gs.build_workout("Pilates 60'", "Pilatesles.", block)
    assert workout["sportType"] == {"sportTypeId": 8, "sportTypeKey": "pilates"}
    (step,) = workout["workoutSegments"][0]["workoutSteps"]
    assert step["endCondition"]["conditionTypeKey"] == "lap.button"
    assert "endConditionValue" not in step and step["description"] == "Pilatesles"


def test_no_marker_is_not_a_block() -> None:
    assert gs.parse_block("Pilatesles.\n\n- 60m free") is None


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("FOO_BAR reps=3", "not in Garmin's exercise library"),
        ("PLANK time=30s", "more than one category"),
        ("SQUAT/GOBLET_SQUAT sets=2", "either reps"),
        ("SQUAT/GOBLET_SQUAT reps=10 tempo=3", "unknown tempo"),
        ("SQUAT/GOBLET_SQUAT reps=0", "at least 1"),
        ("PLANK/PLANK time=fast", "not a duration"),
        ("warmup", "give the warmup a time"),
        ("SQUAT/GOBLET_SQUAT reps=10 weight=16", "not a weight"),
        ("SQUAT/GOBLET_SQUAT reps=10 weight=-5kg", "not a weight"),
        ("SQUAT/GOBLET_SQUAT reps=10 weight=0kg", "not a weight"),
    ],
)
def test_invalid_lines_are_refused(line: str, message: str) -> None:
    with pytest.raises(gs.BlockError, match=message):
        gs.parse_block(f"[garmin]\n{line}")


def test_plan_write_refuses_an_invalid_block_before_calling_intervals(runtime) -> None:
    class Client:
        def create_event(self, body):  # pragma: no cover - must not be reached
            raise AssertionError("intervals.icu was called")

    with pytest.raises(icu_plan.IcuWriteError, match="Garmin"):
        icu_plan.create_event(
            runtime.engine,
            Client(),  # type: ignore[arg-type]
            reason="Kracht",
            category="WORKOUT",
            start_date=DAY,
            name="Kracht",
            sport="WeightTraining",
            description="[garmin]\nNOT_AN_EXERCISE reps=5",
        )


class Garmin:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.next = 500

    def upload_workout(self, workout):
        self.calls.append(("upload", workout["workoutName"]))
        self.next += 1
        return {"workoutId": self.next}

    def schedule_workout(self, workout_id, day):
        self.calls.append(("schedule", workout_id, day))
        return {"workoutScheduleId": workout_id + 10_000}

    def unschedule_workout(self, scheduled_id):
        self.calls.append(("unschedule", scheduled_id))

    def delete_workout(self, workout_id):
        self.calls.append(("delete", workout_id))


def _event(runtime, event: dict) -> None:
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        payload=json.dumps([event]).encode(),
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(stored.id))),
            source="intervals_icu",
            raw_item_id=stored.id,
        )


def _garmin_copy(runtime, scheduled_id: int, title: str, workout_id: int) -> None:
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
                PlannedItem(
                    f"workout:{scheduled_id}",
                    DAY,
                    "workout",
                    title=title,
                    sport="strength_training",
                    extra={"workoutId": workout_id},
                )
            ],
            source="garmin",
            raw_item_id=stored.id,
        )


def _delivery(runtime) -> dict:
    with runtime.engine.connect() as conn:
        return dict(conn.execute(select(garmin_strength_delivery)).mappings().one())


def test_delivers_once_and_removes_intervals_copy(runtime) -> None:
    _event(runtime, EVENT)
    _garmin_copy(runtime, 900, "Kracht AA 40'", 77)  # intervals.icu's earlier push
    _garmin_copy(runtime, 901, "Eigen sessie", 78)  # the athlete's own: stays
    api = Garmin()
    first = gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert first == {"delivered": 1, "withdrawn": 0, "errors": 0, "copies_removed": 1}
    assert api.calls == [
        ("upload", "⚡ Kracht AA 40'"),
        ("schedule", 501, "2026-11-03"),
        ("unschedule", 900),
        ("delete", 77),
    ]
    with runtime.engine.begin() as conn:  # the next calendar sync: the copy is gone
        conn.execute(planned_item.delete().where(planned_item.c.source_id == "workout:900"))
    _garmin_copy(runtime, 10501, "Kracht AA 40'", 501)  # ours, as the calendar sync sees it
    row = _delivery(runtime)
    assert (row["state"], row["workout_id"], row["scheduled_id"]) == ("delivered", 501, 10501)

    # Nothing changed: no Garmin login at all.
    def no_login():
        raise AssertionError("logged in to Garmin without a reason")

    assert gs.reconcile(runtime.engine, no_login, TODAY)["delivered"] == 0

    # An edit in intervals.icu replaces ours.
    _event(runtime, {**EVENT, "description": BLOCK.replace("reps=15", "reps=20")})
    api.calls.clear()
    gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert api.calls[:2] == [("unschedule", 10501), ("delete", 501)]
    assert api.calls[2] == ("upload", "⚡ Kracht AA 40'")

    # The block removed: ours comes off the calendar.
    _event(runtime, {**EVENT, "description": "Anatomische adaptatie.\n\n- 40m free"})
    api.calls.clear()
    result = gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert result["withdrawn"] == 1
    assert api.calls == [("unschedule", 10502), ("delete", 502)]
    assert _delivery(runtime)["state"] == "withdrawn"


def test_a_failed_upload_is_recorded_and_retried(runtime) -> None:
    _event(runtime, EVENT)

    class Down(Garmin):
        def upload_workout(self, workout):
            raise RuntimeError("Garmin Connect unreachable")

    result = gs.reconcile(runtime.engine, Down, TODAY)
    assert result["errors"] == 1
    assert "unreachable" in _delivery(runtime)["last_error"]
    api = Garmin()
    assert gs.reconcile(runtime.engine, lambda: api, TODAY)["delivered"] == 1


def test_a_session_removed_in_garmin_is_put_back_after_the_next_calendar_sync(runtime) -> None:
    from datetime import UTC, datetime, timedelta

    from vibesynergy.store.schema import dataset_state

    _event(runtime, EVENT)
    api = Garmin()
    gs.reconcile(runtime.engine, lambda: api, TODAY)
    delivered_at = _delivery(runtime)["updated_at"]

    def synced(at):
        with runtime.engine.begin() as conn:
            conn.execute(dataset_state.delete())
            conn.execute(
                dataset_state.insert().values(
                    source="garmin", dataset="scheduled_workouts", last_success_at=at
                )
            )

    # A sync right after delivery may not have it yet: no action.
    synced(delivered_at + timedelta(minutes=1))
    api.calls.clear()
    gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert api.calls == []

    # A later sync without it: delivered again.
    synced(datetime.now(UTC) + timedelta(hours=1))
    gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert ("upload", "⚡ Kracht AA 40'") in api.calls
    assert _delivery(runtime)["state"] == "delivered"

    # Listed by the next sync: left alone.
    _garmin_copy(runtime, _delivery(runtime)["scheduled_id"], "Kracht AA 40'", 502)
    synced(datetime.now(UTC) + timedelta(hours=2))
    api.calls.clear()
    gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert api.calls == []


def test_weight_fills_garmin_weight_field_per_set_and_bw_stays_empty() -> None:
    block = gs.parse_block(
        '[garmin]\nSQUAT/GOBLET_SQUAT sets=3 reps=15 weight=16kg rest=75s "Goblet squat"\n'
        'SQUAT/GOBLET_SQUAT reps=10 weight=27,5kg "Zwaarder"\n'
        "DEADLIFT/ROMANIAN_DEADLIFT reps=12 weight=35lb\n"
        "SQUAT/GOBLET_SQUAT reps=20 weight=bw"
    )
    steps = gs.build_workout("Kracht", None, block)["workoutSegments"][0]["workoutSteps"]
    first = steps[0]["workoutSteps"][0]
    assert steps[0]["numberOfIterations"] == 3
    assert (first["weightValue"], first["weightUnit"]["unitKey"]) == (16.0, "kilogram")
    # The two goblet squat lines are one station: no lap pause between them.
    assert steps[1]["weightValue"] == 27.5
    assert (steps[3]["weightValue"], steps[3]["weightUnit"]["unitKey"]) == (35.0, "pound")
    assert "weightValue" not in steps[5]


def test_a_block_without_weight_keeps_its_fingerprint() -> None:
    """Adding weight= must not redeliver every existing workout."""
    from vibesynergy.query import evidence

    block = gs.parse_block(BLOCK)
    old = evidence.digest(
        {
            "name": "Kracht",
            "date": "2026-11-05",
            "intro": gs.intro(BLOCK),
            "sport": block.sport,
            "steps": [
                {k: v for k, v in gs.asdict(step).items() if k != "weight"} for step in block.steps
            ],
            "transitions": 4,  # bumped on purpose: Garmin skipLastRestStep (#57)
        }
    )
    assert gs.fingerprint("Kracht", date(2026, 11, 5), block, BLOCK) == old


def test_warm_up_and_work_sets_of_one_exercise_are_one_station() -> None:
    block = gs.parse_block(
        "[garmin]\nwarmup time=5m\n"
        'SQUAT/GOBLET_SQUAT reps=8 rest=45s "Opwarmset"\n'
        'SQUAT/GOBLET_SQUAT sets=3 reps=15 rest=75s "Goblet squat"\n'
        'DEADLIFT/ROMANIAN_DEADLIFT sets=2 reps=12 rest=75s "RDL"'
    )
    steps = gs.build_workout("Kracht", None, block)["workoutSegments"][0]["workoutSteps"]
    laps = [
        i
        for i, s in enumerate(steps)
        if s.get("endCondition", {}).get("conditionTypeKey") == "lap.button"
    ]
    # after the warm-up, then only before the RDL (not between the two goblet squat lines)
    assert (
        len(laps) == 2
        and steps[laps[1] + 1]["workoutSteps"][0]["exerciseName"] == "ROMANIAN_DEADLIFT"
    )


def _no_calendar(monkeypatch) -> None:
    monkeypatch.setattr(horizon.plan_q, "plan", lambda *a, **k: {"items": [], "not_in_plan": []})


def test_outdated_content_and_short_plan_are_reported(runtime, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    _no_calendar(monkeypatch)
    _event(runtime, EVENT)  # Kracht on 3-11, changed 30-10
    now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    out = horizon.status(runtime.engine, TODAY, 9, now=now)
    # Not delivered yet, and the change is days old: outdated. Plan ends 3-11 of 2..10-11.
    assert [e["title"] for e in out["outdated"]] == ["⚡ Kracht AA 40'"]
    assert out["fill"] == {"planned_through": "2026-11-03", "behind": True}
    assert out["level"] == "warn"

    gs.reconcile(runtime.engine, Garmin, TODAY)
    assert horizon.status(runtime.engine, TODAY, 9, now=now)["outdated"] == []

    # Edited minutes ago: the delivery job still has time.
    fresh = "2026-11-02T08:50:00.000Z"
    _event(
        runtime,
        {**EVENT, "description": EVENT["description"].replace("15", "20"), "updated": fresh},
    )
    assert horizon.status(runtime.engine, TODAY, 9, now=now)["outdated"] == []
    later = now + timedelta(hours=2)
    assert len(horizon.status(runtime.engine, TODAY, 9, now=later)["outdated"]) == 1


def test_one_rest_day_at_the_end_is_not_behind(runtime, monkeypatch) -> None:

    _no_calendar(monkeypatch)
    _event(runtime, {**EVENT, "start_date_local": "2026-11-09T00:00:00"})
    # Window 2..10-11, last workout 9-11: one empty day is a rest day.
    fill = horizon.status(runtime.engine, date(2026, 11, 2), 9)["fill"]
    assert fill == {"planned_through": "2026-11-09", "behind": False}


def test_without_a_plan_nothing_is_behind(runtime, monkeypatch) -> None:
    """A new user has no plan: that is "no plan", not a plan that fell behind (the page
    crashed on the missing date)."""
    _no_calendar(monkeypatch)
    out = horizon.status(runtime.engine, TODAY, 9)
    assert out["has_plan"] is False
    assert out["fill"] == {"planned_through": None, "behind": False}
    assert out["level"] == "ok"

    # A plan whose last workout was last week did fall behind, with no date to name.
    _event(runtime, {**EVENT, "start_date_local": "2026-10-27T00:00:00"})
    out = horizon.status(runtime.engine, TODAY, 9)
    assert out["has_plan"] is True
    assert out["fill"] == {"planned_through": None, "behind": True}

    # Long ago is no plan any more.
    out = horizon.status(runtime.engine, date(2027, 3, 1), 9)
    assert out["has_plan"] is False and out["fill"]["behind"] is False


def test_step_text_must_name_the_same_kit_as_the_exercise() -> None:
    """a barbell Bulgarian split squat whose text said "dumbbells"."""
    wrong = (
        "Kracht\n\n[garmin]\n"
        "LUNGE/BARBELL_BULGARIAN_SPLIT_SQUAT sets=3 reps=10 weight=10kg "
        '"Split squat, 2x10 kg dumbbells"'
    )
    with pytest.raises(gs.BlockError, match="text says dumbbell"):
        gs.check_description(wrong)
    for fine in (
        'LUNGE/BARBELL_BULGARIAN_SPLIT_SQUAT sets=3 reps=10 weight=20kg "Split squat met stang"',
        'CALF_RAISE/STANDING_BARBELL_CALF_RAISE sets=3 reps=15 "Kuitheffing (stang of machine)"',
        'DEADLIFT/ROMANIAN_DEADLIFT sets=3 reps=10 "RDL, dumbbells mag ook"',  # no kit in name
    ):
        gs.check_description(f"Kracht\n\n[garmin]\n{fine}")


def test_the_mark_goes_in_front_once_and_names_compare_without_it() -> None:
    assert gs.marked("Kracht AA 40'") == "⚡ Kracht AA 40'"
    assert gs.marked("⚡ Kracht AA 40'") == "⚡ Kracht AA 40'"
    assert gs.name_key("⚡ Kracht AA 40'") == gs.name_key("Kracht  AA 40'")


def test_a_chosen_mark_renames_and_still_finds_intervals_copies(runtime) -> None:
    _event(runtime, EVENT)
    _garmin_copy(runtime, 900, "Kracht AA 40'", 77)  # intervals.icu's earlier push
    api = Garmin()
    gs.reconcile(runtime.engine, lambda: api, TODAY, mark="\U0001f3cb️")
    assert api.calls[0] == ("upload", "\U0001f3cb️ Kracht AA 40'")
    assert ("delete", 77) in api.calls
    # The ledger knows the chosen name: the guard sees nothing outdated with that mark.
    out = horizon.status(runtime.engine, TODAY, 9, (), mark="\U0001f3cb️")
    assert out["outdated"] == []


def test_the_mark_preference_takes_symbols_only(tmp_path) -> None:
    from vibesynergy.portal import prefs

    assert gs.mark_for(tmp_path) == gs.MARK
    prefs.save(tmp_path, {"garmin_mark": " \U0001f7e3 "})
    assert gs.mark_for(tmp_path) == "\U0001f7e3"
    for bad in ("VS", "", "a⚡", "⚡ ⚡", "_"):
        with pytest.raises(ValueError):
            prefs.save(tmp_path, {"garmin_mark": bad})
    prefs.save(tmp_path, {"garmin_mark": None})
    assert gs.mark_for(tmp_path) == gs.MARK


def test_a_mark_per_workout_type_with_defaults(tmp_path) -> None:
    from vibesynergy.portal import prefs

    prefs.save(tmp_path, {"garmin_mark_mode": "per_type", "garmin_marks": {"run": "\U0001f7e2"}})
    marks = gs.mark_for(tmp_path)
    assert gs.marked("Duurloop", marks, "run") == "\U0001f7e2 Duurloop"
    assert gs.marked("Kracht", marks, "strength_training") == "\U0001f3cb\ufe0f Kracht"
    # Multisport joins its sports' marks in order, unless it has a mark of its own.
    brick = gs.marked("Rit + Brickloop", marks, ("ride", "run"))
    assert brick == "\U0001f6b4\U0001f7e2 Rit + Brickloop"
    tri = gs.marked("Triatlon", marks, ("swim", "ride", "run"))
    assert tri == "\U0001f3ca\U0001f6b4\U0001f7e2 Triatlon"
    prefs.save(tmp_path, {"garmin_marks": {"multisport": "\U0001f9f1", "brick": "x"}})
    assert prefs.load(tmp_path)["garmin_marks"] == {"multisport": "\U0001f9f1"}
    marks = gs.mark_for(tmp_path)
    assert gs.marked("Rit + Brickloop", marks, ("ride", "run")) == "\U0001f9f1 Rit + Brickloop"
    with pytest.raises(ValueError):
        prefs.save(tmp_path, {"garmin_marks": {"kayak": "\U0001f6f6"}})
    prefs.save(tmp_path, {"garmin_mark_mode": "one"})
    assert gs.marked("Duurloop", gs.mark_for(tmp_path), "run") == "\u26a1 Duurloop"
    assert gs.marked("Rit + Loop", gs.mark_for(tmp_path), ("ride", "run")) == "\u26a1 Rit + Loop"


def test_no_mark_keeps_plain_names_and_the_guard_quiet(runtime, tmp_path) -> None:
    from vibesynergy.portal import prefs

    prefs.save(tmp_path, {"garmin_mark_mode": "none"})
    marks = gs.mark_for(tmp_path)
    assert gs.marked("Duurloop", marks, "run") == "Duurloop"
    _event(runtime, EVENT)
    api = Garmin()
    gs.reconcile(runtime.engine, lambda: api, TODAY, mark=marks)
    assert api.calls[0] == ("upload", "Kracht AA 40'")
    assert horizon.status(runtime.engine, TODAY, 9, (), mark=marks)["outdated"] == []


def test_per_type_marks_rename_strength_and_keep_the_guard_quiet(runtime) -> None:
    _event(runtime, EVENT)
    marks = gs.marks_from({"garmin_mark_mode": "per_type"})
    api = Garmin()
    gs.reconcile(runtime.engine, lambda: api, TODAY, mark=marks)
    assert api.calls[0] == ("upload", "\U0001f3cb️ Kracht AA 40'")
    assert horizon.status(runtime.engine, TODAY, 9, (), mark=marks)["outdated"] == []
