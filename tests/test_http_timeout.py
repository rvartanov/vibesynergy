"""Per-request HTTP timeouts for source clients."""

from __future__ import annotations

import time

import pytest
import requests

from vibesynergy.core.config import Settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources.garmin import client as garmin_client
from vibesynergy.sources.http_timeout import http_timeout_seconds, requests_timeout_pair
from vibesynergy.sources.strava.client import StravaClient, _requests_http
from vibesynergy.sources.withings.client import WithingsClient, _requests_post


def test_http_timeout_seconds_schedule_and_source_override(tmp_path):
    settings = Settings(
        config_path=tmp_path / "config.toml",
        data_dir=tmp_path / "data",
        schedule={"http_timeout_seconds": 45},
        sources={"garmin": {"http_timeout_seconds": 30}},
    )
    assert http_timeout_seconds(settings) == 45.0
    assert http_timeout_seconds(settings, "garmin") == 30.0
    assert http_timeout_seconds(settings, "strava") == 45.0


def test_requests_timeout_pair():
    assert requests_timeout_pair(60.0) == (15.0, 60.0)
    assert requests_timeout_pair(10.0) == (10.0, 10.0)


def test_strava_requests_http_uses_timeout(monkeypatch):
    seen: dict = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        raise requests.Timeout()

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(requests.Timeout):
        _requests_http("GET", "http://example.test", {}, {}, timeout=12.5)
    assert seen["timeout"] == 12.5


def test_withings_post_uses_timeout(monkeypatch):
    seen: dict = {}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    _requests_post("http://example.test", {}, {}, timeout=9.0)
    assert seen["timeout"] == 9.0


def test_strava_client_hanging_transport_times_out(tmp_path, monkeypatch):
    store = SecretStore(tmp_path, "strava")
    store.set(
        "tokens",
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
        },
    )
    store.set("app", {"client_id": "1", "client_secret": "s"})

    def slow_get(url, **kwargs):
        assert kwargs["timeout"] == 0.05
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(requests, "get", slow_get)
    client = StravaClient(store, timeout=0.05, clock=lambda: 1000.0)
    with pytest.raises(requests.Timeout):
        client.get("/athlete")


def test_garmin_apply_request_timeout(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def _run_request(self, method, path, **kwargs):
            captured.update(kwargs)
            return object()

    class FakeApi:
        client = FakeClient()

    garmin_client.apply_request_timeout(FakeApi(), 55.0)
    FakeApi.client._run_request("GET", "/x")
    assert captured["timeout"] == 55.0
    captured.clear()
    FakeApi.client._run_request("GET", "/y", timeout=3.0)
    assert captured["timeout"] == 3.0


def test_withings_call_surfaces_timeout(tmp_path, monkeypatch):
    store = SecretStore(tmp_path, "withings")
    store.set(
        "tokens",
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
        },
    )
    store.set("app", {"client_id": "1", "client_secret": "s"})

    def slow_post(url, **kwargs):
        assert kwargs["timeout"] == 0.05
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(requests, "post", slow_post)
    client = WithingsClient(store, timeout=0.05, interval=0)
    with pytest.raises(requests.Timeout):
        client.call("/v2/measure", "getmeas")
