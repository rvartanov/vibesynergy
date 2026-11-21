"""A Garmin copy removed on Garmin (#151): marked, kept, and no longer a live source.

As on 21-11-2026: Zwift's upload (here ``garmin:1``, the group's own id) was deleted on
Garmin while the watch's copy (``garmin:3``) stayed. The mirror retargets to the copy that is
left, and stops retrying the one that is gone. Every provider is synthetic."""

from datetime import UTC, datetime, timedelta

import pytest
from garminconnect import GarminConnectNotFoundError
from sqlalchemy import select
from test_evidence import START
from test_publication_dup_garmin import zwift_and_watch
from test_publication_insight import PUBLIC, write
from test_publication_mirror import FakeStrava

from vibesynergy.core.timeutil import Window
from vibesynergy.query import (
    publication,
    publication_auto,
    publication_format,
    publication_mirror,
    publication_plugins,
    source_removed,
    views,
)
from vibesynergy.store.schema import activity
from vibesynergy.store.schema import publication_mirror as mirror_table
from vibesynergy.sync.engine import DatasetResult


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


class TwoCopies:
    """Garmin with the copies in ``alive``; any other id answers like a deleted activity."""

    def __init__(self, alive=("1", "3")):
        self.alive = set(alive)
        self.text = {i: "Garmin words" for i in ("1", "3")}
        self.title = {i: "Indoorcycling" for i in ("1", "3")}
        self.reads, self.writes = [], []

    def _check(self, source_id):
        if source_id not in self.alive:
            raise publication.Gone("garmin", source_id)

    def read(self, source_id):
        self.reads.append(source_id)
        self._check(source_id)
        return {
            "activityId": int(source_id),
            "activityName": self.title[source_id],
            "description": self.text[source_id],
        }

    def write(self, source_id, description):
        self._check(source_id)
        self.writes.append(source_id)
        self.text[source_id] = description

    def write_title(self, source_id, title):
        self._check(source_id)
        self.title[source_id] = title


@pytest.fixture
def ride(runtime, monkeypatch):
    zwift_and_watch(runtime)
    write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    strava, garmin = FakeStrava(), TwoCopies(alive={"3"})
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )
    return strava, garmin


def publish(runtime):
    item = publication.preview(runtime, "strava:2", "strava", language="nl")
    return publication.publish(
        runtime, "strava:2", "strava", item["preview_version"], language="nl"
    )


def watch(runtime):
    with runtime.engine.connect() as conn:
        return conn.execute(select(mirror_table)).mappings().one()


def test_mirror_retargets_to_the_copy_that_is_left_and_stops_retrying(runtime, ride):
    strava, garmin = ride
    assert publication_format.copy_id(runtime, "garmin:1", "garmin") == "1"  # before
    result = publish(runtime)
    assert result["mirror"]["state"] == "watching" and "error" not in result["mirror"]
    assert result["mirror"]["garmin"] == "updated"
    assert result["mirror"]["garmin_removed"] == ["1"]
    assert garmin.writes == ["3"] and PUBLIC in garmin.text["3"]
    assert garmin.title["3"] == strava.title
    assert source_removed.is_removed(runtime.engine, "garmin", "1")
    row = watch(runtime)
    assert row["garmin_id"] == "3" and row["last_error"] is None
    # The choice rule, the Garmin target and the figures all follow the copy that is left.
    assert publication_format.copy_id(runtime, "garmin:1", "garmin") == "3"
    assert publication._source_id(runtime, "garmin:1", "garmin") == "3"
    assert publication_format.garmin_fields(runtime, "garmin:1")["training_load"] == 59.0
    # Next checks: no more attempts on the removed copy, no errors.
    garmin.reads.clear()
    for minutes in (30, 60):
        summary = publication_mirror.run(
            runtime, now=datetime.now(UTC) + timedelta(minutes=minutes)
        )
        assert summary["failed"] == 0 and summary["checked"] == 1
    assert "1" not in garmin.reads
    assert watch(runtime)["last_error"] is None


def test_the_removed_copy_keeps_its_row_and_shows_on_the_workout_page(runtime, ride):
    publish(runtime)
    with runtime.engine.connect() as conn:
        assert conn.execute(
            select(activity.c.source_id).where(activity.c.source == "garmin")
        ).scalars().all() == ["1", "3"]
    detail = views.activity_detail(runtime.engine, runtime.parquet, "garmin:1")
    by_id = {s["id"]: s for s in detail["sources"]}
    assert by_id["garmin:1"]["removed_at"] is not None
    assert by_id["garmin:3"]["removed_at"] is None and by_id["strava:2"]["removed_at"] is None
    assert detail["activity"]["group_id"] == "garmin:1"  # the group and its history stay


def test_without_a_garmin_copy_left_the_mirror_ends_quietly(runtime, ride):
    _, garmin = ride
    garmin.alive.clear()
    result = publish(runtime)
    assert result["mirror"]["garmin"] == "not_linked"
    assert result["mirror"]["garmin_removed"] == ["1", "3"]
    assert not publication_format.has_garmin(runtime, "garmin:1")
    reads = len(garmin.reads)
    summary = publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert summary["failed"] == 0 and len(garmin.reads) == reads
    assert watch(runtime)["garmin_id"] is None


def test_auto_publish_targets_the_live_copy(runtime, ride):
    from test_publication_auto import NOW, switch_on

    source_removed.mark(runtime.engine, "garmin", "1", reason="sync")
    switch_on(runtime, language="nl")
    found = publication_auto.candidates(runtime, now=NOW)
    assert [(c["strava_id"], c["garmin_id"]) for c in found] == [("2", "3")]


class _Client:
    """A Garmin client whose ``get_activity`` answers 404 the first ``misses`` times."""

    profile_id = 7

    def __init__(self, misses):
        self.misses, self.calls = misses, []

    def get_activity(self, source_id):
        self.calls.append(source_id)
        if len(self.calls) <= self.misses:
            raise GarminConnectNotFoundError("API call client error (404)")
        return {"activityId": int(source_id), "userProfileId": 7}

    def set_activity_description(self, source_id, description):
        raise GarminConnectNotFoundError("API call client error (404)")


def _provider(runtime, monkeypatch, client):
    from vibesynergy.sources.garmin import client as garmin_client

    monkeypatch.setattr(garmin_client, "connect", lambda *_a, **_k: client)
    monkeypatch.setattr(garmin_client, "save_tokens", lambda *_a, **_k: None)
    return publication.GarminProvider(runtime)


def test_provider_rechecks_a_404_once_before_marking(runtime, monkeypatch):
    zwift_and_watch(runtime)
    client = _Client(misses=2)
    with pytest.raises(publication.Gone):
        _provider(runtime, monkeypatch, client).read("1")
    assert client.calls == ["1", "1"]
    assert source_removed.is_removed(runtime.engine, "garmin", "1")


def test_a_single_404_is_not_a_removal(runtime, monkeypatch):
    zwift_and_watch(runtime)
    client = _Client(misses=1)
    with pytest.raises(GarminConnectNotFoundError):
        _provider(runtime, monkeypatch, client).read("1")
    assert not source_removed.is_removed(runtime.engine, "garmin", "1")


def test_a_write_404_is_rechecked_with_a_read(runtime, monkeypatch):
    zwift_and_watch(runtime)
    client = _Client(misses=5)
    with pytest.raises(publication.Gone) as caught:
        _provider(runtime, monkeypatch, client).write("1", "text")
    assert caught.value.reason == "write" and client.calls == ["1"]
    with runtime.engine.connect() as conn:
        assert source_removed.removed(conn, ["garmin:1"])


class _Source:
    """The Garmin source after a sync: the fresh list, and an API that answers per id."""

    def __init__(self, listed, missing=(), failing=()):
        self._listed, self.missing, self.failing = set(listed), set(missing), set(failing)
        self.asked = []
        self.api = self

    def listed(self, window):
        return self._listed

    def get_activity(self, source_id):
        self.asked.append(source_id)
        if source_id in self.failing:
            raise ConnectionError("offline")
        if source_id in self.missing:
            raise GarminConnectNotFoundError("404")
        return {"activityId": int(source_id)}


def _results(day):
    return [DatasetResult("garmin", "activities", Window(day - timedelta(days=3), day))]


def test_the_sync_check_finds_a_copy_garmin_no_longer_lists(runtime):
    zwift_and_watch(runtime)
    day = START.date()
    source = _Source(listed={"3"}, missing={"1"})
    summary = source_removed.check_recent(runtime.engine, source, _results(day), today=day)
    assert summary == {"checked": 1, "removed": 1, "restored": 0}
    assert source.asked == ["1"]
    assert publication_format.copy_id(runtime, "garmin:1", "garmin") == "3"
    # Listed again (it came back): the mark goes.
    back = _Source(listed={"1", "3"})
    summary = source_removed.check_recent(runtime.engine, back, _results(day), today=day)
    assert summary["restored"] == 1 and back.asked == []
    assert not source_removed.is_removed(runtime.engine, "garmin", "1")


def test_the_sync_check_marks_nothing_without_a_404(runtime):
    zwift_and_watch(runtime)
    day = START.date()
    for source in (_Source(listed={"3"}), _Source(listed={"3"}, failing={"1"})):
        source_removed.check_recent(runtime.engine, source, _results(day), today=day)
        assert source.asked == ["1"]
    assert not source_removed.is_removed(runtime.engine, "garmin", "1")
    # Old activities are not asked about: the check stays light.
    late = _Source(listed=set(), missing={"1", "3"})
    later = day + timedelta(days=30)
    source_removed.check_recent(runtime.engine, late, _results(day), today=later)
    assert late.asked == []


def test_a_failed_activities_sync_checks_nothing(runtime):
    zwift_and_watch(runtime)
    day = START.date()
    results = _results(day)
    results[0].error = "boom"
    source = _Source(listed=set(), missing={"1", "3"})
    source_removed.check_recent(runtime.engine, source, results, today=day)
    assert source.asked == []
