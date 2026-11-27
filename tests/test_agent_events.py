"""Agent events: debounce, activity.published, missed runs, the pull cursor, and the
outgoing links that were removed (#182)."""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_evidence import START, activity, put

from vibesynergy.core.secrets import SecretStore
from vibesynergy.query import agent_events
from vibesynergy.store.schema import agent_event, agent_event_delivery, audit_log

TOKEN = "sk-ant-oat01-SYNTHETIC-routine-token-never-shown"
T0 = START + timedelta(hours=1)  # 13:00 in Amsterdam


@pytest.fixture
def started(runtime):
    agent_events.since(runtime, now=START - timedelta(hours=1))
    agent_events.since(runtime, now=START - timedelta(hours=1), key="published_since")
    return runtime


@pytest.fixture
def heard(monkeypatch):
    """What the internal subscribers get, in place of the API coach runner."""
    got = []
    monkeypatch.setattr(
        agent_events, "internal_subscribers", lambda: [lambda _rt, _conn, e: got.append(e)]
    )
    return got


def strava_copy():
    return replace(activity(), source_id="2", name="Strava title", fit_id=None)


def deliveries(rt):
    with rt.engine.connect() as conn:
        return conn.execute(select(agent_event_delivery)).mappings().all()


def audits(rt, action):
    with rt.engine.connect() as conn:
        query = select(audit_log).where(audit_log.c.action == action).order_by(audit_log.c.id)
        return conn.execute(query).mappings().all()


def test_debounce_merges_garmin_and_strava_into_one_event(started, heard):
    rt = started
    put(rt, "strava", strava_copy())
    agent_events.observe(rt, now=T0)
    # Strava alone, Garmin not connected in tests: ready after the debounce only.
    assert agent_events.run(rt, now=T0 + timedelta(minutes=2))["event"] is None
    put(rt, "garmin", activity())  # the Garmin copy merges in: the debounce starts again
    agent_events.observe(rt, now=T0 + timedelta(minutes=4))
    assert not agent_events.due(rt, now=T0 + timedelta(minutes=6))
    assert agent_events.run(rt, now=T0 + timedelta(minutes=6))["event"] is None
    assert agent_events.due(rt, now=T0 + timedelta(minutes=9))
    result = agent_events.run(rt, now=T0 + timedelta(minutes=9))
    assert result["event"]
    events = agent_events.recent(rt, since_at=START.isoformat())["events"]
    assert len(events) == 1
    (event,) = events
    assert event["type"] == "activity.ready" and event["version"] == 1
    (item,) = event["data"]["activities"]
    assert item["sources"] == ["garmin:1", "strava:2"]
    assert item["public_insight_missing"] is True
    assert set(item) == {"id", "sources", "local_date", "sport", "public_insight_missing"}
    assert [e["id"] for e in heard] == [event["id"]]


def test_strava_only_waits_for_garmin_while_connected(started, monkeypatch):
    rt = started
    monkeypatch.setattr(agent_events, "_garmin_connected", lambda _rt: True)
    put(rt, "strava", strava_copy())
    agent_events.observe(rt, now=T0)
    assert agent_events.emit_ready(rt, now=T0 + timedelta(minutes=10)) is None
    assert agent_events.emit_ready(rt, now=T0 + timedelta(minutes=31)) is not None


def test_idempotent_across_runs_restarts_and_later_merges(started, heard):
    rt = started
    put(rt, "strava", strava_copy())
    agent_events.observe(rt, now=T0)
    agent_events.run(rt, now=T0 + timedelta(minutes=6))
    assert len(heard) == 1
    # The Garmin copy arrives late and changes the group id: no second event.
    put(rt, "garmin", activity())
    for minutes in (20, 30, 60):
        agent_events.run(rt, now=T0 + timedelta(minutes=minutes))
    assert len(heard) == 1
    assert len(agent_events.recent(rt, since_at=START.isoformat())["events"]) == 1


def test_several_activities_share_one_event(started):
    rt = started
    put(rt, "garmin", activity())
    put(rt, "garmin", replace(activity(), source_id="5", start_utc=START + timedelta(hours=3)))
    agent_events.run(rt, now=START + timedelta(hours=4))
    agent_events.run(rt, now=START + timedelta(hours=4, minutes=6))
    (event,) = agent_events.recent(rt, since_at=START.isoformat())["events"]
    assert [a["id"] for a in event["data"]["activities"]] == ["garmin:1", "garmin:5"]


def test_no_backfill_before_the_feature_started(runtime):
    agent_events.since(runtime, now=START + timedelta(minutes=30))
    put(runtime, "garmin", activity())  # started before events existed
    agent_events.observe(runtime, now=T0)
    assert agent_events.emit_ready(runtime, now=T0 + timedelta(hours=1)) is None


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


def test_scheduler_hooks(runtime):
    assert agent_events.JOB in runtime.worker()._handlers
    assert agent_events.due(runtime) is True  # once: the stored links still have to go
    agent_events.run(runtime)
    assert agent_events.due(runtime) is False


# --- activity.published (1 Nov 2026): the coach insight waits until the athlete goes public ---


def strava_public(private=False, source_id="2"):
    return replace(strava_copy(), source_id=source_id, extra={"private": private})


def test_published_after_the_strava_copy_turns_public(started, heard):
    rt = started
    put(rt, "garmin", activity())
    put(rt, "strava", strava_public(private=True))
    agent_events.observe(rt, now=T0)
    result = agent_events.run(rt, now=T0 + timedelta(minutes=6))
    assert result["event"] and result["published"] is None
    assert not agent_events.due(rt, now=T0 + timedelta(minutes=7))
    # The athlete makes it public on Strava; the webhook update re-fetches it.
    put(rt, "strava", strava_public(private=False))
    agent_events.after_derive(rt)
    assert agent_events.due(rt, now=T0 + timedelta(hours=2))
    result = agent_events.run(rt, now=T0 + timedelta(hours=2))
    assert result["published"]
    (event,) = agent_events.recent(rt, since_at=START.isoformat(), types=["activity.published"])[
        "events"
    ]
    assert event["type"] == "activity.published" and event["version"] == 1
    assert event["data"]["final"] == ["title", "description", "gear"]
    (item,) = event["data"]["activities"]
    assert item["sources"] == ["garmin:1", "strava:2"] and item["public"] == ["strava:2"]
    assert item["public_insight_missing"] is True
    assert "Strava title" not in json.dumps(event)  # ids only: no names or descriptions
    assert [e["type"] for e in heard] == ["activity.ready", "activity.published"]
    # Once only: private again and public again, or more runs, tell nobody twice.
    put(rt, "strava", strava_public(private=True))
    put(rt, "strava", strava_public(private=False))
    for minutes in (130, 140):
        assert agent_events.run(rt, now=T0 + timedelta(minutes=minutes))["published"] is None
    assert len(heard) == 2


def test_published_right_away_waits_for_the_debounce(started):
    rt = started
    put(rt, "strava", strava_public(private=False))  # public from the start, myWhoosh-style
    agent_events.observe(rt, now=T0)
    assert agent_events.run(rt, now=T0 + timedelta(minutes=2))["published"] is None
    assert agent_events.run(rt, now=T0 + timedelta(minutes=6))["published"]


def test_deleted_duplicate_never_emits(started):
    rt = started
    from sqlalchemy import delete

    from vibesynergy.derive import unify
    from vibesynergy.store.schema import activity as activity_table

    put(rt, "strava", strava_public(private=True, source_id="3"))
    agent_events.observe(rt, now=T0)
    # The athlete deletes the duplicate on Strava (webhook delete → tombstone: no row left).
    with rt.engine.begin() as conn:
        conn.execute(delete(activity_table).where(activity_table.c.source_id == "3"))
    unify.rebuild(rt.engine)
    agent_events.after_derive(rt)
    for minutes in (6, 60):
        assert agent_events.run(rt, now=T0 + timedelta(minutes=minutes))["published"] is None


def test_published_only_for_activities_first_seen_after_the_type_existed(runtime):
    agent_events.since(runtime, now=START - timedelta(hours=1))
    put(runtime, "strava", strava_public(private=False))
    agent_events.observe(runtime, now=T0)  # seen before activity.published started
    agent_events.since(runtime, now=T0 + timedelta(minutes=1), key="published_since")
    assert agent_events.emit_published(runtime, now=T0 + timedelta(minutes=10)) is None


def test_a_missed_coach_run_fires_once_with_task_and_day(started, heard) -> None:
    from vibesynergy.store.schema import coach_decision_log

    rt = started
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
    assert [e["id"] for e in heard] == ids  # the API coach runner's catch-up hears it
    pulled = agent_events.recent(rt, since_at="2026-11-01T00:00:00Z", types=["coach.run_missed"])
    keys = {e["data"]["idempotency_key"] for e in pulled["events"]}
    assert keys == {"morning-2026-11-02", "morning-2026-10-31"}


# --- the outgoing links are gone (#182) -----------------------------------------------------


def test_events_are_emitted_with_no_link_and_nothing_is_queued_to_send(started, heard):
    """No outgoing link exists any more: every type is still stored, pulled and heard
    inside VibeSynergy, and no delivery row is written."""
    rt = started
    assert "subscriptions" not in SecretStore(rt.settings.secrets_dir, "agent_events").documents()
    put(rt, "garmin", activity())
    put(rt, "strava", strava_public(private=False))
    agent_events.observe(rt, now=T0)
    result = agent_events.run(rt, now=T0 + timedelta(minutes=6))
    assert result["event"] and result["published"]
    pulled = agent_events.recent(rt, since_at=START.isoformat())["events"]
    # Same run, same time: the pull orders them by id.
    assert sorted(e["type"] for e in pulled) == ["activity.published", "activity.ready"]
    assert [e["id"] for e in heard] == [result["event"], result["published"]]
    assert deliveries(rt) == []


def _legacy_links(rt):
    """What an install had before #182: two links with secrets, a sent and a waiting
    delivery, and the event they belong to."""
    store = SecretStore(rt.settings.secrets_dir, "agent_events")
    store.set(
        "subscriptions",
        {
            "sub_1": {"id": "sub_1", "name": "Coach na training", "secret": TOKEN},
            "sub_2": {"id": "sub_2", "name": "Webhook", "secret": "whsec_SYNTHETIC"},
        },
    )
    old = datetime(2026, 11, 6, 9, tzinfo=UTC)
    with rt.engine.begin() as conn:
        conn.execute(
            agent_event.insert().values(
                id="evt_old", type="activity.published", version=1, occurred_at=old, data={}
            )
        )
        for sub_id, state in (("sub_1", "sent"), ("sub_2", "pending")):
            conn.execute(
                agent_event_delivery.insert().values(
                    event_id="evt_old",
                    subscription_id=sub_id,
                    state=state,
                    attempts=1,
                    created_at=old,
                    next_at=old,
                    sent_at=old if state == "sent" else None,
                    http_status=200 if state == "sent" else None,
                )
            )
    return store


def test_upgrade_drops_the_stored_links_and_keeps_the_history(started, caplog):
    rt = started
    store = _legacy_links(rt)
    before = agent_events.since(rt)
    with caplog.at_level(logging.WARNING, logger="vibesynergy.query.agent_events"):
        assert agent_events.due(rt) is True
        agent_events.run(rt, now=datetime(2026, 12, 20, 12, tzinfo=UTC))
        assert agent_events.retire_links(rt) == 0  # once only
        agent_events.run(rt, now=datetime(2026, 12, 20, 12, 5, tzinfo=UTC))
    assert sum("outgoing coach links are gone" in r.getMessage() for r in caplog.records) == 1
    assert TOKEN not in caplog.text
    # The links and their secrets are gone; when events started is kept.
    assert "subscriptions" not in store.documents()
    assert agent_events.since(rt) == before
    for path in (rt.settings.secrets_dir / "agent_events").iterdir():
        assert b"whsec_SYNTHETIC" not in path.read_bytes()
    # The history stays, also past KEEP; what was still waiting is closed, not sent.
    rows = {r["subscription_id"]: r for r in deliveries(rt)}
    assert rows["sub_1"]["state"] == "sent" and rows["sub_1"]["http_status"] == 200
    assert rows["sub_2"]["state"] == "skipped"
    with rt.engine.connect() as conn:
        assert conn.scalar(select(agent_event.c.id).where(agent_event.c.id == "evt_old"))
    removed = audits(rt, "events.subscription.delete")
    assert sorted(r["detail"]["name"] for r in removed) == ["Coach na training", "Webhook"]
    assert all(r["actor"] == "vibesynergy" for r in removed)
    assert TOKEN not in json.dumps([dict(r["detail"]) for r in removed])


def test_the_portal_has_no_link_api_any_more(runtime):
    from dataclasses import replace as with_values

    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.runtime import Runtime

    host = "server.example.ts.net"
    rt = Runtime.open(with_values(runtime.settings, server={"public_url": f"https://{host}"}))
    client = TestClient(create_portal_app(rt), base_url=f"https://{host}:8447")
    try:
        assert client.get("/api/events/subscriptions").status_code == 404
    finally:
        rt.close()
