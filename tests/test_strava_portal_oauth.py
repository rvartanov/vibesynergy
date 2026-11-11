"""Portal Strava publishing OAuth: redirect back to Sources without CLI paste-back."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from strava_fake import FakeStravaAPI

from vibesynergy.api.app import create_app
from vibesynergy.core.secrets import SecretStore
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import publication
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth
from vibesynergy.sources.strava import client as client_module
from vibesynergy.sources.strava.client import StravaClient

PORTAL = "http://localhost"
T0 = 1_700_000_000.0


@pytest.fixture
def strava_oauth(runtime, monkeypatch):
    settings = replace(
        runtime.settings,
        sources={"strava": {"enabled": True}},
        server={"public_url": "https://server.example.ts.net"},
    )
    rt = Runtime.open(settings)
    store = SecretStore(rt.settings.secrets_dir, "strava", encrypted=True)
    store.set(oauth.APP, {"client_id": "12345", "client_secret": "ssecret"})
    fake = FakeStravaAPI()

    class _TestStravaClient(StravaClient):
        def __init__(self, saved, **kw):
            super().__init__(saved, http=fake, clock=lambda: T0 + 3600, **kw)

    monkeypatch.setattr(client_module, "StravaClient", _TestStravaClient)
    monkeypatch.setattr("vibesynergy.api.app.time.time", lambda: T0)
    monkeypatch.setattr("vibesynergy.sources.oauth.time.time", lambda: T0)
    portal = TestClient(create_portal_app(rt), base_url=PORTAL)
    public = TestClient(create_app(rt, with_mcp=False), base_url="https://server.example.ts.net")
    yield rt, store, fake, portal, public
    rt.close()


def _connect(portal: TestClient) -> tuple[str, str]:
    start = portal.post(
        "/api/sources/strava/connect",
        json={"portal": PORTAL + "/"},
        headers={"x-vibesynergy": "portal"},
    )
    assert start.status_code == 200
    query = parse_qs(urlsplit(start.json()["data"]["url"]).query)
    assert "activity:write" in query["scope"][0]
    return query["state"][0], start.json()["data"]["url"]


def test_strava_portal_oauth_success(strava_oauth) -> None:
    rt, store, fake, portal, public = strava_oauth
    state, _ = _connect(portal)
    store.delete(oauth.TOKENS)
    back = public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": "synthetic", "scope": "read,activity:write"},
        follow_redirects=False,
    )
    assert back.status_code == 303
    location = back.headers["location"]
    assert location == f"{PORTAL}/#/settings/sources?strava=connected"
    assert "code=" not in location and "state=" not in location and "token" not in location.lower()
    assert store.get(oauth.TOKENS)["scope"] == "read,activity:write"
    assert fake.token_requests[-1]["code"] == "synthetic"
    assert (rt.settings.secrets_dir / "strava/tokens.enc").exists()
    assert not (rt.settings.secrets_dir / "strava/tokens.json").exists()
    assert publication.provider(rt, "strava").client._store.get(oauth.TOKENS)


def test_strava_portal_unknown_state_no_cli(strava_oauth) -> None:
    _rt, _store, _fake, _portal, public = strava_oauth
    state = "A" * 43
    page = public.get("/vibesynergy/strava/callback", params={"state": state, "code": "x"})
    assert "vibesynergy auth" not in page.text.lower()
    assert "expired or already used" in page.text


def test_strava_portal_reused_state(strava_oauth) -> None:
    _rt, _store, _fake, portal, public = strava_oauth
    state, _ = _connect(portal)
    first = public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": "one", "scope": "read,activity:write"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    second = public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": "two", "scope": "read,activity:write"},
    )
    assert "expired or already used" in second.text


def test_strava_portal_expired(strava_oauth, monkeypatch) -> None:
    _rt, _store, _fake, portal, public = strava_oauth
    state, _ = _connect(portal)
    late = T0 + oauth.PUBLICATION_OAUTH_TTL_S + 1
    monkeypatch.setattr("vibesynergy.api.app.time.time", lambda: late)
    back = public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": "late", "scope": "read,activity:write"},
        follow_redirects=False,
    )
    assert back.status_code == 303
    assert back.headers["location"].endswith("strava=expired")


def test_strava_portal_access_denied(strava_oauth) -> None:
    _rt, store, fake, portal, public = strava_oauth
    state, _ = _connect(portal)
    store.delete(oauth.TOKENS)
    back = public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    assert back.status_code == 303 and back.headers["location"].endswith("strava=denied")
    assert store.get(oauth.TOKENS) is None
    assert not fake.token_requests


def test_strava_portal_write_missing(strava_oauth) -> None:
    _rt, store, fake, portal, public = strava_oauth
    state, _ = _connect(portal)
    store.delete(oauth.TOKENS)
    back = public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": "synthetic", "scope": "read"},
        follow_redirects=False,
    )
    assert back.status_code == 303 and back.headers["location"].endswith("strava=write_missing")
    assert store.get(oauth.TOKENS) is None
    assert not fake.token_requests


def test_strava_portal_exchange_failure_keeps_tokens(strava_oauth, monkeypatch) -> None:
    _rt, store, _fake, portal, public = strava_oauth
    oauth.save_tokens(
        store,
        {
            "access_token": "keep",
            "refresh_token": "keep-r",
            "expires_at": int(T0) + 3600,
            "scope": "read,activity:write",
        },
    )

    def boom(*_a, **_k):
        raise oauth.AuthError("synthetic failure")

    monkeypatch.setattr(client_module.StravaClient, "exchange_code", boom)
    state, _ = _connect(portal)
    back = public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": "bad", "scope": "read,activity:write"},
        follow_redirects=False,
    )
    assert back.status_code == 303 and back.headers["location"].endswith("strava=failed")
    assert store.get(oauth.TOKENS)["access_token"] == "keep"


def test_strava_connect_rejects_foreign_portal(strava_oauth) -> None:
    _rt, _store, _fake, portal, _public = strava_oauth
    refused = portal.post(
        "/api/sources/strava/connect",
        json={"portal": "https://evil.example/"},
        headers={"x-vibesynergy": "portal"},
    )
    assert refused.status_code == 400
    assert "not allowed" in refused.json()["error"].lower()


def test_pending_state_stored_hashed(strava_oauth) -> None:
    rt, _store, _fake, portal, _public = strava_oauth
    state, _ = _connect(portal)
    pending_store = SecretStore(rt.settings.secrets_dir, oauth.PUBLICATION_OAUTH, encrypted=True)
    assert pending_store.get(state) is None
    assert pending_store.get(oauth.publication_oauth_name(state)) is not None
