"""Agent events: debounce, outbox, signing, retries, caps and secret hygiene.

Every HTTP exchange is a fake; nothing leaves the test.
"""

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_evidence import START, activity, put

from vibesynergy.query import agent_events, publication_insight
from vibesynergy.store.schema import agent_event_delivery, audit_log

TOKEN = "sk-ant-oat01-SYNTHETIC-routine-token-never-shown"
SECRET = "whsec_" + base64.b64encode(b"synthetic-signing-key-123456").decode()
ROUTINE = "https://api.anthropic.com/v1/claude_code/routines/trig_01SYNTHETIC/fire"
T0 = START + timedelta(hours=1)  # 13:00 in Amsterdam


class FakeHTTP:
    """Records requests and answers from a script of (status, headers, text)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append({"url": url, "headers": dict(headers), "body": body})
        if not self.replies:
            reply = (
                200,
                {},
                '{"type":"routine_fire","claude_code_session_url":"https://claude.ai/code/session_1"}',
            )
        else:
            reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return agent_events.Reply(*reply)


@pytest.fixture
def started(runtime):
    agent_events.since(runtime, now=START - timedelta(hours=1))
    agent_events.since(runtime, now=START - timedelta(hours=1), key="published_since")
    agent_events.migrate(runtime)  # nothing to migrate yet; links below keep their types
    return runtime


def link(rt, auth="claude_routine", url=ROUTINE, secret=TOKEN, **extra):
    values = {
        "name": "Coach",
        "url": url,
        "auth": auth,
        "enabled": True,
        "events": [agent_events.ACTIVITY_READY],
        **extra,
    }
    return agent_events.save_subscription(rt, values, secret=secret)


def strava_copy():
    return replace(activity(), source_id="2", name="Strava title", fit_id=None)


def deliveries(rt):
    with rt.engine.connect() as conn:
        return conn.execute(select(agent_event_delivery)).mappings().all()


def audits(rt, action="events.delivery"):
    with rt.engine.connect() as conn:
        return (
            conn.execute(select(audit_log).where(audit_log.c.action.like(f"{action}%")))
            .mappings()
            .all()
        )


def test_debounce_merges_garmin_and_strava_into_one_event(started):
    rt = started
    sub = link(rt)
    http = FakeHTTP()
    put(rt, "strava", strava_copy())
    agent_events.observe(rt, now=T0)
    # Strava alone, Garmin not connected in tests: ready after the debounce only.
    assert agent_events.run(rt, now=T0 + timedelta(minutes=2), http=http)["event"] is None
    put(rt, "garmin", activity())  # the Garmin copy merges in: the debounce starts again
    agent_events.observe(rt, now=T0 + timedelta(minutes=4))
    assert not agent_events.due(rt, now=T0 + timedelta(minutes=6))
    assert agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)["event"] is None
    assert agent_events.due(rt, now=T0 + timedelta(minutes=9))
    result = agent_events.run(rt, now=T0 + timedelta(minutes=9), http=http)
    assert result["event"] and result["deliveries"] == {"sent": 1}
    events = agent_events.recent(rt, since_at=START.isoformat())["events"]
    assert len(events) == 1
    (event,) = events
    assert event["type"] == "activity.ready" and event["version"] == 1
    (item,) = event["data"]["activities"]
    assert item["sources"] == ["garmin:1", "strava:2"]
    assert item["public_insight_missing"] is True
    assert set(item) == {"id", "sources", "local_date", "sport", "public_insight_missing"}
    (call,) = http.calls
    assert call["url"] == ROUTINE
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["anthropic-beta"] == "experimental-cc-routine-2026-04-01"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    text = json.loads(call["body"])["text"]
    assert text == f"event=activity.ready event_id={event['id']} activity_ids={item['id']}"
    overview = agent_events.overview(rt)["subscriptions"][0]
    assert overview["id"] == sub["id"]
    assert overview["deliveries"][0]["link"] == "https://claude.ai/code/session_1"


def test_strava_only_waits_for_garmin_while_connected(started, monkeypatch):
    rt = started
    monkeypatch.setattr(agent_events, "_garmin_connected", lambda _rt: True)
    put(rt, "strava", strava_copy())
    agent_events.observe(rt, now=T0)
    assert agent_events.emit_ready(rt, now=T0 + timedelta(minutes=10)) is None
    assert agent_events.emit_ready(rt, now=T0 + timedelta(minutes=31)) is not None


def test_idempotent_across_runs_restarts_and_later_merges(started):
    rt = started
    link(rt)
    http = FakeHTTP()
    put(rt, "strava", strava_copy())
    agent_events.observe(rt, now=T0)
    agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)
    assert len(http.calls) == 1
    # The Garmin copy arrives late and changes the group id: no second event.
    put(rt, "garmin", activity())
    for minutes in (20, 30, 60):
        agent_events.run(rt, now=T0 + timedelta(minutes=minutes), http=http)
    assert len(http.calls) == 1
    assert len(agent_events.recent(rt, since_at=START.isoformat())["events"]) == 1


def test_several_activities_share_one_event(started):
    rt = started
    link(rt)
    http = FakeHTTP()
    put(rt, "garmin", activity())
    put(rt, "garmin", replace(activity(), source_id="5", start_utc=START + timedelta(hours=3)))
    agent_events.run(rt, now=START + timedelta(hours=4), http=http)
    agent_events.run(rt, now=START + timedelta(hours=4, minutes=6), http=http)
    (call,) = http.calls
    assert "activity_ids=garmin:1,garmin:5" in json.loads(call["body"])["text"]


def test_no_backfill_before_the_feature_started(runtime):
    agent_events.since(runtime, now=START + timedelta(minutes=30))
    put(runtime, "garmin", activity())  # started before events existed
    agent_events.observe(runtime, now=T0)
    assert agent_events.emit_ready(runtime, now=T0 + timedelta(hours=1)) is None


def test_crash_while_sending_is_never_sent_again(started):
    rt = started
    link(rt)
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    agent_events.emit_ready(rt, now=T0 + timedelta(minutes=6))
    (row,) = deliveries(rt)
    agent_events._claim(rt, row["id"], T0 + timedelta(minutes=6))  # then the process dies
    http = FakeHTTP()
    assert agent_events.due(rt, now=T0 + timedelta(minutes=20))
    agent_events.run(rt, now=T0 + timedelta(minutes=20), http=http)
    assert http.calls == []
    assert deliveries(rt)[0]["state"] == "unknown"


def test_backoff_on_429_and_5xx_then_gives_up(started):
    rt = started
    link(rt)
    http = FakeHTTP(
        (429, {"Retry-After": "120"}, "slow down"),
        (503, {}, "busy"),
        ConnectionError("synthetic network failure"),
        (500, {}, "broken"),
    )
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    now = T0 + timedelta(minutes=6)
    agent_events.run(rt, now=now, http=http)
    row = deliveries(rt)[0]
    assert row["state"] == "retry" and row["attempts"] == 1
    assert row["next_at"].replace(tzinfo=UTC) == now + timedelta(seconds=120)
    assert not agent_events.due(rt, now=now + timedelta(seconds=60))
    now += timedelta(seconds=120)
    agent_events.run(rt, now=now, http=http)
    row = deliveries(rt)[0]
    assert row["attempts"] == 2
    assert row["next_at"].replace(tzinfo=UTC) == now + timedelta(minutes=10)  # 5 min * 2
    now += timedelta(minutes=10)
    agent_events.run(rt, now=now, http=http)
    assert deliveries(rt)[0]["next_at"].replace(tzinfo=UTC) == now + timedelta(minutes=20)
    now += timedelta(minutes=20)
    agent_events.run(rt, now=now, http=http)
    assert deliveries(rt)[0]["state"] == "gave_up"
    agent_events.run(rt, now=now + timedelta(hours=1), http=http)
    assert len(http.calls) == agent_events.MAX_TRIES
    assert [a["outcome"] for a in audits(rt)] == ["retry", "retry", "retry", "gave_up"]


def test_other_4xx_is_not_retried(started):
    rt = started
    link(rt)
    http = FakeHTTP((401, {}, "bad token"))
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)
    agent_events.run(rt, now=T0 + timedelta(hours=2), http=http)
    assert len(http.calls) == 1
    assert deliveries(rt)[0]["state"] == "failed"


def test_daily_cap_waits_for_the_next_day(started):
    rt = started
    sub = link(rt, daily_cap=1)
    http = FakeHTTP()
    agent_events.send_test(rt, sub["id"], now=T0, http=http)
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)
    assert len(http.calls) == 1  # only the test
    row = next(r for r in deliveries(rt) if r["state"] == "pending")
    assert row["error"] == "daily limit reached"
    with pytest.raises(agent_events.SubscriptionError):
        agent_events.send_test(rt, sub["id"], now=T0 + timedelta(minutes=7), http=http)
    # Midnight in Amsterdam, then the next run sends it.
    next_day = datetime(2026, 9, 20, 22, 0, tzinfo=UTC)
    assert row["next_at"].replace(tzinfo=UTC) == next_day
    agent_events.run(rt, now=next_day + timedelta(minutes=1), http=http)
    assert len(http.calls) == 1  # 00:01 local is in the quiet hours
    morning = datetime(2026, 9, 21, 4, 30, tzinfo=UTC)  # 06:30 in Amsterdam
    agent_events.run(rt, now=morning, http=http)
    assert len(http.calls) == 2


def test_quiet_hours_postpone_to_the_morning(started):
    rt = started
    link(rt)
    http = FakeHTTP()
    evening = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)  # 23:00 in Amsterdam
    put(rt, "garmin", replace(activity(), start_utc=evening - timedelta(hours=1)))
    agent_events.observe(rt, now=evening)
    agent_events.run(rt, now=evening + timedelta(minutes=6), http=http)
    assert http.calls == []
    morning = datetime(2026, 9, 21, 4, 30, tzinfo=UTC)
    assert deliveries(rt)[0]["next_at"].replace(tzinfo=UTC) == morning
    assert not agent_events.due(rt, now=morning - timedelta(minutes=1))
    agent_events.run(rt, now=morning, http=http)
    assert len(http.calls) == 1


def test_quiet_window_edges(started):
    sub = {"quiet_start": "22:30", "quiet_end": "06:30"}
    local = lambda h, m: datetime(2026, 9, 20, h - 2, m, tzinfo=UTC)  # noqa: E731 (CEST)
    assert agent_events.quiet_until(started, sub, local(22, 29)) is None
    assert agent_events.quiet_until(started, sub, local(22, 30)) is not None
    assert agent_events.quiet_until(started, sub, local(6, 30)) is None
    day = {"quiet_start": "12:00", "quiet_end": "13:00"}
    assert agent_events.quiet_until(started, day, local(12, 15)) == local(13, 0)


def test_skipped_when_the_insight_arrived_meanwhile(started, monkeypatch):
    rt = started
    link(rt)
    http = FakeHTTP()
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    agent_events.emit_ready(rt, now=T0 + timedelta(minutes=6))
    monkeypatch.setattr(publication_insight, "for_activity", lambda *_a, **_k: {"text": "x"})
    agent_events.deliver(rt, now=T0 + timedelta(minutes=7), http=http)
    assert http.calls == []
    assert deliveries(rt)[0]["state"] == "skipped"


def test_standard_webhooks_signature_and_body(started):
    rt = started
    link(rt, auth="webhook", url="https://agent.example.test/hook", secret=SECRET)
    http = FakeHTTP((204, {}, ""))
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)
    (call,) = http.calls
    headers, body = call["headers"], call["body"]
    assert "Authorization" not in headers
    event = json.loads(body)
    assert headers["webhook-id"] == event["id"]
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert headers["webhook-signature"] == f"v1,{expected}"
    assert headers["webhook-timestamp"] == str(int((T0 + timedelta(minutes=6)).timestamp()))


def test_bearer_mode(started):
    rt = started
    link(rt, auth="bearer", url="http://127.0.0.1:9999/events", secret="synthetic-bearer")
    http = FakeHTTP((200, {}, "{}"))
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)
    assert http.calls[0]["headers"]["Authorization"] == "Bearer synthetic-bearer"
    assert "webhook-signature" not in http.calls[0]["headers"]


def test_validation(started):
    rt = started
    bad = [
        {"auth": "webhook", "url": "http://agent.example.test/hook"},
        {"auth": "webhook", "url": "https://user:pw@agent.example.test/hook"},
        {
            "auth": "claude_routine",
            "url": "https://evil.example.test/v1/claude_code/routines/x/fire",
        },
        {"auth": "nope", "url": "https://agent.example.test/hook"},
        {"auth": "webhook", "url": "https://agent.example.test/hook", "events": ["plan.changed"]},
        {"auth": "webhook", "url": "https://agent.example.test/hook", "daily_cap": 0},
        {"auth": "webhook", "url": "https://agent.example.test/hook", "quiet_start": "25:00"},
    ]
    for values in bad:
        with pytest.raises(agent_events.SubscriptionError):
            agent_events.save_subscription(rt, {"name": "x", **values}, secret="s")
    with pytest.raises(agent_events.SubscriptionError):  # on without a secret
        agent_events.save_subscription(
            rt, {"name": "x", "auth": "bearer", "url": "https://a.example.test", "enabled": True}
        )
    saved = agent_events.save_subscription(
        rt, {"name": "Routine", "auth": "claude_routine", "url": "trig_01SYNTHETIC"}, secret=TOKEN
    )
    assert saved["url"] == ROUTINE and saved["enabled"] is False


def test_off_link_gets_nothing(started):
    rt = started
    sub = link(rt)
    agent_events.save_subscription(rt, {"enabled": False}, sub_id=sub["id"])
    http = FakeHTTP()
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)
    assert http.calls == [] and deliveries(rt) == []
    # Pull still sees the event.
    assert agent_events.recent(rt, since_at=START.isoformat())["events"]


def test_events_pull_cursor_and_types(started):
    rt = started
    put(rt, "garmin", activity())
    agent_events.observe(rt, now=T0)
    agent_events.emit_ready(rt, now=T0 + timedelta(minutes=6))
    first = agent_events.recent(rt, since_at=START.isoformat())
    assert len(first["events"]) == 1
    assert agent_events.recent(rt, since_at=first["next_since"])["events"] == []
    with pytest.raises(ValueError):
        agent_events.recent(rt, types=["vibesynergy.test"])
    with pytest.raises(ValueError):
        agent_events.recent(rt, since_at="yesterday")


def test_secret_never_in_logs_audit_errors_or_api(started, caplog):
    rt = started
    from vibesynergy.portal.app import create_portal_app

    caplog.set_level(logging.DEBUG)
    http = FakeHTTP(
        ConnectionError(f"boom with Bearer {TOKEN} inside"),
        (500, {}, f"echo {TOKEN}"),
    )
    agent_events._requests_exchange, original = http, agent_events._requests_exchange
    try:
        api = TestClient(create_portal_app(rt), base_url="http://localhost")
        write = {"x-vibesynergy": "portal", "origin": "http://localhost"}
        created = api.post(
            "/api/events/subscriptions",
            json={
                "name": "Coach",
                "url": ROUTINE,
                "auth": "claude_routine",
                "secret": TOKEN,
                "events": ["activity.ready"],
            },
            headers=write,
        )
        assert created.status_code == 200, created.text
        sub = created.json()["data"]["subscriptions"][0]
        assert sub["has_secret"] is True
        on = api.post(
            f"/api/events/subscriptions/{sub['id']}", json={"enabled": True}, headers=write
        )
        assert on.status_code == 200
        tested = api.post(f"/api/events/subscriptions/{sub['id']}/test", headers=write)
        assert tested.status_code == 200
        assert tested.json()["data"]["delivery"]["state"] == "gave_up"
        put(rt, "garmin", activity())
        agent_events.observe(rt, now=T0)
        agent_events.run(rt, now=T0 + timedelta(minutes=6))
        listed = api.get("/api/events/subscriptions")
    finally:
        agent_events._requests_exchange = original
    assert len(http.calls) == 2
    for text in (created.text, on.text, tested.text, listed.text, caplog.text):
        assert TOKEN not in text
    for row in audits(rt, "events."):
        assert TOKEN not in json.dumps(row["detail"], default=str)
    for row in deliveries(rt):
        assert TOKEN not in (row["error"] or "")
    # Stored encrypted: no plaintext file holds it.
    store = rt.settings.secrets_dir / "agent_events"
    for path in store.iterdir():
        assert TOKEN.encode() not in path.read_bytes()


def test_scheduler_hooks(runtime):
    assert agent_events.due(runtime) is False
    assert agent_events.JOB in runtime.worker()._handlers


# --- activity.published (1 Nov 2026): the coach insight waits until the athlete goes public ---


def strava_public(private=False, source_id="2"):
    return replace(strava_copy(), source_id=source_id, extra={"private": private})


def published_link(rt, **extra):
    return link(rt, events=[agent_events.ACTIVITY_PUBLISHED], **extra)


def test_published_after_the_strava_copy_turns_public(started):
    rt = started
    published_link(rt)
    http = FakeHTTP()
    put(rt, "garmin", activity())
    put(rt, "strava", strava_public(private=True))
    agent_events.observe(rt, now=T0)
    # Ready after the debounce, but this link only wants the public activity.
    result = agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)
    assert result["event"] and result["published"] is None and http.calls == []
    assert not agent_events.due(rt, now=T0 + timedelta(minutes=7))
    # The athlete makes it public on Strava; the webhook update re-fetches it.
    put(rt, "strava", strava_public(private=False))
    agent_events.after_derive(rt)
    assert agent_events.due(rt, now=T0 + timedelta(hours=2))
    result = agent_events.run(rt, now=T0 + timedelta(hours=2), http=http)
    assert result["published"] and result["deliveries"] == {"sent": 1}
    (event,) = agent_events.recent(rt, since_at=START.isoformat(), types=["activity.published"])[
        "events"
    ]
    assert event["type"] == "activity.published" and event["version"] == 1
    assert event["data"]["final"] == ["title", "description", "gear"]
    (item,) = event["data"]["activities"]
    assert item["sources"] == ["garmin:1", "strava:2"] and item["public"] == ["strava:2"]
    assert item["public_insight_missing"] is True
    assert "Strava title" not in json.dumps(event)  # ids only: no names or descriptions
    (call,) = http.calls
    text = json.loads(call["body"])["text"]
    assert text == f"event=activity.published event_id={event['id']} activity_ids={item['id']}"
    # Once only: private again and public again, or more runs, tell nobody twice.
    put(rt, "strava", strava_public(private=True))
    put(rt, "strava", strava_public(private=False))
    for minutes in (130, 140):
        assert (
            agent_events.run(rt, now=T0 + timedelta(minutes=minutes), http=http)["published"]
            is None
        )
    assert len(http.calls) == 1


def test_published_right_away_waits_for_the_debounce_and_skips_with_an_insight(
    started, monkeypatch
):
    rt = started
    published_link(rt)
    http = FakeHTTP()
    put(rt, "strava", strava_public(private=False))  # public from the start, myWhoosh-style
    agent_events.observe(rt, now=T0)
    assert agent_events.run(rt, now=T0 + timedelta(minutes=2), http=http)["published"] is None
    monkeypatch.setattr(publication_insight, "for_activity", lambda *_a, **_k: {"text": "x"})
    assert agent_events.run(rt, now=T0 + timedelta(minutes=6), http=http)["published"]
    assert http.calls == [] and deliveries(rt)[0]["state"] == "skipped"


def test_deleted_duplicate_never_emits(started):
    rt = started
    from sqlalchemy import delete

    from vibesynergy.derive import unify
    from vibesynergy.store.schema import activity as activity_table

    published_link(rt)
    http = FakeHTTP()
    put(rt, "strava", strava_public(private=True, source_id="3"))
    agent_events.observe(rt, now=T0)
    # The athlete deletes the duplicate on Strava (webhook delete → tombstone: no row left).
    with rt.engine.begin() as conn:
        conn.execute(delete(activity_table).where(activity_table.c.source_id == "3"))
    unify.rebuild(rt.engine)
    agent_events.after_derive(rt)
    for minutes in (6, 60):
        assert (
            agent_events.run(rt, now=T0 + timedelta(minutes=minutes), http=http)["published"]
            is None
        )
    assert http.calls == []


def test_published_only_for_activities_first_seen_after_the_type_existed(runtime):
    agent_events.since(runtime, now=START - timedelta(hours=1))
    put(runtime, "strava", strava_public(private=False))
    agent_events.observe(runtime, now=T0)  # seen before activity.published started
    agent_events.since(runtime, now=T0 + timedelta(minutes=1), key="published_since")
    assert agent_events.emit_published(runtime, now=T0 + timedelta(minutes=10)) is None


def test_routine_link_moves_to_published_once(runtime):
    rt = runtime
    routine = link(rt)  # activity.ready only, made before activity.published existed
    hook = link(rt, auth="webhook", url="https://agent.example.test/hook", secret=SECRET)
    assert agent_events.migrate(rt) == [routine["id"]]
    subs = {s["id"]: s for s in agent_events.subscriptions(rt)}
    assert subs[routine["id"]]["events"] == ["activity.published"]
    assert subs[hook["id"]]["events"] == ["activity.ready"]  # other links keep their choice
    (row,) = [
        r for r in audits(rt, "events.subscription.update") if r["target"].endswith(routine["id"])
    ]
    assert row["actor"] == "vibesynergy" and row["detail"]["fields"] == ["events"]
    # A link set back to activity.ready by hand stays that way.
    agent_events.save_subscription(rt, {"events": ["activity.ready"]}, sub_id=routine["id"])
    assert agent_events.migrate(rt) == []
    assert agent_events.overview(rt)["subscriptions"][0]["events"] == ["activity.ready"]


def test_new_link_listens_for_published_by_default(started):
    sub = agent_events.save_subscription(
        started, {"name": "Coach", "auth": "claude_routine", "url": ROUTINE}, secret=TOKEN
    )
    assert sub["events"] == ["activity.published"]
    both = agent_events.save_subscription(
        started, {"events": ["activity.published", "activity.ready"]}, sub_id=sub["id"]
    )
    assert both["events"] == ["activity.ready", "activity.published"]


def test_a_missed_coach_run_fires_once_with_task_and_day(started) -> None:
    from vibesynergy.store.schema import coach_decision_log

    rt = started
    link(rt, events=[agent_events.COACH_RUN_MISSED])
    # The morning routine ran on 1-11 but not on 2-11: at 12:00 on 2-11 that run is missing.
    with rt.engine.begin() as conn:
        conn.execute(
            coach_decision_log.insert().values(
                subject_id=1,
                at=datetime(2026, 11, 1, 8, tzinfo=UTC),
                kind="note",
                author="Claude",
                entry_type="morning",
                idempotency_key="morning-2026-11-01",
                for_date=datetime(2026, 11, 1).date(),
                text="x",
                meta={},
            )
        )
    now = datetime(2026, 11, 2, 11, tzinfo=UTC)
    with rt.engine.connect() as conn:
        assert agent_events._missed_unsent(rt, conn, now)  # the job wakes up for it
    ids = agent_events.emit_missed(rt, now=now)  # 2-11, and 31-10 inside the lookback
    assert len(ids) == 2
    assert agent_events.emit_missed(rt, now=now + timedelta(minutes=5)) == []  # once
    with rt.engine.connect() as conn:
        assert not agent_events._missed_unsent(rt, conn, now + timedelta(minutes=5))
    assert {d["event_id"] for d in deliveries(rt)} == set(ids)
    event_id = ids[0]
    text = agent_events.claude_text(
        {
            "type": agent_events.COACH_RUN_MISSED,
            "id": event_id,
            "data": {"task": "morning", "date": "2026-11-02"},
        }
    )
    assert text == f"event=coach.run_missed event_id={event_id} task=morning date=2026-11-02"
    pulled = agent_events.recent(rt, since_at="2026-11-01T00:00:00Z", types=["coach.run_missed"])
    keys = {e["data"]["idempotency_key"] for e in pulled["events"]}
    assert keys == {"morning-2026-11-02", "morning-2026-10-31"}
