"""Automatic workout names (#114, phase 7a): the rule, the lock, the cutoff. Synthetic data."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from test_evidence import START, activity, put

from vibesynergy.core import config_edit
from vibesynergy.core.config import load_settings
from vibesynergy.query import auto_names, workout_texts

WRITE = {"x-vibesynergy": "portal"}
NOW = START + timedelta(hours=2)


def _settings(rt, **publication):
    config_edit.apply_changes(rt.settings.config_path, {"publication": publication})


def _workout(rt, *, place="Leeuwarden", sport="running", hour_offset=0, source_id="1"):
    # 08:00 local (UTC+2): a morning.
    garmin = replace(
        activity(),
        source_id=source_id,
        start_utc=START - timedelta(hours=2 - hour_offset),
        tz_offset_s=7200,
        sport=sport,
        name="Leeuwarden Hardlopen",
        fit_id=None,
        extra={"locationName": place} if place else {},
    )
    put(rt, "garmin", garmin)
    return f"garmin:{source_id}"


def _ready(rt, **publication):
    """The setting in effect since before the workouts (as after the first run)."""
    _settings(rt, auto_name_since=(START - timedelta(days=1)).isoformat(), **publication)


def _name(rt, group):
    return workout_texts.texts(rt.engine, group)["name"]


def test_part_of_day():
    assert [auto_names.part_of_day(h) for h in (0, 5, 6, 11, 12, 17, 18, 23)] == [
        "night", "night", "morning", "morning", "afternoon", "afternoon", "evening", "evening",
    ]  # fmt: skip


def test_a_new_workout_gets_sport_part_of_day_and_place(runtime):
    _ready(runtime, language="nl")
    group = _workout(runtime)
    assert auto_names.run(runtime, now=NOW)["named"] == 1
    name = _name(runtime, group)
    assert name["text"] == "Ochtendloop Leeuwarden"
    assert name["lock"] is None
    newest = workout_texts.history(runtime.engine, group, "name")[0]
    assert (newest["source"], newest["kind"], newest["old_text"]) == (
        "vs", "auto", "Leeuwarden Hardlopen",
    )  # fmt: skip


def test_english_and_no_place(runtime):
    _ready(runtime, language="en")
    group = _workout(runtime, place=None, sport="road_biking", hour_offset=11)  # 19:00
    auto_names.run(runtime, now=NOW + timedelta(hours=11))
    assert _name(runtime, group)["text"] == "Evening ride"


def test_the_plan_session_title_comes_first(runtime, monkeypatch):
    _ready(runtime, language="nl")
    group = _workout(runtime)
    monkeypatch.setattr(auto_names, "plan_title", lambda rt, g, day: "Duurloop Z2 60'")
    auto_names.run(runtime, now=NOW)
    assert _name(runtime, group)["text"] == "Duurloop Z2 60'"


def test_no_plan_session_no_plan_title(runtime):
    group = _workout(runtime)
    assert auto_names.plan_title(runtime, group, START.date()) is None


def test_off_leaves_names_alone(runtime):
    _ready(runtime, auto_name_enabled=False)
    group = _workout(runtime)
    assert auto_names.run(runtime, now=NOW) == {"named": 0, "locked": 0, "kept": 0}
    assert _name(runtime, group)["text"] == "Leeuwarden Hardlopen"
    assert workout_texts.history(runtime.engine, group) == []


def test_the_history_is_never_renamed(runtime):
    group = _workout(runtime)
    # First run with the setting on (the default): the cutoff is now, the workout is older.
    assert auto_names.run(runtime, now=NOW)["named"] == 0
    since = load_settings(runtime.settings.config_path).publication["auto_name_since"]
    assert since == NOW.isoformat()
    assert _name(runtime, group)["text"] == "Leeuwarden Hardlopen"


def test_a_name_you_changed_is_locked_and_kept_on_reanalysis(runtime):
    _ready(runtime, language="nl")
    group = _workout(runtime)
    auto_names.run(runtime, now=NOW)
    workout_texts.set_texts(runtime.engine, group, name="Rondje met Sanne", author="portal")
    lock = _name(runtime, group)["lock"]
    assert lock and lock["source"] == "vs"
    # Analysed again (a derive runs the naming again), and a plan match arrives later.
    put(runtime, "garmin", replace(activity(), source_id="9", start_utc=START + timedelta(days=9)))
    assert auto_names.run(runtime, now=NOW)["locked"] == 1
    assert _name(runtime, group)["text"] == "Rondje met Sanne"


def test_a_change_pulled_in_from_strava_locks_the_name(runtime):
    _ready(runtime, language="nl")
    group = _workout(runtime)
    auto_names.run(runtime, now=NOW)
    workout_texts.take_over(runtime.engine, group, "name", "Bosrondje", source="strava")
    assert _name(runtime, group)["lock"]["source"] == "strava"
    assert auto_names.run(runtime, now=NOW)["named"] == 0
    assert _name(runtime, group)["text"] == "Bosrondje"


def test_the_platform_name_from_the_first_sync_does_not_lock(runtime):
    _ready(runtime, language="nl")
    group = _workout(runtime)
    workout_texts.take_over(
        runtime.engine, group, "name", "Morning Run", source="strava", kind="import"
    )
    assert _name(runtime, group)["lock"] is None
    auto_names.run(runtime, now=NOW)
    assert _name(runtime, group)["text"] == "Ochtendloop Leeuwarden"


def test_an_automatic_name_never_writes_over_a_lock(runtime):
    group = _workout(runtime)
    workout_texts.set_texts(runtime.engine, group, name="Mijn naam")
    try:
        workout_texts.set_auto_name(runtime.engine, group, "Ochtendloop")
    except ValueError as exc:
        assert "locked" in str(exc)
    else:
        raise AssertionError("an automatic name wrote over a locked name")


def test_name_lock_from_the_history():
    def v(kind, source, version=1):
        return {"field": "name", "kind": kind, "source": source, "version": version, "at": None}

    assert workout_texts.name_lock([v("seed", "garmin"), v("import", "strava")]) is None
    assert workout_texts.name_lock([v("auto", "vs"), v("overwritten", "strava")]) is None
    assert workout_texts.name_lock([v("edit", "ai")]) is None
    assert workout_texts.name_lock([v("auto", "vs", 3), v("restore", "vs", 2)])["version"] == 2
    assert workout_texts.name_lock([v("edit", "garmin", 5)])["source"] == "garmin"


def test_settings_show_and_save_the_switch(runtime):
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/settings").json()["data"]
    assert data["publication"]["auto_name_enabled"] is True
    client.post(
        "/api/settings",
        json={"changes": {"publication": {"auto_name_enabled": False}}},
        headers=WRITE,
    )
    saved = load_settings(runtime.settings.config_path).publication
    assert saved["auto_name_enabled"] is False
    client.post(
        "/api/settings",
        json={"changes": {"publication": {"auto_name_enabled": True}}},
        headers=WRITE,
    )
    saved = load_settings(runtime.settings.config_path).publication
    assert "auto_name_enabled" not in saved and saved["auto_name_since"]  # on again: from now
