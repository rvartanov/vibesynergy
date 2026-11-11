"""Start page blocks: body and measurements, coming up, best runs over a chosen period,
block order and hidden blocks in the portal prefs, and diary notes with their workout."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.portal import prefs, share
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import journal, start_page, training
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import activity_unified, daily_unified, planned_item, raw_item

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}


def _daily(rt, rows):
    with rt.engine.begin() as conn:
        conn.execute(
            daily_unified.insert(),
            [
                {"subject_id": 1, "date": d, "metric": m, "value": v, "unit": u, "source": s}
                for d, m, v, u, s in rows
            ],
        )


def _activity(rt, group_id, day, *, sport="running", name="Morning run", stream_key=None):
    now = utc_now()
    with rt.engine.begin() as conn:
        conn.execute(
            activity_unified.insert(),
            {
                "group_id": group_id,
                "subject_id": 1,
                "start_utc": now,
                "local_date": day,
                "sport": sport,
                "name": name,
                "elapsed_s": 1800,
                "moving_s": 1700,
                "distance_m": 5000,
                "stream_key": stream_key,
                "primary_source": "garmin",
                "sources": [f"garmin:{group_id}"],
                "updated_at": now,
            },
        )


def _planned(rt, source_id, day, kind, title, *, sport=None, is_race=False):
    now = utc_now()
    with rt.engine.begin() as conn:
        conn.execute(
            raw_item.insert(),
            {
                "id": f"raw-{source_id}",
                "source": "garmin",
                "dataset": "calendar",
                "item_key": source_id,
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
                "source_id": source_id,
                "subject_id": 1,
                "date": day,
                "kind": kind,
                "title": title,
                "sport": sport,
                "is_race": is_race,
                "primary": is_race,
                "extra": {},
                "raw_item_id": f"raw-{source_id}",
                "updated_at": now,
            },
        )


@pytest.fixture
def client(runtime):
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"})
    rt = Runtime.open(settings)
    yield TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    rt.close()


# Body and measurements ---------------------------------------------------------------------


def test_body_has_weight_trend_composition_and_blood_pressure(runtime) -> None:
    when = date(2026, 11, 1)
    rows = [
        (when - timedelta(days=95), "body.weight", 82.0, "kg", "withings"),
        (when - timedelta(days=40), "body.weight", 81.2, "kg", "withings"),
        (when - timedelta(days=10), "body.weight", 80.4, "kg", "withings"),
        (when - timedelta(days=2), "body.weight", 80.1, "kg", "withings"),
        (when - timedelta(days=2), "body.fat_pct", 14.2, "pct", "withings"),
        (when - timedelta(days=200), "body.muscle_mass", 60.0, "kg", "withings"),  # too old
        (when - timedelta(days=5), "bp.systolic", 121.0, "mmHg", "withings"),
        (when - timedelta(days=5), "bp.diastolic", 78.0, "mmHg", "withings"),
        (when + timedelta(days=1), "body.weight", 70.0, "kg", "withings"),  # after the day
    ]
    _daily(runtime, rows)
    out = start_page.body(runtime.engine, when)
    weight = out["weight"]
    assert weight["value"] == 80.1 and weight["date"] == str(when - timedelta(days=2))
    assert weight["change_30d"] == round(80.1 - 81.2, 1)
    assert weight["change_90d"] == round(80.1 - 82.0, 1)
    assert weight["trend"] == "down"
    # The trend line covers the last 90 days only.
    assert weight["points"][0][0] == str(when - timedelta(days=40))
    assert [r["metric"] for r in out["composition"]] == ["body.fat_pct"]
    assert out["composition"][0]["unit"] == "pct"
    assert out["blood_pressure"]["systolic"]["value"] == 121.0
    assert "pulse" not in out["blood_pressure"]


def test_body_without_measurements(runtime) -> None:
    out = start_page.body(runtime.engine, date(2026, 11, 1))
    assert out["weight"] is None and out["composition"] == [] and out["blood_pressure"] is None


def test_weight_stable_within_half_a_kilo(runtime) -> None:
    when = date(2026, 11, 1)
    _daily(
        runtime,
        [
            (when - timedelta(days=35), "body.weight", 80.0, "kg", "withings"),
            (when, "body.weight", 80.3, "kg", "withings"),
        ],
    )
    weight = start_page.body(runtime.engine, when)["weight"]
    assert weight["trend"] == "stable" and weight["change_90d"] is None


def test_body_summary_api(runtime, client) -> None:
    today = today_in(runtime.settings.timezone)
    _daily(runtime, [(today, "body.weight", 79.5, "kg", "withings")])
    data = client.get("/api/body-summary").json()["data"]
    assert data["weight"]["value"] == 79.5 and data["weight"]["trend"] is None
    future = str(today + timedelta(days=1))
    assert client.get("/api/body-summary", params={"date": future}).status_code == 400
    assert client.get("/api/body-summary", params={"date": "soon"}).status_code == 400


# Coming up ---------------------------------------------------------------------------------


def test_upcoming_lists_workouts_and_races_per_day(runtime) -> None:
    today = date(2026, 11, 1)
    _planned(runtime, "w1", today, "workout", "Easy run", sport="running")
    _planned(runtime, "w2", today + timedelta(days=2), "workout", "Intervals", sport="running")
    _planned(runtime, "r1", today + timedelta(days=5), "event", "Trail 30 km", is_race=True)
    _planned(runtime, "late", today + timedelta(days=9), "workout", "Too far", sport="running")
    out = start_page.upcoming(runtime.engine, today)
    assert out["start"] == str(today) and out["end"] == str(today + timedelta(days=6))
    assert len(out["days"]) == 7
    by_day = {d["date"]: d["items"] for d in out["days"]}
    assert [i["title"] for i in by_day[str(today)]] == ["Easy run"]
    assert by_day[str(today)][0]["kind"] == "workout"
    race = by_day[str(today + timedelta(days=5))][0]
    assert race["kind"] == "race" and race["title"] == "Trail 30 km"
    titles = [i["title"] for items in by_day.values() for i in items]
    assert "Too far" not in titles


def test_upcoming_links_a_done_workout(runtime) -> None:
    today = date(2026, 11, 1)
    _planned(runtime, "w1", today, "workout", "Easy run", sport="running")
    _activity(runtime, "g:done", today)
    item = start_page.upcoming(runtime.engine, today)["days"][0]["items"][0]
    assert item["status"] == "done" and item["activity_id"] == "g:done"


def test_upcoming_api(client) -> None:
    data = client.get("/api/upcoming").json()["data"]
    assert len(data["days"]) == 7


# Best runs ---------------------------------------------------------------------------------


def test_best_runs_window_per_period(runtime) -> None:
    today = date(2026, 11, 1)
    engine = runtime.engine
    assert start_page.best_runs_window(engine, "last_90d", today).start == date(2026, 8, 4)
    assert start_page.best_runs_window(engine, "this_year", today).start == date(2026, 1, 1)
    last_year = start_page.best_runs_window(engine, "last_365d", today)
    assert (last_year.end - last_year.start).days == 364
    # All time starts at the first activity with a stream, not in the year 2000.
    assert start_page.best_runs_window(engine, "all", today).start == today
    _activity(runtime, "g:old", date(2019, 4, 2), stream_key="old")
    assert start_page.best_runs_window(engine, "all", today).start == date(2019, 4, 2)
    with pytest.raises(ValueError):
        start_page.best_runs_window(engine, "last_7d", today)


def test_best_runs_is_cached_until_activities_change(runtime, monkeypatch) -> None:
    calls = []

    def fake_curve(engine, parquet, window):
        calls.append(window)
        return {"activities_with_distance": 0, "curve": []}

    monkeypatch.setattr(training, "pace_curve", fake_curve)
    monkeypatch.setattr(start_page, "_bests_cache", {})
    today = date(2026, 11, 1)
    first = start_page.best_runs(runtime.engine, runtime.parquet, "last_90d", today)
    assert first["period"] == "last_90d" and first["start"] == "2026-08-04"
    assert first["end"] == "2026-11-01" and first["periods"] == list(start_page.BEST_RUN_PERIODS)
    start_page.best_runs(runtime.engine, runtime.parquet, "last_90d", today)
    assert len(calls) == 1
    _activity(runtime, "g:new", today - timedelta(days=3), stream_key="new")
    start_page.best_runs(runtime.engine, runtime.parquet, "last_90d", today)
    assert len(calls) == 2


def test_best_runs_api(client) -> None:
    data = client.get("/api/best-runs", params={"period": "all"}).json()["data"]
    assert data["period"] == "all" and {"curve", "start", "end"} <= set(data)
    assert client.get("/api/best-runs", params={"period": "forever"}).status_code == 400


def test_best_run_periods_match_the_prefs() -> None:
    assert prefs.BEST_RUN_PERIODS == start_page.BEST_RUN_PERIODS
    assert prefs.BEST_SPORTS == start_page.BEST_SPORTS


def test_bests_for_every_sport_are_the_mcp_numbers_and_cached(runtime, monkeypatch) -> None:
    from vibesynergy.query import bests as bests_q

    calls = []

    def fake_overview(engine, parquet, window, sport="all"):
        calls.append(sport)
        return {"sport": sport, "start": str(window.start), "end": str(window.end)}

    monkeypatch.setattr(bests_q, "overview", fake_overview)
    monkeypatch.setattr(start_page, "_bests_cache", {})
    today = date(2026, 11, 1)
    for sport in start_page.BEST_SPORTS:
        out = start_page.bests(runtime.engine, runtime.parquet, sport, "last_365d", today)
        assert out["sport"] == sport and out["period"] == "last_365d"
    start_page.bests(runtime.engine, runtime.parquet, "bike", "last_365d", today)
    assert calls == list(start_page.BEST_SPORTS)  # the second bike read came from the cache
    with pytest.raises(ValueError):
        start_page.bests(runtime.engine, runtime.parquet, "chess", "last_365d", today)


def test_bests_api(client) -> None:
    data = client.get("/api/bests", params={"sport": "all", "period": "all"}).json()["data"]
    assert data["sport"] == "all" and {"start", "end", "together"} <= set(data)
    assert client.get("/api/bests", params={"sport": "chess"}).status_code == 400


# Prefs -------------------------------------------------------------------------------------


def test_start_page_prefs(client) -> None:
    changes = {
        "dashboard_order": ["body", "advice", "upcoming"],
        "dashboard_hidden": ["timeline"],
        "best_runs_period": "this_year",
    }
    reply = client.post("/api/prefs", json={"changes": changes}, headers=WRITE)
    assert reply.status_code == 200
    data = client.get("/api/prefs").json()["data"]
    assert data["dashboard_order"] == ["body", "advice", "upcoming"]
    assert data["dashboard_hidden"] == ["timeline"]
    assert data["best_runs_period"] == "this_year"
    for bad in (
        {"dashboard_order": ["body", "body"]},
        {"dashboard_order": "body"},
        {"dashboard_hidden": ["<b>"]},
        {"best_runs_period": "last_7d"},
    ):
        assert client.post("/api/prefs", json={"changes": bad}, headers=WRITE).status_code == 400
    # Back to the default: both lists cleared.
    reset = {"dashboard_order": None, "dashboard_hidden": None}
    data = client.post("/api/prefs", json={"changes": reset}, headers=WRITE).json()["data"]
    assert "dashboard_order" not in data and "dashboard_hidden" not in data


def test_shared_views_reach_the_new_blocks() -> None:
    link = share.Share("x", "t", ("dashboard", "today"), can_write=False)
    for path in ("/upcoming", "/best-runs", "/body-summary"):
        assert link.allows("GET", path)
    assert not share.Share("x", "t", ("sleep",), can_write=False).allows("GET", "/upcoming")


# Diary notes with their workout ------------------------------------------------------------


def test_notes_carry_their_workout(runtime) -> None:
    day = date(2026, 9, 19)
    _activity(runtime, "g:run", day, name="Long run")
    journal.add_note(
        runtime.engine,
        text="Legs heavy at the end.",
        start=day,
        activity_id="g:run",
        source="strava",
        source_ref="s1",
    )
    journal.add_note(runtime.engine, text="Gone", start=day, activity_id="g:gone")
    journal.add_note(runtime.engine, text="Just a note", start=day)
    notes = journal.day_journal(runtime.engine, day)["notes"]
    by_text = {n["text"]: n for n in notes}
    assert by_text["Legs heavy at the end."]["activity"] == {
        "id": "g:run",
        "date": "2026-09-19",
        "sport": "running",
        "name": "Long run",
    }
    assert by_text["Gone"]["activity"] is None
    assert "activity" not in by_text["Just a note"]
