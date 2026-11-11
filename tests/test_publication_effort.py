"""Public Garmin RPE/feel on the intervals.icu scales; no weather line indoors."""

from dataclasses import replace

import pytest
from test_evidence import put
from test_publication import FakeProvider, garmin_activity, ready

from vibesynergy.core import config_edit
from vibesynergy.query import publication, publication_format, publication_plugins


@pytest.mark.parametrize(
    ("garmin", "nl", "en"),
    [
        (
            {"feel": 100, "rpe": 100},
            ["Sterk", "10/10 (Maximaal)"],
            ["Strong", "10/10 (Max effort)"],
        ),
        ({"feel": 75, "rpe": 40}, ["Goed", "4/10 (Comfortabel)"], ["Good", "4/10 (Comfortable)"]),
        ({"feel": 50, "rpe": 70}, ["Normaal", "7/10 (Zwaar)"], ["Normal", "7/10 (Hard)"]),
        ({"feel": 25}, ["Matig"], ["Poor"]),
        (
            {"feel": 0, "rpe": 5},
            ["Zwak", "1/10 (Helemaal niets)"],
            ["Weak", "1/10 (Nothing at all)"],
        ),
        ({"feel": 101, "rpe": 0}, [], []),
    ],
)
def test_garmin_rpe_and_feel_use_the_intervals_scales(garmin, nl, en):
    """Garmin keeps RPE as 10-100 and feel as 0-100; the line shows RPE n/10 and a label."""
    for language, expected in (("nl", nl), ("en", en)):
        lines = publication_format.stats_lines(garmin, language)
        assert [line.split(": ", 1)[1] for line in lines] == expected
        assert not any("/100" in line for line in lines)


def _weather_on(runtime, monkeypatch):
    config_edit.apply_changes(
        runtime.settings.config_path,
        {"publication": {"quote_enabled": False, "weather_enabled": True}},
    )
    calls = []

    def weather(_rt, activity_id, _language):
        calls.append(activity_id)
        return "☀️ Zonnig, 18°C", {"source": "test"}

    monkeypatch.setattr(publication_plugins, "_weather", weather)
    return calls


@pytest.fixture(autouse=True)
def offline_plugins(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)


@pytest.mark.parametrize(
    ("sport", "extra"),
    [
        ("virtual_ride", None),
        ("treadmill_running", None),
        ("lap_swimming", None),
        ("cycling", {"manufacturer": "ZWIFT"}),
        ("cycling", {"manufacturer": "MyWhoosh"}),
    ],
)
def test_indoor_and_virtual_activities_get_no_weather_line(runtime, monkeypatch, sport, extra):
    record = garmin_activity(sport=sport)
    if extra:
        record = replace(record, extra={**record.extra, **extra})
    put(runtime, "garmin", record)
    calls = _weather_on(runtime, monkeypatch)
    item = publication.preview(runtime, "garmin:1", "garmin", client=FakeProvider(""))
    assert item["plugins"]["weather"] == {
        "status": "not_applicable",
        "reason": "indoor",
        "version": publication_plugins.VERSION,
    }
    assert "Zonnig" not in item["owned_block"]
    assert calls == []


def test_outdoor_weather_stays_and_a_cached_line_is_not_reused_indoors(runtime, monkeypatch):
    ready(runtime)
    calls = _weather_on(runtime, monkeypatch)
    outdoor = publication.preview(runtime, "garmin:1", "garmin", client=FakeProvider(""))
    assert "☀️ Zonnig, 18°C" in outdoor["owned_block"]
    assert calls == ["garmin:1"]
    # The same activity turns out to be a treadmill run: the cached line is not reused.
    put(runtime, "garmin", garmin_activity(sport="treadmill_running"))
    indoor = publication.preview(runtime, "garmin:1", "garmin", client=FakeProvider(""))
    assert "Zonnig" not in indoor["owned_block"]
    assert indoor["plugins"]["weather"]["status"] == "not_applicable"


def test_strava_trainer_flag_counts_as_indoor(runtime):
    put(runtime, "garmin", garmin_activity(sport="cycling"))
    assert not publication_plugins.indoor(runtime, "garmin:1")
    put(runtime, "strava", replace(garmin_activity(sport="cycling"), extra={"trainer": True}))
    assert publication_plugins.indoor(runtime, "garmin:1")
