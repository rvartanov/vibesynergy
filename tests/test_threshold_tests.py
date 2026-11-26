"""Race and test marks and the threshold calculations from them (#84).

All data here is made up: laps and streams built in the test, no real activity.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import select

from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.model import Stream
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import (
    plan_model,
    race_test_marks,
    session_purpose,
    threshold_tests,
    thresholds,
)
from vibesynergy.store.schema import activity_lap, activity_link, activity_unified, race
from vibesynergy.store.schema import athlete_threshold as threshold_table

DAY = date(2026, 11, 3)
START = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
WRITE = {"x-vibesynergy": "portal", "origin": "http://127.0.0.1:8447"}


def _raw(rt) -> str:
    return rt.raw.put(
        source="garmin",
        dataset="test",
        item_key="fixture",
        payload=b"{}",
        content_type="application/json",
    ).id


def _activity(
    rt,
    gid: str,
    sport: str,
    *,
    name: str = "Workout",
    elapsed: float = 3600,
    max_hr: float | None = None,
    stream: dict | None = None,
    laps: list[dict] | None = None,
    day: date = DAY,
) -> str:
    """One activity with optional laps (``seconds``, ``distance_m``, ``avg_power``) and a
    per-second stream (``{"power": [...], "hr": [...]}``)."""
    start = START + timedelta(days=(day - DAY).days)
    key = None
    if stream:
        key = f"fixture-{gid.replace(':', '-')}"
        size = max(len(v) for v in stream.values())
        rt.parquet.write_stream(
            Stream(key, {"t": [start + timedelta(seconds=i) for i in range(size)], **stream})
        )
    with rt.engine.begin() as conn:
        conn.execute(
            activity_unified.insert().values(
                group_id=gid,
                subject_id=1,
                start_utc=start,
                local_date=day,
                tz_offset_s=3600,
                sport=sport,
                name=name,
                purpose=session_purpose.derive(sport, name),
                elapsed_s=elapsed,
                max_hr=max_hr,
                stream_key=key,
                primary_source="garmin",
                sources=[gid],
                updated_at=start,
            )
        )
        source, source_id = gid.split(":", 1)
        conn.execute(
            activity_link.insert().values(
                source=source, source_id=source_id, group_id=gid, rule="primary"
            )
        )
    if laps:
        raw = _raw(rt)
        at = start
        rows = []
        for i, lap in enumerate(laps, start=1):
            rows.append(
                {
                    "source": "garmin",
                    "activity_source_id": gid.split(":", 1)[1],
                    "lap_index": i,
                    "start_utc": at,
                    "elapsed_s": lap["seconds"],
                    "moving_s": lap["seconds"],
                    "distance_m": lap.get("distance_m"),
                    "avg_power": lap.get("avg_power"),
                    "extra": {},
                    "raw_item_id": raw,
                    "updated_at": start,
                }
            )
            at += timedelta(seconds=lap["seconds"])
        with rt.engine.begin() as conn:
            conn.execute(activity_lap.insert(), rows)
    return gid


def _mark(rt, gid: str, purpose: str = "test") -> None:
    session_purpose.set_override(rt.engine, gid, purpose)


def _by_metric(result: dict) -> tuple[dict, dict]:
    values = {v["metric"]: v for v in result["values"]}
    missing = {m["metric"]: m for m in result["missing"]}
    return values, missing


# --- the calculations -------------------------------------------------------------------


def test_ftp_from_the_twenty_minute_step(runtime):
    gid = _activity(
        runtime,
        "garmin:1",
        "virtual_ride",
        laps=[
            {"seconds": 1200, "avg_power": 140},
            {"seconds": 1210, "avg_power": 250},  # the 20' work step
            {"seconds": 900, "avg_power": 120},
        ],
    )
    _mark(runtime, gid)
    values, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert values["ftp"]["value"] == round(250 * threshold_tests.PROTOCOL.ftp_factor)
    assert values["ftp"]["basis"]["from"] == "lap"
    assert values["ftp"]["basis"]["lap"] == 2
    assert missing["max_hr"]["reason"] == "no_heart_rate"


def test_ftp_from_the_best_twenty_minutes_of_the_stream(runtime):
    power = [150.0] * 600 + [260.0] * 1200 + [150.0] * 600
    gid = _activity(runtime, "garmin:2", "cycling", elapsed=2400, stream={"power": power})
    _mark(runtime, gid)
    values, _ = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert values["ftp"]["value"] == round(260 * threshold_tests.PROTOCOL.ftp_factor)
    assert values["ftp"]["basis"] | {} == {
        "from": "stream",
        "start_s": 600,
        "seconds": 1200,
        "mean_power": 260.0,
        "factor": threshold_tests.PROTOCOL.ftp_factor,
    }


def test_ftp_reads_the_test_length_from_the_method(runtime, monkeypatch):
    # The method's test is 10': the best 10 minutes count, not 20.
    real = threshold_tests.protocol

    def ten(engine):
        return {**real(engine), "ftp_minutes": 10.0}

    monkeypatch.setattr(threshold_tests, "protocol", ten)
    power = [150.0] * 900 + [300.0] * 600 + [200.0] * 900
    gid = _activity(runtime, "garmin:3", "cycling", stream={"power": power})
    _mark(runtime, gid)
    values, _ = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert values["ftp"]["value"] == round(300 * threshold_tests.PROTOCOL.ftp_factor)
    assert threshold_tests.protocol(runtime.engine)["ftp_minutes"] == 10.0
    assert real(runtime.engine)["ftp_minutes"] == 20.0  # the seeded method


def test_no_power_meter_gives_no_ftp_and_says_why(runtime):
    gid = _activity(runtime, "garmin:4", "cycling", stream={"hr": [120.0] * 2400})
    _mark(runtime, gid)
    result = threshold_tests.propose(runtime.engine, runtime.parquet, gid)
    _, missing = _by_metric(result)
    assert missing["ftp"]["reason"] == "no_power"
    assert result["proposed"] == []


def test_a_ride_shorter_than_the_test_gives_no_ftp(runtime):
    gid = _activity(runtime, "garmin:5", "cycling", stream={"power": [250.0] * 600})
    _mark(runtime, gid)
    _, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert missing["ftp"]["reason"] == "too_short"


def test_css_from_the_400_and_the_200(runtime):
    gid = _activity(
        runtime,
        "garmin:6",
        "lap_swimming",
        laps=[
            {"seconds": 420, "distance_m": 300},
            {"seconds": 30, "distance_m": 0},
            {"seconds": 400, "distance_m": 400},  # the 400 all-out
            {"seconds": 300, "distance_m": 0},
            {"seconds": 180, "distance_m": 200},  # the 200 all-out
            {"seconds": 250, "distance_m": 200},  # an easy 200 cool-down
        ],
    )
    _mark(runtime, gid)
    values, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    # (400 - 200) m in (400 - 180) s: 220 s per 200 m, 110 s per 100 m.
    assert values["css"]["value"] == 110.0
    assert values["css"]["unit"] == "s/100m"
    assert values["css"]["basis"]["long"]["lap"] == 3
    assert values["css"]["basis"]["short"]["lap"] == 5
    assert "max_hr" not in missing  # no max HR from a swim


def test_css_without_laps_says_why(runtime):
    gid = _activity(runtime, "garmin:7", "lap_swimming")
    _mark(runtime, gid)
    _, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert missing["css"]["reason"] == "no_laps"
    gid = _activity(
        runtime, "garmin:8", "lap_swimming", laps=[{"seconds": 900, "distance_m": 1000}]
    )
    _mark(runtime, gid)
    _, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert missing["css"]["reason"] == "no_test_laps"


def test_lthr_and_threshold_pace_from_the_thirty_minute_run(runtime):
    hr = [120.0] * 900 + [150.0] * 600 + [165.0] * 1200 + [130.0] * 600
    gid = _activity(
        runtime,
        "garmin:9",
        "running",
        elapsed=3300,
        max_hr=178,
        stream={"hr": hr},
        laps=[
            {"seconds": 900, "distance_m": 2500},
            {"seconds": 1800, "distance_m": 6000},  # the 30' test
            {"seconds": 600, "distance_m": 1500},
        ],
    )
    _mark(runtime, gid)
    values, _ = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert values["lthr"]["value"] == 165  # the last 20', not the first 10'
    assert values["lthr"]["basis"]["last_seconds"] == 1200
    assert values["threshold_pace"]["value"] == 300.0  # 6 km in 30': 5:00/km
    assert values["max_hr"]["value"] == 178


def test_a_run_without_the_test_lap_gives_no_lthr(runtime):
    gid = _activity(
        runtime,
        "garmin:10",
        "running",
        stream={"hr": [150.0] * 5400},
        laps=[{"seconds": 5400, "distance_m": 15000}],
    )
    _mark(runtime, gid)
    _, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert missing["lthr"]["reason"] == "no_test_lap"
    assert missing["threshold_pace"]["reason"] == "no_test_lap"


def test_a_run_test_without_heart_rate_still_gives_the_pace(runtime):
    gid = _activity(
        runtime,
        "garmin:11",
        "running",
        laps=[{"seconds": 600, "distance_m": 1600}, {"seconds": 1800, "distance_m": 6000}],
    )
    _mark(runtime, gid)
    values, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert missing["lthr"]["reason"] == "no_heart_rate"
    assert values["threshold_pace"]["value"] == 300.0


def test_max_hr_only_above_the_current_value(runtime):
    thresholds.set_value(runtime.engine, sport="run", metric="max_hr", value=180, today=DAY)
    gid = _activity(runtime, "garmin:12", "running", max_hr=181)
    _mark(runtime, gid, "race")
    _, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert missing["max_hr"]["reason"] == "not_higher"
    gid = _activity(runtime, "garmin:13", "running", max_hr=186)
    _mark(runtime, gid, "race")
    result = threshold_tests.propose(runtime.engine, runtime.parquet, gid)
    values, _ = _by_metric(result)
    assert values["max_hr"]["value"] == 186
    assert values["max_hr"]["current"] == 180
    (row,) = [p for p in thresholds.overview(runtime.engine, today=DAY)["proposals"]]
    assert (row["metric"], row["method"], row["activity_id"]) == ("max_hr", "race", gid)
    gid = _activity(runtime, "garmin:14", "running", max_hr=250)  # a spike, not a heart rate
    _mark(runtime, gid, "race")
    _, missing = _by_metric(threshold_tests.evaluate(runtime.engine, runtime.parquet, gid))
    assert missing["max_hr"]["reason"] == "not_reliable"


def test_a_value_equal_to_the_current_one_is_not_proposed(runtime):
    thresholds.set_value(runtime.engine, sport="bike", metric="ftp", value=238, today=DAY)
    gid = _activity(runtime, "garmin:15", "cycling", laps=[{"seconds": 1200, "avg_power": 250}])
    _mark(runtime, gid)
    result = threshold_tests.propose(runtime.engine, runtime.parquet, gid)
    _, missing = _by_metric(result)
    assert missing["ftp"]["reason"] == "same_as_current"
    assert result["proposed"] == []


def test_an_unmarked_activity_gives_nothing(runtime):
    gid = _activity(runtime, "garmin:16", "cycling", laps=[{"seconds": 1200, "avg_power": 250}])
    result = threshold_tests.evaluate(runtime.engine, runtime.parquet, gid)
    assert result["values"] == [] and result["missing"][0]["reason"] == "not_marked"


def test_proposals_are_made_once_and_withdrawn_with_the_mark(runtime):
    gid = _activity(runtime, "garmin:17", "cycling", laps=[{"seconds": 1200, "avg_power": 250}])
    _mark(runtime, gid)
    first = threshold_tests.follow_purpose(runtime.engine, runtime.parquet, gid)
    again = threshold_tests.follow_purpose(runtime.engine, runtime.parquet, gid)
    assert len(first["proposed"]) == 1 and again["proposed"] == []
    (row,) = thresholds.overview(runtime.engine, today=DAY)["proposals"]
    assert (row["sport"], row["metric"], row["method"]) == ("bike", "ftp", "test")
    assert row["date_set"] == DAY.isoformat() and row["activity_id"] == gid
    _mark(runtime, gid, "training")
    gone = threshold_tests.follow_purpose(runtime.engine, runtime.parquet, gid)
    assert gone["withdrawn"] == first["proposed"]
    with runtime.engine.connect() as conn:
        assert conn.execute(select(threshold_table.c.status)).scalar_one() == "dismissed"


# --- the automatic mark -------------------------------------------------------------------


def _plan(monkeypatch, sessions: list[dict]) -> None:
    def view(engine, start, end, plan_id=None):
        return {"sessions": [s for s in sessions if s["date"] == start.isoformat()]}

    monkeypatch.setattr(plan_model, "view", view)


def _purpose(rt, gid: str) -> str | None:
    with rt.engine.connect() as conn:
        return conn.execute(
            select(activity_unified.c.purpose).where(activity_unified.c.group_id == gid)
        ).scalar_one()


FTP_TEST = {"session_id": "p:ftp", "date": DAY.isoformat(), "name": "FTP-test 20'", "sport": "Ride"}


def test_a_ride_on_the_day_of_a_planned_ftp_test_is_marked(runtime, monkeypatch):
    _plan(monkeypatch, [FTP_TEST])
    _activity(runtime, "garmin:20", "running", name="Run")  # another sport: not the test
    _activity(runtime, "garmin:21", "cycling", elapsed=600, name="Commute")
    gid = _activity(
        runtime,
        "garmin:22",
        "virtual_ride",
        elapsed=4200,
        laps=[{"seconds": 1200, "avg_power": 250}],
    )
    done = race_test_marks.run(runtime.engine, runtime.parquet, DAY + timedelta(days=1))
    assert [m["activity_id"] for m in done["marked"]] == [gid]  # the longest ride
    assert _purpose(runtime, gid) == "test"
    assert _purpose(runtime, "garmin:21") is None
    assert len(done["proposals"]) == 1
    with runtime.engine.connect() as conn:
        state = session_purpose.mark_state(conn, [gid])
    assert state == {"auto_mark": "test", "automatic": True, "undone": False, "chosen": False}


def test_an_undone_mark_never_comes_back(runtime, monkeypatch):
    _plan(monkeypatch, [FTP_TEST])
    gid = _activity(runtime, "garmin:23", "cycling", laps=[{"seconds": 1200, "avg_power": 250}])
    race_test_marks.run(runtime.engine, runtime.parquet, DAY)
    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    undone = client.post("/api/activity-purpose", json={"id": gid, "purpose": None}, headers=WRITE)
    assert undone.status_code == 200, undone.text
    assert undone.json()["data"]["thresholds"]["withdrawn"]  # its open proposal goes too
    assert _purpose(runtime, gid) is None
    again = race_test_marks.run(runtime.engine, runtime.parquet, DAY)
    assert again["marked"] == [] and _purpose(runtime, gid) is None
    page = client.get("/api/activity", params={"id": gid}).json()["data"]["activity"]
    assert page["purpose_mark"]["undone"] is True
    # Also after a later choice and a return to the automatic rules.
    _mark(runtime, gid, "training")
    _mark(runtime, gid, None)  # type: ignore[arg-type]
    assert race_test_marks.run(runtime.engine, runtime.parquet, DAY)["marked"] == []
    assert _purpose(runtime, gid) is None


def test_the_mark_never_overrides_the_athletes_choice(runtime, monkeypatch):
    _plan(monkeypatch, [FTP_TEST])
    gid = _activity(runtime, "garmin:24", "cycling")
    _mark(runtime, gid, "recovery")
    assert race_test_marks.run(runtime.engine, runtime.parquet, DAY)["marked"] == []
    assert _purpose(runtime, gid) == "recovery"


def test_skipped_tests_and_sessions_you_give_are_not_marked(runtime, monkeypatch):
    _plan(
        monkeypatch,
        [
            {**FTP_TEST, "status": "skipped"},
            {
                "session_id": "p:run",
                "date": DAY.isoformat(),
                "name": "Looptest 30'",
                "sport": "Run",
            },
        ],
    )
    _activity(runtime, "garmin:25", "cycling")
    _activity(runtime, "garmin:26", "running", name="Training geven groep")
    assert race_test_marks.run(runtime.engine, runtime.parquet, DAY)["marked"] == []
    assert _purpose(runtime, "garmin:26") == "coached_by_me"


def test_a_race_marks_every_part_of_its_sport(runtime, monkeypatch):
    _plan(monkeypatch, [])
    now = START
    with runtime.engine.begin() as conn:
        conn.execute(
            race.insert().values(
                id="r1",
                subject_id=1,
                name="Hometown triathlon",
                date=DAY,
                kind="race",
                sport="triathlon",
                links={},
                origin="test",
                created_at=now,
                updated_at=now,
            )
        )
    swim = _activity(runtime, "garmin:27", "open_water_swimming")
    ride = _activity(runtime, "garmin:28", "cycling", max_hr=170)
    run = _activity(runtime, "garmin:29", "running", max_hr=182)
    other = _activity(runtime, "garmin:30", "strength_training")
    done = race_test_marks.run(runtime.engine, runtime.parquet, DAY)
    assert sorted(m["activity_id"] for m in done["marked"]) == [swim, ride, run]
    assert {_purpose(runtime, g) for g in (swim, ride, run)} == {"race"}
    assert _purpose(runtime, other) is None
    rows = thresholds.overview(runtime.engine, today=DAY)["proposals"]
    assert {(r["sport"], r["metric"], r["value"]) for r in rows} == {
        ("bike", "max_hr", 170.0),
        ("run", "max_hr", 182.0),
    }


def test_the_lookback_reaches_a_day_late_activity_only(runtime, monkeypatch):
    _plan(monkeypatch, [FTP_TEST])
    gid = _activity(runtime, "garmin:31", "cycling")
    later = DAY + timedelta(days=race_test_marks.LOOKBACK_DAYS)
    assert race_test_marks.run(runtime.engine, runtime.parquet, later)["marked"] == []
    assert race_test_marks.run(runtime.engine, runtime.parquet, later - timedelta(days=1))["marked"]
    assert _purpose(runtime, gid) == "test"


def test_derive_marks_after_a_sync(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(race_test_marks, "run", lambda *a, **k: calls.append(a))
    runtime.derive()
    assert calls and calls[0][0] is runtime.engine


def test_a_failing_mark_never_stops_the_derive(runtime, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no plan")

    monkeypatch.setattr(race_test_marks, "run", boom)
    runtime.derive()  # does not raise


# --- the portal and MCP -------------------------------------------------------------------


def test_the_portal_marks_a_test_and_proposes(runtime):
    gid = _activity(runtime, "garmin:40", "cycling", laps=[{"seconds": 1200, "avg_power": 250}])
    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    done = client.post("/api/activity-purpose", json={"id": gid, "purpose": "test"}, headers=WRITE)
    assert done.status_code == 200, done.text
    assert len(done.json()["data"]["thresholds"]["proposed"]) == 1
    page = client.get("/api/activity", params={"id": gid}).json()["data"]["activity"]
    assert page["purpose"] == "test"
    assert [v["metric"] for v in page["threshold_values"]] == ["ftp"]
    assert page["purpose_mark"]["chosen"] is True
    bad = client.post("/api/activity-purpose", json={"id": gid, "purpose": "exam"}, headers=WRITE)
    assert bad.status_code == 422


def test_mcp_sets_test_and_reads_the_calculation(runtime):
    gid = _activity(
        runtime,
        "garmin:41",
        "lap_swimming",
        laps=[{"seconds": 400, "distance_m": 400}, {"seconds": 180, "distance_m": 200}],
    )

    async def go():
        tokens = set_connection("user", "user")
        try:
            async with Client(build_server(runtime)) as client:
                marked = await client.call_tool(
                    "vibesynergy_activity_set_purpose", {"id": gid, "purpose": "test"}
                )
                read = await client.call_tool("vibesynergy_threshold_test", {"id": gid})
                return marked, read
        finally:
            reset_connection(tokens)

    marked, read = anyio.run(go)
    assert not marked.is_error, marked.content
    data = (read.structured_content or json.loads(read.content[0].text))["data"]
    assert data["test"] == "css"
    assert data["values"][0]["value"] == 110.0
    assert data["proposals"][0]["status"] == "proposed"
    assert data["protocol"]["css_long_m"] == 400.0


def test_the_test_evaluation_gets_the_calculation(runtime):
    from vibesynergy.coach_runner import context, registry

    assert "test_result" in registry.BY_ID["test"].chain
    gid = _activity(runtime, "garmin:42", "cycling", laps=[{"seconds": 1200, "avg_power": 250}])
    _mark(runtime, gid)
    threshold_tests.propose(runtime.engine, runtime.parquet, gid)
    (found,) = context._test_result(runtime, DAY, {"activity_ids": [gid]})
    assert found["test"] == "ftp" and found["values"][0]["metric"] == "ftp"
    assert found["proposals"][0]["status"] == "proposed"
    assert "Reken niet opnieuw" in context.BLOCKS["test_result"].title


@pytest.mark.parametrize(
    ("times", "values", "expected"),
    [
        ([0, 1, 2], [1.0, 2.0, 3.0], [1.0, 2.0, 3.0]),
        ([0, 4], [5.0, 6.0], [5.0, 5.0, 5.0, 5.0, 6.0]),  # smart recording: held
        ([0, 30], [5.0, 6.0], [5.0] * 10 + [None] * 20 + [6.0]),  # a long gap is not
    ],
)
def test_the_per_second_grid(times, values, expected):
    assert threshold_tests.grid(times, values, threshold_tests.PROTOCOL.hold_s) == expected
