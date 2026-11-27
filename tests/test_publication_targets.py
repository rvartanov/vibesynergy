"""Publishing to the places under "Publish to" (#175); every provider is synthetic.

VibeSynergy is the source: a publication puts its section on Strava and/or Garmin, each
through the same guarded path. The old Strava → Garmin copy with its 7-day watch is gone;
the name, the public text and the gear are the text sync's (``text_sync``) only.
"""

import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_evidence import put
from test_publication import DEFAULT_OPENER, garmin_activity

from vibesynergy.core import config_edit
from vibesynergy.core.errors import RateLimited
from vibesynergy.query import publication, publication_auto, publication_plugins, workout_page
from vibesynergy.store.schema import publication_mirror


class FakeStrava:
    def __init__(self, text="Strava words", title="Morning trail"):
        self.text, self.title = text, title
        self.reads = 0
        self.writes = []
        self.titles = []
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

    def write_title(self, source_id, title):
        self.titles.append(title)
        self.title = title


class FakeGarmin:
    def __init__(self, text="Garmin words", title="Ochtendloop"):
        self.text, self.title = text, title
        self.reads = 0
        self.writes = []
        self.titles = []

    def read(self, source_id):
        assert source_id == "1"
        self.reads += 1
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


def targets(runtime, *chosen):
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"targets": list(chosen)}}
    )


def publish_strava(runtime):
    item = publication.preview(runtime, "strava:2", "strava")
    return item, publication.publish(runtime, "strava:2", "strava", item["preview_version"])


def test_publish_puts_the_section_on_strava_and_garmin_and_keeps_their_own_texts(runtime, fakes):
    strava, garmin = fakes
    item, result = publish_strava(runtime)
    assert result["state"] == "verified"
    assert result["others"] == {"garmin": {"state": "published"}}
    assert strava.text == f"Strava words\n\n{item['owned_block']}"
    # Garmin keeps its own text; the section goes below it. No title, no Strava text.
    assert garmin.text.startswith(f"Garmin words\n\n{DEFAULT_OPENER}")
    assert garmin.titles == [] and strava.titles == []
    assert publication._ledger(runtime, "garmin", "1")["state"] == "verified"


def test_strava_only_leaves_garmin_alone(runtime, fakes):
    _strava, garmin = fakes
    targets(runtime, "strava")
    _item, result = publish_strava(runtime)
    assert "others" not in result
    assert garmin.writes == [] and garmin.text == "Garmin words"


def test_garmin_only_publishes_on_garmin_and_never_on_strava(runtime, fakes):
    strava, garmin = fakes
    targets(runtime, "garmin")
    state = workout_page.publish_state(runtime, "garmin:1")
    assert state["destination"] == "garmin" and state["destinations"] == ["garmin"]
    workout_page.set_published(runtime, "garmin:1", True, language="en")
    assert garmin.text.startswith(f"Garmin words\n\n{DEFAULT_OPENER}")
    assert strava.writes == [] and strava.text == "Strava words"
    assert workout_page.publish_state(runtime, "garmin:1")["state"] == "published"
    workout_page.set_published(runtime, "garmin:1", False, language="en")
    assert garmin.text == "Garmin words" and strava.writes == []


def test_garmin_only_new_workouts_are_published_on_garmin(runtime, fakes):
    strava, garmin = fakes
    targets(runtime, "garmin")
    start = garmin_activity().start_utc
    now = start + timedelta(hours=2)
    config_edit.apply_changes(
        runtime.settings.config_path,
        {
            "publication": {
                "auto_publish": True,
                "auto_publish_since": (start - timedelta(hours=1)).isoformat(),
            }
        },
    )
    found = publication_auto.candidates(runtime, now=now)
    assert [(c["destination"], c["source_id"]) for c in found] == [("garmin", "1")]
    summary = publication_auto.run(runtime, now=now)
    assert summary["published"] == 1
    assert garmin.text.startswith(f"Garmin words\n\n{DEFAULT_OPENER}")
    assert strava.writes == []
    assert publication_auto.attempts(runtime)["garmin:1"]["state"] == "published"
    assert publication_auto.candidates(runtime, now=now + timedelta(minutes=5)) == []


def test_a_garmin_failure_never_undoes_the_strava_publish(runtime, fakes, monkeypatch):
    strava, garmin = fakes

    def broken(*_):
        raise OSError("garmin down")

    monkeypatch.setattr(garmin, "read", broken)
    item, result = publish_strava(runtime)
    assert result["state"] == "verified"
    assert result["others"]["garmin"]["state"] == "failed"
    assert "garmin down" in result["others"]["garmin"]["error"]
    assert strava.text.endswith(item["owned_block"])


def test_a_place_added_later_gets_the_section_on_the_next_tick(runtime, fakes):
    _strava, garmin = fakes
    targets(runtime, "strava")
    workout_page.set_published(runtime, "strava:2", True, language="en")
    assert garmin.writes == []
    targets(runtime, "strava", "garmin")
    out = workout_page.set_published(runtime, "strava:2", True, language="en")
    assert out["result"]["others"] == {"garmin": {"state": "published"}}
    assert DEFAULT_OPENER in garmin.text


def test_unpublish_removes_the_section_everywhere_and_keeps_the_texts(runtime, fakes):
    strava, garmin = fakes
    publish_strava(runtime)
    again = publication.preview(runtime, "strava:2", "strava")
    result = publication.unpublish(runtime, "strava:2", "strava", again["preview_version"])
    assert result["state"] == "removed"
    assert result["others"] == {"garmin": {"state": "removed"}}
    assert strava.text == "Strava words" and garmin.text == "Garmin words"
    assert publication._ledger(runtime, "garmin", "1") is None


def test_untick_also_removes_a_place_no_longer_chosen(runtime, fakes):
    strava, garmin = fakes
    publish_strava(runtime)
    targets(runtime, "strava")
    workout_page.set_published(runtime, "strava:2", False, language="en")
    assert strava.text == "Strava words" and garmin.text == "Garmin words"


def test_an_uncertain_removal_is_left_as_it_is(runtime, fakes, monkeypatch):
    strava, garmin = fakes
    publish_strava(runtime)
    again = publication.preview(runtime, "strava:2", "strava")
    garmin_writes = len(garmin.writes)

    def refused(*_):
        raise publication.Conflict("synthetic")

    monkeypatch.setattr(strava, "write", refused)
    with pytest.raises(publication.Conflict):
        publication.unpublish(runtime, "strava:2", "strava", again["preview_version"])
    assert publication._ledger(runtime, "strava", "2")["state"] == "removing"
    assert len(garmin.writes) == garmin_writes  # nothing else moved


def test_an_interrupted_garmin_write_is_verified_not_rewritten(runtime, fakes):
    _, garmin = fakes
    publish_strava(runtime)
    ledger = publication.publication_ledger
    with runtime.engine.begin() as conn:
        conn.execute(ledger.update().where(ledger.c.source == "garmin").values(state="pending"))
    writes = len(garmin.writes)
    out = publication.publish_others(runtime, "strava:2", "strava", language="en")
    assert out == {"garmin": {"state": "unchanged"}}
    assert len(garmin.writes) == writes
    assert publication._ledger(runtime, "garmin", "1")["state"] == "verified"


def test_publish_never_drops_text_outside_the_block(runtime, fakes, monkeypatch):
    """A composed description that loses the athlete's text is refused before any write."""
    strava, _ = fakes
    item, _ = publish_strava(runtime)
    strava.text = f"Mine\n\n{item['owned_block']}"
    real = publication.preview

    def losing(*args, **kwargs):
        out = real(*args, **kwargs)
        return {**out, "description": out["owned_block"] + "\n", "already_published": False}

    monkeypatch.setattr(publication, "preview", losing)
    preview = losing(runtime, "strava:2", "strava")
    writes = len(strava.writes)
    with pytest.raises(publication.Conflict, match="outside the VibeSynergy section"):
        publication.publish(runtime, "strava:2", "strava", preview["preview_version"])
    assert len(strava.writes) == writes and strava.text.startswith("Mine\n\n")


# --- the old Strava → Garmin copy is gone (#175) --------------------------------------------


def test_the_old_copy_and_its_watch_are_gone():
    assert importlib.util.find_spec("vibesynergy.query.publication_mirror") is None


def test_publishing_starts_no_watch_and_reads_strava_only_to_publish(runtime, fakes):
    """Text sync off: nothing follows Strava afterwards, and nothing but the section goes to
    Garmin (no Strava text, no title)."""
    strava, garmin = fakes
    publish_strava(runtime)
    reads = strava.reads
    with runtime.engine.connect() as conn:
        assert conn.execute(select(publication_mirror)).first() is None
    from vibesynergy.query import text_sync

    assert text_sync.due(runtime, now=datetime.now(UTC) + timedelta(hours=3)) is False
    assert strava.reads == reads
    assert "Strava words" not in garmin.text and garmin.titles == []


def test_garmin_originals_from_the_old_copy_are_kept(runtime, fakes):
    """Garmin's own text and title from before the first copy stay stored (for a restore)."""
    now = datetime.now(UTC)
    with runtime.engine.begin() as conn:
        conn.execute(
            publication_mirror.insert().values(
                group_id="garmin:1",
                strava_id="2",
                garmin_id="1",
                language="en",
                state="active",
                first_published_at=now,
                watch_until=now,
                next_check_at=now,
                garmin_original="Garmin words before",
                garmin_original_title="Ochtendloop",
                updated_at=now,
            )
        )
    _item, _ = publish_strava(runtime)
    again = publication.preview(runtime, "strava:2", "strava")
    publication.unpublish(runtime, "strava:2", "strava", again["preview_version"])
    with runtime.engine.connect() as conn:
        kept = conn.execute(select(publication_mirror)).mappings().one()
    assert kept["garmin_original"] == "Garmin words before"
    assert kept["garmin_original_title"] == "Ochtendloop"


def test_settings_show_and_save_where_to_publish(runtime):
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/settings").json()["data"]
    assert data["publication"]["targets"] == ["strava", "garmin"]
    assert "garmin_mirror_enabled" not in data["publication"]
    assert set(data["cross_sync"]["publication"]["garmin"]) >= {"needs_review", "last_at"}
    saved = client.post(
        "/api/settings",
        json={"changes": {"publication": {"targets": ["garmin"]}}},
        headers={"x-vibesynergy": "portal"},
    ).json()["data"]
    assert saved["publication"]["targets"] == ["garmin"]
    assert publication.targets(runtime) == ["garmin"]
