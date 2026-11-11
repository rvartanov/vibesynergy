"""intervals.icu read source: mocked HTTP, recorded fixtures, no live calls."""

from __future__ import annotations

import base64
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import Window, today_in, utc_now
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.sources.intervals_icu.client import (
    USER_AGENT,
    IntervalsIcuClient,
    IntervalsIcuError,
    IntervalsIcuRateLimited,
    account_label,
)
from vibesynergy.sources.intervals_icu.mappers import PLAN, assign_sessions
from vibesynergy.sources.registry import SourceError
from vibesynergy.store.schema import activity, icu_event, planned_item
from vibesynergy.sync.scheduler import Scheduler

FIXTURES = Path(__file__).parent / "fixtures" / "intervals_icu"
KEY = "icu-secret-key-9f3a"
ATHLETE = "i000001"
TAILNET = "server.example.ts.net"


def _forbid_live_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("live HTTP")

    monkeypatch.setattr("requests.sessions.Session.request", boom)


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _transport(calls: list):
    bodies = {
        "/events": (FIXTURES / "events.json").read_bytes(),
        "/workouts": (FIXTURES / "workouts.json").read_bytes(),
        "/activities": (FIXTURES / "activities.json").read_bytes(),
        "/wellness": (FIXTURES / "wellness.json").read_bytes(),
    }

    def get(url, params, headers):
        calls.append((url, dict(params), dict(headers)))
        for suffix, body in bodies.items():
            if url.endswith(suffix):
                return 200, {}, body
        raise AssertionError(url)

    return get


def _client(get, key: str = KEY) -> IntervalsIcuClient:
    return IntervalsIcuClient(key, ATHLETE, get=get, interval=0)


def _open(env: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> Runtime:
    if enabled:
        env.write_text(env.read_text() + "\n[sources.intervals_icu]\nenabled = true\n")
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", KEY)
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", ATHLETE)
    runtime = Runtime.open(load_settings())
    runtime.migrate()
    return runtime


def _bind(runtime: Runtime, get) -> None:
    source = runtime.registry.get("intervals_icu")
    source._client = _client(get)


def test_brick_parts_share_a_session_and_events_without_an_external_id_are_other() -> None:
    assigned = assign_sessions(_load("events.json"))
    assert assigned["1002"] == (PLAN, "2026-11-08", "fiets")
    assert assigned["1003"] == (PLAN, "2026-11-08", "loop")
    assert assigned["1002"][1] == assigned["1003"][1]
    # Same day, not a brick: the session key stays <date>:<sport>.
    assert assigned["1010"] == (PLAN, "2026-11-08:Swim", None)
    assert assigned["1001"] == (PLAN, "2026-11-02:Run", None)
    assert assigned["1009"] == ("other", None, None)


def test_client_reads_events_workouts_activities_and_weekly_notes() -> None:
    calls: list = []
    client = _client(_transport(calls))
    oldest = today_in("Europe/Amsterdam")
    newest = oldest + timedelta(days=3)
    events = client.events(oldest, newest)
    workouts = client.workouts()
    activities = client.activities(oldest, newest)
    weekly = client.weekly_targets(oldest, newest)
    notes = client.notes(oldest, newest)

    assert [event["category"] for event in events].count("WORKOUT") == 4
    races = {event["category"] for event in events if str(event["category"]).startswith("RACE_")}
    assert races == {"RACE_A", "RACE_B", "RACE_C"}
    assert workouts[0]["description"] == "- 40m Z2"
    assert activities[0]["id"] == "i9001"
    assert [event["id"] for event in weekly] == [1008]
    assert {event["id"] for event in notes} == {1007, 1008, 1009}

    urls = [url for url, _, _ in calls]
    assert urls[0].endswith(f"/athlete/{ATHLETE}/events")
    assert urls[1].endswith(f"/athlete/{ATHLETE}/workouts")
    assert urls[2].endswith(f"/athlete/{ATHLETE}/activities")
    assert all(KEY not in url for url in urls)
    _params = calls[0][1]
    assert _params == {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
    header = calls[0][2]
    assert header["User-Agent"] == USER_AGENT
    assert "python-urllib" not in header["User-Agent"]
    token = base64.b64encode(f"API_KEY:{KEY}".encode()).decode()
    assert header["Authorization"] == f"Basic {token}"
    assert KEY not in repr(client)


def test_rate_limit_uses_retry_after() -> None:
    def get(_url, _params, _headers):
        return 429, {"Retry-After": "15"}, b""

    client = _client(get)
    before = utc_now()
    with pytest.raises(IntervalsIcuRateLimited) as caught:
        client.events(today_in("UTC"), today_in("UTC"))
    assert before + timedelta(seconds=14) < caught.value.retry_at < before + timedelta(seconds=20)
    assert KEY not in str(caught.value)


def test_http_errors_do_not_include_the_key() -> None:
    def get(_url, _params, _headers):
        return 500, {}, f"nope {KEY}".encode()

    client = _client(get)
    with pytest.raises(IntervalsIcuError) as caught:
        client.workouts()
    assert KEY not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_client_is_read_only() -> None:
    import vibesynergy.sources.intervals_icu.client as client_module

    text = Path(client_module.__file__).read_text()
    assert "requests.post" not in text
    assert "requests.put" not in text
    assert "requests.delete" not in text
    assert "requests.patch" not in text


def test_sync_caches_events_and_leaves_workouts_in_the_raw_archive(env, monkeypatch) -> None:
    _forbid_live_http(monkeypatch)
    runtime = _open(env, monkeypatch)
    calls: list = []
    _bind(runtime, _transport(calls))
    try:
        results = runtime.sync.sync("intervals_icu")
        assert all(result.error is None for result in results)
        today = today_in("Europe/Amsterdam")
        event_call = next(call for call in calls if call[0].endswith("/events"))
        assert event_call[1]["oldest"] == (today - timedelta(days=14)).isoformat()
        assert event_call[1]["newest"] == (today + timedelta(days=300)).isoformat()
        assert any(call[0].endswith("/workouts") for call in calls)
        activity_calls = [call for call in calls if call[0].endswith("/activities")]
        assert activity_calls
        # Whole months (one raw item per month replaces the previous version), never later
        # than today and never earlier than the month of the 14-day lookback.
        first_month = (today - timedelta(days=14)).replace(day=1).isoformat()
        for _url, params, _headers in activity_calls:
            assert params["oldest"] >= first_month
            assert params["oldest"].endswith("-01")
            assert params["newest"] <= today.isoformat()
        # intervals.icu's fitness is not read any more (VibeSynergy's own is the only one).
        assert not [call for call in calls if call[0].endswith("/wellness")]

        with runtime.engine.connect() as conn:
            rows = conn.execute(select(icu_event).order_by(icu_event.c.id)).all()
            activities = conn.execute(
                select(func.count())
                .select_from(activity)
                .where(activity.c.source == "intervals_icu")
            ).scalar()
            planned = conn.execute(
                select(func.count())
                .select_from(planned_item)
                .where(planned_item.c.source == "intervals_icu")
            ).scalar()
        assert activities == 0 and planned == 0
        assert "icu_workout" not in inspect(runtime.engine).get_table_names()
        by_id = {row.id: row for row in rows}
        assert set(by_id) == {str(event["id"]) for event in _load("events.json")}
        assert by_id["1001"].external_id == "2026-11-02:Run"
        assert by_id["1001"].session_key == "2026-11-02:Run"
        assert by_id["1001"].source == "plan"
        assert by_id["1001"].workout_doc["steps"][0]["target"] == "Z2"
        assert by_id["1001"].description == "- 40m Z2"
        assert by_id["1002"].session_key == by_id["1003"].session_key == "2026-11-08"
        assert by_id["1002"].brick_part == "fiets"
        assert by_id["1003"].brick_part == "loop"
        assert by_id["1002"].external_id == "2026-11-08:Ride:fiets"
        assert by_id["1010"].session_key == "2026-11-08:Swim"
        assert {by_id[str(i)].category for i in (1004, 1005, 1006)} == {
            "RACE_A",
            "RACE_B",
            "RACE_C",
        }
        assert by_id["1008"].for_week is True
        assert by_id["1008"].targets["time_target"] == 25200
        assert by_id["1009"].source == "other"
        assert by_id["1009"].external_id is None
        assert by_id["1009"].name == "Dentist"
        workout_ids = list(runtime.raw.current_ids("intervals_icu", "workouts"))
        workouts = runtime.raw.load(workout_ids[0]).json()
        assert workouts[0]["id"] == 50
        assert all(row.id != "50" for row in rows)
    finally:
        runtime.close()


def test_resync_drops_removed_events_and_keeps_the_brick_link(env, monkeypatch) -> None:
    _forbid_live_http(monkeypatch)
    runtime = _open(env, monkeypatch)
    calls: list = []
    _bind(runtime, _transport(calls))
    try:
        runtime.sync.sync("intervals_icu")
        changed = [event for event in _load("events.json") if event.get("id") != 1009]
        changed[0] = {**changed[0], "name": "Easy run, shorter"}
        body = json.dumps(changed).encode()

        def get(url, params, headers):
            calls.append((url, dict(params), dict(headers)))
            if url.endswith("/events"):
                return 200, {}, body
            if url.endswith("/workouts"):
                return 200, {}, (FIXTURES / "workouts.json").read_bytes()
            if url.endswith("/activities"):
                return 200, {}, (FIXTURES / "activities.json").read_bytes()
            if url.endswith("/wellness"):
                return 200, {}, (FIXTURES / "wellness.json").read_bytes()
            raise AssertionError(url)

        _bind(runtime, get)
        runtime.sync.sync("intervals_icu")
        with runtime.engine.connect() as conn:
            rows = {row.id: row for row in conn.execute(select(icu_event))}
        assert "1009" not in rows
        assert rows["1001"].name == "Easy run, shorter"
        assert rows["1002"].session_key == rows["1003"].session_key == "2026-11-08"
    finally:
        runtime.close()


def test_a_one_day_sync_keeps_the_rest_of_the_plan(env, monkeypatch) -> None:
    """Refreshing only today must not drop the planned days around it (1 Nov 2026)."""
    _forbid_live_http(monkeypatch)
    runtime = _open(env, monkeypatch)
    calls: list = []
    _bind(runtime, _transport(calls))
    try:
        runtime.sync.sync("intervals_icu", ["events"])
        today = today_in("Europe/Amsterdam")
        calls.clear()
        runtime.sync.sync("intervals_icu", ["events"], Window(today, today))
        event_call = next(call for call in calls if call[0].endswith("/events"))
        assert event_call[1]["oldest"] == (today - timedelta(days=14)).isoformat()
        assert event_call[1]["newest"] == (today + timedelta(days=300)).isoformat()
        with runtime.engine.connect() as conn:
            ids = {row.id for row in conn.execute(select(icu_event))}
        assert ids == {str(event["id"]) for event in _load("events.json")}
    finally:
        runtime.close()


def test_a_next_week_sync_keeps_races_notes_and_past_workouts(env, monkeypatch) -> None:
    """A sync of just next week (vibesynergy_refresh with a period) must not shrink the cached
    calendar to that week: races, season start, later weeks' notes and targets and the
    workouts of the last days stay (1 Nov 2026). The fake API answers only the dates asked
    for, as intervals.icu does."""
    _forbid_live_http(monkeypatch)
    runtime = _open(env, monkeypatch)
    today = today_in("Europe/Amsterdam")
    monday = today + timedelta(days=7 - today.weekday())

    def event(event_id: int, category: str, day, **extra) -> dict:
        return {
            "id": event_id,
            "category": category,
            "start_date_local": f"{day.isoformat()}T00:00:00",
            "name": f"{category} {event_id}",
            **extra,
        }

    calendar = [
        event(1, "WORKOUT", today - timedelta(days=1), type="Run"),
        event(2, "WORKOUT", today, type="Ride"),
        event(3, "WORKOUT", monday, type="Run"),
        event(4, "NOTE", monday + timedelta(days=2)),
        event(5, "TARGET", monday, for_week=True),
        event(6, "WORKOUT", monday + timedelta(days=9), type="Swim"),
        event(7, "NOTE", monday + timedelta(days=14)),
        event(8, "TARGET", monday + timedelta(days=21), for_week=True),
        event(9, "RACE_C", today + timedelta(days=50)),
        event(10, "RACE_B", today + timedelta(days=200)),
        event(11, "RACE_A", today + timedelta(days=240)),
        event(12, "SEASON_START", today + timedelta(days=260)),
    ]
    calls: list = []

    def get(url, params, headers):
        calls.append((url, dict(params), dict(headers)))
        if url.endswith("/events"):
            oldest, newest = params["oldest"], params["newest"]
            asked = [e for e in calendar if oldest <= e["start_date_local"][:10] <= newest]
            return 200, {}, json.dumps(asked).encode()
        raise AssertionError(url)

    _bind(runtime, get)
    everything = {str(e["id"]) for e in calendar}
    try:
        runtime.sync.sync("intervals_icu", ["events"])
        with runtime.engine.connect() as conn:
            assert {row.id for row in conn.execute(select(icu_event))} == everything
        result = runtime.sync.sync(
            "intervals_icu", ["events"], Window(monday, monday + timedelta(days=6))
        )
        assert [r.error for r in result] == [None]
        with runtime.engine.connect() as conn:
            rows = {row.id: row for row in conn.execute(select(icu_event))}
        assert set(rows) == everything
        assert {rows[i].category for i in ("9", "10", "11", "12")} == {
            "RACE_A",
            "RACE_B",
            "RACE_C",
            "SEASON_START",
        }
        current = list(runtime.raw.current_ids("intervals_icu", "events"))
        assert len(current) == 1
        assert {str(e["id"]) for e in runtime.raw.load(current[0]).json()} == everything
    finally:
        runtime.close()


def test_sync_failure_does_not_log_the_key(env, monkeypatch, caplog) -> None:
    _forbid_live_http(monkeypatch)
    runtime = _open(env, monkeypatch)

    def get(_url, _params, headers):
        raise RuntimeError(f"boom {KEY} {headers['Authorization']}")

    _bind(runtime, get)
    try:
        with caplog.at_level(logging.ERROR):
            results = runtime.sync.sync("intervals_icu", ["events"])
        token = base64.b64encode(f"API_KEY:{KEY}".encode()).decode()
        assert results[0].error
        assert KEY not in results[0].error
        assert token not in results[0].error
        assert KEY not in caplog.text
        assert token not in caplog.text
        logging.getLogger("vibesynergy.sync.engine").error("leaked %s", KEY)
        assert KEY not in caplog.text
    finally:
        runtime.close()


def test_scheduler_runs_only_when_the_source_is_set_up(env, monkeypatch) -> None:
    env.write_text(env.read_text() + "\n[sources.intervals_icu]\nenabled = true\n")
    monkeypatch.delenv("INTERVALS_ICU_API_KEY", raising=False)
    monkeypatch.delenv("INTERVALS_ICU_ATHLETE_ID", raising=False)
    runtime = Runtime.open(load_settings())
    runtime.migrate()
    try:
        assert account_label() is None
        schedule = Scheduler(runtime.settings, runtime.registry, runtime.jobs)
        assert "intervals_icu" not in schedule.intervals()
        assert runtime.sync.skip_reason("intervals_icu") == "not set up"
        monkeypatch.setenv("INTERVALS_ICU_API_KEY", KEY)
        monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", ATHLETE)
        assert schedule.intervals()["intervals_icu"] == 60
        with pytest.raises(SourceError):
            runtime.registry.get("intervals_icu").authenticate()
    finally:
        runtime.close()


def test_settings_show_the_athlete_and_not_the_key(env, monkeypatch) -> None:
    from dataclasses import replace

    env.write_text(env.read_text() + "\n[sources.intervals_icu]\nenabled = true\n")
    monkeypatch.setenv("INTERVALS_ICU_API_KEY", KEY)
    monkeypatch.setenv("INTERVALS_ICU_ATHLETE_ID", ATHLETE)
    settings = replace(load_settings(), server={"public_url": f"https://{TAILNET}"})
    runtime = Runtime.open(settings)
    runtime.migrate()
    try:
        client = TestClient(create_portal_app(runtime), base_url=f"https://{TAILNET}:8447")
        data = client.get("/api/settings").json()["data"]
        row = next(source for source in data["sources"] if source["id"] == "intervals_icu")
        assert row["name"] == "intervals.icu"
        assert row["login"]["state"] == "logged in"
        assert row["login"]["account"] == ATHLETE
        names = {item["name"] for item in row["datasets"]}
        assert names == {"events", "workouts", "activities"}
        assert KEY not in json.dumps(data)
    finally:
        runtime.close()
