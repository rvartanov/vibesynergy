"""The workout page (#114, phase 2): texts, history, Restore and the Publish tick, through
the portal API. Synthetic data; every provider is a fake, nothing leaves the test."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_evidence import START, put
from test_publication import garmin_activity
from test_publication_mirror import FakeStrava

from vibesynergy.core import config_edit
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import (
    coach_workouts,
    publication,
    publication_auto,
    publication_plugins,
    workout_page,
    workout_texts,
)
from vibesynergy.store.schema import publication_ledger

WRITE = {"x-vibesynergy": "portal"}
SECRET = "Linkerkuit iets stijf, niet aan de groep vertellen."
COACH_PRIVATE = "Rustig gehouden; let op de kuit morgen."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


def _workout(rt, *, strava=True):
    a = garmin_activity()
    put(rt, "garmin", a)
    if strava:
        put(rt, "strava", replace(a, source_id="2", name="Strava title", fit_id=None))
    return "garmin:1"


def _client(rt):
    return TestClient(create_portal_app(rt), base_url="http://localhost")


def _fake(rt, monkeypatch, fake=None):
    fake = fake or FakeStrava()
    monkeypatch.setattr(publication, "provider", lambda _rt, _dest: fake)
    config_edit.apply_changes(
        rt.settings.config_path, {"publication": {"garmin_mirror_enabled": False}}
    )
    return fake


# --- reading and writing the texts ------------------------------------------------------------


def test_the_page_reads_texts_parts_history_and_the_tick(runtime):
    group = _workout(runtime)
    coach_workouts.coach_write(
        runtime.engine,
        summary=COACH_PRIVATE,
        entry_type="activity_assessment",
        for_date=START.date(),
        activity_id=group,
        public_insight="Mooi gelijkmatig gelopen.",
    )
    data = _client(runtime).get("/api/activity-texts", params={"id": group}).json()["data"]
    assert set(data["texts"]) == {"name", "public", "private"}
    assert data["texts"]["name"]["text"] and data["history"] == []
    assert data["public"]["insight"]["text"] == "Mooi gelijkmatig gelopen."
    assert data["public"]["stats"]  # the Garmin figures, read-only
    assert [c["text"] for c in data["private"]["coach"]] == [COACH_PRIVATE]
    # The coach's private words are only in the private part.
    assert COACH_PRIVATE not in json.dumps(data["public"])
    assert data["publish"] == {
        "destination": "strava",
        "default": "only_me",  # the setting's default: what VibeSynergy did before
        "mirror": True,
        "state": "off",
        "checked": False,
    }


def test_saving_and_restoring_through_the_api_keeps_every_version(runtime):
    group = _workout(runtime)
    client = _client(runtime)
    saved = client.post(
        "/api/activity-texts", json={"id": group, "public": "Eerste"}, headers=WRITE
    ).json()["data"]
    assert saved["changed"] == ["public"] and saved["texts"]["public"]["text"] == "Eerste"
    client.post(
        "/api/activity-texts", json={"id": group, "name": "  Nieuwe   naam "}, headers=WRITE
    )
    client.post("/api/activity-texts", json={"id": group, "public": "Tweede"}, headers=WRITE)
    history = workout_texts.history(runtime.engine, group, "public")
    assert [v["text"] for v in history] == ["Tweede", "Eerste"]
    assert {v["source"] for v in history} == {"vs"}
    assert {v["author"] for v in history} == {"portal"}
    first = history[-1]["version"]
    back = client.post(
        "/api/activity-texts/restore", json={"id": group, "version": first}, headers=WRITE
    ).json()["data"]
    assert back["texts"]["public"]["text"] == "Eerste"
    assert back["history"][0]["kind"] == "restore" and back["history"][0]["restored_from"] == first
    assert back["texts"]["name"]["text"] == "Nieuwe naam"
    assert len(workout_texts.history(runtime.engine, group, "public")) == 3


def test_writes_need_the_portal_header_and_good_input(runtime):
    group = _workout(runtime)
    client = _client(runtime)
    assert client.post("/api/activity-texts", json={"id": group, "public": "x"}).status_code == 403
    bad = client.post(
        "/api/activity-texts", json={"id": group, "public": "[/*] mine */]"}, headers=WRITE
    )
    assert bad.status_code == 400 and "block markers" in bad.json()["error"]
    nothing = client.post("/api/activity-texts", json={"id": group}, headers=WRITE)
    assert nothing.status_code == 400
    extra = client.post("/api/activity-texts", json={"id": group, "x": 1}, headers=WRITE)
    assert extra.status_code == 422
    # A version of another workout is refused.
    put(runtime, "garmin", replace(garmin_activity(), source_id="9", fit_id="other",
                                   start_utc=START + timedelta(days=1)))  # fmt: skip
    other = workout_texts.set_texts(runtime.engine, "garmin:9", public="Ander")
    version = other["texts"]["public"]["version"]
    refused = client.post(
        "/api/activity-texts/restore", json={"id": group, "version": version}, headers=WRITE
    )
    assert refused.status_code == 400 and "another workout" in refused.json()["error"]


# --- privacy ------------------------------------------------------------------------------------


def test_the_private_text_stays_out_of_every_public_output(runtime, monkeypatch):
    fake = _fake(runtime, monkeypatch)
    group = _workout(runtime)
    client = _client(runtime)
    client.post(
        "/api/activity-texts",
        json={"id": group, "public": "Voor iedereen", "private": SECRET},
        headers=WRITE,
    )
    # The activity view (also what a share link shows) has no texts of its own.
    detail = client.get("/api/activity", params={"id": group}).text
    assert SECRET not in detail
    # The preview of what publishing writes, and what it then writes.
    preview = client.get(
        "/api/activity-publication-preview", params={"id": group, "destination": "strava"}
    ).text
    assert SECRET not in preview
    assert client.post("/api/activity-publish", json={"id": group, "publish": True},
                       headers=WRITE).status_code == 200  # fmt: skip
    assert fake.writes and all(SECRET not in w for w in fake.writes)
    assert workout_texts.public_texts(runtime.engine, group)["public"]["text"] == "Voor iedereen"


def test_a_share_link_cannot_read_or_write_the_texts(runtime):
    from vibesynergy.api.app import create_app
    from vibesynergy.portal import share
    from vibesynergy.runtime import Runtime

    host = "server.example.ts.net"
    settings = replace(runtime.settings, server={"public_url": f"https://{host}"})
    rt = Runtime.open(settings)
    try:
        group = _workout(rt)
        workout_texts.set_texts(rt.engine, group, private=SECRET)
        code, _ = share.create(rt.engine, "Coach Anna", ["activities"], can_write=True)
        client = TestClient(create_app(rt, with_mcp=False), base_url=f"https://{host}")
        base = f"/vibesynergy/share/{code}/api"
        shown = client.get(f"{base}/activity", params={"id": group})
        assert shown.status_code == 200 and SECRET not in shown.text
        assert client.get(f"{base}/activity-texts", params={"id": group}).status_code == 403
        write = {"x-vibesynergy": "portal", "origin": f"https://{host}"}
        for path, body in (
            ("activity-texts", {"id": group, "private": "x"}),
            ("activity-texts/restore", {"id": group, "version": 1}),
            ("activity-publish", {"id": group, "publish": True}),
        ):
            assert client.post(f"{base}/{path}", json=body, headers=write).status_code == 403
    finally:
        rt.close()


# --- the Publish tick --------------------------------------------------------------------------


def test_ticking_publishes_and_unticking_removes_the_section(runtime, monkeypatch):
    fake = _fake(runtime, monkeypatch)
    group = _workout(runtime)
    client = _client(runtime)
    on = client.post(
        "/api/activity-publish", json={"id": group, "publish": True},
        headers=WRITE,
    ).json()["data"]  # fmt: skip
    assert on["result"]["state"] == "verified"
    assert on["publish"]["state"] == "published" and on["publish"]["checked"] is True
    assert fake.text.startswith("Strava words\n\n") and "[/*]" in fake.text
    # Ticking again changes nothing.
    again = client.post("/api/activity-publish", json={"id": group, "publish": True},
                        headers=WRITE).json()["data"]  # fmt: skip
    assert again["result"]["state"] == "verified" and len(fake.writes) == 1
    off = client.post("/api/activity-publish", json={"id": group, "publish": False},
                      headers=WRITE).json()["data"]  # fmt: skip
    assert off["result"]["state"] == "removed" and off["publish"]["state"] == "off"
    assert fake.text == "Strava words"  # the athlete's own text stays
    with runtime.engine.connect() as conn:
        assert conn.execute(publication_ledger.select()).first() is None


def test_without_a_strava_copy_the_tick_publishes_to_garmin(runtime, monkeypatch):
    from test_publication import FakeProvider

    fake = FakeProvider()
    monkeypatch.setattr(publication, "provider", lambda _rt, dest: fake)
    group = _workout(runtime, strava=False)
    state = workout_page.publish_state(runtime, group)
    assert state["destination"] == "garmin" and state["mirror"] is False
    done = workout_page.set_published(runtime, group, True)
    assert done["publish"]["state"] == "published" and fake.writes


def test_the_tick_says_why_it_cannot_publish(runtime, monkeypatch):
    _fake(runtime, monkeypatch)
    # No figures at all: a bare Strava copy without Garmin.
    from test_evidence import activity

    put(runtime, "strava", replace(activity(), source_id="2", fit_id=None, avg_hr=None,
                                   distance_m=None, moving_s=None, elapsed_s=None))  # fmt: skip
    group = "strava:2"
    reply = _client(runtime).post("/api/activity-publish", json={"id": group, "publish": True},
                                  headers=WRITE)  # fmt: skip
    assert reply.status_code == 409, reply.text
    assert reply.json()["code"] == "no_figures"


def test_new_workouts_public_ticks_by_itself_and_unticking_keeps_it_private(runtime, monkeypatch):
    _fake(runtime, monkeypatch)
    group = _workout(runtime)
    since = (START - timedelta(hours=1)).isoformat()
    config_edit.apply_changes(
        runtime.settings.config_path,
        {"publication": {"auto_publish": True, "auto_publish_since": since}},
    )
    soon = START + timedelta(hours=1)
    state = workout_page.publish_state(runtime, group, now=soon)
    assert (state["default"], state["state"], state["checked"]) == ("public", "automatic", True)
    assert [c["strava_id"] for c in publication_auto.candidates(runtime, now=soon)] == ["2"]
    workout_page.set_published(runtime, group, False)
    state = workout_page.publish_state(runtime, group, now=soon)
    assert (state["state"], state["checked"]) == ("off", False)
    # The automatic run leaves it alone from now on.
    assert publication_auto.candidates(runtime, now=soon) == []
    assert publication_auto.status(runtime)["needs_review"] == 0
    # Ticking it after all publishes it.
    assert workout_page.set_published(runtime, group, True)["publish"]["state"] == "published"


def test_the_setting_for_new_workouts_defaults_to_only_me(runtime):
    client = _client(runtime)
    data = client.get("/api/settings").json()["data"]
    assert data["publication"]["auto_publish"] is False  # only me, as before this change
    group = _workout(runtime)
    assert workout_page.publish_state(runtime, group)["default"] == "only_me"
