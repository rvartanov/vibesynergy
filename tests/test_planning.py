"""Planning & progress: plan against done per week and phase, the projection of
fitness and form to each race from the planned load, the page's API and the MCP tools."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

from fastapi.testclient import TestClient
from test_mcp_today import call
from test_plan_intervals import EVENTS, MONDAY, _event, _seed
from test_week_progress import WEDNESDAY, _more

from vibesynergy.core.timeutil import Window
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import fitness_model, planning, training
from vibesynergy.runtime import Runtime
from vibesynergy.sources.intervals_icu.mappers import map_events

TAILNET = "server.example.ts.net"


def _future_workout(runtime) -> None:
    """A planned ride on Friday with steps (an hour at 85 % FTP), and next week's target.
    intervals.icu's own load (100) is there but not read."""
    extra = [
        _event(
            60,
            "2026-11-06",
            "WORKOUT",
            "Tempo rit",
            type="Ride",
            external_id="lake27:2026-11-06:ride",
            moving_time=3600,
            icu_training_load=100,
            description="Main set\n- 60m 85%",
        ),
        _event(
            61,
            "2026-11-09",
            "TARGET",
            "Fiets 4u",
            type="Ride",
            external_id="lake27:target:02:ride",
            for_week=True,
            time_target=14400,
            load_target=280,
        ),
    ]
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        payload=json.dumps(EVENTS + extra).encode(),
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(stored.id))),
            source="intervals_icu",
            raw_item_id=stored.id,
        )


def test_plan_vs_done_per_week_and_phase(runtime) -> None:
    _seed(runtime)
    _more(runtime)
    out = planning.plan_vs_done(runtime.engine, runtime.raw, WEDNESDAY, weeks=2)
    assert [w["start"] for w in out["weeks"]] == ["2026-10-26", "2026-11-02"]
    week = out["weeks"][-1]
    assert week["when"] == "current" and week["has_plan"]
    assert week["number"] == 1 and week["phase"] == "Instap"
    ride = next(s for s in week["sports"] if s["family"] == "ride")
    assert ride["goal_s"] == 11700 and ride["basis"] == "target"
    # Monday's long ride was paired by intervals.icu at 95.2 % compliance.
    assert week["compliance_avg"] == 95.2
    assert week["counts"]["done"] >= 1
    assert week["done_pct"] == round(week["done_s"] / week["goal_s"] * 100, 1)
    # The week before has plan workouts but no weekly note, so no phase name.
    assert [p["phase"] for p in out["phases"]] == [None, "Instap"]
    assert out["totals"]["week_count"] == 2


def test_projection_runs_the_planned_load_to_each_race(runtime) -> None:
    _seed(runtime)
    _more(runtime)
    _future_workout(runtime)
    out = planning.projection(runtime.engine, runtime.raw, WEDNESDAY)
    assert out["label"] == "projection" and "plan_done_as_written" in out["assumptions"]
    assert out["fitness_source"] == "vibesynergy"
    # To the last race, the example triathlon on 3 July 2027.
    assert out["rows"][0]["date"] == "2026-11-04" and out["end"] == "2027-07-03"
    assert out["rows"][-1]["date"] == "2027-07-03"
    by_day = {row["date"]: row for row in out["rows"]}
    # From its steps: an hour at 85 % FTP is 0.85² x 100, not intervals.icu's 100.
    assert by_day["2026-11-06"]["load"] == 72.2
    # Next week only has a weekly target: its load spread over the seven days.
    assert by_day["2026-11-10"]["load"] == 40.0
    assert out["basis"]["workouts_with_load"] == 1 and out["basis"]["weeks_from_targets"] == 1
    # It continues from today's own values (yesterday's state plus today's load).
    past = training.training_load(runtime.engine, Window(WEDNESDAY - timedelta(days=89), WEDNESDAY))
    yesterday = next(r for r in past["rows"] if r[0] == "2026-11-03")
    ctl, _ = fitness_model.step(yesterday[2], yesterday[3], by_day["2026-11-04"]["load"])
    assert by_day["2026-11-04"]["ctl"] == round(ctl, 1)
    races = {r["title"]: r for r in out["races"]}
    assert set(races) == {"WINTER TRAIL", "Example triathlon 2027"}
    assert races["WINTER TRAIL"]["form_zone"] in {name for _, name in fitness_model.FORM_ZONES}
    assert races["WINTER TRAIL"]["ctl"] == by_day["2026-12-20"]["ctl"]


def _pin_today(monkeypatch) -> None:
    monkeypatch.setattr("vibesynergy.portal.api.today_in", lambda _tz: WEDNESDAY)
    monkeypatch.setattr("vibesynergy.mcp.server.today_in", lambda _tz: WEDNESDAY)


def test_planning_without_a_plan_has_no_watch_check(runtime, monkeypatch) -> None:
    """A new user (no plan): the page gets no horizon block, so no "plan runs only to"
    line without a date (that crashed the page)."""
    _pin_today(monkeypatch)
    settings = replace(runtime.settings, sources={"garmin": {"enabled": True}})
    rt = Runtime.open(settings)
    try:
        client = TestClient(create_portal_app(rt), base_url="http://127.0.0.1:8447")
        data = client.get("/api/planning").json()["data"]
        assert data["horizon"] is None
        assert data["week"]["has_plan"] is False
    finally:
        rt.close()


def test_planning_api_and_old_addresses(runtime, monkeypatch) -> None:
    _pin_today(monkeypatch)
    _seed(runtime)
    _more(runtime)
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"})
    rt = Runtime.open(settings)
    try:
        client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
        data = client.get("/api/planning", params={"week": "2026-11-04"}).json()["data"]
        assert data["week"]["start"] == "2026-11-02"
        assert {"week", "trend", "fitness", "projection", "plan"} <= set(data)
        # Without a connected Garmin there is nothing to check on the watch.
        assert data["horizon"] is None
    finally:
        rt.close()
    settings = replace(settings, sources={**settings.sources, "garmin": {"enabled": True}})
    rt = Runtime.open(settings)
    try:
        client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
        data = client.get("/api/planning", params={"week": "2026-11-04"}).json()["data"]
        # Planning shows whether the coming days are on the Garmin.
        assert data["horizon"] is not None and {"missing", "fill"} <= set(data["horizon"])
        assert data["horizon"]["has_plan"] is True
        assert data["plan_source"] == "intervals"  # the page's text follows who leads
        assert data["trend"]["weeks"][-1]["start"] == "2026-11-02"
        assert data["fitness"]["columns"][7] == "source"
        assert data["projection"]["label"] == "projection"
        assert client.get("/api/planning", params={"week": "soon"}).status_code == 400
        # The fitness chart's range: the past it covers, ending today.
        for days in (42, 365):
            rows = client.get("/api/planning", params={"range": days}).json()["data"]["fitness"][
                "rows"
            ]
            assert rows[-1][0] == "2026-11-04"
            assert len(rows) == days
        assert client.get("/api/planning", params={"range": 7}).status_code == 400
        # Fitness → Form carries the same chart and projection on its own.
        form = client.get("/api/form", params={"range": 42}).json()["data"]
        assert set(form) == {"fitness", "projection"} and len(form["fitness"]["rows"]) == 42
        assert client.get("/api/form", params={"range": 7}).status_code == 400
        # The old pages' endpoints still answer for older share links.
        assert client.get("/api/training-plan").status_code == 200
    finally:
        rt.close()


def test_share_links_for_the_old_pages_open_the_merged_page() -> None:
    from vibesynergy.portal.share import Share

    for page in ("training", "plan", "planning"):
        assert Share("x", "t", (page,), False).allows("GET", "/planning")
    assert not Share("x", "t", ("sleep",), False).allows("GET", "/planning")


def test_mcp_plan_vs_done_and_projection(runtime, monkeypatch) -> None:
    _pin_today(monkeypatch)
    _seed(runtime)
    _more(runtime)
    done = call(runtime, "vibesynergy_plan_vs_done", start="2026-11-02", end="2026-11-08")["data"]
    assert [w["start"] for w in done["weeks"]] == ["2026-11-02"]
    assert done["weeks"][0]["compliance_avg"] == 95.2
    projected = call(runtime, "vibesynergy_projection")["data"]
    assert projected["label"] == "projection" and "assumptions" in projected


def test_monday_of() -> None:
    assert planning.monday_of(date(2026, 11, 8)) == MONDAY


def test_totals_compare_only_weeks_with_a_plan() -> None:
    def row(goal, done):
        return {
            "goal_s": goal,
            "done_s": done,
            "counts": {"done": 0, "changed": 0, "missed": 0},
            "compliance_avg": None,
        }

    out = planning._totals([row(None, 36000.0), row(7200.0, 7200.0), row(3600.0, 1800.0)])
    assert out["done_s"] == 45000.0
    assert out["done_in_planned_weeks_s"] == 9000.0
    assert out["done_pct"] == round(9000 / 10800 * 100, 1)
