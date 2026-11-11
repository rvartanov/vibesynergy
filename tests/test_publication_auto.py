"""Garmin → Strava publication without a click; every provider is synthetic."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_evidence import START, put
from test_publication import DEFAULT_OPENER, garmin_activity
from test_publication_mirror import FakeGarmin, FakeStrava

from vibesynergy.core import config_edit
from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.query import publication, publication_auto, publication_plugins
from vibesynergy.store.schema import audit_log, publication_ledger

NOW = START + timedelta(hours=1)


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


def switch_on(runtime, since=START - timedelta(hours=1), **extra):
    config_edit.apply_changes(
        runtime.settings.config_path,
        {"publication": {"auto_publish": True, "auto_publish_since": since.isoformat(), **extra}},
    )


def ledger(runtime):
    with runtime.engine.connect() as conn:
        return conn.execute(select(publication_ledger)).mappings().all()


def test_off_by_default_nothing_is_due(runtime, fakes):
    assert not publication_auto.enabled(runtime)
    assert publication_auto.candidates(runtime, now=NOW) == []
    assert publication_auto.run(runtime, now=NOW)["published"] == 0
    assert fakes[0].writes == []


def test_new_activity_is_published_on_strava_and_copied_to_garmin(runtime, fakes):
    strava, garmin = fakes
    switch_on(runtime, language="nl")
    assert publication_auto.due(runtime, now=NOW)
    summary = publication_auto.run(runtime, now=NOW)
    assert summary["published"] == 1
    assert strava.text.startswith(f"Strava words\n\n{DEFAULT_OPENER}")
    assert "Aerobe trainingseffect" in strava.text  # the chosen language
    assert garmin.text.endswith(strava.text.split("\n\n", 1)[1])
    assert {(row["source"], row["state"]) for row in ledger(runtime)} == {
        ("strava", "verified"),
        ("garmin", "verified"),
    }
    # Done once: the next run finds nothing and writes nothing.
    assert not publication_auto.due(runtime, now=NOW + timedelta(minutes=5))
    assert publication_auto.run(runtime, now=NOW + timedelta(minutes=5))["published"] == 0
    assert len(strava.writes) == 1
    with runtime.engine.connect() as conn:
        audit = conn.execute(select(audit_log).where(audit_log.c.action == "publication.auto"))
        rows = audit.mappings().all()
    assert [(r["outcome"], r["target"]) for r in rows] == [("ok", "strava:2")]
    status = publication_auto.status(runtime)
    assert status["enabled"] and status["last_published_at"] and status["needs_review"] == 0


def test_activities_from_before_the_switch_are_left_alone(runtime, fakes):
    switch_on(runtime, since=START + timedelta(minutes=1))
    assert publication_auto.candidates(runtime, now=NOW) == []
    switch_on(runtime, since=START - timedelta(days=5))
    # Never further back than two days, however early the switch went on.
    assert publication_auto.candidates(runtime, now=START + timedelta(days=3)) == []


def test_waits_until_the_activity_has_settled_and_needs_both_copies(runtime, fakes):
    switch_on(runtime)
    assert publication_auto.candidates(runtime, now=START + timedelta(minutes=5)) == []
    assert len(publication_auto.candidates(runtime, now=NOW)) == 1
    put(runtime, "garmin", replace(garmin_activity(), source_id="7", start_utc=NOW))
    later = NOW + timedelta(hours=1)
    assert [c["strava_id"] for c in publication_auto.candidates(runtime, now=later)] == ["2"]


def test_existing_vibesynergy_section_is_left_for_review_and_not_retried(runtime, fakes):
    strava, _ = fakes
    strava.text = f"Mine\n\n{DEFAULT_OPENER}\nold\n[Generated with VibeSynergy */]"
    switch_on(runtime)
    summary = publication_auto.run(runtime, now=NOW)
    assert summary["review"] == 1 and strava.writes == []
    assert publication_auto.candidates(runtime, now=NOW + timedelta(days=1)) == []
    status = publication_auto.status(runtime)
    assert status["needs_review"] == 1 and "review" in status["problem"]


def test_failures_retry_later_and_rate_limit_ends_the_run(runtime, fakes, monkeypatch):
    strava, _ = fakes
    switch_on(runtime)
    strava.limited = True
    assert publication_auto.run(runtime, now=NOW)["rate_limited"] is True
    assert publication_auto.candidates(runtime, now=NOW) == []  # waits for the retry time
    strava.limited = False
    retry = NOW + timedelta(minutes=31)
    assert publication_auto.run(runtime, now=retry)["published"] == 1

    def broken(*_args, **_kwargs):
        raise OSError("network down")

    calls = []
    monkeypatch.setattr(publication, "preview", lambda *a, **k: calls.append(1) or broken())
    put(runtime, "garmin", replace(garmin_activity(), source_id="8", start_utc=NOW))
    put(
        runtime,
        "strava",
        replace(garmin_activity(), source_id="9", name="Second", start_utc=NOW),
    )
    when = NOW + timedelta(hours=1)
    for _ in range(publication_auto.MAX_TRIES):
        publication_auto.run(runtime, now=when)
        when += timedelta(hours=3)
    assert len(calls) == publication_auto.MAX_TRIES
    assert publication_auto.candidates(runtime, now=when) == []
    assert publication_auto.attempts(runtime)["9"]["state"] == "gave_up"


def test_due_never_raises(runtime, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("bad store")

    monkeypatch.setattr(publication_auto, "candidates", boom)
    assert publication_auto.due(runtime) is False


def test_config_accepts_the_switch_and_rejects_bad_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[publication]\nauto_publish = true\nauto_publish_since = "2026-10-31T09:00:00+00:00"\n'
        'language = "nl"\n'
    )
    assert load_settings(path).publication["auto_publish"] is True
    for bad, message in (
        ('auto_publish = "yes"', "auto_publish"),
        ('auto_publish_since = "soon"', "auto_publish_since"),
        ('language = "de"', "language"),
    ):
        path.write_text(f"[publication]\n{bad}\n")
        with pytest.raises(ConfigError, match=message):
            load_settings(path)


def test_settings_switch_stamps_the_start_once(runtime, monkeypatch):
    from dataclasses import replace as dc_replace

    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.runtime import Runtime

    host = "vibesynergy.test"
    write = {"x-vibesynergy": "portal"}
    runtime.settings.config_path.write_text(
        f'[server]\npublic_url = "https://{host}"\n[portal]\nport = 8447\n'
    )
    settings = dc_replace(
        load_settings(runtime.settings.config_path), data_dir=runtime.settings.data_dir
    )
    rt = Runtime.open(settings)
    try:
        client = TestClient(create_portal_app(rt), base_url=f"https://{host}:8447")
        on = {"changes": {"publication": {"auto_publish": True, "language": "nl"}}}
        data = client.post("/api/settings", json=on, headers=write).json()["data"]
        stamped = load_settings(settings.config_path).publication["auto_publish_since"]
        assert data["publication"]["auto_publish"] is True
        assert data["publication"]["auto"]["since"] == stamped
        # Saving again while on keeps the first start, and a forged start is ignored.
        again = {
            "changes": {
                "publication": {
                    "auto_publish": True,
                    "auto_publish_since": "2020-01-01T00:00:00+00:00",
                }
            }
        }
        client.post("/api/settings", json=again, headers=write)
        assert load_settings(settings.config_path).publication["auto_publish_since"] == stamped
        off = {"changes": {"publication": {"auto_publish": False}}}
        data = client.post("/api/settings", json=off, headers=write).json()["data"]
        assert data["publication"]["auto_publish"] is False
        assert datetime.fromisoformat(stamped) <= datetime.now(UTC)
    finally:
        rt.close()
