"""Best source per metric: the default order follows what is connected, and can be reset."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from itertools import count

from fastapi.testclient import TestClient

from vibesynergy.core.config import load_settings
from vibesynergy.model import DailyMetric
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import views
from vibesynergy.runtime import Runtime

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
DAY = date(2026, 9, 20)
_ids = count()
PREFIXES = ["", "body.", "sleep."]


def _put(rt, source: str, *records) -> None:
    stored = rt.raw.put(
        source=source,
        dataset="test",
        item_key=str(next(_ids)),
        payload=str(next(_ids)).encode(),
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)


def _metric(name: str, value: float = 1.0) -> DailyMetric:
    return DailyMetric(DAY, name, value, "x")


def test_default_keeps_connected_sources_with_data_hot_to_cold(runtime) -> None:
    _put(runtime, "garmin", _metric("body.weight"), _metric("sleep.score"), _metric("hr.rest"))
    _put(runtime, "withings", _metric("body.weight"))
    _put(runtime, "strava", _metric("body.weight"))  # not connected: left out
    # garmin_export is connected but has no sleep data: left out of that metric.
    _put(runtime, "garmin_export", _metric("body.weight"), _metric("hr.rest"))
    eligible = {"garmin", "withings", "garmin_export"}
    got = views.default_source_order(runtime.engine, PREFIXES, eligible)
    assert got["body."] == ["withings", "garmin", "garmin_export"]  # the built-in ranking
    assert got["sleep."] == ["garmin"]
    assert got[""] == ["garmin", "garmin_export"]


def test_new_source_slots_into_its_ranked_place(runtime) -> None:
    _put(runtime, "garmin", _metric("body.weight"))
    eligible = {"garmin", "withings"}
    assert views.default_source_order(runtime.engine, PREFIXES, eligible)["body."] == ["garmin"]
    _put(runtime, "withings", _metric("body.weight"))  # connected later: goes first
    assert views.default_source_order(runtime.engine, PREFIXES, eligible)["body."] == [
        "withings",
        "garmin",
    ]
    _put(runtime, "newcomer", _metric("body.weight"))  # unknown to the ranking: last
    eligible.add("newcomer")
    assert views.default_source_order(runtime.engine, PREFIXES, eligible)["body."][-1] == "newcomer"


def test_no_data_means_empty_default(runtime) -> None:
    got = views.default_source_order(runtime.engine, PREFIXES, {"garmin"})
    assert got == {prefix: [] for prefix in PREFIXES}


def _portal(runtime, tmp_path, extra=""):
    config = tmp_path / "config.toml"
    config.write_text(
        '[user]\ntimezone = "Europe/Amsterdam"\n\n[server]\n'
        f'public_url = "https://{TAILNET}"\n\n[sources.garmin]\nenabled = true\n'
        f"[sources.withings]\nenabled = true\n{extra}"
    )
    settings = replace(load_settings(config), data_dir=runtime.settings.data_dir)
    rt = Runtime.open(settings)
    _put(rt, "garmin", _metric("body.weight"), _metric("sleep.score"), _metric("hr.rest"))
    _put(rt, "withings", _metric("body.weight"))
    _put(rt, "strava", _metric("body.weight"))
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    return rt, client, config


def test_settings_follow_default_until_overridden(runtime, tmp_path) -> None:
    rt, client, config = _portal(runtime, tmp_path)
    data = client.get("/api/settings").json()["data"]
    assert data["best_source"]["body."] == ["withings", "garmin"]  # strava is not connected
    assert data["best_source"]["sleep."] == ["garmin"]
    assert data["best_source_custom"] == []

    saved = client.post(
        "/api/settings", json={"changes": {"best_source": {"body.": ["garmin"]}}}, headers=WRITE
    ).json()["data"]
    assert saved["best_source"]["body."] == ["garmin"]  # the override wins
    assert saved["best_source"]["sleep."] == ["garmin"]  # the rest still follows the default
    assert saved["best_source_custom"] == ["body."]
    assert load_settings(config).best_source == {"body.": ["garmin"]}
    rt.close()


def test_reset_one_metric_then_all(runtime, tmp_path) -> None:
    rt, client, config = _portal(
        runtime,
        tmp_path,
        '[best_source]\n"body." = ["garmin"]\n"sleep." = ["withings"]\n"hr." = ["x"]\n',
    )
    reset = "/api/settings/best_source/reset"
    assert client.post(reset, json={}).status_code == 403  # no portal header

    data = client.post(reset, json={"prefix": "body."}, headers=WRITE).json()["data"]
    assert data["best_source"]["body."] == ["withings", "garmin"]
    assert data["best_source_custom"] == ["hr.", "sleep."]
    assert "body." not in load_settings(config).best_source

    # Resetting something already on the default changes nothing.
    again = client.post(reset, json={"prefix": "body."}, headers=WRITE).json()["data"]
    assert again["best_source_custom"] == ["hr.", "sleep."]

    data = client.post(reset, json={}, headers=WRITE).json()["data"]
    assert data["best_source_custom"] == []
    assert load_settings(config).best_source == {}
    assert "hr." not in data["best_source"]  # a prefix of its own disappears
    assert data["best_source"]["sleep."] == ["garmin"]
    rt.close()
