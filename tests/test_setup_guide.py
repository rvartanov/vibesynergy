"""The "Get started" guide: its three steps from what is already there, and putting it away."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vibesynergy.portal import share
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import setup_guide, thresholds
from vibesynergy.runtime import Runtime

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}


@pytest.fixture
def client(runtime):
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"})
    rt = Runtime.open(settings)
    yield TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    rt.close()


def test_a_new_install_has_three_steps_to_do(runtime, client) -> None:
    # Only the demo source is on: synthetic data is not "a source connected".
    assert setup_guide.state(runtime) == {
        "steps": {"source": False, "zones": False, "coach": False},
        "complete": False,
    }
    data = client.get("/api/setup").json()["data"]
    assert data["steps"] == {"source": False, "zones": False, "coach": False}
    assert data["complete"] is False and data["dismissed"] is False


def test_each_step_is_done_from_existing_data(runtime, monkeypatch) -> None:
    thresholds.set_value(
        runtime.engine, sport="run", metric="lthr", value=165, today=date(2026, 11, 1)
    )
    assert setup_guide.state(runtime)["steps"] == {"source": False, "zones": True, "coach": False}

    settings = replace(
        runtime.settings,
        sources={
            **runtime.settings.sources,
            "garmin": {"enabled": True},
            "caldav": {"enabled": True},
        },
    )
    rt = Runtime.open(settings)
    try:
        assert setup_guide.state(rt)["steps"]["source"] is True
        # The API coach switched on with a key counts as a coach.
        from vibesynergy.coach_runner import settings as runner_settings

        monkeypatch.setattr(
            runner_settings, "load", lambda _rt: SimpleNamespace(enabled=True, api_key_set=True)
        )
        state = setup_guide.state(rt)
        assert state["steps"]["coach"] is True and state["complete"] is True
    finally:
        rt.close()


def test_a_calendar_or_the_plan_alone_is_no_data_source(runtime) -> None:
    settings = replace(
        runtime.settings, sources={"caldav": {"enabled": True}, "intervals_icu": {"enabled": True}}
    )
    rt = Runtime.open(settings)
    try:
        assert setup_guide.state(rt)["steps"]["source"] is False
    finally:
        rt.close()


def test_putting_the_guide_away_is_remembered(client) -> None:
    reply = client.post(
        "/api/prefs", json={"changes": {"setup_guide_dismissed": True}}, headers=WRITE
    )
    assert reply.status_code == 200
    assert client.get("/api/setup").json()["data"]["dismissed"] is True
    bad = client.post(
        "/api/prefs", json={"changes": {"setup_guide_dismissed": "yes"}}, headers=WRITE
    )
    assert bad.status_code == 400
    # Blocks pinned to stay although empty: a list of block ids, like the hidden ones.
    pin = client.post(
        "/api/prefs", json={"changes": {"dashboard_pinned": ["sleep", "body"]}}, headers=WRITE
    )
    assert pin.json()["data"]["dashboard_pinned"] == ["sleep", "body"]
    bad = client.post("/api/prefs", json={"changes": {"dashboard_pinned": "sleep"}}, headers=WRITE)
    assert bad.status_code == 400


def test_a_shared_view_cannot_read_the_setup_state() -> None:
    link = share.Share("id", "title", (share.ALL,), True)
    assert not link.allows("GET", "/setup")
