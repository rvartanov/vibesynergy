"""Coach writes to intervals.icu: mocked HTTP, audit snapshots, no live calls."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from vibesynergy.core.timeutil import Window
from vibesynergy.model import PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import icu_plan, plan
from vibesynergy.query.coach_workouts import coach_feed
from vibesynergy.sources.intervals_icu.client import (
    IntervalsIcuClient,
    IntervalsIcuError,
)
from vibesynergy.sources.intervals_icu.mappers import map_events
from vibesynergy.store.schema import audit_log, icu_event

KEY = "icu-secret-key-9f3a"
ATHLETE = "i000001"
DAY = date(2026, 11, 2)
UPDATED = "2026-10-30T08:15:00.000Z"
EVENT = {
    "id": 1001,
    "external_id": "2026-11-02:Run",
    "category": "WORKOUT",
    "type": "Run",
    "start_date_local": "2026-11-02T09:00:00",
    "end_date_local": "2026-11-02T10:00:00",
    "name": "Easy run",
    "description": "- 40m Z2",
    "updated": UPDATED,
}


class Api:
    def __init__(self, event: dict | None = None) -> None:
        self.events = {} if event is None else {str(event["id"]): dict(event)}
        self.calls: list[tuple] = []
        self.next_id = 2000

    def exchange(self, method, url, params, headers, body):
        self.calls.append((method, url, body))
        assert "bulk" not in url and "upsert" not in url
        assert KEY not in url
        if method == "GET" and url.rstrip("/").endswith("/events"):
            return 200, {}, json.dumps(list(self.events.values())).encode()
        if method == "GET":
            eid = url.rstrip("/").split("/")[-1]
            if eid not in self.events:
                return 404, {}, b"missing"
            return 200, {}, json.dumps(self.events[eid]).encode()
        if method == "PUT":
            eid = url.rstrip("/").split("/")[-1]
            payload = json.loads(body)
            current = dict(self.events[eid])
            current.update(payload)
            current["id"] = int(eid) if eid.isdigit() else eid
            current["updated"] = "2026-10-30T09:00:00.000Z"
            self.events[eid] = current
            return 200, {}, json.dumps(current).encode()
        if method == "DELETE":
            eid = url.rstrip("/").split("/")[-1]
            self.events.pop(eid, None)
            return 200, {}, b""
        if method == "POST":
            payload = json.loads(body)
            assert isinstance(payload, dict)
            payload["id"] = self.next_id
            payload["updated"] = "2026-10-30T09:00:00.000Z"
            self.events[str(self.next_id)] = payload
            self.next_id += 1
            return 200, {}, json.dumps(payload).encode()
        raise AssertionError(method + " " + url)


def _client(api: Api, **kwargs) -> IntervalsIcuClient:
    return IntervalsIcuClient(
        KEY,
        ATHLETE,
        exchange=api.exchange,
        interval=0,
        write_interval=0,
        **kwargs,
    )


def _seed(runtime, event: dict = EVENT) -> str:
    runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        payload=json.dumps([event]).encode(),
        content_type="application/json",
    )
    raw_id = next(runtime.raw.current_ids("intervals_icu", "events"))
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(raw_id))),
            source="intervals_icu",
            raw_item_id=raw_id,
        )
        row = conn.execute(select(icu_event).where(icu_event.c.id == str(event["id"]))).one()
    return icu_plan.version_token(row.updated, row.raw)


def _audit(runtime, audit_id: int) -> dict:
    with runtime.engine.connect() as conn:
        return (
            conn.execute(select(audit_log).where(audit_log.c.id == audit_id))
            .one()
            ._mapping["detail"]
        )


def test_update_uses_the_event_id_and_refuses_a_stale_version(runtime) -> None:
    version = _seed(runtime)
    api = Api(EVENT)
    client = _client(api)
    with pytest.raises(icu_plan.IcuWriteError):
        icu_plan.update_event(
            runtime.engine,
            client,
            reason="Make it shorter",
            expected_version="stale",
            external_id="2026-11-02:Run",
            changes={"name": "Shorter"},
        )
    assert api.calls == []

    api.events["1001"]["updated"] = "2026-10-30T12:00:00.000Z"
    with pytest.raises(icu_plan.IcuWriteError):
        icu_plan.update_event(
            runtime.engine,
            client,
            reason="Make it shorter",
            expected_version=version,
            external_id="2026-11-02:Run",
            changes={"name": "Shorter"},
        )
    assert [call[0] for call in api.calls] == ["GET"]

    api.events["1001"]["updated"] = UPDATED
    saved = icu_plan.update_event(
        runtime.engine,
        client,
        reason="Make it shorter",
        expected_version=version,
        external_id="2026-11-02:Run",
        changes={"name": "Shorter"},
    )
    put = next(call for call in api.calls if call[0] == "PUT")
    assert put[1].endswith(f"/athlete/{ATHLETE}/events/1001")
    assert json.loads(put[2])["name"] == "Shorter"
    detail = _audit(runtime, saved["audit_id"])
    assert detail["before"]["name"] == "Easy run"
    assert detail["after"]["name"] == "Shorter"
    assert detail["reason"] == "Make it shorter"
    assert detail["revertable"] is True
    feed = coach_feed(runtime)
    assert any(
        row["entry_type"] == "plan_change" and row["text"] == "Make it shorter" for row in feed
    )


def test_create_posts_one_event_and_revert_deletes_it(runtime) -> None:
    _seed(runtime)
    api = Api(EVENT)
    client = _client(api)
    with pytest.raises(icu_plan.IcuWriteError):
        icu_plan.create_event(
            runtime.engine,
            client,
            reason="  ",
            category="WORKOUT",
            start_date=DAY,
            name="Strides",
        )
    assert api.calls == []
    created = icu_plan.create_event(
        runtime.engine,
        client,
        reason="Add strides",
        category="WORKOUT",
        start_date=DAY,
        name="Strides",
        sport="Run",
        external_id="2026-11-02:Run:strides",
    )
    post = api.calls[-1]
    assert post[0] == "POST"
    assert post[1].endswith(f"/athlete/{ATHLETE}/events")
    assert isinstance(json.loads(post[2]), dict)
    undone = icu_plan.revert(
        runtime.engine, client, audit_id=created["audit_id"], reason="Take strides off"
    )
    deleted = any(
        call[0] == "DELETE" and call[1].endswith(f"/athlete/{ATHLETE}/events/{created['event_id']}")
        for call in api.calls
    )
    assert deleted
    assert undone["action"] == "icu.revert"
    with runtime.engine.connect() as conn:
        left = conn.execute(
            select(icu_event.c.id).where(icu_event.c.id == created["event_id"])
        ).first()
    assert left is None


def test_revert_of_a_delete_posts_the_before_snapshot(runtime) -> None:
    version = _seed(runtime)
    api = Api(EVENT)
    client = _client(api)
    deleted = icu_plan.delete_event(
        runtime.engine,
        client,
        reason="Drop the easy run",
        expected_version=version,
        event_id="1001",
    )
    assert _audit(runtime, deleted["audit_id"])["before"]["external_id"] == "2026-11-02:Run"
    assert _audit(runtime, deleted["audit_id"])["after"] is None
    restored = icu_plan.revert(
        runtime.engine, client, audit_id=deleted["audit_id"], reason="Put the easy run back"
    )
    post = next(call for call in api.calls if call[0] == "POST")
    body = json.loads(post[2])
    assert body["external_id"] == "2026-11-02:Run"
    assert "id" not in body
    assert "bulk" not in post[1]
    with runtime.engine.connect() as conn:
        row = conn.execute(select(icu_event).where(icu_event.c.id == restored["event_id"])).one()
    assert row.external_id == "2026-11-02:Run"


def test_move_shifts_the_end_and_revert_puts_the_old_day_back(runtime) -> None:
    version = _seed(runtime)
    api = Api(EVENT)
    client = _client(api)
    moved = icu_plan.move_event(
        runtime.engine,
        client,
        reason="Move it a day",
        expected_version=version,
        start_date=DAY + timedelta(days=1),
        external_id="2026-11-02:Run",
    )
    put = next(call for call in api.calls if call[0] == "PUT")
    body = json.loads(put[2])
    assert body["start_date_local"].startswith("2026-11-03T09:00:00")
    assert body["end_date_local"].startswith("2026-11-03T10:00:00")
    icu_plan.revert(runtime.engine, client, audit_id=moved["audit_id"], reason="Move it back")
    last = [call for call in api.calls if call[0] == "PUT"][-1]
    restored = json.loads(last[2])
    assert restored["start_date_local"].startswith("2026-11-02T09:00:00")


def test_writes_stay_about_a_second_apart() -> None:
    api = Api()
    slept: list[float] = []
    clock = {"now": 1000.0}
    client = IntervalsIcuClient(
        KEY,
        ATHLETE,
        exchange=api.exchange,
        interval=0,
        write_interval=1.0,
        sleep=slept.append,
        clock=lambda: clock["now"],
    )
    one = {"category": "NOTE", "name": "One", "start_date_local": "2026-11-02T00:00:00"}
    two = {"category": "NOTE", "name": "Two", "start_date_local": "2026-11-03T00:00:00"}
    client.create_event(one)
    client.create_event(two)
    assert slept == [1.0]


def test_write_errors_do_not_log_the_key(caplog) -> None:
    def exchange(method, url, params, headers, body):
        return 500, {}, f"nope {KEY} {headers['Authorization']}".encode()

    client = IntervalsIcuClient(KEY, ATHLETE, exchange=exchange, interval=0, write_interval=0)
    with caplog.at_level(logging.ERROR), pytest.raises(IntervalsIcuError) as caught:
        client.create_event({"category": "NOTE", "name": "x", "start_date_local": "2026-11-02"})
    logging.getLogger("vibesynergy.sync.engine").error("leaked %s", KEY)
    assert KEY not in str(caught.value)
    assert KEY not in caplog.text


def _garmin(runtime) -> None:
    stored = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="2026-11",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem("coach-1", DAY, "plan_workout", title="Garmin Easy", sport="running"),
                PlannedItem("push-1", DAY, "workout", title="Easy run", sport="running"),
            ],
            source="garmin",
            raw_item_id=stored.id,
        )


def test_pushed_copy_is_folded_and_another_garmin_workout_is_not_in_plan(runtime) -> None:
    _seed(runtime)
    _garmin(runtime)
    data = plan.plan(runtime.engine, Window(DAY, DAY), DAY, include_sync_status=False)
    assert not [item for item in data["items"] if item["source"] == "garmin"]
    (workout,) = data["today"]
    assert workout["source"] == "intervals_icu"
    assert [copy["source_id"] for copy in workout["garmin_copies"]] == ["push-1"]
    (garmin,) = data["not_in_plan"]
    assert garmin["source_id"] == "coach-1"
    assert garmin["in_plan"] is False
    assert "flag" not in garmin and "duplicate" not in garmin


def test_plan_write_scope_is_separate(runtime) -> None:
    import anyio
    from mcp import Client

    from vibesynergy.mcp.identity import reset_connection, set_connection
    from vibesynergy.mcp.server import build_server

    arguments = {
        "reason": "Add strides",
        "category": "WORKOUT",
        "start_date": "2026-11-02",
        "name": "Strides",
    }

    def call(scopes: frozenset[str]):
        async def go():
            tokens = set_connection("reader", scopes=scopes, label="Reader")
            try:
                async with Client(build_server(runtime)) as client:
                    return await client.call_tool("vibesynergy_icu_create_event", arguments)
            finally:
                reset_connection(tokens)

        return anyio.run(go)

    refused = call(frozenset({"plan.propose"}))
    assert refused.is_error
    assert "isn't allowed" in refused.content[0].text
    missing = call(frozenset({"plan.write"}))
    assert missing.is_error
    assert "not set up" in missing.content[0].text
    assert KEY not in missing.content[0].text


def test_plan_snapshot_row_names_the_coach_app(runtime) -> None:
    from vibesynergy.mcp.identity import reset_connection, set_connection

    _seed(runtime)
    tokens = set_connection("pat:7")
    try:
        created = icu_plan.create_event(
            runtime.engine,
            _client(Api(EVENT)),
            reason="Add strides",
            category="WORKOUT",
            start_date=DAY,
            name="Strides",
            sport="Run",
            external_id="2026-11-02:Run:strides",
        )
    finally:
        reset_connection(tokens)
    with runtime.engine.connect() as conn:
        row = conn.execute(select(audit_log).where(audit_log.c.id == created["audit_id"])).one()
    assert row.actor == icu_plan.PLAN_ACTOR == "plan"
    assert row.client_id == "pat:7"
    assert row.author == "client:pat:7"


def test_create_sends_weekly_targets_and_rejects_unknown_ones(runtime) -> None:
    _seed(runtime)
    api = Api(EVENT)
    client = _client(api)
    for bad in ({"pace_target": 5}, {"time_target": 0}):
        with pytest.raises(icu_plan.IcuWriteError):
            icu_plan.create_event(
                runtime.engine,
                client,
                reason="Week target",
                category="TARGET",
                start_date=DAY,
                name="Ride hours",
                sport="Ride",
                targets=bad,
            )
    assert api.calls == []
    icu_plan.create_event(
        runtime.engine,
        client,
        reason="Week target",
        category="TARGET",
        start_date=DAY,
        name="Ride hours",
        sport="Ride",
        for_week=True,
        targets={"time_target": 11700, "load_target": 180},
    )
    body = json.loads(api.calls[-1][2])
    assert body["time_target"] == 11700
    assert body["load_target"] == 180
    assert body["category"] == "TARGET"


def test_list_events_gives_handles_that_the_write_tools_accept(runtime) -> None:
    _seed(runtime)
    events = icu_plan.list_events(runtime.engine, DAY, DAY + timedelta(days=7))
    found = next(e for e in events if e["external_id"] == "2026-11-02:Run")
    assert found["event_id"] == "1001"
    assert found["date"] == DAY.isoformat()
    assert icu_plan.list_events(runtime.engine, DAY, DAY, "NOTE") == []
    api = Api(EVENT)
    client = _client(api)
    with pytest.raises(icu_plan.IcuWriteError, match="changed since"):
        icu_plan.move_event(
            runtime.engine,
            client,
            reason="Move it a day",
            expected_version="2020-01-01T00:00:00+00:00",
            start_date=DAY + timedelta(days=1),
            event_id=found["event_id"],
        )
    icu_plan.move_event(
        runtime.engine,
        client,
        reason="Move it a day",
        expected_version=found["version"],
        start_date=DAY + timedelta(days=1),
        event_id=found["event_id"],
    )
    assert any(call[0] == "PUT" for call in api.calls)


def test_missing_session_points_to_the_read_tool(runtime) -> None:
    _seed(runtime)
    client = _client(Api(EVENT))
    with pytest.raises(icu_plan.IcuWriteError, match="vibesynergy_icu_events"):
        icu_plan.move_event(
            runtime.engine,
            client,
            reason="Move",
            expected_version="x",
            start_date=DAY,
            external_id="lake27-2026-11-07-brick",
        )
