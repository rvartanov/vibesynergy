"""intervals.icu key in the portal: stored encrypted, write-only, env moved once."""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vibesynergy.core.config import load_settings
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.sources.intervals_icu import credentials
from vibesynergy.sources.intervals_icu.client import (
    IntervalsIcuClient,
    IntervalsIcuError,
    account_label,
)

KEY = "icu-secret-key-7c1d"
ATHLETE = "i000002"
TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}


@pytest.fixture(autouse=True)
def _no_icu_env(monkeypatch):
    monkeypatch.delenv(credentials.API_KEY_ENV, raising=False)
    monkeypatch.delenv(credentials.ATHLETE_ID_ENV, raising=False)
    credentials._warned.clear()


def test_saved_key_is_encrypted_and_used_by_the_client(settings) -> None:
    assert credentials.load() is None
    assert account_label() is None
    with pytest.raises(IntervalsIcuError, match="Settings → Sources"):
        IntervalsIcuClient.from_env()
    credentials.save(settings.secrets_dir, f"  {KEY} ", ATHLETE)
    files = [
        p for p in (settings.secrets_dir / "intervals_icu").glob("api.*") if p.suffix != ".lock"
    ]
    assert [path.suffix for path in files] == [".enc"]
    assert KEY.encode() not in files[0].read_bytes()
    assert account_label() == ATHLETE
    client = IntervalsIcuClient.from_env(interval=0)
    assert client._api_key == KEY and client._athlete_id == ATHLETE
    assert KEY not in repr(credentials.load())
    # A new athlete id with an empty key field keeps the stored key.
    credentials.save(settings.secrets_dir, "", "i000003")
    assert credentials.load().api_key == KEY
    with pytest.raises(ValueError, match="athlete id"):
        credentials.save(settings.secrets_dir, KEY, "not valid!")
    credentials.clear(settings.secrets_dir)
    assert credentials.load() is None
    with pytest.raises(ValueError, match="API key"):
        credentials.save(settings.secrets_dir, "", ATHLETE)


def test_environment_is_moved_once_and_then_ignored(settings, monkeypatch, caplog) -> None:
    monkeypatch.setenv(credentials.API_KEY_ENV, KEY)
    monkeypatch.setenv(credentials.ATHLETE_ID_ENV, ATHLETE)
    with caplog.at_level(logging.WARNING, logger=credentials.__name__):
        legacy = credentials.load()
        assert legacy.origin == "environment"
        assert credentials.migrate_from_env(settings.secrets_dir) is True
        assert credentials.migrate_from_env(settings.secrets_dir) is False  # once
    assert "deprecated" in caplog.text and "copied" in caplog.text
    assert KEY not in caplog.text
    moved = credentials.load()
    assert moved.origin == "store" and moved.api_key == KEY
    # The store wins over a different value still in the environment.
    monkeypatch.setenv(credentials.API_KEY_ENV, "other-key")
    assert credentials.load().api_key == KEY


def test_portal_card_saves_the_key_and_never_returns_it(env) -> None:
    env.write_text(env.read_text() + "\n[sources.intervals_icu]\nenabled = true\n")
    settings = replace(load_settings(), server={"public_url": f"https://{TAILNET}"})
    runtime = Runtime.open(settings)
    runtime.migrate()
    try:
        client = TestClient(create_portal_app(runtime), base_url=f"https://{TAILNET}:8447")
        assert client.get("/api/sources/intervals_icu/credentials").json()["data"] == {
            "set": False,
            "athlete_id": None,
            "origin": None,
        }
        bad = client.post(
            "/api/sources/intervals_icu/credentials",
            json={"athlete_id": ATHLETE, "api_key": ""},
            headers=WRITE,
        )
        assert bad.status_code == 400
        assert "Enter your intervals.icu API key." in bad.text
        saved = client.post(
            "/api/sources/intervals_icu/credentials",
            json={"athlete_id": ATHLETE, "api_key": KEY},
            headers=WRITE,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"] == {"set": True, "athlete_id": ATHLETE, "origin": "store"}
        page = client.get("/api/settings").json()["data"]
        row = next(source for source in page["sources"] if source["id"] == "intervals_icu")
        assert row["login"]["state"] == "logged in"
        assert row["login"]["account"] == ATHLETE
        assert row["login"]["origin"] == "store"
        for body in (saved.text, json.dumps(page)):
            assert KEY not in body
        off = client.post("/api/sources/intervals_icu/disconnect", headers=WRITE)
        assert off.status_code == 200
        assert credentials.load() is None
    finally:
        runtime.close()
