"""MCP server end to end: synthetic data through all sources, tools called by a real client."""

from __future__ import annotations

import json
from datetime import date, timedelta

import anyio
import pytest
from garmin_fake import FakeGarmin
from mcp import Client
from strava_fake import FakeStravaAPI
from withings_fake import FakeWithingsAPI

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.server import build_server
from vibesynergy.query import periods
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth
from vibesynergy.sources.garmin.source import Throttled
from vibesynergy.sources.strava.client import StravaClient
from vibesynergy.sources.withings.client import WithingsClient

DAY = date(2026, 9, 20)


@pytest.fixture(scope="module")
def rt(tmp_path_factory):
    from vibesynergy.core.config import Settings

    base = tmp_path_factory.mktemp("mcp")
    settings = Settings(
        config_path=base / "config.toml",
        data_dir=base / "data",
        timezone="Europe/Amsterdam",
        sources={s: {"enabled": True} for s in ("garmin", "strava", "withings")},
    )
    rt = Runtime.open(settings)
    rt.migrate()
    garmin = rt.registry.get("garmin")
    garmin._api = Throttled(FakeGarmin(), 0)
    strava = rt.registry.get("strava")
    store = strava.ctx.secrets
    store.set(oauth.APP, {"client_id": "12345", "client_secret": "s"})
    oauth.save_tokens(store, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    strava._client = StravaClient(store, http=FakeStravaAPI(), sleep=lambda s: None)
    withings = rt.registry.get("withings")
    wstore = withings.ctx.secrets
    wstore.set(oauth.APP, {"client_id": "w", "client_secret": "w"})
    oauth.save_tokens(wstore, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    withings._client = WithingsClient(wstore, post=FakeWithingsAPI(), interval=0)

    history = Window(DAY - timedelta(days=20), DAY)
    rt.sync.sync("garmin", ["user_summary", "sleep", "hrv"], history)
    rt.sync.sync(
        "garmin",
        ["activities", "activity_files", "body_composition", "heart_rate_zones"],
        Window(DAY, DAY),
    )
    rt.sync.sync("strava", ["activities", "activity_streams"], Window(DAY, DAY))
    rt.sync.sync("withings", ["measures", "sleep_summary"], Window(DAY, DAY))
    yield rt
    rt.close()


def call(rt, tool: str, **args) -> dict:
    async def go():
        async with Client(build_server(rt)) as client:
            return await client.call_tool(tool, args)

    result = anyio.run(go)
    text = result.content[0].text if result.content else ""
    if result.is_error:
        raise ValueError(text)
    return result.structured_content or json.loads(text)


def test_tools_listed_with_annotations(rt) -> None:
    async def go():
        async with Client(build_server(rt)) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(go)}
    assert len(tools) == 101
    assert not {
        "vibesynergy_coach_propose",
        "vibesynergy_plan_status",
        "vibesynergy_plan_withdraw",
    } & set(tools)
    assert tools["vibesynergy_events"].annotations.read_only_hint is True
    assert tools["vibesynergy_coach_prompt"].annotations.read_only_hint is True
    assert tools["vibesynergy_activity_set_effort"].annotations.read_only_hint is False
    assert tools["vibesynergy_icu_events"].annotations.read_only_hint is True
    assert tools["vibesynergy_weather"].annotations.read_only_hint is True
    assert tools["vibesynergy_model_run_write"].annotations.read_only_hint is False
    assert tools["vibesynergy_model_runs"].annotations.read_only_hint is True
    assert tools["vibesynergy_race_set_priority"].annotations.read_only_hint is False
    assert tools["vibesynergy_current_coaching_state"].annotations.read_only_hint is True
    assert tools["vibesynergy_metric_status"].annotations.read_only_hint is True
    assert tools["vibesynergy_activity_set_coaching"].annotations.read_only_hint is False
    assert tools["vibesynergy_activity_annotation_preview"].annotations.read_only_hint is True
    assert tools["vibesynergy_activity_assessment"].annotations.read_only_hint is True
    assert tools["vibesynergy_day"].annotations.read_only_hint is True
    assert tools["vibesynergy_refresh"].annotations.read_only_hint is False
    assert tools["vibesynergy_refresh"].annotations.open_world_hint is True
    assert tools["vibesynergy_activity_publish"].annotations.open_world_hint is True
    assert tools["vibesynergy_activity_unpublish"].annotations.open_world_hint is True
    assert tools["vibesynergy_edit_note"].annotations.destructive_hint is True


def test_day_combines_sources(rt) -> None:
    out = call(rt, "vibesynergy_day", date=str(DAY))
    data, meta = out["data"], out["meta"]
    assert data["metrics"]["daily"]["daily.steps"]["value"] == 9876
    assert data["metrics"]["body"]["body.weight"]["source"] == "withings"  # best source
    assert data["sleep"]["source"] == "garmin" and "withings" in data["sleep"]["other_sources"]
    names = {a["name"] for a in data["activities"]}
    assert {"Morning Ride", "Zwift - Watopia"} <= names
    assert "hr.resting" in data["vs_baseline_60d"]
    assert meta["timezone"] == "Europe/Amsterdam"


def test_series_and_stats(rt) -> None:
    out = call(
        rt,
        "vibesynergy_series",
        metrics=["hr.resting", "activity.count"],
        start=str(DAY - timedelta(days=13)),
        end=str(DAY),
        granularity="week",
    )
    data = out["data"]
    assert data["columns"] == ["period", "hr.resting", "activity.count"]
    assert data["granularity"] == "week" and len(data["rows"]) in (2, 3)
    assert out["meta"]["units"]["hr.resting"] == "bpm"
    stats = call(
        rt,
        "vibesynergy_stats",
        metrics=["daily.steps"],
        period="last_28d",
        start=str(DAY - timedelta(days=27)),
        end=str(DAY),
    )["data"]
    assert stats["daily.steps"]["n"] == 21 and stats["daily.steps"]["mean"] == 9876


def test_unknown_metric_suggests(rt) -> None:
    with pytest.raises(ValueError, match=r"did you mean hr\.resting"):
        call(rt, "vibesynergy_series", metrics=["hr.restin"])


def test_activities_detail_and_streams(rt) -> None:
    listing = call(rt, "vibesynergy_activities", start=str(DAY), end=str(DAY))["data"]
    assert listing["totals"]["count"] == len(listing["activities"]) >= 2
    ride = next(a for a in listing["activities"] if a["name"] == "Morning Ride")
    detail = call(rt, "vibesynergy_activity", id=ride["group_id"])["data"]
    assert detail["activity"]["sport_units"]["pace_or_speed"].endswith(" km/h")
    assert detail["laps"] and detail["zones"]
    assert all(lap["sport_units"]["family"] == "cycle" for lap in detail["laps"])
    assert detail["streams"]["channels"]
    streams = call(
        rt,
        "vibesynergy_activity_streams",
        id=ride["group_id"],
        channels=["hr", "power"],
        max_points=10,
    )["data"]
    assert set(streams["columns"]) == {"elapsed_s", "hr", "power"}
    assert len(streams["columns"]["hr"]) <= 10
    by_strava = call(rt, "vibesynergy_activity", id="strava:9001")["data"]
    assert by_strava["activity"]["sport"] == "VirtualRide"
    # RPE and Feel come with their source; the coach can save what the athlete said.
    assert set(ride["effort"]) == {"rpe", "rpe_source", "feel", "feel_source"}
    saved = call(rt, "vibesynergy_activity_set_effort", id="strava:9001", rpe=6, feel=2)["data"]
    assert (saved["rpe"]["value"], saved["rpe"]["source"], saved["rpe"]["label"]) == (
        6,
        "vibesynergy",
        "Difficult",
    )
    again = call(rt, "vibesynergy_activity", id="strava:9001")["data"]["effort"]
    assert (again["feel"]["value"], again["feel"]["label"]) == (2, "Good")
    with pytest.raises(ValueError, match="1 to 10"):
        call(rt, "vibesynergy_activity_set_effort", id="strava:9001", rpe=11)


def test_sleep_recovery_body_profile(rt) -> None:
    sleep = call(rt, "vibesynergy_sleep", start=str(DAY), end=str(DAY), stages=True)["data"]
    assert sleep["nights"][0]["score"] == 84 and sleep["nights"][0]["stages"]
    recovery = call(rt, "vibesynergy_recovery", start=str(DAY - timedelta(days=2)), end=str(DAY))
    assert "hrv.night_avg" in recovery["data"]["columns"]
    body = call(rt, "vibesynergy_body", start=str(DAY), end=str(DAY))
    assert body["data"]["measurements"] and body["meta"]["warnings"]
    profile = call(rt, "vibesynergy_profile")["data"]
    assert any(z["kind"] == "hr" for z in profile["current_zones"])


def test_explain_shows_alternatives(rt) -> None:
    data = call(rt, "vibesynergy_explain", metric="body.weight", date=str(DAY))["data"]
    assert data["shown"]["source"] == "withings"
    assert {a["source"] for a in data["all_sources"]} == {"garmin", "withings"}
    assert data["best_source_rule"]["prefix"] == "body."


def test_sql_is_read_only(rt) -> None:
    out = call(
        rt, "vibesynergy_sql", query="select metric, count(*) n from daily_unified group by metric"
    )["data"]
    assert out["columns"] == ["metric", "n"] and out["rows"]
    for bad in (
        "delete from daily_unified",
        "select * from raw_item",
        "select 1; select 2",
        "attach database 'x' as y",
        "pragma table_info(job)",
    ):
        with pytest.raises(ValueError):
            call(rt, "vibesynergy_sql", query=bad)


def test_overview_catalog_status_coverage_refresh(rt) -> None:
    overview = call(rt, "vibesynergy_overview")["data"]
    assert overview["store"]["activities"] >= 2
    cat = call(rt, "vibesynergy_catalog", query="weight")["data"]["metrics"]
    assert any(m["id"] == "body.weight" and m["coverage"]["days_with_data"] >= 1 for m in cat)
    assert call(rt, "vibesynergy_sync_status", source="garmin")["data"]["datasets"]
    cov = call(rt, "vibesynergy_coverage", metrics=["daily.steps"], period="2026-09")["data"]
    assert cov["days_with_data"]["daily.steps"]["2026-09"] == 20
    queued = call(rt, "vibesynergy_refresh", source="withings")["data"]["queued"]
    assert queued["withings"]


def test_periods() -> None:
    today = date(2026, 9, 24)  # a Thursday
    assert periods.resolve("this_week", today=today) == Window(date(2026, 9, 21), today)
    assert periods.resolve("last_week", today=today).start == date(2026, 9, 14)
    assert periods.resolve("2026-W38", today=today) == Window(date(2026, 9, 14), date(2026, 9, 20))
    assert periods.resolve("2026-02", today=today).end == date(2026, 2, 28)
    assert periods.resolve("last_month", today=today) == Window(date(2026, 8, 1), date(2026, 8, 31))
    with pytest.raises(periods.PeriodError, match="Periods"):
        periods.resolve("fortnight", today=today)


def test_prompts_listed(rt) -> None:
    async def go():
        async with Client(build_server(rt)) as client:
            prompts = (await client.list_prompts()).prompts
            got = await client.get_prompt("weekly_review", {"week": "2026-W38"})
            return prompts, got

    prompts, got = anyio.run(go)
    assert len(prompts) == 8
    assert "2026-W38" in got.messages[0].content.text


def test_analysis_tools_over_mcp(rt) -> None:
    span = {"start": str(DAY - timedelta(days=20)), "end": str(DAY)}
    period = call(rt, "vibesynergy_period", **span)["data"]
    assert period["period"]["averages"]["hr.resting"] == 47 and "delta" in period
    trend = call(rt, "vibesynergy_trend", metric="hr.resting", **span)["data"]
    assert trend["slope_per_week"] == 0
    load = call(rt, "vibesynergy_training_load", **span)["data"]
    assert load["current"]["ctl"] > 0
    assert call(rt, "vibesynergy_volume", **span)["data"]["totals"]
    assert call(rt, "vibesynergy_bests", **span)["data"]["activities_with_power"] >= 1
    intraday = call(rt, "vibesynergy_intraday", date=str(DAY - timedelta(days=5)))
    assert intraday["data"]["rows"] or intraday["meta"]["warnings"]
    corr = call(rt, "vibesynergy_correlate", x="daily.steps", y="hr.resting", **span)["data"]
    assert corr["by_lag"][0]["n"] == 21
    assert call(rt, "vibesynergy_fitness", **span)["data"]["thresholds"]
    assert call(rt, "vibesynergy_anomalies", **span)["data"]["anomalies"] == []
    cmp = call(rt, "vibesynergy_compare_periods", metrics=["daily.steps"], period_a="2026-W38")
    assert cmp["data"]["metrics"]["daily.steps"]["a"]["n"] == 7


def test_bp_intensity_and_audit(rt) -> None:
    from sqlalchemy import select

    from vibesynergy.store.schema import audit_log

    bp = call(rt, "vibesynergy_blood_pressure", start=str(DAY), end=str(DAY))["data"]
    (reading,) = bp["readings"]
    assert (reading["systolic"], reading["diastolic"], reading["pulse"]) == (128, 82, 61)
    assert reading["category"] == "stage 1" and reading["part_of_day"] == "morning"
    dist = call(rt, "vibesynergy_intensity_distribution", start=str(DAY), end=str(DAY))["data"]
    assert dist["activities_with_zones"] >= 1 and dist["three_zone_share"]["low"] > 0
    with pytest.raises(ValueError):
        call(rt, "vibesynergy_series", metrics=["nope.nope"])
    with rt.engine.connect() as conn:
        rows = conn.execute(
            select(audit_log.c.action, audit_log.c.detail).where(audit_log.c.actor == "mcp")
        ).all()
    actions = [a for a, _ in rows]
    assert "vibesynergy_blood_pressure" in actions
    failed = [d for a, d in rows if a == "vibesynergy_series" and d["outcome"].startswith("error")]
    assert failed and failed[-1]["arguments"]["metrics"] == ["nope.nope"]


def test_writes_with_nested_models_are_audited(rt) -> None:
    """predictions and KB rules arrive as Pydantic models; the audit row still lands."""
    from sqlalchemy import select

    from vibesynergy.store.schema import audit_log

    call(
        rt,
        "vibesynergy_coach_write",
        entry_type="morning",
        summary="Easy day.",
        idempotency_key="audit-154",
        for_date=str(DAY),
        predictions=[{"metric": "hrv.night_avg", "horizon_days": 1, "predicted": 52.0}],
    )
    call(
        rt,
        "vibesynergy_kb_upsert",
        slug="audit-154",
        title="Audit test",
        body_md="Body",
        topic="test",
        rules=[{"id": "AUD-1", "text": "Keep it easy.", "grade": "C"}],
    )
    with rt.engine.connect() as conn:
        rows = dict(
            conn.execute(
                select(audit_log.c.action, audit_log.c.detail).where(
                    audit_log.c.actor == "mcp",
                    audit_log.c.action.in_(["vibesynergy_coach_write", "vibesynergy_kb_upsert"]),
                )
            ).all()
        )
    prediction = rows["vibesynergy_coach_write"]["arguments"]["predictions"][0]
    assert prediction["metric"] == "hrv.night_avg" and prediction["predicted"] == 52.0
    assert rows["vibesynergy_kb_upsert"]["arguments"]["rules"][0]["id"] == "AUD-1"


def test_diary_tools(rt) -> None:
    added = call(rt, "vibesynergy_add_note", text="Cold, stayed home", tags=["sick"])["data"]
    assert added["source"] == "user" and added["tags"] == ["sick"]
    call(rt, "vibesynergy_checkin", sleep_quality=2, mood=3)
    out = call(rt, "vibesynergy_notes", period="today")["data"]
    assert [n["text"] for n in out["notes"]] == ["Cold, stayed home"]
    assert out["checkins"][0]["sleep_quality"] == 2

    edited = call(rt, "vibesynergy_edit_note", id=added["id"], text="Cold")["data"]
    assert edited["text"] == "Cold"
    assert call(rt, "vibesynergy_edit_note", id=added["id"], delete=True)["data"]["deleted"]

    logged = call(
        rt, "vibesynergy_log_injury", body_part="calf", side="left", pain=5, timing="after"
    )
    assert logged["data"]["title"] == "Left calf"
    assert any("5/10" in w for w in logged["meta"]["warnings"])
    listed = call(rt, "vibesynergy_injuries")["data"]
    assert [i["id"] for i in listed["injuries"]] == [logged["data"]["id"]]
    assert "not a diagnosis" in listed["note"]

    with pytest.raises(ValueError, match="side must be one of"):
        call(rt, "vibesynergy_log_injury", body_part="calf", side="middle")
    call(rt, "vibesynergy_log_injury", injury_id=logged["data"]["id"], status="resolved")


def test_diary_in_analyses(rt) -> None:
    call(rt, "vibesynergy_log_injury", body_part="knee", side="left", pain=2, date=str(DAY))
    out = call(rt, "vibesynergy_correlate", x="activity.load", y="pain.max", period="last_28d")
    assert out["meta"]["units"]["pain.max"] == "score_0_10"
    base = call(rt, "vibesynergy_baselines", metrics=["hr.resting"], date=str(DAY))
    assert "sick" in base["meta"]["method"]
    assert base["data"]["diary"]["pain"][0]["pain"] == 2
    kept = call(rt, "vibesynergy_baselines", metrics=["hr.resting"], date=str(DAY), exclude_tags=[])
    assert "method" not in kept["meta"]
    stats = call(rt, "vibesynergy_stats", metrics=["hr.resting"], exclude_tags=["sick"])
    assert "sick" in stats["meta"]["method"]


def test_icu_events_reads_the_cache(rt) -> None:
    out = call(rt, "vibesynergy_icu_events", start="2026-03-01", end="2026-03-03")
    assert out["data"]["events"] == []
    with pytest.raises(ValueError, match="before start"):
        call(rt, "vibesynergy_icu_events", start="2026-03-03", end="2026-03-01")


def test_plan_writes_are_refused_without_a_plan_and_while_intervals_leads(rt) -> None:
    """the coach writes the model only when there is a plan and VibeSynergy leads it."""
    from datetime import date as day

    from vibesynergy.query import plan_model

    def refused(why: str) -> None:
        writes = [
            (
                "vibesynergy_plan_session_set",
                {"date": "2026-11-18", "name": "Duurloop", "description": "- 40m Z2 HR"},
            ),
            ("vibesynergy_plan_week_set", {"week": 3, "note": "Rustweek"}),
            ("vibesynergy_plan_target_set", {"week": 3, "sport": "run", "time_s": 3600}),
            ("vibesynergy_plan_session_move", {"session_id": "x:1", "date": "2026-11-19"}),
            ("vibesynergy_plan_session_skip", {"session_id": "x:1"}),
        ]
        for name, args in writes:
            with pytest.raises(ValueError, match=why):
                call(rt, name, reason="test", **args)

    refused(r"there is no plan yet")
    view = call(rt, "vibesynergy_plan_model")["data"]
    assert view["source"] == "intervals" and view["sessions"] == []

    plan_model.create_plan(
        rt.engine,
        name="Test",
        start=day(2026, 11, 2),
        end=day(2026, 12, 27),
        reason="test",
        author="test",
    )
    plan_model.set_source(rt.engine, "intervals")
    refused(r"intervals\.icu still leads")
