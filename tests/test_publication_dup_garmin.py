"""Two Garmin copies of one ride (Zwift's upload and the watch recording, #146): the block
takes its figures from one of them, so the ride is previewed, shows the coach's insight and
is published by itself. Every provider is synthetic."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_evidence import START, put
from test_publication import garmin_activity
from test_publication_auto import NOW, switch_on
from test_publication_insight import PUBLIC, write
from test_publication_targets import FakeGarmin, FakeStrava

from vibesynergy.query import (
    annotations,
    publication,
    publication_auto,
    publication_format,
    publication_plugins,
)
from vibesynergy.store.schema import activity_unified


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


def zwift_and_watch(runtime):
    """As on 21-11-2026: Zwift's own upload, the watch's indoor ride, one Strava copy."""
    zwift = replace(
        garmin_activity(sport="virtual_ride", name="Zwift - Lange rit", fit_id=None),
        elapsed_s=4989, moving_s=4672, training_load=66.0, aerobic_te=2.8,
    )  # fmt: skip
    watch = replace(
        zwift, source_id="3", sport="indoor_cycling", name="Indoorcycling",
        start_utc=START + timedelta(seconds=37), elapsed_s=4985, moving_s=4631,
        training_load=59.0, aerobic_te=2.9,
    )  # fmt: skip
    put(runtime, "garmin", zwift, watch)
    put(runtime, "strava", replace(zwift, source_id="2", name="Strava title"))
    with runtime.engine.connect() as conn:
        sources = conn.scalar(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == "garmin:1")
        )
    assert sorted(sources) == ["garmin:1", "garmin:3", "strava:2"]


@pytest.fixture
def fakes(runtime, monkeypatch):
    zwift_and_watch(runtime)
    strava, garmin = FakeStrava(), FakeGarmin()
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )
    return strava, garmin


def test_the_groups_own_garmin_copy_gives_the_figures(runtime):
    zwift_and_watch(runtime)
    assert publication_format.copy_id(runtime, "garmin:3", "garmin") == "1"
    fields = publication_format.garmin_fields(runtime, "garmin:3")
    assert fields["training_load"] == 66.0 and fields["aerobic_te"] == 2.8
    item = annotations.preview(runtime, "garmin:1", language="nl")
    assert item["state"] == "preview" and "Training load: 66" in item["block"]


def test_otherwise_the_most_complete_then_the_longest_copy():
    rows = [
        {"source_id": "5", "elapsed_s": 900.0, "load": 1.0, "te": None},
        {"source_id": "6", "elapsed_s": 600.0, "load": 2.0, "te": 3.0},
        {"source_id": "7", "elapsed_s": 700.0, "load": 2.0, "te": 3.0},
    ]

    def values(row):
        return {"load": row["load"], "te": row["te"]}

    def choose(group, rows):
        return publication_format._choose(group, "garmin", rows, values, ("load", "te"))[0]

    assert choose("strava:9", rows)["source_id"] == "7"  # complete beats long; then longest
    assert choose("garmin:5", rows)["source_id"] == "5"  # the group's own copy first
    tie = [{**rows[1], "source_id": "8"}, rows[1]]
    assert choose("strava:9", tie)["source_id"] == "8"  # a full tie: the first in id order
    assert choose("strava:9", []) is None


def test_preview_has_the_statistics_and_the_insight(runtime, fakes):
    entry = write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    item = publication.preview(runtime, "garmin:1", "strava", language="nl")
    assert item["state"] == "ready"
    assert f"-Persoonlijke AI inzicht: {PUBLIC}\n-Garmin stats:\n" in item["owned_block"]
    assert "Training load: 66" in item["owned_block"]
    assert item["insight"] == {"state": "present", "entry_id": entry["id"]}
    # The Garmin description goes to the same copy as the figures.
    assert publication._source_id(runtime, "garmin:1", "garmin") == "1"


def test_blocked_preview_still_carries_the_insight(runtime, fakes, monkeypatch):
    entry = write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    monkeypatch.setattr(publication_format, "garmin_fields", lambda *_: {})
    blocked = annotations.preview(runtime, "garmin:1", language="nl")
    assert blocked["state"] == "blocked" and blocked["reason"] == "no_public_statistics"
    assert blocked["insight"] == {"state": "present", "entry_id": entry["id"], "text": PUBLIC}
    item = publication.preview(runtime, "garmin:1", "strava", language="nl")
    assert item["state"] == "blocked" and item["insight"]["text"] == PUBLIC


def test_auto_publish_picks_the_ride_up(runtime, fakes):
    strava, garmin = fakes
    write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    switch_on(runtime, language="nl")
    found = publication_auto.candidates(runtime, now=NOW)
    assert [(c["strava_id"], c["garmin_id"]) for c in found] == [("2", "1")]
    assert publication_auto.run(runtime, now=NOW)["published"] == 1
    assert PUBLIC in strava.text and "Training load: 66" in strava.text
    assert garmin.writes  # mirrored to the group's own Garmin copy
