"""Strava source against a fake API: datasets, mappings, streams, rate limits, login."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from strava_fake import FakeStravaAPI

from vibesynergy.api.app import create_app
from vibesynergy.core.timeutil import Window
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth
from vibesynergy.sources.strava.client import Response, StravaClient, StravaRateLimited, budget_wait
from vibesynergy.store.schema import activity, activity_lap, activity_zone_time

DAY = date(2026, 9, 20)


@pytest.fixture
def strava(runtime):
    settings = replace(
        runtime.settings,
        sources={"strava": {"enabled": True}},
        server={"public_url": "https://server.example.ts.net"},
    )
    rt = Runtime.open(settings)
    source = rt.registry.get("strava")
    fake = FakeStravaAPI()
    store = source.ctx.secrets
    store.set(oauth.APP, {"client_id": "12345", "client_secret": "ssecret"})
    oauth.save_tokens(store, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    sleeps: list[float] = []
    source._client = StravaClient(store, http=fake, sleep=sleeps.append, clock=lambda: 1000.0)
    yield rt, fake, sleeps
    rt.close()


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


def test_all_datasets_sync(strava) -> None:
    rt, _, _ = strava
    results = rt.sync.sync("strava", window=Window(DAY, DAY))
    assert {r.dataset: r.error for r in results if r.error or r.normalise_errors} == {}
    assert len(results) == 7


def test_activities_laps_zones(strava) -> None:
    rt, _, _ = strava
    rt.sync.sync("strava", ["activities"], Window(DAY, DAY))
    rows = {r["source_id"]: r for r in _rows(rt, activity)}
    ride = rows["9001"]
    assert ride["source"] == "strava" and ride["sport"] == "VirtualRide"
    assert ride["tz_offset_s"] == 7200 and ride["local_date"] == DAY
    assert ride["calories"] == 800 and ride["norm_power"] == 228
    assert ride["extra"]["description"] == "Easy spin" and ride["extra"]["trainer"] is True
    assert ride["fit_id"] == "strava-9001"
    assert rows["9002"]["fit_id"] is None  # manual: no streams
    laps = _rows(rt, activity_lap, activity_lap.c.activity_source_id == "9001")
    assert [lap["avg_power"] for lap in laps] == [210, 220]
    zones = _rows(rt, activity_zone_time, activity_zone_time.c.activity_source_id == "9001")
    assert {z["zone"]: z["seconds"] for z in zones} == {1: 600, 2: 3000}


def test_streams_once_and_to_parquet(strava) -> None:
    rt, fake, _ = strava
    first = rt.sync.sync("strava", ["activity_streams"], Window(DAY, DAY))[0]
    assert (first.fetched, first.normalise_errors) == (1, 0)  # manual activity skipped
    table = rt.parquet.read_stream("strava-9001").to_pydict()
    assert table["power"] == [200, 210, None] and table["lat"][1] == 52.1001
    assert table["moving"] == [1, 1, 0]
    assert str(table["t"][0]) == "2026-09-20 17:00:00+00:00"

    again = rt.sync.sync("strava", ["activity_streams"], Window(DAY, DAY))[0]
    assert again.skipped == 1
    assert sum(1 for c in fake.calls if c.endswith("/streams")) == 1


@pytest.mark.parametrize(
    ("usage", "retry_at", "message"),
    [
        ("198,500", datetime(1970, 1, 1, 0, 31, tzinfo=UTC), "short request budget"),
        ("10,1999", datetime(1970, 1, 2, 0, 1, tzinfo=UTC), "daily request budget"),
    ],
)
def test_budget_preserves_reply_and_defers_next_request(strava, usage, retry_at, message) -> None:
    rt, fake, sleeps = strava
    fake.usage = usage
    result = rt.sync.sync("strava", ["clubs"])[0]
    assert result.error is None
    calls = list(fake.calls)
    client = rt.registry.get("strava").client
    with pytest.raises(StravaRateLimited, match=message) as limited:
        client.get("/athlete/clubs")
    assert limited.value.retry_at == retry_at
    assert fake.calls == calls and sleeps == []
    client._clock = lambda: retry_at.timestamp()
    fake.usage = "10,100"
    client.get("/athlete/clubs")
    assert len(fake.calls) == len(calls) + 1


def test_read_and_global_budgets_use_latest_reset(strava) -> None:
    rt, _, _ = strava
    client = rt.registry.get("strava").client
    client._respect_limits(
        {
            "x-ratelimit-limit": "200,2000",
            "x-ratelimit-usage": "198,100",
            "x-readratelimit-limit": "100,1000",
            "x-readratelimit-usage": "10,999",
        }
    )
    with pytest.raises(StravaRateLimited) as limited:
        client.get("/athlete/clubs")
    assert limited.value.retry_at == datetime(1970, 1, 2, 0, 1, tzinfo=UTC)


def test_rate_limit_releases_worker_for_other_jobs(strava) -> None:
    from vibesynergy.sync.worker import Worker, default_handlers

    rt, fake, sleeps = strava
    # A real future deadline makes the deferred job ineligible for the next claim.
    rt.registry.get("strava").client._clock = lambda: datetime.now(UTC).timestamp()
    fake.usage = "10,1999"
    rt.jobs.enqueue(
        "fetch_one", {"source": "strava", "dataset": "activities", "key": "9001"}, priority=5
    )
    other = rt.jobs.enqueue("other", {})
    handled = []
    worker = Worker(
        rt.jobs,
        {**default_handlers(rt.sync, rt.normaliser), "other": lambda job: handled.append(job.id)},
    )
    assert worker.run_once()  # Strava defers without sleeping.
    assert worker.run_once()  # The independent job remains runnable.
    assert handled == [other] and sleeps == []


def test_429_is_a_rate_limit_error(strava) -> None:
    rt, fake, _ = strava
    fake.status_for["/athlete/clubs"] = 429
    with pytest.raises(StravaRateLimited):
        rt.registry.get("strava").client.get("/athlete/clubs")


def test_rate_limits_say_when_to_retry(strava) -> None:
    rt, fake, _ = strava  # the fake clock is 1970-01-01 00:16:40 UTC
    client = rt.registry.get("strava").client
    fake.status_for["/athlete/clubs"] = 429
    with pytest.raises(StravaRateLimited) as short:
        client.get("/athlete/clubs")
    assert short.value.retry_at == datetime(1970, 1, 1, 0, 31, tzinfo=UTC)  # next window
    fake.usage = "10,2000"
    client._clock = lambda: 1861.0  # expire the persisted short-window deferral first
    with pytest.raises(StravaRateLimited) as daily:
        client.get("/athlete/clubs")
    assert daily.value.retry_at == datetime(1970, 1, 2, 0, 1, tzinfo=UTC)  # after midnight


def test_rate_limited_webhook_fetch_waits_for_the_reset(strava) -> None:
    rt, fake, _ = strava
    fake.usage = "10,2000"  # daily budget used up; headroom alone must not cause this wait
    job_id = rt.jobs.enqueue(
        "fetch_one", {"source": "strava", "dataset": "activities", "key": "9001"}
    )
    assert rt.worker().run_once()
    (job,) = [j for j in rt.jobs.recent() if j["id"] == job_id]
    assert job["status"] == "queued" and job["attempts"] == 0  # no attempt used up
    assert job["run_after"].replace(tzinfo=UTC) == datetime(1970, 1, 2, 0, 1, tzinfo=UTC)
    assert "daily request budget" in job["last_error"]


def test_zone_errors_do_not_lose_the_activity(strava) -> None:
    rt, fake, _ = strava
    fake.status_for["/activities/9001/zones"] = 402  # subscription feature
    rt.sync.sync("strava", ["activities"], Window(DAY, DAY))
    ride = _rows(rt, activity, activity.c.source_id == "9001")[0]
    assert "zones" in ride["extra"]["detail_errors"]


def test_webhook_fetch_uses_the_headroom_scheduled_syncs_leave(strava) -> None:
    rt, fake, _ = strava
    fake.usage = "196,100"  # inside the reserved calls, not at the hard limit
    client = rt.registry.get("strava").client
    client.get("/athlete/clubs")
    with pytest.raises(StravaRateLimited, match="short request budget"):
        client.get("/athlete/clubs")
    # The reserved calls are for this upload. The portal must not say new activities are waiting.
    assert budget_wait(rt.registry.get("strava").ctx.secrets, now=1000.0) is None
    before = len(fake.calls)
    rt.jobs.enqueue(
        "fetch_one", {"source": "strava", "dataset": "activities", "key": "9001"}, priority=10
    )
    assert rt.worker().run_once()
    assert len(fake.calls) > before
    assert _rows(rt, activity, activity.c.source_id == "9001")


def test_a_hard_limit_still_blocks_a_webhook_when_headroom_is_later(strava) -> None:
    rt, fake, _ = strava
    client = rt.registry.get("strava").client
    soon = datetime.fromtimestamp(1100, UTC)
    later = datetime.fromtimestamp(5000, UTC)
    client._save_limit(soon, "rate limit exceeded; the sync resumes later", soft=False)
    client._save_limit(later, "short request budget used; continuing next window", soft=True)
    with pytest.raises(StravaRateLimited) as scheduled:
        client.get("/athlete")
    with pytest.raises(StravaRateLimited) as hooked, client.priority_budget():
        client.get("/athlete")
    assert scheduled.value.retry_at == later
    assert hooked.value.retry_at == soon
    assert fake.calls == []


def test_fetch_one_for_webhooks(strava) -> None:
    rt, _, _ = strava
    source = rt.registry.get("strava")
    items = list(source.fetch_one("activities", "9001"))
    assert items[0].item_key == "9001" and items[0].resolve()["detail"]["name"] == "Zwift - Watopia"


def test_portal_grant_and_refresh(strava) -> None:
    rt, fake, _ = strava
    source = rt.registry.get("strava")
    store = source.ctx.secrets
    store.delete(oauth.TOKENS)
    with pytest.raises(oauth.AuthError, match="Settings"):
        source.authenticate()
    source.client.exchange_code(oauth.load_app(store, "strava"), "xyz", "read,activity:write")
    assert fake.token_requests[-1]["code"] == "xyz"
    assert store.get("athlete") == {"id": 77}
    assert store.get(oauth.TOKENS)["scope"] == "read,activity:write"

    tokens = store.get(oauth.TOKENS)
    store.set(oauth.TOKENS, {**tokens, "expires_at": 0})
    source.client.get("/athlete")
    assert fake.token_requests[-1]["grant_type"] == "refresh_token"
    assert store.get(oauth.TOKENS)["refresh_token"] == "s-refresh-2"
    assert store.get(oauth.TOKENS)["scope"] == "read,activity:write"


def test_portal_strava_connection_records_only_granted_scope(strava, monkeypatch):
    rt, fake, _ = strava
    store = rt.registry.get("strava").ctx.secrets
    store.delete(oauth.TOKENS)
    portal_base = "http://localhost"
    portal = TestClient(create_portal_app(rt), base_url=portal_base)
    start = portal.post(
        "/api/sources/strava/connect",
        json={"portal": portal_base + "/"},
        headers={"x-vibesynergy": "portal"},
    )
    assert start.status_code == 200
    query = parse_qs(urlsplit(start.json()["data"]["url"]).query)
    assert "activity:write" in query["scope"][0]
    from vibesynergy.sources.strava import client as client_module

    client_class = client_module.StravaClient
    monkeypatch.setattr(
        client_module,
        "StravaClient",
        lambda saved, **kwargs: client_class(saved, http=fake, **kwargs),
    )
    public = TestClient(create_app(rt, with_mcp=False), base_url="https://server.example.ts.net")
    denied = public.get(
        "/vibesynergy/strava/callback",
        params={"state": query["state"][0], "code": "synthetic", "scope": "read"},
        follow_redirects=False,
    )
    assert denied.status_code == 303
    assert denied.headers["location"].endswith("strava=write_missing")
    assert store.get(oauth.TOKENS) is None
    start = portal.post(
        "/api/sources/strava/connect",
        json={"portal": portal_base + "/"},
        headers={"x-vibesynergy": "portal"},
    )
    state = parse_qs(urlsplit(start.json()["data"]["url"]).query)["state"][0]
    approved = public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": "synthetic", "scope": "read activity:write"},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    assert approved.headers["location"].endswith("strava=connected")
    assert store.get(oauth.TOKENS)["scope"] == "read activity:write"
    source_status = next(
        s for s in portal.get("/api/settings").json()["data"]["sources"] if s["id"] == "strava"
    )
    assert source_status["login"]["write_access"] == "granted"
    assert (rt.settings.secrets_dir / "strava/tokens.enc").exists()
    disconnected = portal.post(
        "/api/sources/strava/disconnect", headers={"x-vibesynergy": "portal"}
    )
    assert disconnected.json()["data"]["state"] == "disconnected"
    assert store.get(oauth.TOKENS) is None
    assert store.get(oauth.APP) is None


def test_description_put_retries_one_rejected_token_without_duplicate_body(tmp_path, monkeypatch):
    from vibesynergy.core.secrets import SecretStore

    store = SecretStore(tmp_path, "strava")
    store.set(oauth.APP, {"client_id": "123", "client_secret": "synthetic"})
    store.set(
        oauth.TOKENS,
        {
            "access_token": "old",
            "refresh_token": "old-refresh",
            "expires_at": 10000,
            "scope": "read activity:write",
        },
    )
    monkeypatch.setattr(oauth, "needs_refresh", lambda *_: False)
    calls = []

    def http(method, url, params, headers):
        calls.append((method, url, params, headers))
        if url.endswith("/oauth/token"):
            return Response(
                200,
                {},
                {"access_token": "new", "refresh_token": "new-refresh", "expires_at": 10000},
            )
        if headers["Authorization"] == "Bearer old":
            return Response(401, {}, {"message": "expired"})
        return Response(200, {}, {"description": params["description"]})

    client = StravaClient(store, http=http, clock=lambda: 1000)
    client.update_description("9001", "Human note\n\n[VibeSynergy] Test")
    puts = [call for call in calls if call[0] == "PUT"]
    assert len(puts) == 2
    assert puts[0][2] == puts[1][2] == {"description": "Human note\n\n[VibeSynergy] Test"}
    assert store.get(oauth.TOKENS)["scope"] == "read activity:write"


def test_request_budget_survives_new_client_and_resumes_after_reset(strava):
    rt, fake, sleeps = strava
    store = rt.registry.get("strava").ctx.secrets
    fake.usage = "198,500"
    first = StravaClient(store, http=fake, clock=lambda: 1000.0)
    first.get("/athlete/clubs")
    calls = list(fake.calls)
    restarted = StravaClient(store, http=fake, clock=lambda: 1001.0)
    with pytest.raises(StravaRateLimited) as limited:
        restarted.get("/athlete/clubs")
    assert limited.value.retry_at.timestamp() == 1860.0 and fake.calls == calls
    fake.usage = "1,501"
    after_reset = StravaClient(store, http=fake, clock=lambda: 1861.0)
    after_reset.get("/athlete/clubs")
    assert len(fake.calls) == len(calls) + 1 and sleeps == []


def test_explicit_429_is_shared_and_later_deadline_is_not_shortened(strava):
    rt, fake, _ = strava
    store = rt.registry.get("strava").ctx.secrets
    fake.status_for["/athlete/clubs"] = 429
    first = StravaClient(store, http=fake, clock=lambda: 1000.0)
    with pytest.raises(StravaRateLimited):
        first.get("/athlete/clubs")
    calls = list(fake.calls)
    other = StravaClient(store, http=fake, clock=lambda: 1001.0)
    with pytest.raises(StravaRateLimited):
        other.get("/athlete/clubs")
    assert fake.calls == calls
    tomorrow = datetime(1970, 1, 2, tzinfo=UTC)
    first._save_limit(tomorrow, "daily request budget")
    other._save_limit(datetime(1970, 1, 1, 1, tzinfo=UTC), "short request budget")
    assert store.get("request_budget")["until"] == tomorrow.timestamp()


def test_budget_checkpoint_failure_does_not_discard_successful_reply(strava, monkeypatch):
    rt, fake, _ = strava
    store = rt.registry.get("strava").ctx.secrets
    fake.usage = "198,500"
    original = store.set

    def failed(name, value):
        if name == "request_budget":
            raise OSError("synthetic checkpoint failure")
        return original(name, value)

    monkeypatch.setattr(store, "set", failed)
    client = StravaClient(store, http=fake, clock=lambda: 1000.0)
    assert client.get("/athlete/clubs") is not None
    calls = list(fake.calls)
    with pytest.raises(StravaRateLimited):
        client.get("/athlete/clubs")
    assert fake.calls == calls


@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '{"until": 5000}', '{"until": "soon", "message": "x"}', '"text"'],
)
def test_corrupt_budget_checkpoint_is_ignored(strava, content, caplog):
    rt, fake, _ = strava
    store = rt.registry.get("strava").ctx.secrets
    store.set("request_budget", {})  # creates the directory
    (store._dir / "request_budget.json").write_text(content)
    client = StravaClient(store, http=fake, clock=lambda: 1000.0)
    assert client.get("/athlete/clubs") is not None
    assert "unreadable Strava request budget" in caplog.text
