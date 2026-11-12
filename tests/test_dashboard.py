"""Dashboard verdict rules: recovery combined with today's plan and the race ahead."""

from __future__ import annotations

from datetime import date, timedelta

from vibesynergy.query import dashboard


def _b(value, mean, sd):
    return {"value": value, "baseline_mean": mean, "baseline_std": sd, "z": (value - mean) / sd}


HRV_OK, RHR_OK, SLEEP_OK = _b(55, 50, 5), _b(44, 44, 1), _b(8 * 3600, 7.5 * 3600, 1800)


def _recovery(hrv=HRV_OK, rhr=RHR_OK, sleep=SLEEP_OK, low_days=0):
    return {
        "hrv.night_avg": hrv,
        "hr.resting": rhr,
        "sleep.duration": sleep,
        "recovery.readiness": _b(70, 60, 10),
        "sleep.score": _b(85, 85, 5),
        "hrv_low_days": low_days,
    }


def _plan(title="VO2 max", status="today", days_away=40):
    return {
        "today": [{"title": title, "status": status}] if title else [],
        "next_event": {
            "title": "Ultra 75 km",
            "days_away": days_away,
            "is_race": True,
            "primary": True,
        },
        "next_race": {"name": "Ultra 75 km", "days_to_go": days_away},
        "plan_phase": {
            "phase": "taper" if days_away <= 14 else "build",
            "days_left": days_away if days_away <= 14 else days_away - 14,
        },
    }


LOAD = {"tsb": -5, "form": "neutral", "acwr": 1.1}


def test_green_goes_ahead_with_the_plan() -> None:
    v = dashboard._verdict(_recovery(), LOAD, _plan(), [], sick=False)
    assert v["level"] == "green" and v["advice"] == "Today's plan: VO2 max, go ahead."


def test_amber_tones_down_a_hard_session() -> None:
    v = dashboard._verdict(_recovery(hrv=_b(40, 50, 5)), LOAD, _plan(), [], sick=False)
    assert v["level"] == "amber" and "HRV below your usual range" in v["reasons"]
    assert "controlled or swap for an easy session" in v["advice"]
    easy = dashboard._verdict(_recovery(hrv=_b(40, 50, 5)), LOAD, _plan("Basis"), [], sick=False)
    assert "fine at an easy effort" in easy["advice"]


def test_red_when_sick_or_injury_stops_movement() -> None:
    v = dashboard._verdict(_recovery(), LOAD, _plan(), [], sick=True)
    assert v["level"] == "red" and v["advice"].startswith("Swap today's VO2 max")
    injury = {"title": "Left calf", "latest_pain": 6, "flags": [{"level": "stop"}]}
    assert dashboard._verdict(_recovery(), LOAD, _plan(), [injury], sick=False)["level"] == "red"
    both = _recovery(hrv=_b(40, 50, 5), rhr=_b(51, 44, 1), low_days=3)
    assert dashboard._verdict(both, LOAD, _plan(), [], sick=False)["level"] == "red"


def test_taper_and_done_and_nothing_planned() -> None:
    v = dashboard._verdict(_recovery(), LOAD, _plan(status="done", days_away=10), [], sick=False)
    assert (
        v["advice"].startswith("Today's VO2 max is done")
        and "Ultra 75 km in 10 days" in v["advice"]
    )
    free = dashboard._verdict(_recovery(), LOAD, _plan(title=None), [], sick=False)
    assert free["advice"].startswith("Nothing planned")


def test_taper_conflict_warns_without_dropping_suggestion() -> None:
    advice = dashboard._verdict(_recovery(), LOAD, _plan(days_away=9), [], sick=False)["advice"]
    assert "Today's plan: VO2 max, go ahead." in advice
    assert "Warning: this hard session may conflict with the taper." in advice


def test_dashboard_api(runtime) -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.portal.share import PAGES
    from vibesynergy.runtime import Runtime

    rt = Runtime.open(
        replace(runtime.settings, server={"public_url": "https://server.example.ts.net"})
    )
    client = TestClient(create_portal_app(rt), base_url="https://server.example.ts.net:8447")
    data = client.get("/api/dashboard").json()["data"]
    assert {
        "verdict",
        "attention",
        "plan",
        "week",
        "trends",
        "injuries",
        "recovery",
        "load",
    } <= set(data)
    assert data["verdict"]["level"] in ("green", "amber", "red")
    assert "dashboard" in PAGES
    rt.close()


def test_unplanned_completed_activity_changes_advice_without_hiding_safety() -> None:
    plan = _plan(title=None)
    v = dashboard._verdict(_recovery(), LOAD, plan, [], sick=False, completed_count=1)
    assert "One activity recorded" in v["advice"]
    assert "quality" not in v["advice"]
    pending = dashboard._verdict(_recovery(), LOAD, _plan(), [], sick=False, completed_count=1)
    assert "Still on the plan: VO2 max" in pending["advice"]
    assert "go ahead" not in pending["advice"]
    stop = dashboard._verdict(_recovery(), LOAD, plan, [], sick=True, completed_count=1)
    assert stop["level"] == "red" and "rest" in stop["advice"].lower()


def test_dashboard_receives_new_and_corrected_activities(runtime) -> None:
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from vibesynergy.core.timeutil import today_in, utc_now
    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.store.schema import activity_unified

    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    today = today_in(runtime.settings.timezone)
    first = client.get("/api/dashboard").json()["data"]
    assert first["completed"]["totals"]["count"] == 0
    now = utc_now()
    row = {
        "group_id": "test:one",
        "subject_id": 1,
        "start_utc": now,
        "local_date": today,
        "sport": "Run",
        "name": "Synthetic workout",
        "elapsed_s": 1800,
        "moving_s": 1700,
        "distance_m": 4000,
        "primary_source": "garmin",
        "sources": ["garmin:test", "strava:test"],
        "updated_at": now,
    }
    with runtime.engine.begin() as conn:
        conn.execute(activity_unified.insert(), row)
        # An old activity must not appear in today's completed list.
        conn.execute(
            activity_unified.insert(),
            {**row, "group_id": "test:old", "local_date": today - timedelta(days=1)},
        )
    second = client.get("/api/dashboard").json()["data"]
    assert second["completed"]["totals"]["count"] == 1
    assert second["completed"]["activities"][0]["name"] == "Synthetic workout"
    assert second["completed"]["activities"][0]["sources"] == row["sources"]
    assert "One activity recorded" in second["verdict"]["advice"]
    assert second["updated_at"] >= first["updated_at"]
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.update()
            .where(activity_unified.c.group_id == "test:one")
            .values(
                name="Corrected workout", distance_m=4500, updated_at=now + timedelta(seconds=1)
            )
        )
    third = client.get("/api/dashboard?lang=nl").json()["data"]
    assert third["completed"]["totals"]["count"] == 1
    assert third["completed"]["activities"][0]["name"] == "Corrected workout"
    assert third["completed"]["activities"][0]["distance_m"] == 4500
    assert third["completed"]["changed_at"] > second["completed"]["changed_at"]
    assert "Vandaag één activiteit" in third["verdict"]["advice"]
    assert second["delivery"]["activity_sync"] == "idle"
    assert second["delivery"]["follow_up"] == "idle"
    assert second["delivery"]["queried_at"] >= first["updated_at"]
    assert third["delivery"]["queried_at"] >= second["delivery"]["queried_at"]


def test_dashboard_coaching_state_degrades_for_unbacked_activity(runtime) -> None:
    from starlette.requests import Request

    from vibesynergy.core.timeutil import today_in, utc_now
    from vibesynergy.portal.api import build_router
    from vibesynergy.query import assessments, coach
    from vibesynergy.store.schema import activity_unified

    coach.save_draft(runtime.engine, "policy", {}, expected_version=0, reason="test")
    coach.activate(runtime.engine, "policy", expected_version=1, reason="test")
    now = utc_now()
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.insert(),
            {
                "group_id": "synthetic:unbacked",
                "subject_id": 1,
                "start_utc": now,
                "local_date": today_in(runtime.settings.timezone),
                "sport": "Run",
                "name": "Synthetic workout",
                "elapsed_s": 1800,
                "moving_s": 1700,
                "distance_m": 4000,
                "primary_source": "garmin",
                "sources": ["garmin:unbacked"],
                "updated_at": now,
            },
        )
    route = next(r.endpoint for r in build_router(runtime).routes if r.path == "/dashboard")
    request = Request({"type": "http", "method": "GET", "path": "/dashboard", "headers": []})
    data = route(request)["data"]
    assert data["completed"]["totals"]["count"] == 1
    assert "coaching_state" not in data
    state = assessments.current_state(runtime)
    assert state["freshness"] == "missing"
    assert state["reason"] == "retired"
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.update()
            .where(activity_unified.c.group_id == "synthetic:unbacked")
            .values(name="Corrected synthetic workout", distance_m=4500)
        )
    corrected = route(request)["data"]
    assert corrected["completed"]["activities"][0]["name"] == "Corrected synthetic workout"
    assert assessments.current_state(runtime)["reason"] == "retired"


def test_dashboard_coaching_state_uses_default_profile(runtime) -> None:
    from starlette.requests import Request

    from vibesynergy.portal.api import build_router
    from vibesynergy.query import assessments

    route = next(r.endpoint for r in build_router(runtime).routes if r.path == "/dashboard")
    request = Request({"type": "http", "method": "GET", "path": "/dashboard", "headers": []})
    assert "coaching_state" not in route(request)["data"]
    state = assessments.current_state(runtime)
    assert state["freshness"] == "missing"
    assert state["profile"]["policy"]["version"] == 0


def test_planned_session_follows_the_stored_workout(runtime) -> None:
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from vibesynergy.core.timeutil import today_in, utc_now
    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.store.schema import activity_unified, planned_item, raw_item

    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    today = today_in(runtime.settings.timezone)
    now = utc_now()
    with runtime.engine.begin() as conn:
        conn.execute(
            raw_item.insert(),
            {
                "id": "raw-synthetic-plan",
                "source": "garmin",
                "dataset": "calendar",
                "item_key": "synthetic-plan",
                "sha256": "ab" * 32,
                "content_type": "application/json",
                "size": 2,
                "request": {},
                "fetched_at": now,
                "last_seen_at": now,
                "is_current": True,
            },
        )
        conn.execute(
            planned_item.insert(),
            {
                "source": "garmin",
                "source_id": "synthetic-easy",
                "subject_id": 1,
                "date": today,
                "kind": "plan_workout",
                "title": "Easy run",
                "sport": "running",
                "is_race": False,
                "primary": False,
                "extra": {},
                "raw_item_id": "raw-synthetic-plan",
                "updated_at": now,
            },
        )
    planned = client.get("/api/dashboard").json()["data"]
    today_plan = planned["plan"]["today"]
    assert today_plan[0]["title"] == "Easy run"
    assert today_plan[0]["status"] == "today"
    assert "Easy run" in planned["verdict"]["advice"]
    assert "done" not in planned["verdict"]["advice"]
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.insert(),
            {
                "group_id": "test:easy",
                "subject_id": 1,
                "start_utc": now,
                "local_date": today,
                "sport": "Run",
                "name": "Synthetic easy run",
                "elapsed_s": 1800,
                "moving_s": 1700,
                "distance_m": 5000,
                "primary_source": "garmin",
                "sources": ["garmin:easy"],
                "updated_at": now,
            },
        )
    done = client.get("/api/dashboard?lang=nl").json()["data"]
    assert done["completed"]["activities"][0]["name"] == "Synthetic easy run"
    assert done["plan"]["today"][0]["status"] == "done"
    assert done["plan"]["today"][0]["done_by"][0]["id"] == "test:easy"
    assert "Easy run van vandaag is gedaan" in done["verdict"]["advice"]
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.update()
            .where(activity_unified.c.group_id == "test:easy")
            .values(
                name="Corrected easy run",
                distance_m=5200,
                updated_at=now + timedelta(seconds=1),
            )
        )
    corrected = client.get("/api/dashboard").json()["data"]
    assert corrected["completed"]["activities"][0]["name"] == "Corrected easy run"
    assert corrected["completed"]["activities"][0]["distance_m"] == 5200
    assert corrected["plan"]["today"][0]["status"] == "done"
    assert "Today's Easy run is done" in corrected["verdict"]["advice"]


def test_stored_workout_stays_visible_while_sync_and_follow_up_change(runtime) -> None:
    from vibesynergy.core.timeutil import today_in, utc_now
    from vibesynergy.query.dashboard import dashboard as build
    from vibesynergy.store.schema import activity_unified, dataset_state, raw_item

    today = today_in(runtime.settings.timezone)
    now = utc_now()
    with runtime.engine.begin() as conn:
        conn.execute(
            raw_item.insert(),
            {
                "id": "raw-synthetic-activity",
                "source": "garmin",
                "dataset": "activities",
                "item_key": "synthetic-activity",
                "sha256": "cd" * 32,
                "content_type": "application/json",
                "size": 2,
                "request": {},
                "fetched_at": now,
                "last_seen_at": now,
                "is_current": True,
            },
        )
        conn.execute(
            dataset_state.insert().values(
                source="garmin_export", dataset="activities", last_success_at=now
            )
        )

    def read():
        return build(
            runtime.engine,
            today,
            timezone=runtime.settings.timezone,
            athlete=None,
            sources=["garmin"],
        )

    waiting = read()
    assert waiting["completed"]["totals"]["count"] == 0
    assert waiting["delivery"]["activity_sync"] == "running"
    assert waiting["delivery"]["follow_up"] == "idle"
    assert "garmin_export" not in {row["source"] for row in waiting["delivery"]["sources"]}
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.insert(),
            {
                "group_id": "test:visible",
                "subject_id": 1,
                "start_utc": now,
                "local_date": today,
                "sport": "Run",
                "name": "Synthetic workout",
                "elapsed_s": 1800,
                "moving_s": 1700,
                "distance_m": 4000,
                "primary_source": "garmin",
                "sources": ["garmin:visible"],
                "updated_at": now,
            },
        )
    visible = read()
    assert visible["completed"]["activities"][0]["name"] == "Synthetic workout"
    assert visible["delivery"]["activity_sync"] == "running"
    with runtime.engine.begin() as conn:
        conn.execute(
            raw_item.update()
            .where(raw_item.c.id == "raw-synthetic-activity")
            .values(normalised_at=now)
        )
    assert read()["delivery"]["activity_sync"] == "idle"
    with runtime.engine.begin() as conn:
        conn.execute(
            dataset_state.insert().values(
                source="garmin",
                dataset="activities",
                last_success_at=now,
                last_error="synthetic fetch failure",
            )
        )
    failed = read()
    assert failed["delivery"]["activity_sync"] == "failed"
    assert failed["completed"]["activities"][0]["name"] == "Synthetic workout"
    garmin = next(row for row in failed["delivery"]["sources"] if row["source"] == "garmin")
    assert garmin["last_collected_at"] == now.isoformat()
    with runtime.engine.begin() as conn:
        conn.execute(
            dataset_state.update()
            .where(dataset_state.c.source == "garmin", dataset_state.c.dataset == "activities")
            .values(last_error=None)
        )
    assert read()["delivery"]["activity_sync"] == "idle"
    runtime.jobs.enqueue(
        "sync", {"source": "garmin", "dataset": "sleep", "window": None}, dedupe_key="sync:sleep"
    )
    runtime.jobs.enqueue(
        "sync",
        {"source": "garmin", "dataset": "activities", "window": "2020-01-01"},
        dedupe_key="sync:old",
    )
    assert read()["delivery"]["activity_sync"] == "idle"
    sync_job = runtime.jobs.enqueue(
        "sync",
        {"source": "garmin", "dataset": "activities", "window": None},
        dedupe_key="sync:activities",
    )
    syncing = read()
    assert syncing["delivery"]["activity_sync"] == "running"
    assert syncing["completed"]["activities"][0]["distance_m"] == 4000
    runtime.jobs.complete(sync_job)
    assert read()["delivery"]["activity_sync"] == "idle"
    # An old assessment job kind is not a dashboard follow-up.
    runtime.jobs.enqueue("assess_activity", {"activity": "garmin:visible"}, dedupe_key="old")
    assert read()["delivery"]["follow_up"] == "idle"
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.update()
            .where(activity_unified.c.group_id == "test:visible")
            .values(name="Corrected synthetic workout", distance_m=4500)
        )
    corrected = read()
    assert corrected["completed"]["activities"][0]["name"] == "Corrected synthetic workout"
    assert corrected["completed"]["activities"][0]["distance_m"] == 4500
    assert corrected["delivery"]["follow_up"] == "idle"
    assert corrected["delivery"]["activity_sync"] == "idle"


def test_dashboard_refresh_policy_script() -> None:
    import subprocess
    from pathlib import Path

    script = Path(__file__).parents[1] / "tools" / "check_dashboard_refresh.mjs"
    subprocess.run(["node", str(script)], check=True)


def test_missing_recovery_is_not_a_green_light() -> None:
    recovery = {k: None for k in _recovery()}
    recovery["hrv_low_days"] = 0
    verdict = dashboard._verdict(recovery, LOAD, _plan(title=None), [], sick=False)
    assert verdict["level"] == "amber"
    assert "not enough recovery data" in verdict["reasons"][0]
    assert "quality" not in verdict["advice"]


def test_null_hrv_sleep_and_readiness_are_not_well_recovered() -> None:
    """HRV, sleep and readiness missing must not read as well recovered.

    Resting HR on its own used to leave the day green. A block whose value is
    null is the same missing input, and must not crash the verdict.
    """
    rested = _recovery()
    rested["hrv.night_avg"] = None
    rested["sleep.duration"] = None
    rested["recovery.readiness"] = None
    verdict = dashboard._verdict(rested, LOAD, _plan(), [], sick=False)
    assert verdict["level"] == "amber"
    assert verdict["headline"] != "Well recovered"
    assert verdict["safety_level"] == "ok"
    assert "not enough recovery data" in verdict["reasons"][0]
    assert "go ahead" not in verdict["advice"]
    assert "quality" not in verdict["advice"]

    nulls = _recovery()
    for key in ("hrv.night_avg", "sleep.duration", "recovery.readiness"):
        nulls[key] = {"value": None, "baseline_mean": None, "baseline_std": None, "z": None}
    null_verdict = dashboard._verdict(nulls, LOAD, _plan(title=None), [], sick=False)
    assert null_verdict["level"] == "amber"
    assert null_verdict["headline"] != "Well recovered"
    assert "not enough recovery data" in null_verdict["reasons"][0]
    assert "quality" not in null_verdict["advice"]


def test_uncertain_completion_does_not_invite_more_training():
    verdict = dashboard._verdict(_recovery(), LOAD, _plan(status="uncertain"), [], sick=False)
    assert "cannot be matched reliably" in verdict["advice"]
    assert "go ahead" not in verdict["advice"]
    sick = dashboard._verdict(_recovery(), LOAD, _plan(status="uncertain"), [], sick=True)
    assert sick["level"] == "red"
    assert sick["advice"].startswith("Swap today's")


def test_no_sources_and_no_data_is_onboarding(runtime) -> None:
    data = dashboard.dashboard(
        runtime.engine,
        date(2026, 10, 16),
        timezone="Europe/Amsterdam",
        athlete=None,
        sources=[],
    )
    assert data["onboarding"] is True
    assert data["date"] == "2026-10-16"
    assert set(data) == {"onboarding", "date", "updated_at"}


def test_one_failing_block_stays_in_that_block(runtime, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    def boom(*_args, **_kwargs):
        raise RuntimeError("week broke")

    monkeypatch.setattr(dashboard, "_week", boom)
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = client.get("/api/dashboard")
    assert reply.status_code == 200
    data = reply.json()["data"]
    assert data["onboarding"] is False
    assert data["week"] == {"error": "unavailable"}
    assert data["verdict"]["level"] in ("green", "amber", "red")


def test_week_compares_with_the_plan_and_keeps_changed(runtime) -> None:
    from vibesynergy.model import PlannedItem
    from vibesynergy.normalize.normaliser import write_records

    today = date(2026, 10, 16)
    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="week-plan",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(
                    "week-tempo",
                    date(2026, 10, 14),
                    "workout",
                    title="Tempo",
                    sport="running",
                    extra={"duration": 3600, "distance": 13500, "ascent_m": 400},
                ),
                PlannedItem(
                    "week-easy",
                    date(2026, 10, 15),
                    "workout",
                    title="Easy",
                    sport="running",
                    extra={"duration": 1800, "distance": 5000, "ascent_m": 50},
                ),
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    data = dashboard.dashboard(
        runtime.engine,
        today,
        timezone="Europe/Amsterdam",
        athlete=None,
        sources=["demo"],
    )
    week = data["week"]
    assert week["compare"] == "plan"
    assert week["target"]["moving_s"] == 5400
    assert week["target"]["distance_m"] == 18500
    assert week["target"]["ascent_m"] == 450
    assert week["chips"]
    changed = dashboard._week_chips(
        [{"date": "2026-10-14", "status": "changed", "title": "Tempo"}],
        today,
        date(2026, 10, 12),
    )
    assert changed == [{"date": "2026-10-14", "status": "changed", "title": "Tempo"}]
    piece = dashboard._planned_piece(
        {"extra": {"completionTarget": {"unitType": "distance", "value": 12}}}
    )
    assert piece["distance_m"] == 12000


def test_week_without_a_plan_uses_the_eight_week_average(runtime) -> None:
    data = dashboard.dashboard(
        runtime.engine,
        date(2026, 10, 16),
        timezone="Europe/Amsterdam",
        athlete=None,
        sources=["demo"],
    )
    week = data["week"]
    assert week["compare"] == "usual"
    assert week["chips"] == []
    assert set(week["usual_week"]) >= {"moving_s", "distance_m", "ascent_m", "count"}
    assert week["target"] == week["usual_week"]


def test_current_load_matches_dashboard_and_training(runtime) -> None:
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from vibesynergy.core.timeutil import today_in
    from vibesynergy.derive import unify
    from vibesynergy.model import Activity, ZoneSet
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.query import training

    today = today_in(runtime.settings.timezone)
    raw = runtime.raw.put(
        source="garmin",
        dataset="activities",
        item_key="load-run",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                ZoneSet(
                    "hr",
                    "running",
                    today - timedelta(days=30),
                    (120, 140, 155, 170, 185),
                    max_value=190,
                ),
                Activity(
                    "load-run",
                    datetime(today.year, today.month, today.day, 7, tzinfo=UTC),
                    today,
                    "running",
                    moving_s=3600,
                    avg_hr=150,
                    name="Load run",
                ),
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    unify.rebuild(runtime.engine)
    snapshot = training.current_load(runtime, today)
    assert snapshot["ctl"] > 0
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    dash = client.get("/api/dashboard").json()["data"]["load"]
    trained = client.get("/api/training").json()["data"]["load"]["current"]
    for key in ("ctl", "atl", "tsb"):
        assert dash[key] == snapshot[key] == trained[key]
