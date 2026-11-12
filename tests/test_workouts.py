"""Intervals, comparing activities and gear mileage."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from vibesynergy.derive import unify
from vibesynergy.model import Activity, ActivityLap
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import workouts

DAY = date(2026, 9, 25)
T = datetime(2026, 9, 25, 12, tzinfo=UTC)


def _put(rt, source: str, records, key: str) -> None:
    stored = rt.raw.put(
        source=source, dataset="test", item_key=key, payload=key.encode(), content_type="text/plain"
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)


def _session(rt, source_id: str, powers: list[tuple[float, float]], **kw) -> None:
    laps = [
        ActivityLap(
            source_id, i + 1, elapsed_s=s, moving_s=s, distance_m=s * 3, avg_power=p, avg_hr=140.0
        )
        for i, (s, p) in enumerate(powers)
    ]
    run = Activity(
        source_id, T, DAY, "running", elapsed_s=sum(s for s, _ in powers), distance_m=9000.0, **kw
    )
    _put(rt, "garmin", [run, *laps], source_id)


def test_intervals_split_work_and_recovery(runtime) -> None:
    _session(
        runtime,
        "1",
        [(600, 450), (240, 590), (120, 300), (240, 600), (120, 290), (240, 580), (600, 440)],
    )
    unify.rebuild(runtime.engine)
    out = workouts.intervals(runtime.engine, runtime.parquet, "garmin:1")
    assert out["basis"] == "power" and out["structure"] == "3 work intervals"
    assert [lap["kind"] for lap in out["laps"]] == [
        "recovery",
        "work",
        "recovery",
        "work",
        "recovery",
        "work",
        "recovery",
    ]
    assert [lap["lap"] for lap in out["laps"]] == list(range(1, 8))
    assert out["work"]["avg_power"] == pytest.approx(590) and out["fade_pct"] == pytest.approx(-1.7)


def test_steady_session_is_not_split(runtime) -> None:
    _session(runtime, "2", [(600, 400), (600, 410), (600, 395)])
    unify.rebuild(runtime.engine)
    out = workouts.intervals(runtime.engine, runtime.parquet, "garmin:2")
    assert out["structure"] == "steady" and {lap["kind"] for lap in out["laps"]} == {"steady"}


def test_compare(runtime) -> None:
    _session(runtime, "3", [(600, 400)], avg_hr=140.0)
    _session(runtime, "4", [(600, 400)], avg_hr=150.0)
    unify.rebuild(runtime.engine)
    out = workouts.compare(runtime.engine, runtime.parquet, ["garmin:3", "garmin:4"])
    assert out["activities"][1]["vs_first"]["avg_hr"] == 10
    with pytest.raises(ValueError):
        workouts.compare(runtime.engine, runtime.parquet, ["garmin:3"])


def test_gear_mileage_and_shoe_warning(runtime) -> None:
    gear = {
        "g1": {"name": "Old trail shoes", "distance": 820_000, "retired": False},
        "g2": {"name": "New shoes", "distance": 120_000, "retired": False},
        "g3": {"name": "Retired", "distance": 1_200_000, "retired": True},
        "b1": {"name": "Road bike", "distance": 4_000_000, "retired": False},
    }
    runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps(gear).encode(),
        content_type="application/json",
    )
    _put(
        runtime,
        "strava",
        [Activity("s1", T, DAY, "Run", distance_m=10_000.0, extra={"gear_id": "g1"})],
        "s1",
    )
    result = workouts.gear(runtime.engine, runtime.raw, DAY)
    out = {g["id"]: g for g in result["gear"]}
    assert out["g1"]["total_km"] == 820 and "past the usual" in out["g1"]["warning"]
    assert out["g1"]["activities_seen"] == 1 and out["g1"]["used_last_30_days"]
    assert "warning" not in out["g2"] and "warning" not in out["g3"] and out["b1"]["kind"] == "bike"
    # Identity is namespaced per source (V-C6): never bare "g1", so a second source using
    # the same native id cannot silently collide with or merge into this record.
    assert out["g1"]["ref"] == "strava:g1" and out["g1"]["source"] == "strava"
    # No Garmin gear dataset has ever been collected in this test: reported as missing, not
    # silently absent from the response.
    assert result["other_sources"]["garmin"] == {
        "state": "missing",
        "collected_at": None,
        "item_count": None,
        "reference": None,
    }
    assert result["activity_gear"] == []
    assert result["references"][0]["source"] == "strava"
    assert result["references"][0]["dataset"] == "gear"
    assert len(result["references"][0]["sha256"]) == 64


def test_own_gear_settings_status_limit_and_note(runtime) -> None:
    gear = {
        "g1": {"name": "Trail", "distance": 700_000},
        "g2": {"name": "Race", "distance": 650_000},
        "b1": {"name": "Road bike", "distance": 4_000_000},
    }
    runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps(gear).encode(),
        content_type="application/json",
    )
    before = {g["id"]: g for g in workouts.gear(runtime.engine, runtime.raw, DAY)["gear"]}
    assert before["g1"]["status"] == "active" and before["g1"]["life_km"] == 800
    assert before["g1"]["warning_level"] == "near" and before["b1"]["life_km"] is None

    workouts.set_gear(runtime.engine, "strava:g1", status="retired")
    workouts.set_gear(runtime.engine, "strava:g2", life_km=700, note="Alleen wedstrijden")
    workouts.set_gear(runtime.engine, "strava:b1", status="spare", life_km=3000)
    out = {g["id"]: g for g in workouts.gear(runtime.engine, runtime.raw, DAY)["gear"]}
    assert out["g1"]["retired"] and "warning" not in out["g1"]  # retired: no nagging
    assert out["g2"]["warning_level"] == "near" and out["g2"]["note"] == "Alleen wedstrijden"
    assert "warning" not in out["b1"]  # a spare is not warned about
    workouts.set_gear(runtime.engine, "strava:g2", life_km=600)
    g2 = next(g for g in workouts.gear(runtime.engine, runtime.raw, DAY)["gear"] if g["id"] == "g2")
    assert g2["warning_level"] == "past" and g2["note"] is None  # each save sets every field
    with pytest.raises(ValueError):
        workouts.set_gear(runtime.engine, "strava:g2", status="lost")


def test_portal_gear_page_reads_and_saves(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    assert api.get("/api/gear").json()["data"]["gear"] == []
    headers = {"x-vibesynergy": "portal"}
    ok = api.post(
        "/api/gear/strava%3Ag1", json={"status": "spare", "life_km": 700}, headers=headers
    )
    assert ok.status_code == 200 and ok.json()["data"]["status"] == "spare"
    bad = api.post("/api/gear/strava%3Ag1", json={"status": "gone"}, headers=headers)
    assert bad.status_code == 422


def test_gear_reports_garmin_presence_without_merging_items(runtime) -> None:
    """Garmin's own gear snapshot is reported for freshness/reconciliation only; its items
    never appear in the Strava-sourced mileage list (no unverified merge, no duplicates)."""
    runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps({"g1": {"name": "Shoes", "distance": 50_000}}).encode(),
        content_type="application/json",
    )
    runtime.raw.put(
        source="garmin",
        dataset="gear",
        item_key="current",
        payload=json.dumps({"gear": [{"uuid": "g-1"}, {"uuid": "g-2"}], "stats": {}}).encode(),
        content_type="application/json",
    )
    result = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert [g["id"] for g in result["gear"]] == ["g1"]  # Garmin items never leak into this list
    assert result["other_sources"]["garmin"]["item_count"] == 2
    assert result["other_sources"]["garmin"]["state"] == "pending"  # collected, never mapped
    garmin_ref = result["other_sources"]["garmin"]["reference"]
    assert garmin_ref["source"] == "garmin" and garmin_ref["sha256"]
    assert garmin_ref in result["references"]
    assert all(g["source"] == "strava" for g in result["gear"])


def test_gear_garmin_invalid_shape_does_not_crash(runtime) -> None:
    runtime.raw.put(
        source="garmin",
        dataset="gear",
        item_key="current",
        payload=b"not json",
        content_type="application/json",
    )
    result = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert result["gear"] == []
    assert result["other_sources"]["garmin"]["item_count"] is None
    # The snapshot is still citable when its JSON cannot be read.
    assert result["other_sources"]["garmin"]["reference"]["sha256"]


def test_exercises_known_variant_and_readable_unknown_fallback(runtime) -> None:
    _put(
        runtime,
        "garmin",
        [
            Activity(
                "e1",
                T,
                DAY,
                "strength_training",
                elapsed_s=1800.0,
                extra={"exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"}]},
            )
        ],
        "e1",
    )
    _put(
        runtime,
        "garmin",
        [
            Activity(
                "e2",
                T,
                DAY + timedelta(days=1),
                "strength_training",
                elapsed_s=1800.0,
                extra={"exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"}]},
            )
        ],
        "e2",
    )
    _put(
        runtime,
        "garmin",
        [
            Activity(
                "e3",
                T,
                DAY,
                "strength_training",
                elapsed_s=1800.0,
                extra={"exercises": [{"category": "MADE_UP_CATEGORY", "name": "NOT_A_REAL_ONE"}]},
            )
        ],
        "e3",
    )
    out = workouts.exercises(runtime.engine)
    by_key = {(e["category"], e["name"]): e for e in out["exercises"]}
    known = by_key[("BENCH_PRESS", "BARBELL_BENCH_PRESS")]
    assert known["known"] is True and known["label"] == "Barbell Bench Press"
    assert known["sessions"] == 2
    assert known["first_seen"] == str(DAY) and known["last_seen"] == str(DAY + timedelta(days=1))
    unknown = by_key[("MADE_UP_CATEGORY", "NOT_A_REAL_ONE")]
    assert unknown["known"] is False and unknown["label"] == "Not a real one"
    assert out["catalog"]["package"] == "garminconnect" and out["catalog"]["known_variants"] > 1000
    assert out["catalog"]["duplicate_rows"] == 0 and len(out["catalog"]["fingerprint"]) == 64
    assert known["ref"] == "garmin:exercise:BENCH_PRESS/BARBELL_BENCH_PRESS"
    # Usage can exist before the activities dataset itself has been collected.
    assert out["freshness"] == {
        "source": "garmin",
        "dataset": "activities",
        "state": "missing",
        "collected_at": None,
    }


def test_exercises_empty_without_strength_activities(runtime) -> None:
    assert workouts.exercises(runtime.engine)["exercises"] == []


def test_gear_snapshot_update_keeps_one_identity(runtime) -> None:
    """A new snapshot replaces the content hash and does not add a second shoe."""
    payload = {"g1": {"name": "Shoes", "distance": 50_000}}
    runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps(payload).encode(),
        content_type="application/json",
    )
    first = workouts.gear(runtime.engine, runtime.raw, DAY)
    same = runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps(payload).encode(),
        content_type="application/json",
    )
    assert same.is_new is False
    again = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert again["references"][0]["sha256"] == first["references"][0]["sha256"]
    runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps({"g1": {"name": "Shoes", "distance": 60_000}}).encode(),
        content_type="application/json",
    )
    updated = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert [g["ref"] for g in updated["gear"]] == ["strava:g1"]
    assert updated["gear"][0]["total_km"] == 60
    assert updated["references"][0]["sha256"] != first["references"][0]["sha256"]
    assert updated["references"][0]["raw_item_id"] != first["references"][0]["raw_item_id"]


def test_activity_gear_is_identity_only(runtime) -> None:
    _put(
        runtime,
        "garmin",
        [
            Activity("a1", T, DAY, "running", distance_m=5000.0, extra={"gear_ids": ["shoe-1"]}),
            Activity(
                "a2",
                T,
                DAY + timedelta(days=1),
                "running",
                distance_m=8000.0,
                extra={"gear_ids": ["shoe-1", "shoe-1"]},
            ),
        ],
        "gear-use",
    )
    result = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert result["gear"] == []
    assert result["activity_gear"] == [
        {
            "id": "shoe-1",
            "ref": "garmin:shoe-1",
            "source": "garmin",
            "sessions": 2,
            "first_seen": str(DAY),
            "last_seen": str(DAY + timedelta(days=1)),
        }
    ]
    assert "total_km" not in result["activity_gear"][0]


def test_exercise_catalog_fingerprint_is_stable() -> None:
    first = workouts.exercise_catalog()
    second = workouts.exercise_catalog()
    assert first == second
    assert first["duplicate_rows"] == 0
    labels, duplicates, fingerprint = workouts._index_exercise_rows(
        [
            {"name": "Back Squat", "category": "SQUAT", "exercise": "BACK_SQUAT"},
            {"name": "Back Squat again", "category": "SQUAT", "exercise": "BACK_SQUAT"},
            {"name": "Bench", "category": "BENCH_PRESS", "exercise": "BARBELL_BENCH_PRESS"},
        ]
    )
    assert duplicates == 1
    assert labels[("SQUAT", "BACK_SQUAT")] == "Back Squat"
    assert (
        fingerprint
        == workouts._index_exercise_rows(
            [
                {"name": "Bench", "category": "BENCH_PRESS", "exercise": "BARBELL_BENCH_PRESS"},
                {"name": "Back Squat", "category": "SQUAT", "exercise": "BACK_SQUAT"},
            ]
        )[2]
    )


def test_exercise_references_mark_duplicate_and_unknown_steps() -> None:
    resolved = workouts.exercise_references(
        [
            {"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"},
            {"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"},
            {"category": "MADE_UP_CATEGORY", "name": "NOT_A_REAL_ONE"},
            {"name": "no category"},
        ]
    )
    assert resolved["unique"] == 2 and resolved["unknown"] == 2
    assert resolved["steps"][1]["duplicate"] is True and resolved["steps"][1]["duplicate_of"] == 0
    assert resolved["steps"][1]["ref"] == resolved["steps"][0]["ref"]
    assert resolved["steps"][3]["problem"] == "missing_category"
    with pytest.raises(ValueError):
        workouts.exercise_references("BENCH_PRESS")  # type: ignore[arg-type]


def test_exercises_freshness_follows_activities_collection(runtime) -> None:
    runtime.raw.put(
        source="garmin",
        dataset="activities",
        item_key="1",
        payload=b"{}",
        content_type="application/json",
    )
    assert workouts.exercises(runtime.engine)["freshness"]["state"] == "pending"


def _put_gear(rt, source: str, payload: bytes) -> None:
    rt.raw.put(
        source=source,
        dataset="gear",
        item_key="current",
        payload=payload,
        content_type="application/json",
    )


def test_gear_survives_unreadable_or_malformed_strava_snapshot(runtime) -> None:
    _put_gear(runtime, "strava", b"not json")
    broken = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert broken["gear"] == []
    assert broken["references"][0]["source"] == "strava"
    _put_gear(runtime, "strava", json.dumps({"g1": None, "g2": [], "g3": {"name": "Ok"}}).encode())
    mixed = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert [g["ref"] for g in mixed["gear"]] == ["strava:g3"]
    _put_gear(runtime, "strava", b"[1, 2]")
    assert workouts.gear(runtime.engine, runtime.raw, DAY)["gear"] == []


def test_gear_identity_collision_across_sources_stays_separate(runtime) -> None:
    _put_gear(runtime, "strava", json.dumps({"g1": {"name": "Strava shoe"}}).encode())
    _put_gear(runtime, "garmin", json.dumps({"gear": [{"uuid": "g1", "name": "Garmin"}]}).encode())
    _put(
        runtime,
        "garmin",
        [Activity("c1", T, DAY, "running", extra={"gear_ids": [" g1 ", "g1", "", "  "]})],
        "c1",
    )
    result = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert [g["ref"] for g in result["gear"]] == ["strava:g1"]
    assert [g["ref"] for g in result["activity_gear"]] == ["garmin:g1"]
    assert result["activity_gear"][0]["sessions"] == 1
    assert result["other_sources"]["garmin"]["item_count"] == 1


def test_gear_empty_garmin_snapshot_is_reported_not_merged(runtime) -> None:
    _put_gear(runtime, "garmin", json.dumps({"gear": []}).encode())
    result = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert result["gear"] == [] and result["activity_gear"] == []
    assert result["other_sources"]["garmin"]["item_count"] == 0


def test_exercise_history_and_validation_share_one_identity(runtime) -> None:
    messy = [
        {"category": " BENCH_PRESS ", "name": " BARBELL_BENCH_PRESS "},
        {"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"},
        {"category": "BENCH_PRESS", "name": ""},
        {"category": "MADE_UP_CATEGORY", "name": " NOT_A_REAL_ONE"},
        {"category": None, "name": "ORPHAN"},
        "junk",
    ]
    _put(
        runtime,
        "garmin",
        [Activity("p1", T, DAY, "strength_training", extra={"exercises": messy})],
        "p1",
    )
    history = workouts.exercises(runtime.engine)
    steps = [s for s in messy if isinstance(s, dict)]
    validation = workouts.exercise_references(steps)
    history_refs = {e["ref"]: e for e in history["exercises"]}
    validated = {s["ref"]: s for s in validation["steps"] if s["ref"]}
    assert set(history_refs) == set(validated)
    assert len(history_refs) == 3
    for ref, entry in history_refs.items():
        assert validated[ref]["known"] is entry["known"]
        assert validated[ref]["label"] == entry["label"]
    assert history_refs["garmin:exercise:BENCH_PRESS/BARBELL_BENCH_PRESS"]["sessions"] == 1
    assert history["catalog"] == validation["catalog"] == workouts.exercise_catalog()
    assert validation["steps"][4]["problem"] == "missing_category"
    assert validation["steps"][0]["ref"] == validation["steps"][1]["ref"]
    assert validation["steps"][1]["duplicate_of"] == 0


def test_exercise_history_ignores_malformed_extra(runtime) -> None:
    _put(
        runtime,
        "garmin",
        [
            Activity("m1", T, DAY, "strength_training", extra={"exercises": "SQUAT"}),
            Activity("m2", T, DAY, "strength_training", extra={"exercises": {"category": "X"}}),
            Activity("m3", T, DAY, "strength_training", extra={"exercises": [None, 3, {}]}),
        ],
        "m",
    )
    assert workouts.exercises(runtime.engine)["exercises"] == []


def test_gear_maintenance_is_due_by_km_or_days(runtime) -> None:
    from datetime import timedelta

    runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps({"b1": {"name": "Road bike", "distance": 4_000_000}}).encode(),
        content_type="application/json",
    )
    chain = workouts.add_service(
        runtime.engine,
        "strava:b1",
        "Ketting",
        every_km=2000,
        every_days=None,
        km_now=1900.0,
        today=DAY,
    )
    workouts.add_service(
        runtime.engine,
        "strava:b1",
        "Service",
        every_km=None,
        every_days=365,
        km_now=None,
        today=DAY - timedelta(days=340),
    )
    (bike,) = workouts.gear(runtime.engine, runtime.raw, DAY)["gear"]
    states = {s["what"]: s["state"] for s in bike["services"]}
    assert states == {"Ketting": "due", "Service": "soon"}  # 2100 of 2000 km; 340 of 365 days
    workouts.service_done(runtime.engine, chain, km_now=4000.0, today=DAY)
    (bike,) = workouts.gear(runtime.engine, runtime.raw, DAY)["gear"]
    assert {s["what"]: s["state"] for s in bike["services"]}["Ketting"] == "ok"
    with pytest.raises(ValueError):
        workouts.add_service(
            runtime.engine, "strava:b1", "x", every_km=None, every_days=None, km_now=None, today=DAY
        )


def test_activity_gear_and_default_per_sport_count_at_once(runtime) -> None:
    # your gear on an activity moves its km; a sport's default covers gearless ones.
    gear = {
        "g1": {"name": "Shoes A", "distance": 100_000},
        "g2": {"name": "Shoes B", "distance": 50_000},
        "b1": {"name": "MTB", "distance": 1_000_000},
    }
    runtime.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps(gear).encode(),
        content_type="application/json",
    )
    _put(
        runtime,
        "strava",
        [Activity("s1", T, DAY, "Run", distance_m=10_000.0, extra={"gear_id": "g1"})],
        "s1",
    )
    ride = Activity(
        "m1", T + timedelta(days=1), DAY + timedelta(days=1), "mountain_biking", distance_m=30_000.0
    )
    _put(runtime, "garmin", [ride], "m1")
    unify.rebuild(runtime.engine)

    def totals() -> dict[str, float]:
        return {
            g["id"]: g["total_km"] for g in workouts.gear(runtime.engine, runtime.raw, DAY)["gear"]
        }

    assert totals() == {"g1": 100, "g2": 50, "b1": 1000}
    workouts.set_activity_gear(runtime.engine, "strava:s1", "strava:g2")
    assert totals() == {"g1": 90, "g2": 60, "b1": 1000}
    picked = workouts.activity_gear_of(runtime.engine, runtime.raw, "strava:s1", DAY)
    assert picked["ref"] == "strava:g2" and picked["how"] == "own"
    workouts.set_activity_gear(runtime.engine, "strava:s1", workouts.NO_GEAR)
    assert totals()["g1"] == 90 and totals()["g2"] == 50
    workouts.set_activity_gear(runtime.engine, "strava:s1", None)
    assert totals()["g1"] == 100
    # A default counts from the day it was set on, only for activities without gear.
    workouts.set_gear_default(runtime.engine, "mountain_biking", "strava:b1", DAY)
    assert totals()["b1"] == 1030
    out = workouts.gear(runtime.engine, runtime.raw, DAY)
    assert out["defaults"][0]["sport"] == "mountain_biking"
    assert "mountain_biking" in out["sports"]
    assert (
        workouts.activity_gear_of(runtime.engine, runtime.raw, "garmin:m1", DAY)["how"] == "default"
    )
    workouts.set_gear_default(
        runtime.engine, "mountain_biking", "strava:b1", DAY + timedelta(days=2)
    )
    assert totals()["b1"] == 1000
    workouts.set_gear_default(runtime.engine, "mountain_biking", None, DAY)
    assert workouts.gear(runtime.engine, runtime.raw, DAY)["defaults"] == []
