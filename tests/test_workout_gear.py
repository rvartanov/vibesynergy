"""Gear on the workout page and the gear mapping (#114, phase 5).

Synthetic gear lists and activities only; nothing here reaches Strava or Garmin."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_evidence import put

from vibesynergy.model import Activity
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import gear_map, workout_gear, workouts

DAY = date(2026, 11, 20)
T = datetime(2026, 11, 20, 7, tzinfo=UTC)
WRITE = {"x-vibesynergy": "portal"}
STRAVA_GEAR = {
    "g1": {"name": "Pegasus 40", "brand_name": "Nike", "distance": 300_000},
    "g2": {"name": "Speedgoat 5", "brand_name": "Hoka", "distance": 120_000},
    "b1": {"name": "Canyon Endurace", "distance": 4_000_000},
}
GARMIN_GEAR = [
    {"uuid": "u-peg", "displayName": "Nike Pegasus", "gearTypeName": "Shoes"},
    {"uuid": "u-goat", "customMakeModel": "Hoka Speedgoat 5", "gearTypeName": "Shoes"},
    {"uuid": "u-bike", "displayName": "Racefiets", "gearTypeName": "Bike"},
    {
        "uuid": "u-old",
        "displayName": "Old shoes",
        "gearTypeName": "Shoes",
        "gearStatusName": "retired",
    },
]


def _snapshots(rt, strava=STRAVA_GEAR, garmin=GARMIN_GEAR):
    rt.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps(strava).encode(),
        content_type="application/json",
    )
    rt.raw.put(
        source="garmin",
        dataset="gear",
        item_key="current",
        payload=json.dumps({"gear": garmin, "stats": {}}).encode(),
        content_type="application/json",
    )


def _act(rt, sid, sport, start, gear_id=None, source="strava"):
    extra = {"gear_id": gear_id} if gear_id else {}
    put(
        rt,
        source,
        Activity(
            sid,
            start,
            start.date(),
            sport,
            name=f"{sport} {sid}",
            distance_m=10_000.0,
            moving_s=3600,
            elapsed_s=3600,
            extra=extra,
        ),
    )
    return f"{source}:{sid}"


def _client(rt):
    return TestClient(create_portal_app(rt), base_url="http://localhost")


# --- the mapping -------------------------------------------------------------------------


def test_garmin_items_are_read_defensively(runtime):
    _snapshots(runtime, garmin=[*GARMIN_GEAR, {"no": "uuid"}, "junk", {"uuid": "u-x"}])
    found = {g["uuid"]: g for g in gear_map.garmin_items(runtime.raw)}
    assert found["u-goat"]["name"] == "Hoka Speedgoat 5" and found["u-goat"]["kind"] == "shoes"
    assert found["u-bike"]["kind"] == "bike" and found["u-old"]["retired"] is True
    assert found["u-x"]["name"] == "u-x" and found["u-x"]["kind"] == "other"
    runtime.raw.put(
        source="garmin",
        dataset="gear",
        item_key="current",
        payload=b"not json",
        content_type="application/json",
    )
    assert gear_map.garmin_items(runtime.raw) == []


def test_mapping_suggests_matches_by_type_and_name(runtime):
    _snapshots(runtime)
    found = gear_map.mapping(runtime.engine, runtime.raw, DAY)
    rows = {r["ref"]: r for r in found["items"]}
    assert rows["strava:g1"]["suggestion"] == "u-peg" and rows["strava:g1"]["reason"] == "name"
    assert rows["strava:g2"]["suggestion"] == "u-goat"
    # The only bike on both sides, although its names differ.
    assert rows["strava:b1"]["suggestion"] == "u-bike" and rows["strava:b1"]["reason"] == "only"
    assert rows["strava:g1"]["strava"] == "g1" and not rows["strava:g1"]["saved"]
    assert "u-old" not in {g["uuid"] for g in found["garmin"]}  # retired: not offered
    # Deterministic: the same lists give the same suggestions.
    assert gear_map.mapping(runtime.engine, runtime.raw, DAY) == found


def test_a_bike_is_never_matched_to_shoes_and_each_garmin_item_once():
    vs = [
        {"ref": "strava:g1", "name": "Canyon", "kind": "shoes"},
        {"ref": "strava:g2", "name": "Canyon", "kind": "shoes"},
    ]
    garmin = [
        {"uuid": "u1", "name": "Canyon", "kind": "bike", "retired": False},
        {"uuid": "u2", "name": "Canyon", "kind": "shoes", "retired": False},
    ]
    hints = gear_map.suggest(vs, garmin)
    assert hints == {"strava:g1": {"uuid": "u2", "reason": "name"}}


def test_saving_the_mapping_and_using_the_suggestions(runtime):
    _snapshots(runtime)
    api = _client(runtime)
    saved = api.post("/api/gear-map", json={"accept_suggestions": True}, headers=WRITE)
    rows = {r["ref"]: r for r in saved.json()["data"]["items"]}
    assert rows["strava:g1"]["garmin"] == "u-peg" and rows["strava:g1"]["saved"]
    # Edit one: the Garmin item moves, the item it belonged to is no longer mapped.
    edited = api.post("/api/gear-map", json={"choices": {"strava:g2": "u-peg"}}, headers=WRITE)
    rows = {r["ref"]: r for r in edited.json()["data"]["items"]}
    assert rows["strava:g2"]["garmin"] == "u-peg" and rows["strava:g1"]["garmin"] is None
    assert rows["strava:g1"]["saved"]  # "not on Garmin" is a choice too: no new suggestion
    assert rows["strava:g1"]["suggestion"] is None
    bad = api.post(
        "/api/gear-map",
        json={"choices": {"strava:g1": "u-bike", "strava:b1": "u-bike"}},
        headers=WRITE,
    )
    assert bad.status_code == 400
    assert api.post("/api/gear-map", json={"accept_suggestions": True}).status_code == 403


def test_gear_of_your_own_is_in_the_gear_list(runtime):
    _snapshots(runtime)
    api = _client(runtime)
    made = api.post("/api/gear-own", json={"name": "Pull buoy", "kind": "swim"}, headers=WRITE)
    ref = made.json()["data"]["ref"]
    assert ref.startswith("vs:")
    again = api.post("/api/gear-own", json={"name": "pull  buoy", "kind": "swim"}, headers=WRITE)
    assert again.json()["data"]["ref"] == ref  # the same item, not a second one
    listed = {g["ref"]: g for g in workouts.gear(runtime.engine, runtime.raw, DAY)["gear"]}
    assert listed[ref]["kind"] == "swim" and listed[ref]["source"] == "vs"
    assert listed[ref]["total_km"] is None
    renamed = api.post("/api/gear-own", json={"ref": ref, "name": "Pullbuoy"}, headers=WRITE)
    assert renamed.status_code == 200
    assert gear_map.own_items(runtime.engine)[0]["name"] == "Pullbuoy"
    assert (
        api.post("/api/gear-own", json={"name": " ", "kind": "swim"}, headers=WRITE).status_code
        == 400
    )


# --- the suggestion ----------------------------------------------------------------------


def test_the_suggestion_is_the_gear_of_the_last_workout_of_that_sport(runtime):
    _snapshots(runtime)
    _act(runtime, "r1", "running", T - timedelta(days=3), "g1")
    _act(runtime, "r2", "trail_running", T - timedelta(days=1), "g2")
    _act(runtime, "b1x", "cycling", T - timedelta(hours=10), "b1")
    run = _act(runtime, "r3", "running", T)
    hint = workout_gear.suggest(runtime.engine, runtime.raw, run, DAY)
    assert hint == {
        "main": "strava:g2",
        "extras": [],
        "reason": "last",
        "date": str((T - timedelta(days=1)).date()),
    }
    ride = _act(runtime, "b2", "cycling", T + timedelta(hours=2))
    assert workout_gear.suggest(runtime.engine, runtime.raw, ride, DAY)["main"] == "strava:b1"
    # Retired gear is never suggested: the run before it counts.
    workouts.set_gear(runtime.engine, "strava:g2", status="retired")
    assert workout_gear.suggest(runtime.engine, runtime.raw, run, DAY)["main"] == "strava:g1"


def test_without_an_earlier_workout_the_default_then_the_kind(runtime):
    _snapshots(runtime)
    ride = _act(runtime, "c1", "cycling", T, source="garmin")
    hint = workout_gear.suggest(runtime.engine, runtime.raw, ride, DAY)
    assert hint["main"] == "strava:b1" and hint["reason"] == "kind"
    workouts.set_gear_default(runtime.engine, "running", "strava:g2", DAY - timedelta(days=30))
    run = _act(runtime, "x1", "running", T + timedelta(hours=1), source="garmin")
    assert workout_gear.suggest(runtime.engine, runtime.raw, run, DAY)["main"] == "strava:g2"
    yoga = _act(runtime, "y1", "yoga", T + timedelta(hours=3), source="garmin")
    assert workout_gear.suggest(runtime.engine, runtime.raw, yoga, DAY) is None


def test_swim_gear_is_suggested_from_the_last_swim(runtime):
    _snapshots(runtime)
    buoy = gear_map.add_own(runtime.engine, "Pull buoy", "swim")["ref"]
    paddles = gear_map.add_own(runtime.engine, "Paddles", "swim")["ref"]
    first = _act(runtime, "s1", "lap_swimming", T - timedelta(days=2), source="garmin")
    workout_gear.set_gear(
        runtime.engine, runtime.raw, first, main=None, extras=[buoy, paddles], today=DAY
    )
    swim = _act(runtime, "s2", "lap_swimming", T, source="garmin")
    hint = workout_gear.suggest(runtime.engine, runtime.raw, swim, DAY)
    assert hint["main"] is None and hint["extras"] == sorted([buoy, paddles])


# --- link and unlink ---------------------------------------------------------------------


def test_one_tap_links_the_suggestion_and_the_picker_changes_or_unlinks(runtime):
    _snapshots(runtime)
    _act(runtime, "r1", "running", T - timedelta(days=1), "g2")
    run = _act(runtime, "r2", "running", T, source="garmin")
    api = _client(runtime)
    page = api.get("/api/workout-gear", params={"id": run}).json()["data"]
    assert page["main"] is None and page["suggestion"]["main"]["ref"] == "strava:g2"
    assert page["suggestion"]["reason"] == "last"
    assert {o["ref"] for o in page["options"]["main"]} >= {"strava:g1", "strava:g2", "strava:b1"}
    hint = page["suggestion"]
    tapped = api.post(
        "/api/workout-gear",
        json={"id": run, "main": hint["main"]["ref"], "extras": []},
        headers=WRITE,
    ).json()["data"]
    assert tapped["main"]["ref"] == "strava:g2" and tapped["how"] == "own"
    assert tapped["suggestion"]["linked"] is True
    assert tapped["main"]["goes_to"] == ["strava"]  # no Garmin mapping yet
    # It counts in the km totals at once (as the old picker did).
    totals = {
        g["id"]: g["activities_seen"]
        for g in workouts.gear(runtime.engine, runtime.raw, DAY)["gear"]
    }
    assert totals["g2"] == 2
    other = api.post("/api/workout-gear", json={"id": run, "main": "strava:g1"}, headers=WRITE)
    assert other.json()["data"]["main"]["ref"] == "strava:g1"
    none = api.post("/api/workout-gear", json={"id": run, "main": "none"}, headers=WRITE)
    data = none.json()["data"]
    assert data["main"] is None and data["how"] == "own"
    history = data["history"]
    assert [h["names"] for h in history] == [[], ["Pegasus 40"], ["Speedgoat 5"]]
    assert all(h["source"] == "vs" and h["kind"] == "edit" for h in history)
    assert history[0]["old_names"] == ["Pegasus 40"]


def test_several_swim_items_and_the_checks(runtime):
    _snapshots(runtime)
    buoy = gear_map.add_own(runtime.engine, "Pull buoy", "swim")["ref"]
    fins = gear_map.add_own(runtime.engine, "Fins", "swim")["ref"]
    swim = _act(runtime, "s1", "lap_swimming", T, source="garmin")
    done = workout_gear.set_gear(
        runtime.engine, runtime.raw, swim, main=None, extras=[fins, buoy, buoy], today=DAY
    )
    assert done["extras"] == sorted([buoy, fins]) and done["changed"]
    again = workout_gear.set_gear(
        runtime.engine, runtime.raw, swim, main=None, extras=[buoy, fins], today=DAY
    )
    assert again["changed"] is False
    assert len(workout_gear.history(runtime.engine, swim)) == 1
    with pytest.raises(ValueError):  # swim gear is not a main item
        workout_gear.set_gear(runtime.engine, runtime.raw, swim, main=buoy, extras=[], today=DAY)
    with pytest.raises(ValueError):  # a Strava item has its one slot as the main item
        workout_gear.set_gear(
            runtime.engine, runtime.raw, swim, main=None, extras=["strava:g1"], today=DAY
        )
    with pytest.raises(ValueError):
        workout_gear.set_gear(
            runtime.engine, runtime.raw, swim, main="strava:nope", extras=[], today=DAY
        )
    api = _client(runtime)
    assert api.post("/api/workout-gear", json={"id": swim, "extras": []}).status_code == 403
    bad = api.post("/api/workout-gear", json={"id": swim, "extras": ["vs:nope"]}, headers=WRITE)
    assert bad.status_code == 400
    cleared = api.post("/api/workout-gear", json={"id": swim, "extras": []}, headers=WRITE)
    assert cleared.json()["data"]["extras"] == []
    # Extra gear counts as used in the gear list.
    listed = {g["ref"]: g for g in workouts.gear(runtime.engine, runtime.raw, DAY)["gear"]}
    assert listed[buoy]["activities_seen"] == 0


def test_a_share_link_cannot_read_or_change_gear_or_the_mapping(runtime):
    from dataclasses import replace

    from vibesynergy.api.app import create_app
    from vibesynergy.portal import share
    from vibesynergy.runtime import Runtime

    host = "server.example.ts.net"
    settings = replace(runtime.settings, server={"public_url": f"https://{host}"})
    rt = Runtime.open(settings)
    try:
        _snapshots(rt)
        swim = _act(rt, "s1", "lap_swimming", T, source="garmin")
        code, _ = share.create(rt.engine, "Coach Anna", ["activities", "gear"], can_write=True)
        client = TestClient(create_app(rt, with_mcp=False), base_url=f"https://{host}")
        base = f"/vibesynergy/share/{code}/api"
        assert client.get(f"{base}/gear").status_code == 200  # the gear page itself is shared
        assert client.get(f"{base}/workout-gear", params={"id": swim}).status_code == 403
        assert client.get(f"{base}/gear-map").status_code == 403
        write = {"x-vibesynergy": "portal", "origin": f"https://{host}"}
        for path, body in (
            ("workout-gear", {"id": swim, "extras": []}),
            ("gear-map", {"accept_suggestions": True}),
            ("gear-own", {"name": "x", "kind": "swim"}),
        ):
            assert client.post(f"{base}/{path}", json=body, headers=write).status_code == 403
    finally:
        rt.close()
