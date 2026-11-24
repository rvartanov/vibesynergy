"""Two-way sync of a published workout's name and public text (#114, phase 3).

Every platform is a synthetic fake: nothing here reaches Strava or Garmin."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_evidence import put
from test_publication import garmin_activity

from vibesynergy.core import config_edit
from vibesynergy.core.errors import RateLimited
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import (
    publication,
    publication_plugins,
    text_sync,
    workout_page,
    workout_texts,
)
from vibesynergy.store.schema import text_sync as text_sync_table

SECRET = "Linkerkuit stijf, niet aan de groep vertellen."
WRITE = {"x-vibesynergy": "portal"}


class Fake:
    """One platform's copy: its name and description, every write kept."""

    def __init__(self, platform: str, source_id: str, name: str, text: str):
        self.platform, self.source_id = platform, source_id
        self.name, self.text = name, text
        self.reads = 0
        self.writes: list[str] = []
        self.titles: list[str] = []
        self.limited = False
        self.on_read = None  # called before a read answers: someone types meanwhile

    def read(self, source_id):
        assert source_id == self.source_id
        if self.limited:
            raise RateLimited("synthetic budget")
        self.reads += 1
        if self.on_read:
            self.on_read(self)
        if self.platform == "strava":
            return {"id": int(self.source_id), "name": self.name, "description": self.text}
        return {
            "activityId": int(self.source_id),
            "activityName": self.name,
            "description": self.text,
        }

    def write(self, source_id, description):
        assert source_id == self.source_id
        self.writes.append(description)
        self.text = description

    def write_title(self, source_id, title):
        assert source_id == self.source_id
        self.titles.append(title)
        self.name = title

    def sent(self) -> str:
        return json.dumps([self.writes, self.titles])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


def _setting(rt, on: bool = True, **more):
    config_edit.apply_changes(
        rt.settings.config_path, {"publication": {"text_sync_enabled": on or None, **more}}
    )


@pytest.fixture
def fakes(runtime, monkeypatch):
    a = garmin_activity()
    put(runtime, "garmin", replace(a, name="Amsterdam Hardlopen"))
    put(runtime, "strava", replace(a, source_id="2", name="Morning trail", fit_id=None))
    strava = Fake("strava", "2", "Morning trail", "Strava words")
    garmin = Fake("garmin", "1", "Amsterdam Hardlopen", "")
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )
    return strava, garmin


def _publish(rt):
    item = publication.preview(rt, "strava:2", "strava")
    return item, publication.publish(rt, "strava:2", "strava", item["preview_version"])


def _row(rt):
    with rt.engine.connect() as conn:
        return conn.execute(select(text_sync_table)).mappings().one()


def _later(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _texts(rt):
    found = workout_texts.texts(rt.engine, "garmin:1")
    return found["name"]["text"], found["public"]["text"]


def _counts(*fakes):
    return [(len(f.writes), len(f.titles)) for f in fakes]


@pytest.fixture
def synced(runtime, fakes):
    """Sync on, published: the first sync has run."""
    _setting(runtime)
    item, result = _publish(runtime)
    return item["owned_block"], result


# --- off by default ------------------------------------------------------------------------


def test_off_by_default_nothing_is_synced(runtime, fakes):
    strava, garmin = fakes
    assert text_sync.enabled(runtime) is False
    item, result = _publish(runtime)
    reads = strava.reads
    assert "texts" not in result
    # Only VibeSynergy's section goes to Garmin (#175): no Strava text, no Strava name.
    assert garmin.text == item["owned_block"]
    assert garmin.name == "Amsterdam Hardlopen"
    before = _counts(strava, garmin)
    workout_texts.set_texts(runtime.engine, "garmin:1", name="Nieuw", public="Andere tekst")
    assert text_sync.due(runtime, now=_later(60)) is False
    assert text_sync.run(runtime, now=_later(60))["state"] == "off"
    assert _counts(strava, garmin) == before
    assert "Andere tekst" not in strava.sent() + garmin.sent()
    # Nothing follows Strava afterwards: the old copy's watch is gone (#175).
    assert strava.reads == reads


def test_the_setting_is_saved_and_off_is_the_default(runtime):
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/settings").json()["data"]
    assert data["publication"]["text_sync_enabled"] is False
    saved = client.post(
        "/api/settings",
        json={"changes": {"publication": {"text_sync_enabled": True}}},
        headers=WRITE,
    ).json()["data"]
    assert saved["publication"]["text_sync_enabled"] is True
    assert text_sync.enabled(runtime) is True
    client.post(
        "/api/settings",
        json={"changes": {"publication": {"text_sync_enabled": False}}},
        headers=WRITE,
    )
    assert "text_sync_enabled" not in runtime.settings.config_path.read_text()


# --- the first sync ------------------------------------------------------------------------


def test_publishing_merges_both_texts_and_pushes_name_and_text(runtime, fakes):
    strava, garmin = fakes
    garmin.text = "Garmin words"
    _setting(runtime)
    workout_texts.set_texts(runtime.engine, "garmin:1", private=SECRET)
    item, result = _publish(runtime)
    owned = item["owned_block"]
    assert result["texts"]["state"] == "checked"
    # Nothing lost, nothing doubled: VS's text holds both, every platform gets it + block.
    assert _texts(runtime) == ("Morning trail", "Strava words\n\nGarmin words")
    assert strava.text == f"Strava words\n\nGarmin words\n\n{owned}"
    assert garmin.text == f"Strava words\n\nGarmin words\n\n{owned}"
    assert strava.text.count(owned) == 1 and garmin.text.count(owned) == 1
    assert garmin.name == "Morning trail"  # the Strava name
    assert SECRET not in strava.sent() + garmin.sent()
    history = workout_texts.history(runtime.engine, "garmin:1", "public")
    assert history[0]["source"] == "strava" and history[0]["author"] == "sync"


def test_a_name_given_in_vibesynergy_wins_the_first_sync(runtime, fakes):
    strava, garmin = fakes
    _setting(runtime)
    workout_texts.set_texts(runtime.engine, "garmin:1", name="Duinloop")
    _publish(runtime)
    assert strava.name == "Duinloop" and garmin.name == "Duinloop"


# --- three-way: no change, VS change, outside changes ---------------------------------------


def test_no_change_writes_nothing(runtime, fakes, synced):
    strava, garmin = fakes
    before = _counts(strava, garmin)
    summary = text_sync.run(runtime, now=_later(3 * 60))
    assert summary["checked"] == 1 and summary["written"] == 0
    assert _counts(strava, garmin) == before


def test_a_change_in_vibesynergy_goes_out_after_the_wait_and_keeps_the_block(
    runtime, fakes, synced
):
    strava, garmin = fakes
    owned, _ = synced
    workout_texts.set_texts(
        runtime.engine, "garmin:1", name="Duinloop", public="Zwaar maar mooi", private=SECRET
    )
    before = _counts(strava, garmin)
    assert text_sync.run(runtime, now=_later(1))["checked"] == 0  # still in the wait
    assert _counts(strava, garmin) == before
    assert text_sync.due(runtime, now=_later(6)) is True
    summary = text_sync.run(runtime, now=_later(6))
    assert summary["written"] == 2
    assert strava.text == f"Zwaar maar mooi\n\n{owned}" and strava.name == "Duinloop"
    assert garmin.text == f"Zwaar maar mooi\n\n{owned}" and garmin.name == "Duinloop"
    assert SECRET not in strava.sent() + garmin.sent()
    # No echo: VibeSynergy's own write does not come back as an outside change.
    again = _counts(strava, garmin)
    text_sync.run(runtime, now=_later(4 * 60))
    assert _counts(strava, garmin) == again
    assert _texts(runtime) == ("Duinloop", "Zwaar maar mooi")


def test_an_edit_on_strava_is_taken_in_and_passed_to_garmin(runtime, fakes, synced):
    strava, garmin = fakes
    owned, _ = synced
    strava.text = f"Edited on Strava\n\n{owned}\n\nAdded below"
    strava.name = "Renamed on Strava"
    strava_writes = len(strava.writes)
    text_sync.run(runtime, now=_later(3 * 60))
    assert _texts(runtime) == ("Renamed on Strava", "Edited on Strava\n\nAdded below")
    assert garmin.text == f"Edited on Strava\n\nAdded below\n\n{owned}"
    assert garmin.name == "Renamed on Strava"
    # Strava is not written: it holds the winning text already, as typed.
    assert len(strava.writes) == strava_writes
    assert strava.text == f"Edited on Strava\n\n{owned}\n\nAdded below"
    history = workout_texts.history(runtime.engine, "garmin:1", "name")
    assert history[0]["source"] == "strava" and history[0]["kind"] == "edit"
    # And no echo afterwards.
    counts = _counts(strava, garmin)
    text_sync.run(runtime, now=_later(10 * 60))
    assert _counts(strava, garmin) == counts


def test_an_edit_on_garmin_seen_by_the_normal_sync_reaches_strava(runtime, fakes, synced):
    strava, garmin = fakes
    owned, _ = synced
    garmin.text = f"Typed on Garmin\n\n{owned}"
    garmin.name = "Garmin rename"
    # The normal sync (Garmin activity details) stores the changed copy.
    put(
        runtime,
        "garmin",
        replace(garmin_activity(), name="Garmin rename", extra={"description": garmin.text}),
    )
    now = _later(10)  # after the first periodic check would be far away
    _update_next(runtime, _later(24 * 60))
    assert text_sync.due(runtime, now=now) is True
    text_sync.run(runtime, now=now)  # noticed: checked after the wait
    assert strava.name == "Morning trail"
    text_sync.run(runtime, now=now + text_sync.DEBOUNCE + timedelta(seconds=1))
    assert strava.name == "Garmin rename"
    assert strava.text == f"Typed on Garmin\n\n{owned}"
    assert _texts(runtime) == ("Garmin rename", "Typed on Garmin")
    assert workout_texts.history(runtime.engine, "garmin:1", "public")[0]["source"] == "garmin"


def _update_next(rt, when):
    with rt.engine.begin() as conn:
        conn.execute(text_sync_table.update().values(next_check_at=when))


# --- both changed: the latest wins, the other stays flagged ---------------------------------


def test_both_changed_the_later_outside_change_wins_and_vs_is_flagged(runtime, fakes, synced):
    strava, garmin = fakes
    owned, _ = synced
    workout_texts.set_texts(runtime.engine, "garmin:1", public="Mijn versie in VS")
    strava.text = f"Later on Strava\n\n{owned}"  # first seen by the next read: later
    text_sync.run(runtime, now=_later(6))
    assert _texts(runtime)[1] == "Later on Strava"
    assert garmin.text == f"Later on Strava\n\n{owned}"
    history = workout_texts.history(runtime.engine, "garmin:1", "public")
    lost = [v for v in history if v["kind"] == workout_texts.OVERWRITTEN]
    assert [(v["text"], v["source"]) for v in lost] == [("Mijn versie in VS", "vs")]


def test_both_changed_a_later_vs_edit_wins_and_the_platform_text_is_flagged(runtime, fakes, synced):
    strava, garmin = fakes
    owned, _ = synced
    strava.text = f"Eerder op Strava\n\n{owned}"
    put(
        runtime,
        "strava",
        replace(
            garmin_activity(),
            source_id="2",
            name="Morning trail",
            fit_id=None,
            extra={"description": strava.text},
        ),
    )
    _update_next(runtime, _later(24 * 60))
    text_sync.run(runtime, now=_later(0.5))  # noticed now, before the VS edit
    workout_texts.set_texts(runtime.engine, "garmin:1", public="Later in VS")
    text_sync.run(runtime, now=_later(6))
    assert _texts(runtime)[1] == "Later in VS"
    assert strava.text == f"Later in VS\n\n{owned}" and garmin.text == f"Later in VS\n\n{owned}"
    history = workout_texts.history(runtime.engine, "garmin:1", "public")
    assert history[0]["kind"] == workout_texts.OVERWRITTEN
    assert (history[0]["text"], history[0]["source"]) == ("Eerder op Strava", "strava")
    # The flagged version is history, not the current text; it can be restored.
    assert workout_texts.texts(runtime.engine, "garmin:1")["public"]["text"] == "Later in VS"
    workout_texts.restore(runtime.engine, history[0]["version"])
    assert _texts(runtime)[1] == "Eerder op Strava"


def test_strava_and_garmin_both_edited_the_tie_goes_to_strava(runtime, fakes, synced):
    strava, garmin = fakes
    owned, _ = synced
    strava.text = f"Op Strava\n\n{owned}"
    garmin.text = f"Op Garmin\n\n{owned}"
    text_sync.run(runtime, now=_later(3 * 60))
    assert _texts(runtime)[1] == "Op Strava"
    assert garmin.text == f"Op Strava\n\n{owned}"
    lost = [
        (v["text"], v["source"])
        for v in workout_texts.history(runtime.engine, "garmin:1", "public")
        if v["kind"] == workout_texts.OVERWRITTEN
    ]
    assert lost == [("Op Garmin", "garmin")]


# --- the #161 guard ------------------------------------------------------------------------


def test_a_change_between_read_and_write_is_never_overwritten(runtime, fakes, synced):
    strava, garmin = fakes
    owned, _ = synced
    workout_texts.set_texts(runtime.engine, "garmin:1", public="Uit VS")
    reads = {"n": 0}

    def typing(fake):
        reads["n"] += 1
        if reads["n"] == 2:  # right after the decision read, before the write
            fake.text = f"Net getypt op Garmin\n\n{owned}"

    garmin.on_read = typing
    text_sync.run(runtime, now=_later(6))
    # The write based on the old read was refused; the check ran again and took it in.
    assert "Uit VS" not in json.dumps(garmin.writes)
    assert _texts(runtime)[1] == "Net getypt op Garmin"
    assert strava.text == f"Net getypt op Garmin\n\n{owned}"


def test_a_platform_that_keeps_changing_gets_no_write_and_is_tried_later(runtime, fakes, synced):
    _, garmin = fakes
    workout_texts.set_texts(runtime.engine, "garmin:1", public="Uit VS")
    count = {"n": 0}

    def always(fake):
        count["n"] += 1
        fake.name = f"Typing {count['n']}"

    garmin.on_read = always
    before = _counts(garmin)
    now = _later(6)
    summary = text_sync.run(runtime, now=now)
    assert summary["failed"] == 1
    assert _counts(garmin) == before
    assert _row(runtime)["next_check_at"] >= now + text_sync.DEBOUNCE - timedelta(seconds=1)


def test_a_hand_edited_block_is_left_for_review(runtime, fakes, synced):
    strava, garmin = fakes
    owned, _ = synced
    lines = owned.splitlines()
    edited = "\n".join([lines[0], "-changed by hand", *lines[2:]])
    strava.text = f"Strava words\n\n{edited}"
    before = _counts(strava, garmin)
    summary = text_sync.run(runtime, now=_later(3 * 60))
    assert summary["review"] == 1
    assert _row(runtime)["state"] == "conflict"
    assert _counts(strava, garmin) == before


# --- privacy, rate limits, one sync path, visibility ------------------------------------------


def test_the_private_text_never_goes_out(runtime, fakes, synced):
    strava, garmin = fakes
    workout_texts.set_texts(runtime.engine, "garmin:1", public="Openbaar", private=SECRET)
    text_sync.run(runtime, now=_later(6))
    assert "Openbaar" in strava.text
    assert SECRET not in strava.sent() + garmin.sent()


def test_strava_rate_limit_ends_the_run_and_waits(runtime, fakes, synced):
    strava, garmin = fakes
    workout_texts.set_texts(runtime.engine, "garmin:1", public="Nieuw")
    strava.limited = True
    before = _counts(strava, garmin)
    now = _later(6)
    summary = text_sync.run(runtime, now=now)
    assert summary["rate_limited"] is True and summary["checked"] == 0
    assert _counts(strava, garmin) == before
    assert _row(runtime)["next_check_at"] > now
    strava.limited = False
    text_sync.run(runtime, now=now + text_sync.RETRY)
    assert strava.text.startswith("Nieuw")


def test_periodic_reads_stay_within_the_strava_budget(runtime, fakes, synced):
    strava, _ = fakes
    reads = strava.reads
    now = datetime.now(UTC)
    # Every 5 minutes for the 7-day watch: the scheduler's tick.
    for minute in range(5, 7 * 24 * 60 + 60, 5):
        if text_sync.due(runtime, now=now + timedelta(minutes=minute)):
            text_sync.run(runtime, now=now + timedelta(minutes=minute))
    # 2-hourly for 48 hours, then 6-hourly: a few reads a day, far under 1000 a day.
    assert strava.reads - reads <= 24 + 20 + 2
    assert text_sync.due(runtime, now=now + timedelta(days=8)) is False


def test_the_text_sync_is_the_only_path_that_follows_a_garmin_edit(runtime, fakes, synced):
    """#175: the old copy no longer reads Strava or writes Garmin next to the text sync."""
    strava, garmin = fakes
    owned, _ = synced
    garmin.text = f"Alleen op Garmin getypt\n\n{owned}"
    garmin.name = "Garmin naam"
    # The text sync takes the Garmin edit in and passes it to Strava.
    text_sync.run(runtime, now=_later(3 * 60))
    assert strava.name == "Garmin naam"
    assert strava.text == f"Alleen op Garmin getypt\n\n{owned}"


def test_unpublishing_stops_the_sync(runtime, fakes, synced):
    strava, garmin = fakes
    again = publication.preview(runtime, "strava:2", "strava")
    publication.unpublish(runtime, "strava:2", "strava", again["preview_version"])
    workout_texts.set_texts(runtime.engine, "garmin:1", public="Niet meer verstuurd")
    before = _counts(strava, garmin)
    text_sync.run(runtime, now=_later(6))
    assert _row(runtime)["state"] == "stopped"
    assert _counts(strava, garmin) == before


def test_strava_updates_never_send_visibility(tmp_path, monkeypatch):
    from vibesynergy.core.secrets import SecretStore
    from vibesynergy.sources import oauth
    from vibesynergy.sources.strava.client import Response, StravaClient

    store = SecretStore(tmp_path, "strava")
    store.set(oauth.TOKENS, {"access_token": "t", "expires_at": 10000, "scope": "activity:write"})
    monkeypatch.setattr(oauth, "needs_refresh", lambda *_: False)
    sent = []

    def http(method, url, params, headers):
        sent.append((method, params))
        return Response(200, {}, {})

    client = StravaClient(store, http=http, clock=lambda: 1000)
    client.update_activity("5", {"name": "A", "description": "B"})
    assert sent == [("PUT", {"name": "A", "description": "B"})]
    with pytest.raises(ValueError):
        client.update_activity("5", {"visibility": "everyone"})


def test_the_workout_page_says_what_is_sent(runtime, fakes):
    _publish(runtime)
    state = workout_page.publish_state(runtime, "garmin:1")
    assert state["texts_synced"] is False and state["strava_private"] is False
    _setting(runtime)
    put(
        runtime,
        "strava",
        replace(
            garmin_activity(), source_id="2", name="Morning trail", fit_id=None,
            extra={"private": True},
        ),
    )  # fmt: skip
    state = workout_page.publish_state(runtime, "garmin:1")
    assert state["texts_synced"] is True and state["strava_private"] is True


def test_a_multisport_holder_is_synced_with_garmin_only(runtime):
    a = garmin_activity()
    put(runtime, "garmin", replace(a, sport="multi_sport"))
    put(runtime, "strava", replace(a, source_id="2", name="Leg", fit_id=None))
    group = workout_texts.texts(runtime.engine, "garmin:1")["activity_id"]
    assert text_sync.targets(runtime, group) == {"garmin": "1"}


def test_a_copy_that_joins_later_gets_the_agreed_name(runtime, fakes, synced):
    strava, garmin = fakes
    owned, _ = synced
    workout_texts.set_texts(runtime.engine, "garmin:1", name="Duinloop")
    text_sync.run(runtime, now=_later(6))
    assert strava.name == "Duinloop" and garmin.name == "Duinloop"
    # Garmin as a copy never synced before (it joined the workout later), with its own name.
    row = _row(runtime)
    synced_now = {k: v for k, v in row["synced"].items() if k != "garmin"}
    with runtime.engine.begin() as conn:
        conn.execute(text_sync_table.update().values(synced=synced_now))
    garmin.name = "Amsterdam Hardlopen"
    garmin.text = f"Eigen Garmin-tekst\n\n{owned}"
    text_sync.run(runtime, now=_later(3 * 60))
    assert garmin.name == "Duinloop" and strava.name == "Duinloop"
    # Its own text is added, not dropped, and reaches Strava too.
    assert _texts(runtime)[1] == "Strava words\n\nEigen Garmin-tekst"
    assert strava.text == f"Strava words\n\nEigen Garmin-tekst\n\n{owned}"
