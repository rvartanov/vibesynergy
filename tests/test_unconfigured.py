"""A source that is enabled but not set up is never scheduled, never fails, never alerts."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from vibesynergy import alerts
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.sources.caldav import account
from vibesynergy.sync.scheduler import Scheduler

CAL = {"url": "https://caldav.example.com/cal/", "name": "Workouts"}


def open_caldav(runtime, calendars, *, login):
    settings = replace(
        runtime.settings,
        sources={"caldav": {"enabled": True, "calendars": calendars}},
        alerts={"email_to": "me@example.com"},
        server={"public_url": "https://server.example.ts.net"},
    )
    rt = Runtime.open(settings)
    if login:
        account.store_for(settings).set(account.ACCOUNT, {"username": "u", "password": "p"})
    return rt


@pytest.mark.parametrize(
    ("calendars", "login", "expected"),
    [([], False, False), ([CAL], False, False), ([], True, False), ([CAL], True, True)],
)
def test_caldav_configured_needs_calendar_and_login(runtime, calendars, login, expected):
    rt = open_caldav(runtime, calendars, login=login)
    try:
        assert rt.registry.get("caldav").configured() is expected
    finally:
        rt.close()


def test_scheduler_skips_unconfigured_source(runtime):
    rt = open_caldav(runtime, [], login=False)
    try:
        scheduler = Scheduler(rt.settings, rt.registry, rt.jobs, clock=lambda: 0)
        assert "caldav" not in scheduler.intervals()
        assert "caldav" not in scheduler.run_once()
    finally:
        rt.close()
    rt = open_caldav(runtime, [CAL], login=True)
    try:
        scheduler = Scheduler(rt.settings, rt.registry, rt.jobs, clock=lambda: 0)
        assert "caldav" in scheduler.intervals()
    finally:
        rt.close()


def test_queued_sync_for_unconfigured_source_ends_quietly(runtime, caplog):
    rt = open_caldav(runtime, [], login=False)
    try:
        job_id = rt.jobs.enqueue(
            "sync", {"source": "caldav", "dataset": None, "window": None}, max_attempts=1
        )
        with caplog.at_level(logging.INFO):
            assert rt.worker().run_once()
        assert rt.jobs.recent(status="dead") == []
        assert [j["id"] for j in rt.jobs.recent(status="done")] == [job_id]
        assert sum("skipped: not set up" in r.message for r in caplog.records) == 1
    finally:
        rt.close()


def test_queued_sync_for_disabled_source_ends_quietly(runtime):
    settings = replace(runtime.settings, sources={"caldav": {"enabled": False}})
    rt = Runtime.open(settings)
    try:
        rt.jobs.enqueue("sync", {"source": "caldav"}, max_attempts=1)
        rt.worker().run_once()
        assert rt.jobs.recent(status="dead") == []
    finally:
        rt.close()


def fail_job(rt, error, source="caldav"):
    rt.jobs.enqueue("sync", {"source": source, "dataset": "calendar"}, max_attempts=1)
    rt.jobs.fail(rt.jobs.claim("w").id, error)


def test_job_alert_is_plain_and_deduplicated_for_24_hours(runtime):
    rt = open_caldav(runtime, [CAL], login=True)
    sent = []
    record = lambda subject, body, channel: sent.append(body)  # noqa: E731
    try:
        alerts.check(rt, send=record)  # first check
        msg = (
            "ValueError: Select at least one calendar (Settings → Sources → Calendar) and a window"
        )
        fail_job(rt, f"JobError: calendar: {msg}")
        alerts.check(rt, send=record)
        recent = alerts.for_portal(rt.settings)["recent"]
        assert len(recent) == 1
        text = recent[0]["text"]
        assert "calendar" in text and "Settings → Sources → Calendar" in text
        assert "Error" not in text and "Traceback" not in text

        fail_job(rt, f"JobError: calendar: {msg}")  # the same problem again, hours later
        alerts.check(rt, send=record)
        assert len(alerts.for_portal(rt.settings)["recent"]) == 1

        store = SecretStore(rt.settings.secrets_dir, alerts.SECRETS)
        state = store.get(alerts.STATE)
        old = (utc_now() - timedelta(hours=25)).isoformat()
        state[alerts.SENT] = {k: old for k in state[alerts.SENT]}
        store.set(alerts.STATE, state)
        fail_job(rt, f"JobError: calendar: {msg}")
        alerts.check(rt, send=record)
        assert len(alerts.for_portal(rt.settings)["recent"]) == 2
    finally:
        rt.close()


def test_other_errors_get_plain_text_without_class_names(runtime):
    rt = open_caldav(runtime, [CAL], login=True)
    try:
        alerts.check(rt, send=lambda *a: None)
        fail_job(rt, "JobError: calendar: httpx.ConnectError: boom 42")
        alerts.check(rt, send=lambda *a: None)
        text = alerts.for_portal(rt.settings)["recent"][0]["text"]
        assert "ConnectError" not in text and "JobError" not in text
        assert text == "Your calendar couldn't be read. Try again later."
    finally:
        rt.close()


def test_data_page_shows_unconfigured_source_as_not_set_up(runtime):
    rt = open_caldav(runtime, [], login=False)
    try:
        client = TestClient(create_portal_app(rt), base_url="https://server.example.ts.net:8447")
        data = client.get("/api/data").json()["data"]
        caldav = next(s for s in data["sources"] if s["id"] == "caldav")
        assert caldav["enabled"] and caldav["configured"] is False
        assert all(p["source"] != "caldav" for p in data["problems"])
    finally:
        rt.close()
