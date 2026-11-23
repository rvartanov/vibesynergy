"""Gear of a published workout synced with Strava and Garmin (#114, phase 5): on the text
sync's switch and check.

Every platform is a synthetic fake: nothing here reaches Strava or Garmin."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_evidence import put
from test_publication import garmin_activity
from test_text_sync import Fake
from test_workout_gear import _snapshots

from vibesynergy.core import config_edit
from vibesynergy.core.errors import RateLimited
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import (
    gear_map,
    gear_sync,
    publication,
    publication_plugins,
    text_sync,
    workout_gear,
)
from vibesynergy.sources.strava.client import StravaError
from vibesynergy.store.schema import text_sync as text_sync_table

DAY = date(2026, 9, 20)
WRITE = {"x-vibesynergy": "portal"}
GROUP = "garmin:1"


class GearFake(Fake):
    """A platform copy with gear: Strava one id, Garmin a set of uuids."""

    def __init__(self, platform, source_id, name, text, gear):
        super().__init__(platform, source_id, name, text)
        self.gear = gear  # Strava: "g1" or None; Garmin: set of uuids
        self.gear_reads = 0
        self.gear_writes: list = []
        self.on_gear_read = None
        self.refuse: Exception | None = None
        self.attempts = 0

    def read(self, source_id):
        detail = super().read(source_id)
        if self.platform == "strava":
            detail["gear_id"] = self.gear
        return detail

    def read_gear(self, source_id):
        assert source_id == self.source_id
        if self.limited:
            raise RateLimited("synthetic budget")
        self.gear_reads += 1
        if self.on_gear_read:
            self.on_gear_read(self)
        if self.platform == "strava":
            return self.gear or "none"
        return sorted(self.gear)

    def write_gear(self, source_id, gear_id):
        assert self.platform == "strava" and source_id == self.source_id
        self.attempts += 1
        if self.refuse:
            raise self.refuse
        self.gear_writes.append(gear_id)
        self.gear = None if gear_id == "none" else gear_id

    def add_gear(self, source_id, uuid):
        assert self.platform == "garmin" and source_id == self.source_id
        self.gear_writes.append(("add", uuid))
        self.gear = set(self.gear) | {uuid}

    def remove_gear(self, source_id, uuid):
        assert self.platform == "garmin" and source_id == self.source_id
        self.gear_writes.append(("remove", uuid))
        self.gear = set(self.gear) - {uuid}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


def _setting(rt, on: bool = True):
    """One switch for the name, the texts and the gear (#114)."""
    config_edit.apply_changes(
        rt.settings.config_path, {"publication": {"text_sync_enabled": on or None}}
    )


@pytest.fixture
def fakes(runtime, monkeypatch):
    _snapshots(runtime)
    gear_map.accept_suggestions(runtime.engine, runtime.raw, DAY)  # g1-u-peg, g2-u-goat, b1-u-bike
    a = garmin_activity()
    put(runtime, "garmin", replace(a, extra={**a.extra, "gear_ids": ["u-peg", "u-other"]}))
    put(runtime, "strava", replace(a, source_id="2", fit_id=None, extra={"gear_id": "g1"}))
    strava = GearFake("strava", "2", "Test", "", "g1")
    garmin = GearFake("garmin", "1", "Test", "", {"u-peg", "u-other"})  # u-other: not matched
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )
    return strava, garmin


def _publish(rt):
    item = publication.preview(rt, "strava:2", "strava")
    return publication.publish(rt, "strava:2", "strava", item["preview_version"])


def _later(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _row(rt):
    with rt.engine.connect() as conn:
        return conn.execute(select(text_sync_table)).mappings().one()


def _gear(rt):
    return workout_gear.refs_of(workout_gear.current(rt.engine, GROUP))


def _set(rt, main, extras=()):
    workout_gear.set_gear(rt.engine, rt.raw, GROUP, main=main, extras=list(extras), today=DAY)


@pytest.fixture
def synced(runtime, fakes):
    _setting(runtime)
    result = _publish(runtime)
    assert result["texts"]["state"] == "checked"
    return fakes


# --- off by default ----------------------------------------------------------------------


def test_off_by_default_nothing_is_read_or_written(runtime, fakes):
    strava, garmin = fakes
    assert gear_sync.enabled(runtime) is False
    result = _publish(runtime)
    assert "texts" not in result
    _set(runtime, "strava:g2")
    assert text_sync.due(runtime, now=_later(60)) is False
    assert text_sync.run(runtime, now=_later(60))["state"] == "off"
    assert strava.gear_reads == garmin.gear_reads == 0
    assert strava.gear_writes == garmin.gear_writes == []
    assert strava.gear == "g1" and garmin.gear == {"u-peg", "u-other"}


def test_one_switch_for_texts_and_gear(runtime):
    """No separate gear setting: the text sync's switch covers the gear (#114)."""
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/settings").json()["data"]["publication"]
    assert "gear_sync_enabled" not in data and data["text_sync_enabled"] is False
    client.post(
        "/api/settings",
        json={"changes": {"publication": {"text_sync_enabled": True}}},
        headers=WRITE,
    )
    assert gear_sync.enabled(runtime) is True


# --- pushing VibeSynergy's gear ------------------------------------------------------------


def test_publishing_agrees_when_the_gear_is_the_same(runtime, synced):
    strava, garmin = synced
    assert strava.gear_writes == [] and garmin.gear_writes == []
    gear = _row(runtime)["synced"]["gear"]
    assert gear["strava"] == "g1" and gear["garmin"] == ["u-peg"]
    # The gear rides on the text sync's read: no extra Strava read for it.
    assert strava.gear_reads == 0


def test_a_vs_change_is_pushed_to_strava_one_gear_and_garmin(runtime, synced):
    strava, garmin = synced
    buoy = gear_map.add_own(runtime.engine, "Pull buoy", "swim")["ref"]
    _set(runtime, "strava:g2", [buoy])
    assert text_sync.due(runtime, now=_later(1)) is False  # the wait after a change
    assert text_sync.due(runtime, now=_later(6)) is True
    text_sync.run(runtime, now=_later(6))
    assert strava.gear_writes == ["g2"]  # Strava: one gear id, the main item
    assert garmin.gear_writes == [("add", "u-goat"), ("remove", "u-peg")]
    # A Garmin item that is not matched is left alone; the pull buoy stays in VibeSynergy.
    assert garmin.gear == {"u-goat", "u-other"}
    assert _gear(runtime) == ["strava:g2", buoy]
    # No echo: the next check reads VibeSynergy's own write back as "nothing changed".
    before = (list(strava.gear_writes), list(garmin.gear_writes))
    text_sync.run(runtime, now=_later(60 * 3))
    assert (strava.gear_writes, garmin.gear_writes) == before
    assert len(workout_gear.history(runtime.engine, GROUP)) == 1


def test_no_gear_clears_strava_and_the_matched_garmin_items(runtime, synced):
    strava, garmin = synced
    _set(runtime, "none")
    text_sync.run(runtime, now=_later(6))
    assert strava.gear_writes == ["none"] and strava.gear is None
    assert garmin.gear == {"u-other"}


def test_a_main_item_of_your_own_leaves_strava_alone(runtime, synced):
    strava, garmin = synced
    trainer = gear_map.add_own(runtime.engine, "Tacx", "bike")["ref"]
    _set(runtime, trainer)
    text_sync.run(runtime, now=_later(6))
    assert strava.gear_writes == []  # not on Strava: Strava keeps what it has
    assert garmin.gear == {"u-other"}  # its Garmin item (u-peg) is no longer the gear


# --- outside changes -----------------------------------------------------------------------


def test_a_strava_change_is_taken_in_and_passed_to_garmin(runtime, synced):
    strava, garmin = synced
    strava.gear = "g2"  # changed on Strava
    text_sync.run(runtime, now=_later(60 * 3))
    assert _gear(runtime) == ["strava:g2"]
    assert garmin.gear == {"u-goat", "u-other"}
    assert strava.gear_writes == []
    taken = workout_gear.history(runtime.engine, GROUP)[0]
    assert taken["source"] == "strava" and taken["kind"] == "taken" and taken["author"] == "sync"
    assert taken["refs"] == ["strava:g2"] and taken["old_refs"] == ["strava:g1"]


def test_a_garmin_change_reaches_strava(runtime, synced):
    strava, garmin = synced
    garmin.gear = {"u-bike", "u-other"}
    a = garmin_activity()
    put(runtime, "garmin", replace(a, extra={**a.extra, "gear_ids": ["u-bike", "u-other"]}))
    text_sync.run(runtime, now=_later(60 * 3))
    assert _gear(runtime) == ["strava:b1"]
    assert strava.gear_writes == ["b1"]
    assert workout_gear.history(runtime.engine, GROUP)[0]["source"] == "garmin"


def test_taking_a_garmin_change_keeps_gear_that_lives_in_vibesynergy_only(runtime, synced):
    _strava, garmin = synced
    buoy = gear_map.add_own(runtime.engine, "Pull buoy", "swim")["ref"]
    _set(runtime, "strava:g1", [buoy])
    text_sync.run(runtime, now=_later(6))
    garmin.gear = {"u-goat", "u-other"}
    text_sync.run(runtime, now=_later(60 * 3))
    assert _gear(runtime) == ["strava:g2", buoy]


def test_both_changed_the_latest_wins_and_the_other_is_kept(runtime, synced):
    _strava, garmin = synced
    _set(runtime, "strava:b1")
    _strava.gear = "g2"  # found later than the VibeSynergy change: Strava wins
    text_sync.run(runtime, now=_later(6))
    assert _gear(runtime) == ["strava:g2"]
    history = workout_gear.history(runtime.engine, GROUP)
    assert [(h["source"], h["kind"]) for h in history[:2]] == [
        ("strava", "taken"),
        ("vs", "overwritten"),
    ]
    assert history[1]["refs"] == ["strava:b1"]
    assert garmin.gear == {"u-goat", "u-other"}


def test_the_guard_never_overwrites_a_change_made_during_the_check(runtime, synced):
    strava, garmin = synced
    _set(runtime, "strava:g2")

    def typed(fake):  # other gear picked on Strava between the read and the write
        if fake.gear_reads == 1:
            fake.gear = "b1"

    strava.on_gear_read = typed
    text_sync.run(runtime, now=_later(6))
    # The write was refused at the guard; the check ran again and took Strava's change in.
    assert strava.attempts == 0
    assert strava.gear == "b1" and _gear(runtime) == ["strava:b1"]
    assert garmin.gear == {"u-bike", "u-other"}


def test_a_platform_that_keeps_changing_is_left_for_the_next_run(runtime, synced):
    strava, _garmin = synced
    _set(runtime, "strava:g2")
    # Every check reads what was agreed, then other gear right before the write.
    strava.on_read = lambda fake: setattr(fake, "gear", "g1")
    strava.on_gear_read = lambda fake: setattr(fake, "gear", "b1")
    summary = text_sync.run(runtime, now=_later(6))
    assert summary["failed"] == 1 and strava.attempts == 0
    assert _row(runtime)["next_check_at"] is not None


def test_refused_gear_is_not_retried_and_the_texts_keep_syncing(runtime, synced):
    strava, _garmin = synced
    strava.refuse = StravaError(400, "gear does not fit this activity type")
    _set(runtime, "strava:b1")
    text_sync.run(runtime, now=_later(6))
    assert strava.attempts == 1
    assert _row(runtime)["state"] == "active"
    page = gear_sync.page_state(runtime, GROUP)
    assert page["state"] == "refused" and "did not take" in page["problem"]
    text_sync.run(runtime, now=_later(60 * 3))
    assert strava.attempts == 1  # the same gear is not pushed again
    _set(runtime, "strava:g2")
    strava.refuse = None
    text_sync.run(runtime, now=_later(60 * 3 + 6))
    assert strava.gear_writes == ["g2"] and gear_sync.page_state(runtime, GROUP)["problem"] is None


def test_strava_rate_limit_ends_the_run(runtime, synced):
    strava, _garmin = synced
    _set(runtime, "strava:g2")
    strava.limited = True
    summary = text_sync.run(runtime, now=_later(6))
    assert summary["rate_limited"] is True and strava.gear_writes == []
    strava.limited = False
    text_sync.run(runtime, now=_later(40))
    assert strava.gear_writes == ["g2"]


# --- first sync, unpublish, the page -------------------------------------------------------


def test_first_sync_takes_the_platform_gear_when_vibesynergy_has_none(runtime, monkeypatch):
    _snapshots(runtime)
    gear_map.accept_suggestions(runtime.engine, runtime.raw, DAY)
    a = garmin_activity()
    put(runtime, "garmin", replace(a, extra={**a.extra, "gear_ids": ["u-goat"]}))
    put(runtime, "strava", replace(a, source_id="2", fit_id=None))
    strava = GearFake("strava", "2", "Test", "", None)
    garmin = GearFake("garmin", "1", "Test", "", {"u-goat"})
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )
    assert _gear(runtime) == []
    _setting(runtime)
    _publish(runtime)
    assert _gear(runtime) == ["strava:g2"]
    assert strava.gear_writes == ["g2"]
    assert workout_gear.history(runtime.engine, GROUP)[0]["source"] == "garmin"


def test_unpublishing_stops_the_gear_sync(runtime, synced):
    strava, _garmin = synced
    item = publication.preview(runtime, "strava:2", "strava")
    publication.unpublish(runtime, "strava:2", "strava", item["preview_version"])
    _set(runtime, "strava:g2")
    text_sync.run(runtime, now=_later(6))
    assert strava.gear_writes == [] and _row(runtime)["state"] == "stopped"


def test_the_workout_page_says_where_the_gear_goes(runtime, synced):
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/workout-gear", params={"id": GROUP}).json()["data"]
    assert data["sync"] == {
        "enabled": True,
        "published": True,
        "state": "active",
        "problem": None,
    }
    assert data["main"]["ref"] == "strava:g1"
    assert data["main"]["goes_to"] == ["strava", "garmin"]


def test_strava_gets_one_gear_id_and_never_a_visibility(tmp_path, monkeypatch):
    from vibesynergy.core.secrets import SecretStore
    from vibesynergy.sources import oauth
    from vibesynergy.sources.strava.client import Response, StravaClient

    store = SecretStore(tmp_path, "strava")
    store.set(oauth.TOKENS, {"access_token": "t", "expires_at": 10000, "scope": "activity:write"})
    monkeypatch.setattr(oauth, "needs_refresh", lambda *_: False)
    sent = []

    def http(method, url, params, headers):
        sent.append((method, url.rsplit("/", 1)[-1], params))
        return Response(200, {}, {})

    client = StravaClient(store, http=http, clock=lambda: 1000)
    provider = publication.StravaProvider.__new__(publication.StravaProvider)
    provider.store, provider.client = store, client
    provider.write_gear("5", "b12")
    provider.write_gear("5", "none")
    assert sent == [("PUT", "5", {"gear_id": "b12"}), ("PUT", "5", {"gear_id": "none"})]
    with pytest.raises(ValueError):
        client.update_activity("5", {"visibility": "everyone"})


def test_garmin_gear_calls_name_the_activity_for_a_missing_copy(runtime, monkeypatch):
    """Garmin's link/unlink take the gear uuid first: a 404 is about the activity, not it."""
    calls = []

    def fake_call(self, method, *args, activity_id=None):
        calls.append((method, args, activity_id))
        if method == "get_activity":
            return {"activityId": 7, "userProfileId": 3}, 3
        return [{"uuid": "u-b"}, {"uuid": "u-a"}, {}], 3

    monkeypatch.setattr(publication.GarminProvider, "_call", fake_call)
    garmin = publication.GarminProvider(runtime)
    assert garmin.read_gear("7") == ["u-a", "u-b"]
    garmin.add_gear("7", "u-a")
    garmin.remove_gear("7", "u-b")
    assert calls[-2:] == [
        ("add_gear_to_activity", ("u-a", "7"), "7"),
        ("remove_gear_from_activity", ("u-b", "7"), "7"),
    ]
