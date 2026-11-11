"""Finishing OAuth logins without paste-back: the callback page hands the code to the CLI."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from vibesynergy.api.app import create_app
from vibesynergy.core.secrets import SecretStore
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth


def test_handoff_is_one_time_and_needs_a_waiting_login(tmp_path) -> None:
    store = SecretStore(tmp_path, oauth.HANDOFF)
    assert not oauth.complete_handoff(store, "nobody-waits", code="x")  # can't inject a code
    oauth.begin_handoff(store, "s1")
    assert oauth.take_handoff(store, "s1") is None  # nothing yet
    assert oauth.complete_handoff(store, "s1", code="abc")
    assert oauth.take_handoff(store, "s1")["code"] == "abc"
    assert oauth.take_handoff(store, "s1") is None  # taken once
    oauth.begin_handoff(store, "old")
    store.set(oauth._handoff_name("old"), {"created": time.time() - oauth.HANDOFF_TTL_S - 1})
    assert not oauth.complete_handoff(store, "old", code="late")


def test_login_finishes_when_the_browser_comes_back(tmp_path, monkeypatch) -> None:
    store = SecretStore(tmp_path, oauth.HANDOFF)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    finished = threading.Event()

    def ask(_prompt: str) -> str:
        # ask runs after begin_handoff; the callback can now deliver its result.
        link = next(line for line in printed if "state=" in line).strip()
        state = parse_qs(urlparse(link).query)["state"][0]
        assert oauth.complete_handoff(store, state, code="from-browser")
        finished.wait()  # keep paste-back input from winning over the browser result
        return ""

    try:
        code = oauth.paste_back_code(
            "https://example.com/authorize",
            {"client_id": "1"},
            ask=ask,
            handoff=store,
            poll_s=0.02,
        )
    finally:
        finished.set()
    assert code == "from-browser"
    assert not list((tmp_path / oauth.HANDOFF).glob("pending-*"))  # cleaned up


def test_denied_in_the_browser(tmp_path, monkeypatch) -> None:
    store = SecretStore(tmp_path, oauth.HANDOFF)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    finished = threading.Event()

    def ask(_prompt: str) -> str:
        link = next(line for line in printed if "state=" in line).strip()
        state = parse_qs(urlparse(link).query)["state"][0]
        assert oauth.complete_handoff(store, state, code=None, error="access_denied")
        finished.wait()
        return ""

    try:
        with pytest.raises(oauth.AuthError, match="access_denied"):
            oauth.paste_back_code(
                "https://example.com/authorize",
                {},
                ask=ask,
                handoff=store,
                poll_s=0.02,
            )
    finally:
        finished.set()


def test_callback_page_hands_over(runtime) -> None:
    rt = Runtime.open(
        replace(runtime.settings, server={"public_url": "https://server.example.ts.net"})
    )
    client = TestClient(create_app(rt, with_mcp=False), base_url="https://server.example.ts.net")
    store = SecretStore(rt.settings.secrets_dir, oauth.HANDOFF)
    oauth.begin_handoff(store, "st-1")
    page = client.get("/vibesynergy/strava/callback", params={"state": "st-1", "code": "c-9"})
    assert "Logged in" in page.text
    assert oauth.take_handoff(store, "st-1")["code"] == "c-9"
    other = client.get("/vibesynergy/strava/callback", params={"state": "unknown", "code": "c"})
    assert "Almost done" in other.text  # paste-back fallback
    rt.close()
