"""Strava -> Garmin mirror and the 7-day watch; every provider is synthetic."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_evidence import put
from test_publication import (
    DEFAULT_CLOSER,
    DEFAULT_OPENER,
    LEGACY_CLOSER,
    LEGACY_OPENER,
    garmin_activity,
)

from vibesynergy.core import config_edit
from vibesynergy.core.errors import RateLimited
from vibesynergy.query import publication, publication_mirror, publication_plugins, views
from vibesynergy.store.schema import publication_ledger
from vibesynergy.store.schema import publication_mirror as mirror_table


class FakeStrava:
    def __init__(self, text="Strava words", title="Morning trail"):
        self.text, self.title = text, title
        self.reads = 0
        self.writes = []
        self.limited = False

    def read(self, source_id):
        assert source_id == "2"
        if self.limited:
            raise RateLimited("synthetic budget")
        self.reads += 1
        return {"id": 2, "name": self.title, "description": self.text}

    def write(self, source_id, description):
        self.writes.append(description)
        self.text = description


class FakeGarmin:
    def __init__(self, text="Garmin words", title="Ochtendloop"):
        self.text, self.title = text, title
        self.writes = []
        self.titles = []

    def read(self, source_id):
        assert source_id == "1"
        return {"activityId": 1, "activityName": self.title, "description": self.text}

    def write(self, source_id, description):
        self.writes.append(description)
        self.text = description

    def write_title(self, source_id, title):
        self.titles.append(title)
        self.title = title


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


@pytest.fixture
def fakes(runtime, monkeypatch):
    a = garmin_activity()
    put(runtime, "garmin", a)
    put(runtime, "strava", replace(a, source_id="2", name="Strava title"))
    strava, garmin = FakeStrava(), FakeGarmin()
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )
    return strava, garmin


def publish_strava(runtime):
    item = publication.preview(runtime, "strava:2", "strava")
    return item, publication.publish(runtime, "strava:2", "strava", item["preview_version"])


def row(runtime):
    with runtime.engine.connect() as conn:
        return conn.execute(select(mirror_table)).mappings().one()


def test_strava_publish_mirrors_text_block_and_title_to_garmin(runtime, fakes):
    strava, garmin = fakes
    item, result = publish_strava(runtime)
    owned = item["owned_block"]
    assert strava.text == f"Strava words\n\n{owned}"
    assert garmin.text == f"Strava words\n\n{owned}"
    assert garmin.title == "Morning trail"
    assert result["mirror"] == {
        "state": "watching",
        "strava": "unchanged",
        "garmin": "updated",
        "title": "updated",
    }
    watched = row(runtime)
    assert watched["state"] == "active" and watched["garmin_original"] == "Garmin words"
    assert watched["garmin_original_title"] == "Ochtendloop"
    assert watched["watch_until"] - watched["first_published_at"] == timedelta(days=7)
    # Another check without changes writes nothing anywhere.
    publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert len(strava.writes) == 1 and len(garmin.writes) == 1 and len(garmin.titles) == 1


def test_changed_strava_text_and_title_follow_to_garmin_without_duplicates(runtime, fakes):
    strava, garmin = fakes
    item, _ = publish_strava(runtime)
    owned = item["owned_block"]
    strava.text = f"Edited on Strava\n\n{owned}\n\nAdded below"
    strava.title = "Renamed on Strava"
    summary = publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert summary["checked"] == 1
    assert strava.text == f"Edited on Strava\n\nAdded below\n\n{owned}"
    assert garmin.text == strava.text
    assert garmin.title == "Renamed on Strava"
    assert strava.text.count(DEFAULT_OPENER) == 1 and garmin.text.count(DEFAULT_OPENER) == 1


def test_remove_on_strava_removes_the_mirrored_block_and_keeps_text(runtime, fakes):
    strava, garmin = fakes
    publish_strava(runtime)
    again = publication.preview(runtime, "strava:2", "strava")
    result = publication.unpublish(runtime, "strava:2", "strava", again["preview_version"])
    assert result["state"] == "removed"
    assert result["mirror"] == {"state": "removed", "garmin": "removed"}
    assert strava.text == "Strava words"
    assert garmin.text == "Strava words"
    assert row(runtime)["state"] == "removed"
    assert publication_mirror.due(runtime, now=datetime.now(UTC) + timedelta(days=1)) is False


def test_watch_expires_after_seven_days_and_stops(runtime, fakes):
    strava, _ = fakes
    publish_strava(runtime)
    reads = strava.reads
    later = datetime.now(UTC) + timedelta(days=7, minutes=1)
    assert publication_mirror.run(runtime, now=later)["expired"] == 1
    assert row(runtime)["state"] == "expired"
    assert strava.reads == reads
    assert publication_mirror.due(runtime, now=later + timedelta(hours=1)) is False


def test_cadence_is_fast_first_then_hourly(runtime, fakes):
    publish_strava(runtime)
    first = row(runtime)["first_published_at"]
    soon = first + timedelta(hours=1)
    publication_mirror.run(runtime, now=soon)
    assert row(runtime)["next_check_at"] == soon + publication_mirror.FAST
    late = first + timedelta(days=3)
    publication_mirror.run(runtime, now=late)
    assert row(runtime)["next_check_at"] == late + publication_mirror.SLOW
    assert not publication_mirror.due(runtime, now=late + timedelta(minutes=30))


def test_rate_limit_stops_the_run_and_keeps_the_watch_due(runtime, fakes):
    strava, _ = fakes
    publish_strava(runtime)
    strava.limited = True
    later = datetime.now(UTC) + timedelta(minutes=30)
    assert publication_mirror.run(runtime, now=later)["rate_limited"] is True
    assert row(runtime)["state"] == "active"
    assert publication_mirror.due(runtime, now=later)


def test_edited_strava_block_stops_the_watch_for_review(runtime, fakes):
    strava, garmin = fakes
    publish_strava(runtime)
    strava.text = strava.text.replace("Training load", "Changed")
    writes = len(garmin.writes)
    summary = publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert summary["conflict"] == 1 and row(runtime)["state"] == "conflict"
    assert len(garmin.writes) == writes


def test_garmin_only_publication_starts_no_watch(runtime, fakes):
    _, garmin = fakes
    item = publication.preview(runtime, "garmin:1", "garmin")
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"])
    assert garmin.text.startswith(f"Garmin words\n\n{DEFAULT_OPENER}")
    assert garmin.titles == []
    with runtime.engine.connect() as conn:
        assert conn.execute(select(mirror_table)).first() is None


def test_garmin_failure_never_undoes_the_strava_publish(runtime, fakes, monkeypatch):
    strava, garmin = fakes

    def broken(*_):
        raise OSError("garmin down")

    monkeypatch.setattr(garmin, "read", broken)
    item, result = publish_strava(runtime)
    assert result["state"] == "verified" and "garmin down" in result["mirror"]["error"]
    assert strava.text.endswith(item["owned_block"])
    assert row(runtime)["last_error"] == "garmin down"


def test_manual_strava_block_removal_clears_garmin_copy(runtime, fakes):
    strava, garmin = fakes
    item, _ = publish_strava(runtime)
    owned = item["owned_block"]
    assert garmin.text.endswith(owned)
    strava.text = "Strava words"
    summary = publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert summary["checked"] == 1
    assert garmin.text == "Strava words"
    assert publication._ledger(runtime, "strava", "2") is None
    assert row(runtime)["state"] == "removed"


def test_mirror_off_still_clears_an_earlier_garmin_copy(runtime, fakes):
    strava, garmin = fakes
    publish_strava(runtime)
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"garmin_mirror_enabled": False}}
    )
    later = datetime.now(UTC) + timedelta(minutes=30)
    assert publication_mirror.due(runtime, now=later)
    strava.text = "Strava words"
    before = len(garmin.writes)
    summary = publication_mirror.run(runtime, now=later)
    assert summary["checked"] == 1
    assert garmin.text == "Strava words"
    assert len(garmin.writes) == before + 1
    assert row(runtime)["state"] == "removed"


def test_mirror_off_does_not_refresh_a_block_that_is_still_there(runtime, fakes):
    strava, garmin = fakes
    publish_strava(runtime)
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"garmin_mirror_enabled": False}}
    )
    before = (len(strava.writes), len(garmin.writes), len(garmin.titles))
    summary = publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert summary["checked"] == 1
    assert (len(strava.writes), len(garmin.writes), len(garmin.titles)) == before
    assert row(runtime)["state"] == "active"


def test_mirror_disabled_skips_garmin_on_strava_publish(runtime, fakes):
    _strava, garmin = fakes
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"garmin_mirror_enabled": False}}
    )
    _item, result = publish_strava(runtime)
    assert result["mirror"] == {"state": "disabled"}
    assert garmin.writes == []
    group = views.resolve_activity(runtime.engine, "strava:2")
    assert publication_mirror.watch(runtime, group) is None


def test_a_run_never_re_adds_a_block_the_user_is_removing(runtime, fakes, monkeypatch):
    strava, _ = fakes
    publish_strava(runtime)
    original = publication_mirror.watch
    rows = []

    def removed_meanwhile(rt, group):
        found = original(rt, group)
        rows.append(found)
        if len(rows) == 1:  # the run's re-check inside the lock sees the removal
            return {**found, "state": "removed"}
        return found

    monkeypatch.setattr(publication_mirror, "watch", removed_meanwhile)
    strava.text = "Strava words"  # block already removed on Strava
    writes = len(strava.writes)
    summary = publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert summary["checked"] == 0 and len(strava.writes) == writes


def test_uncertain_strava_removal_keeps_the_watch_stopped(runtime, fakes, monkeypatch):
    strava, _ = fakes
    publish_strava(runtime)
    again = publication.preview(runtime, "strava:2", "strava")

    def refused(*_):
        raise publication.Conflict("synthetic")

    monkeypatch.setattr(strava, "write", refused)
    with pytest.raises(publication.Conflict):
        publication.unpublish(runtime, "strava:2", "strava", again["preview_version"])
    # The removal checkpoint exists, so the outcome is uncertain: never re-add the block.
    assert publication._ledger(runtime, "strava", "2")["state"] == "removing"
    assert row(runtime)["state"] == "removed"


def test_removal_refused_before_any_write_keeps_the_watch_active(runtime, fakes):
    publish_strava(runtime)
    with pytest.raises(publication.Conflict):
        publication.unpublish(runtime, "strava:2", "strava", "stale-version")
    assert row(runtime)["state"] == "active"


def test_transient_conflicts_retry_instead_of_ending_the_watch(runtime, fakes, monkeypatch):
    publish_strava(runtime)
    real = publication_mirror.sync

    def stale(*_args, **_kwargs):
        raise publication.Conflict("A current assessment is required before publication")

    monkeypatch.setattr(publication_mirror, "sync", stale)
    later = datetime.now(UTC) + timedelta(minutes=30)
    assert publication_mirror.run(runtime, now=later)["failed"] == 1
    assert row(runtime)["state"] == "active" and "assessment" in row(runtime)["last_error"]
    monkeypatch.setattr(publication_mirror, "sync", real)
    assert publication_mirror.run(runtime, now=later + timedelta(minutes=30))["checked"] == 1
    assert row(runtime)["last_error"] is None


def test_interrupted_garmin_mirror_write_is_verified_not_rewritten(runtime, fakes, monkeypatch):
    _, garmin = fakes
    publish_strava(runtime)
    ledger = publication.publication_ledger
    with runtime.engine.begin() as conn:
        conn.execute(ledger.update().where(ledger.c.source == "garmin").values(state="pending"))
    writes = len(garmin.writes)
    publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert len(garmin.writes) == writes
    assert publication._ledger(runtime, "garmin", "1")["state"] == "verified"


def test_legacy_block_on_strava_is_rewritten_on_mirror_sync(runtime, fakes):
    strava, garmin = fakes
    item, _ = publish_strava(runtime)
    body = item["owned_block"].split("\n", 1)[1].rsplit("\n", 1)[0]
    legacy_owned = f"{LEGACY_OPENER}\n{body}\n{LEGACY_CLOSER}"
    strava.text = f"Strava words\n\n{legacy_owned}"
    garmin.text = f"Garmin words\n\n{legacy_owned}"
    with runtime.engine.begin() as conn:
        conn.execute(
            publication_ledger.update()
            .where(
                publication_ledger.c.source == "strava",
                publication_ledger.c.source_id == "2",
            )
            .values(owned_block=legacy_owned)
        )
    summary = publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert summary["checked"] == 1
    assert strava.text.count(DEFAULT_OPENER) == 1
    assert strava.text.endswith(DEFAULT_CLOSER)
    assert garmin.text == strava.text


def test_marker_change_replaces_mirrored_blocks_without_duplicates(runtime, fakes):
    strava, garmin = fakes
    publish_strava(runtime)
    config_edit.apply_changes(
        runtime.settings.config_path,
        {"publication": {"opener": "[/* Mirror opener]", "closer": "[Mirror closer */]"}},
    )
    summary = publication_mirror.run(runtime, now=datetime.now(UTC) + timedelta(minutes=30))
    assert summary["checked"] == 1
    assert strava.text.count("[/*") == 1 and garmin.text.count("[/*") == 1
    assert "[/* Mirror opener]" in strava.text and "[/* Mirror opener]" in garmin.text


def test_republish_after_expiry_starts_a_new_seven_days(runtime, fakes):
    publish_strava(runtime)
    later = datetime.now(UTC) + timedelta(days=8)
    publication_mirror.run(runtime, now=later)
    assert row(runtime)["state"] == "expired"
    publication_mirror.start(runtime, "strava:2", "nl", now=later)
    assert row(runtime)["state"] == "active"
    assert row(runtime)["first_published_at"] == later
