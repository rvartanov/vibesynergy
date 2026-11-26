"""Automatic workout names (#114 phase 7a, #188): the AI coach's name from its insight, the
lock, the cutoff. Synthetic data; no AI provider is called."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import anyio
import pytest
from mcp import Client
from test_evidence import START, activity, put

from vibesynergy.core import config_edit
from vibesynergy.core.config import load_settings
from vibesynergy.mcp.server import build_server
from vibesynergy.query import auto_names, publication_insight, workout_texts

WRITE = {"x-vibesynergy": "portal"}


def _settings(rt, **publication):
    config_edit.apply_changes(rt.settings.config_path, {"publication": publication})


def _workout(rt, *, place="Utrecht", sport="running", source_id="1"):
    garmin = replace(
        activity(),
        source_id=source_id,
        start_utc=START - timedelta(hours=2),
        tz_offset_s=7200,
        sport=sport,
        name="Utrecht Hardlopen",
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


def _insight(rt, group, name, key="insight-1"):
    """The AI coach writes its insight with a workout name, as over MCP."""
    args = {
        "entry_type": "activity_assessment",
        "summary": "Rustige duurloop, goed gedoseerd.",
        "idempotency_key": key,
        "for_date": START.date().isoformat(),
        "activity_id": group,
        "public_insight": "Rustig en gelijkmatig gelopen.",
    }
    if name is not None:
        args["workout_name"] = name

    async def go():
        async with Client(build_server(rt)) as client:
            return await client.call_tool("vibesynergy_coach_write", args)

    result = anyio.run(go)
    text = result.content[0].text if result.content else ""
    assert not result.is_error, text
    found = result.structured_content or json.loads(text)
    return found.get("data", found)


def test_the_insight_names_the_workout(runtime):
    _ready(runtime)
    group = _workout(runtime)
    entry = _insight(runtime, group, "  Duurloop Z2 10 km\nUtrecht ")
    assert entry["meta"]["workout_name"] == "Duurloop Z2 10 km Utrecht"
    name = _name(runtime, group)
    assert name["text"] == "Duurloop Z2 10 km Utrecht" and name["lock"] is None
    newest = workout_texts.history(runtime.engine, group, "name")[0]
    assert (newest["kind"], newest["source"], newest["author"]) == ("auto", "ai", "ai-coach")


def test_a_rewritten_insight_renames_an_unlocked_name(runtime):
    _ready(runtime)
    group = _workout(runtime)
    _insight(runtime, group, "Duurloop")
    _insight(runtime, group, "Duurloop met 4x1 km tempo", key="insight-1-redo")
    assert _name(runtime, group)["text"] == "Duurloop met 4x1 km tempo"


def test_a_locked_name_is_not_overwritten(runtime):
    _ready(runtime)
    group = _workout(runtime)
    _insight(runtime, group, "Duurloop")
    workout_texts.set_texts(runtime.engine, group, name="Rondje met Sanne", author="portal")
    _insight(runtime, group, "Duurloop Z2", key="insight-1-redo")
    assert _name(runtime, group)["text"] == "Rondje met Sanne"
    assert auto_names.from_insight(runtime, group, "Iets anders") == "locked"


def test_a_change_pulled_in_from_strava_locks_the_name(runtime):
    _ready(runtime)
    group = _workout(runtime)
    workout_texts.take_over(runtime.engine, group, "name", "Bosrondje", source="strava")
    assert auto_names.from_insight(runtime, group, "Duurloop") == "locked"
    assert _name(runtime, group)["text"] == "Bosrondje"


def test_the_platform_name_from_the_first_sync_does_not_lock(runtime):
    _ready(runtime)
    group = _workout(runtime)
    workout_texts.take_over(
        runtime.engine, group, "name", "Morning Run", source="strava", kind="import"
    )
    assert auto_names.from_insight(runtime, group, "Duurloop Utrecht") == "named"
    assert _name(runtime, group)["text"] == "Duurloop Utrecht"


def test_off_leaves_names_alone(runtime):
    _ready(runtime, auto_name_enabled=False)
    group = _workout(runtime)
    entry = _insight(runtime, group, "Duurloop")
    assert entry["meta"]["workout_name"] == "Duurloop"  # kept on the entry only
    assert _name(runtime, group)["text"] == "Utrecht Hardlopen"
    assert workout_texts.history(runtime.engine, group, "name") == []


def test_no_name_in_the_insight_changes_nothing(runtime):
    _ready(runtime)
    group = _workout(runtime)
    entry = _insight(runtime, group, None)
    assert "workout_name" not in entry["meta"]
    assert _name(runtime, group)["text"] == "Utrecht Hardlopen"
    assert workout_texts.history(runtime.engine, group, "name") == []
    assert auto_names.from_insight(runtime, group, "   ") == "kept"


def test_the_history_is_never_renamed(runtime):
    group = _workout(runtime)
    # The first derive with the setting on (the default) sets the cutoff: the workout is older.
    auto_names.after_derive(runtime)
    assert load_settings(runtime.settings.config_path).publication["auto_name_since"]
    assert auto_names.from_insight(runtime, group, "Duurloop") == "old"
    assert _name(runtime, group)["text"] == "Utrecht Hardlopen"


def test_a_late_insight_still_names_a_new_workout(runtime):
    """No window after the start: an insight written days later still names it."""
    _ready(runtime)
    group = _workout(runtime)
    later = START + timedelta(days=9)
    assert auto_names.from_insight(runtime, group, "Duurloop", now=later) == "named"


def test_derive_no_longer_names(runtime):
    _ready(runtime)
    group = _workout(runtime)
    runtime.derive()
    assert _name(runtime, group)["text"] == "Utrecht Hardlopen"
    assert workout_texts.history(runtime.engine, group, "name") == []
    assert not hasattr(auto_names, "run")


@pytest.mark.parametrize("bad", ["Duurloop na blessure knie", "Rit www.example.com"])
def test_a_private_or_linked_name_is_refused(bad):
    with pytest.raises(publication_insight.InsightError):
        publication_insight.validate_name(bad)


def test_an_automatic_name_never_writes_over_a_lock(runtime):
    group = _workout(runtime)
    workout_texts.set_texts(runtime.engine, group, name="Mijn naam")
    with pytest.raises(ValueError, match="locked"):
        workout_texts.set_auto_name(runtime.engine, group, "Ochtendloop")


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
