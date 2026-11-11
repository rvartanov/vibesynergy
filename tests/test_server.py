"""Webhook service end to end: HTTP -> job -> worker -> canonical rows; scheduler."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from strava_fake import FakeStravaAPI
from withings_fake import T0, FakeWithingsAPI

from vibesynergy.api.app import WEBHOOK, create_app
from vibesynergy.query import latency
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth
from vibesynergy.sources.strava import webhooks as strava_hooks
from vibesynergy.sources.strava.client import StravaClient
from vibesynergy.sources.withings import webhooks as withings_hooks
from vibesynergy.sources.withings.client import WithingsClient
from vibesynergy.store.schema import activity, audit_log, body_measurement
from vibesynergy.sync.scheduler import Scheduler

PUBLIC = "https://server.example.ts.net"


@pytest.fixture
def service(runtime):
    settings = replace(
        runtime.settings,
        sources={"withings": {"enabled": True}, "strava": {"enabled": True}},
        server={"public_url": PUBLIC},
    )
    rt = Runtime.open(settings)
    withings = rt.registry.get("withings")
    wfake = FakeWithingsAPI()
    wstore = withings.ctx.secrets
    wstore.set(oauth.APP, {"client_id": "cid", "client_secret": "cs"})
    oauth.save_tokens(wstore, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    withings._client = WithingsClient(wstore, post=wfake, interval=0)
    wstore.set(WEBHOOK, {"token": "tok"})

    strava = rt.registry.get("strava")
    sfake = FakeStravaAPI()
    sstore = strava.ctx.secrets
    sstore.set(oauth.APP, {"client_id": "12345", "client_secret": "ss"})
    oauth.save_tokens(sstore, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    sstore.set("athlete", {"id": 77})
    sstore.set(WEBHOOK, {"verify_token": "vt", "subscription_id": 5})
    strava._client = StravaClient(sstore, http=sfake, sleep=lambda s: None)

    yield rt, TestClient(create_app(rt)), wfake, sfake
    rt.close()


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


def _drain(rt) -> int:
    worker, ran = rt.worker(), 0
    while worker.run_once():
        ran += 1
    return ran


def test_health_and_callback_page(service) -> None:
    _, client, _, _ = service
    assert client.get("/vibesynergy/health").json()["ok"] is True
    page = client.get("/vibesynergy/withings/callback?code=x&state=y")
    assert page.status_code == 200 and "vibesynergy auth withings" in page.text
    assert client.head("/vibesynergy/withings/callback").status_code == 200
    assert client.post("/vibesynergy/withings/callback").status_code == 200


def test_withings_notification_to_rows(service) -> None:
    rt, client, _, _ = service
    assert client.head("/vibesynergy/withings/notify/tok").status_code == 200
    assert client.post("/vibesynergy/withings/notify/wrong", content="appli=1").status_code == 404

    body = f"userid=1234&appli=1&startdate={T0}&enddate={T0 + 60}"
    reply = client.post(
        "/vibesynergy/withings/notify/tok",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert reply.status_code == 200
    jobs = rt.jobs.recent("queued")
    assert [(j["payload"]["dataset"], j["payload"]["window"], j["priority"]) for j in jobs] == [
        ("measures", "2026-09-20", 10)
    ]
    assert _drain(rt) == 1
    assert _rows(rt, body_measurement, body_measurement.c.metric == "body.weight")
    assert _rows(rt, audit_log)[0]["action"] == "withings.notify"


def test_withings_notification_for_a_dataset_that_is_off(service) -> None:
    rt, client, _, _ = service
    rt.registry.get("withings").ctx.config["datasets"] = ["measures", "heart"]
    body = f"userid=1234&appli=16&startdate={T0}&enddate={T0 + 60}"  # activity: not synced
    reply = client.post(
        "/vibesynergy/withings/notify/tok",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert reply.status_code == 200 and rt.jobs.recent("queued") == []


def test_strava_validation(service) -> None:
    _, client, _, _ = service
    ok = client.get(
        "/vibesynergy/strava/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "c123"},
    )
    assert ok.json() == {"hub.challenge": "c123"}
    bad = client.get(
        "/vibesynergy/strava/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "c"},
    )
    assert bad.status_code == 403


def test_strava_create_then_delete(service) -> None:
    rt, client, _, _ = service
    event = {
        "object_type": "activity",
        "object_id": 9001,
        "aspect_type": "create",
        "owner_id": 77,
        "subscription_id": 5,
        "event_time": 1789900000,
        "updates": {},
    }
    assert client.post("/vibesynergy/strava/webhook", json=event).status_code == 200
    queued = rt.jobs.recent()
    evidence = _rows(rt, audit_log, audit_log.c.action == "strava.event")[-1]["detail"]
    assert evidence["event_time"] == event["event_time"]
    assert datetime.fromisoformat(evidence["received_at"]).tzinfo is not None
    assert {j["job_id"] for j in evidence["queued_jobs"]} == {j["id"] for j in queued}
    assert all(j["payload"]["received_at"] == evidence["received_at"] for j in queued)
    assert all(j["payload"]["event_time"] == event["event_time"] for j in queued)
    assert _drain(rt) == 2  # activity + streams
    assert _rows(rt, activity, activity.c.source_id == "9001")
    assert rt.parquet.stream_path("strava-9001").exists()
    delivery = latency.recent(rt.engine)[0]
    assert delivery["source_id"] == "9001"
    assert (
        delivery["source_event_at"] == datetime.fromtimestamp(event["event_time"], UTC).isoformat()
    )
    assert delivery["received_at"] == evidence["received_at"]
    stages = ("finished_at", "queued_at", "fetch_started_at", "fetched_at", "processed_at")
    assert all(delivery[key] is not None for key in stages)
    assert delivery["dashboard_at"] is None

    # the same event again while queued is not duplicated
    client.post("/vibesynergy/strava/webhook", json={**event, "aspect_type": "update"})
    client.post("/vibesynergy/strava/webhook", json={**event, "aspect_type": "update"})
    assert _drain(rt) == 1

    client.post("/vibesynergy/strava/webhook", json={**event, "aspect_type": "delete"})
    _drain(rt)
    assert not _rows(rt, activity, activity.c.source_id == "9001")
    versions = list(rt.raw.all_ids("strava", "activities"))
    assert len(versions) >= 2  # history kept; the tombstone is the latest version


def test_strava_made_public_emits_activity_published(service) -> None:
    """Webhook update {"private": "false"} → fetch → derive → one activity.published event."""
    from datetime import timedelta

    from vibesynergy.query import agent_events

    rt, client, _, sfake = service
    start = datetime(2026, 9, 20, 17, tzinfo=UTC)
    agent_events.since(rt, now=start - timedelta(hours=1))
    agent_events.since(rt, now=start - timedelta(hours=1), key="published_since")
    sfake.detail["9001"] = {"private": True}
    event = {
        "object_type": "activity",
        "object_id": 9001,
        "aspect_type": "create",
        "owner_id": 77,
        "subscription_id": 5,
        "updates": {},
    }
    client.post("/vibesynergy/strava/webhook", json=event)
    _drain(rt)
    later = start + timedelta(hours=2)
    assert agent_events.run(rt, now=later)["published"] is None  # still private
    sfake.detail["9001"] = {"private": False}
    update = {**event, "aspect_type": "update", "updates": {"private": "false"}}
    assert client.post("/vibesynergy/strava/webhook", json=update).status_code == 200
    _drain(rt)
    audit = _rows(rt, audit_log, audit_log.c.action == "strava.event")[-1]["detail"]
    assert audit["updates"] == {"private": "false"}
    first = agent_events.run(rt, now=later + timedelta(minutes=6))["published"]
    assert first is not None
    assert agent_events.run(rt, now=later + timedelta(minutes=20))["published"] is None
    (published,) = agent_events.recent(
        rt, since_at=start.isoformat(), types=["activity.published"]
    )["events"]
    assert published["data"]["activities"][0]["public"] == ["strava:9001"]
    assert "Easy spin" not in str(published)  # the description itself never travels


def test_strava_rejects_foreign_events(service) -> None:
    rt, client, _, _ = service
    event = {
        "object_type": "activity",
        "object_id": 1,
        "aspect_type": "create",
        "owner_id": 999,
        "subscription_id": 5,
    }
    assert client.post("/vibesynergy/strava/webhook", json=event).status_code == 403
    assert (
        client.post(
            "/vibesynergy/strava/webhook", json={**event, "owner_id": 77, "subscription_id": 6}
        ).status_code
        == 403
    )
    assert rt.jobs.recent() == []


def test_subscribe_commands(service) -> None:
    rt, _, wfake, sfake = service
    withings = rt.registry.get("withings")
    outcome = withings_hooks.subscribe(withings.client, rt.settings, withings.ctx.secrets)
    assert set(outcome.values()) == {"subscribed"}
    subscribe_calls = [c for c in wfake.calls if c[1] == "subscribe"]
    assert len(subscribe_calls) == 5
    assert subscribe_calls[0][2]["callbackurl"] == f"{PUBLIC}/vibesynergy/withings/notify/tok"

    strava = rt.registry.get("strava")
    store = strava.ctx.secrets
    store.set(WEBHOOK, {})
    sub = strava_hooks.subscribe(strava.client, rt.settings, store)
    assert sub["id"] == 5 and store.get(WEBHOOK)["subscription_id"] == 5
    method, _, params = sfake.push_calls[-1]
    assert method == "POST" and params["callback_url"] == f"{PUBLIC}/vibesynergy/strava/webhook"
    assert params["verify_token"] == store.get(WEBHOOK)["verify_token"]
    assert strava_hooks.unsubscribe(strava.client, store) == [5]


def test_scheduler(service) -> None:
    rt, _, _, _ = service
    now = [0.0]
    settings = replace(rt.settings, schedule={"strava": 0, "withings": 5})
    scheduler = Scheduler(settings, rt.registry, rt.jobs, clock=lambda: now[0])
    assert scheduler.intervals() == {"withings": 5.0}  # strava disabled by 0
    assert scheduler.run_once() == ["dropfolder", "withings"]  # the drop folder always
    now[0] = 100
    assert scheduler.run_once() == []  # not due yet
    now[0] = 301
    assert scheduler.run_once() == []  # due, but the first job is still queued
    _drain(rt)
    now[0] = 700
    assert scheduler.run_once() == ["dropfolder", "withings"]  # the drop folder always


def test_one_dataset_and_the_service_checks_have_their_own_interval(service) -> None:
    rt, _, _, _ = service
    now = [0.0]
    schedule = {"strava": 0, "withings": 0, "withings.measures": 2, "dropfolder": 0}
    settings = replace(rt.settings, schedule=schedule)
    scheduler = Scheduler(settings, rt.registry, rt.jobs, clock=lambda: now[0])
    assert scheduler.intervals() == {"withings.measures": 2.0}
    assert scheduler.run_once() == ["withings.measures"]  # no drop folder: turned off
    queued = rt.jobs.recent(limit=1)[0]
    assert queued["payload"] == {"source": "withings", "dataset": "measures", "window": None}
    # A dataset that isn't collected isn't scheduled on its own either.
    sources = {**rt.settings.sources, "withings": {"enabled": True, "datasets": ["activity"]}}
    only = replace(settings, sources=sources)
    rt2 = Runtime.open(only)
    assert Scheduler(only, rt2.registry, rt2.jobs).intervals() == {}
    rt2.close()


def test_routes_work_with_and_without_prefix(service) -> None:
    _, client, _, _ = service
    assert client.get("/health").status_code == 200
    assert client.head("/withings/notify/tok").status_code == 200
