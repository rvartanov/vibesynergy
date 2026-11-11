"""Training calendar."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import schedule


def test_calendar_has_every_day_and_refuses_long_ranges(runtime) -> None:
    out = schedule.calendar(runtime.engine, date(2026, 11, 2), date(2026, 11, 8), date(2026, 11, 6))
    assert out["days"][0]["date"] == "2026-11-02" and len(out["days"]) == 7
    assert all(d["planned"] == [] and d["done"] == [] for d in out["days"])
    with pytest.raises(ValueError):
        schedule.calendar(runtime.engine, date(2026, 1, 1), date(2026, 6, 1), date(2026, 11, 6))


def test_schedule_api(runtime) -> None:
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = api.get("/api/schedule", params={"start": "2026-10-26", "end": "2026-12-06"}).json()[
        "data"
    ]
    assert len(data["days"]) == 42 and data["today"]


def test_day_has_wellness_and_session_stats_and_week_summary(runtime) -> None:
    # the day panel's recovery line, the stats per session and the week's totals.
    from datetime import UTC, datetime

    from vibesynergy.derive import unify
    from vibesynergy.model import Activity, DailyMetric
    from vibesynergy.normalize.normaliser import write_records

    day = date(2026, 11, 6)
    stored = runtime.raw.put(
        source="garmin", dataset="t", item_key="k", payload=b"k", content_type="text/plain"
    )
    records = [
        Activity(
            "a1",
            datetime(2026, 11, 6, 12, tzinfo=UTC),
            day,
            "lap_swimming",
            moving_s=1341.0,
            distance_m=1050.0,
            avg_hr=116.0,
            name="Middagzwemsessie",
        ),
        DailyMetric(day, "hrv.night_avg", 39.0, "ms"),
        DailyMetric(day, "hr.resting", 45.0, "bpm"),
    ]
    with runtime.engine.begin() as conn:
        write_records(conn, records, source="garmin", raw_item_id=stored.id)
    unify.rebuild(runtime.engine)
    out = schedule.calendar(runtime.engine, date(2026, 11, 2), date(2026, 11, 8), day)
    friday = next(d for d in out["days"] if d["date"] == "2026-11-06")
    assert friday["wellness"] == {"hrv_ms": 39.0, "rhr_bpm": 45.0}  # only what was measured
    assert friday["done"][0]["avg_hr"] == 116.0 and friday["done"][0]["distance_m"] == 1050.0
    week = out["week_summary"]["2026-11-02"]
    assert week["count"] == 1 and week["time_s"] == 1341
    assert list(week["sports"]) == ["swim"]


def test_weather_endpoint_is_empty_without_an_outdoor_session(runtime, monkeypatch) -> None:
    from vibesynergy.query import activity_weather

    monkeypatch.setattr(activity_weather, "_FORECAST", {"at": 0.0, "value": None, "point": None})
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = api.get("/api/weather")
    assert reply.status_code == 200 and reply.json()["data"]["days"] == []


class _Reply:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


def test_activity_weather_outdoor_only_cached_and_independent(runtime, monkeypatch) -> None:
    # weather of a done outdoor session from Garmin/Open-Meteo, never intervals.icu.
    from vibesynergy.query import activity_weather, publication_plugins

    monkeypatch.setattr(activity_weather.views, "resolve_activity", lambda _e, ident: ident)
    monkeypatch.setattr(publication_plugins, "indoor", lambda _rt, ident: ident == "garmin:in")
    calls = []

    def weather(_rt, ident, language, *, http=None, recent=False):
        calls.append((ident, recent))
        return "☁️ Bewolkt, 9°C", {"source": "https://api.open-meteo.com/v1/forecast"}

    monkeypatch.setattr(publication_plugins, "_weather", weather)
    assert activity_weather.for_activity(runtime, "garmin:in") is None
    out = activity_weather.for_activity(runtime, "garmin:out")
    assert out == {"line": "☁️ Bewolkt, 9°C", "source": "open-meteo", "place": None}
    assert activity_weather.for_activity(runtime, "garmin:out") == out
    assert calls == [("garmin:out", True)]  # once, then from the cache


def test_forecast_from_open_meteo_at_the_last_outdoor_start(runtime, monkeypatch) -> None:
    from vibesynergy.query import activity_weather

    monkeypatch.setattr(activity_weather, "_FORECAST", {"at": 0.0, "value": None, "point": None})
    monkeypatch.setattr(activity_weather, "_last_outdoor_point", lambda _rt: (53.2012, 5.7999))
    seen = {}

    def http(url, params, timeout):
        seen.update(url=url, **params)
        return _Reply(
            {
                "daily": {
                    "time": ["2026-11-07", "2026-11-08"],
                    "weather_code": [61, 0],
                    "temperature_2m_min": [1.0, 4.0],
                    "temperature_2m_max": [7.0, 9.0],
                    "precipitation_sum": [5.2, 0.0],
                    "snowfall_sum": [0.0, 0.0],
                    "wind_speed_10m_max": [6.0, 3.0],
                    "wind_gusts_10m_max": [14.0, 7.0],
                }
            }
        )

    out = activity_weather.forecast(runtime, http=http, now=100.0)
    assert seen["url"].startswith("https://api.open-meteo.com/")
    assert (seen["latitude"], seen["longitude"]) == (53.2, 5.8)  # about a kilometre, not more
    assert out["days"][0]["flags"] == ["rain", "gusts", "cold"]
    assert out["days"][1]["flags"] == [] and out["days"][1]["temp_max_c"] == 9.0
    assert activity_weather.forecast(runtime, http=None, now=200.0) is out  # cached


def test_coach_weather_tool_uses_the_independent_forecast(runtime, monkeypatch) -> None:
    import anyio
    from mcp import Client

    from vibesynergy.mcp.server import build_server
    from vibesynergy.query import activity_weather

    days = [{"date": f"2026-11-{d:02d}", "flags": []} for d in range(7, 21)]
    monkeypatch.setattr(
        activity_weather, "forecast", lambda _rt: {"source": "open-meteo", "days": days}
    )

    async def go():
        async with Client(build_server(runtime)) as client:
            return await client.call_tool("vibesynergy_weather", {"days": 3})

    data = anyio.run(go).structured_content["data"]
    assert data["source"] == "open-meteo" and len(data["days"]) == 3


def test_activity_weather_route(runtime, monkeypatch) -> None:
    from vibesynergy.query import activity_weather

    monkeypatch.setattr(
        activity_weather, "for_activity", lambda _rt, ident, lang: {"line": f"{ident} {lang}"}
    )
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = api.get("/api/activity-weather", params={"id": "garmin:1", "lang": "nl"}).json()
    assert data["data"] == {"line": "garmin:1 nl"}


def test_calendar_moves_and_skips_a_session_once_vibesynergy_leads(runtime) -> None:
    # with the day panel changes the model; the mirror follows.
    from test_plan_model import EVENTS, _put

    from vibesynergy.query import plan_model

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    write = {"x-vibesynergy": "portal", "origin": "http://localhost"}
    span = {"start": "2026-11-16", "end": "2026-11-22"}
    data = api.get("/api/schedule", params=span).json()["data"]
    assert data["editable"] is False
    planned = [p for d in data["days"] for p in d["planned"]]
    assert planned and planned[0]["session_id"] is None
    body = {"session_id": "lake27:2026-11-17:run-test", "date": "2026-11-19"}
    assert api.post("/api/plan/session/move", json=body, headers=write).status_code == 400
    plan_model.set_source(runtime.engine, "vibesynergy")
    data = api.get("/api/schedule", params=span).json()["data"]
    planned = [p for d in data["days"] for p in d["planned"]]
    assert data["editable"] is True and planned[0]["session_id"] == "lake27:2026-11-17:run-test"
    moved = api.post("/api/plan/session/move", json=body, headers=write)
    assert moved.status_code == 200 and moved.json()["data"]["date"] == "2026-11-19"
    assert runtime.jobs.recent()[0]["kind"] == plan_model.MIRROR_JOB
    skip = api.post(
        "/api/plan/session/skip", json={"session_id": body["session_id"]}, headers=write
    )
    assert skip.status_code == 200 and skip.json()["data"]["status"] == "skipped"


def test_weather_follows_the_chosen_place(runtime, monkeypatch) -> None:
    """Settings → System → Weather on the calendar: a fixed place wins over the last ride."""
    from vibesynergy.portal import prefs
    from vibesynergy.query import activity_weather

    monkeypatch.setattr(activity_weather, "_FORECAST", {"at": 0.0, "value": None, "point": None})
    asked = []

    def http(url, params, timeout):
        asked.append((url, params))
        if "geocoding" in url:
            return _Reply(
                {
                    "results": [
                        {
                            "name": "Stad",
                            "admin1": "Friesland",
                            "country": "Nederland",
                            "latitude": 53.2012,
                            "longitude": 5.7999,
                        },
                        {"name": "broken"},
                    ]
                }
            )
        return _Reply(
            {
                "daily": {
                    "time": ["2026-11-08"],
                    "temperature_2m_min": [2.0],
                    "temperature_2m_max": [9.0],
                    "precipitation_sum": [0.0],
                }
            }
        )

    found = activity_weather.places("Leeu", "nl", http=http)
    assert found == [
        {
            "name": "Stad",
            "where": "Friesland, Nederland",
            "latitude": 53.2012,
            "longitude": 5.7999,
        }
    ]
    assert activity_weather.places("L", http=http) == []  # too short: nothing sent

    place = {"name": "Stad, Friesland", "latitude": 53.2012, "longitude": 5.7999}
    prefs.save(runtime.settings.data_dir, {"weather_location": place})
    out = activity_weather.forecast(runtime, http=http, now=100.0)
    assert out["location"]["kind"] == "chosen" and out["days"][0]["date"] == "2026-11-08"
    assert asked[-1][1]["latitude"] == 53.2 and asked[-1][1]["longitude"] == 5.8
    calls = len(asked)
    activity_weather.forecast(runtime, http=http, now=200.0)
    assert len(asked) == calls  # cached for the same place

    # The portal route names the place, never its coordinates (the calendar can be shared).
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = api.get("/api/weather").json()["data"]
    assert data["location"] == {"kind": "chosen", "name": "Stad, Friesland"}

    prefs.save(runtime.settings.data_dir, {"weather_location": None})
    assert activity_weather.location(runtime) == {"kind": "none"}  # no outdoor session here
    for bad in (
        {"name": "X"},
        {"name": "", "latitude": 1, "longitude": 1},
        {"name": "X", "latitude": 91, "longitude": 0},
        "Stad",
    ):
        with pytest.raises(ValueError):
            prefs.save(runtime.settings.data_dir, {"weather_location": bad})
