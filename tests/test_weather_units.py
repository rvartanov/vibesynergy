"""Weather units: °C or °F, and wind in km/h, m/s, knots or Beaufort (Robert 11-11)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vibesynergy.core import weather_units as wu
from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import activity_weather, publication_format

WRITE = {"x-vibesynergy": "portal", "origin": "http://127.0.0.1:8447"}


def test_conversions() -> None:
    assert wu.temperature(11, "c") == "11°C" and wu.temperature(11, "f") == "52°F"
    assert wu.temperature(-3.4, "f") == "26°F"
    assert wu.wind(5, "ms") == "5 m/s" and wu.wind(5, "kmh") == "18 km/h"
    assert wu.wind(5, "kn") == "10 kn" and wu.wind(5, "bft") == "3 Bft"
    # Beaufort bounds: 0.3 m/s is force 1, 32.7 m/s and up is 12 (hurricane).
    assert [wu.beaufort(v) for v in (0.0, 0.29, 0.3, 7.9, 8.0, 32.6, 32.7, 50)] == [
        0,
        0,
        1,
        4,
        5,
        11,
        12,
        12,
    ]


def test_config_defaults_and_validation(env, settings) -> None:
    assert (settings.temperature, settings.wind) == ("c", "kmh")
    original = env.read_text()
    for line, word in (('temperature = "k"', "temperature"), ('wind = "mph"', "wind")):
        env.write_text(original.replace("[user]\n", f"[user]\n{line}\n", 1))
        with pytest.raises(ConfigError, match=word):
            load_settings(env)


def test_imperial_defaults_to_fahrenheit(env) -> None:
    env.write_text(env.read_text().replace("[user]\n", '[user]\nunits = "imperial"\n', 1))
    assert load_settings(env).temperature == "f"


def test_published_weather_and_temperatures_follow_the_choice() -> None:
    line = publication_format.weather_line("Cloudy", 11, 18, "W", None, "en")
    assert line == "☁️ Cloudy, 11°C, 18 km/h W"
    line = publication_format.weather_line(
        "Cloudy", 11, 18, "W", None, "en", temperature="f", wind="bft"
    )
    assert line == "☁️ Cloudy, 52°F, 3 Bft W"
    garmin = {"temp": 50, "windSpeed": 10, "windDirectionCompassPoint": "nw"}
    assert "10°C" in publication_format.garmin_weather(garmin, "en")
    assert "9 kn" in publication_format.garmin_weather(garmin, "en", wind="kn")
    fields = {"temp_min": 3, "temp_max": 9, "temp_avg": 6}
    text = " ".join(publication_format.stats_lines(fields, "en", temperature="f"))
    assert "37-48°F" in text and "43°F" in text and "°C" not in text


def test_saved_choice_reaches_meta_and_settings(runtime) -> None:
    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    reply = client.post(
        "/api/settings",
        json={"changes": {"user": {"temperature": "f", "wind": "bft"}}},
        headers=WRITE,
    )
    assert reply.status_code == 200, reply.text
    user = client.get("/api/settings").json()["data"]["user"]
    assert (user["temperature"], user["wind"]) == ("f", "bft")
    refused = client.post(
        "/api/settings", json={"changes": {"user": {"wind": "mph"}}}, headers=WRITE
    )
    assert refused.status_code == 400
    running = replace(runtime.settings, temperature="f", wind="bft")
    runtime.settings = running
    meta = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    units = meta.get("/api/meta").json()["data"]["weather_units"]
    assert units == {"temperature": "f", "wind": "bft"}


def test_a_new_choice_does_not_reuse_a_cached_line(runtime, monkeypatch) -> None:
    keys = []
    monkeypatch.setattr(activity_weather.views, "resolve_activity", lambda engine, a: a)
    monkeypatch.setattr(activity_weather.publication_plugins, "indoor", lambda rt, g: False)
    monkeypatch.setattr(
        activity_weather.publication_plugins,
        "_cached",
        lambda rt, key, plugin: keys.append(key) or None,
    )
    monkeypatch.setattr(activity_weather.publication_plugins, "_weather", lambda *a, **k: None)
    activity_weather.for_activity(runtime, "g1", "nl")
    runtime.settings = replace(runtime.settings, wind="bft")
    activity_weather.for_activity(runtime, "g1", "nl")
    assert keys[0] != keys[1] and keys[1].endswith(":bft")


def test_session_weather_says_where(runtime) -> None:
    from datetime import UTC, date, datetime

    from vibesynergy.store.schema import activity, activity_unified

    at = datetime(2026, 11, 11, 9, tzinfo=UTC)
    rows = [("1", "Leeuwarden"), ("2", "<script>"), ("3", None)]
    stored = runtime.raw.put(
        source="garmin",
        dataset="activities",
        item_key="places",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        for source_id, name in rows:
            extra = {"locationName": name} if name else {}
            conn.execute(
                activity.insert().values(
                    source="garmin",
                    source_id=source_id,
                    subject_id=1,
                    start_utc=at,
                    local_date=date(2026, 11, 11),
                    sport="run",
                    extra=extra,
                    updated_at=at,
                    raw_item_id=stored.id,
                )
            )
            conn.execute(
                activity_unified.insert().values(
                    group_id=f"g{source_id}",
                    subject_id=1,
                    start_utc=at,
                    local_date=date(2026, 11, 11),
                    sport="run",
                    primary_source="garmin",
                    sources=[f"garmin:{source_id}"],
                    updated_at=at,
                )
            )
    assert activity_weather.place(runtime, "g1") == "Leeuwarden"
    assert activity_weather.place(runtime, "g2") is None  # not a place name
    assert activity_weather.place(runtime, "g3") is None
    assert activity_weather.place(runtime, "nope") is None
