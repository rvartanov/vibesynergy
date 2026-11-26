"""Races and events in VibeSynergy (#91): add, change, delete and undo through the MCP and
the portal, one validation, dedupe against Garmin and intervals.icu, the copy to
intervals.icu, and a change made there never coming back."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_coach_roles import FULL, OWN_AGENT, _call, _data
from test_coach_roles import rt as rt
from test_mcp_today import call

from vibesynergy.coach_runner.tools import COACH_SCOPES as RUNNER_SCOPES
from vibesynergy.coach_runner.tools import ToolBox
from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.model import CalendarSpan, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import plan, plan_proposals, race_events, race_mirror
from vibesynergy.sources.intervals_icu.mappers import map_events
from vibesynergy.store.schema import audit_log, icu_event, race

HEADERS = {"x-vibesynergy": "portal"}


def _today(runtime) -> date:
    return today_in(runtime.settings.timezone)


def _row(runtime, ident: str) -> dict[str, Any]:
    with runtime.engine.connect() as conn:
        return dict(conn.execute(select(race).where(race.c.id == ident)).mappings().one())


def _garmin(runtime, day: date, title: str, source_id: str = "event:g1") -> None:
    stored = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key=f"events-{source_id}",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(source_id, day, "event", title=title, is_race=True),
                CalendarSpan(day, day),
            ],
            source="garmin",
            raw_item_id=stored.id,
        )


def _icu(runtime, events: list[dict[str, Any]]) -> None:
    stored = runtime.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        payload=json.dumps(events).encode(),
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(runtime.raw.load(stored.id))),
            source="intervals_icu",
            raw_item_id=stored.id,
        )


class FakeIcu:
    """intervals.icu's calendar API, in memory."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.n = 500

    def _stamp(self) -> str:
        self.n += 1
        return (datetime(2026, 11, 1, tzinfo=UTC) + timedelta(seconds=self.n)).isoformat()

    def create_event(self, body: dict[str, Any]) -> dict[str, Any]:
        self.n += 1
        ident = str(self.n)
        self.events[ident] = {**body, "id": ident, "updated": self._stamp()}
        self.calls.append(("create", ident))
        return dict(self.events[ident])

    def get_event(self, ident: str) -> dict[str, Any]:
        return dict(self.events[str(ident)])

    def update_event(self, ident: str, body: dict[str, Any]) -> dict[str, Any]:
        self.events[str(ident)] = {**body, "id": str(ident), "updated": self._stamp()}
        self.calls.append(("update", str(ident)))
        return dict(self.events[str(ident)])

    def delete_event(self, ident: str) -> None:
        self.events.pop(str(ident))
        self.calls.append(("delete", str(ident)))


# Validation: one function for every write path -----------------------------------------


def test_validate_checks_and_cleans_every_field() -> None:
    row = race_events.validate(
        {
            "name": "  City marathon ",
            "date": "2027-04-11",
            "sport": "run",
            "distance_km": "42,195",
            "priority": "a",
            "start_time": "9:30",
            "goal_time": "03:30:00",
            "url": "https://example.test/race",
        }
    )
    assert row["name"] == "City marathon" and row["date"] == date(2027, 4, 11)
    assert row["distance_m"] == 42195.0 and row["priority"] == "A"
    assert row["start_time"] == "09:30" and row["goal_time"] == "3:30:00"
    assert row["kind"] == "race" and row["end_date"] is None
    for bad, says in (
        ({"name": "", "date": "2027-04-11", "sport": "run"}, "a name is required"),
        ({"name": "X", "date": "11-4-2027", "sport": "run"}, "dates look like"),
        ({"name": "X", "date": "2027-04-11"}, "a race needs a sport"),
        ({"name": "X", "date": "2027-04-11", "sport": "row"}, "sport must be one of"),
        ({"name": "X", "date": "2027-04-11", "sport": "run", "priority": "D"}, "priority"),
        ({"name": "X", "date": "2027-04-11", "sport": "run", "goal_time": "3:30"}, "goal time"),
        ({"name": "X", "date": "2027-04-11", "sport": "run", "start_time": "25:00"}, "start"),
        ({"name": "X", "date": "2027-04-11", "sport": "run", "url": "example.test"}, "https"),
        ({"name": "X", "date": "2027-04-11", "sport": "run", "distance_m": -5}, "distance"),
        ({"name": "X", "date": "2027-04-11", "end_date": "2027-04-01", "sport": "run"}, "end"),
        ({"name": "X", "date": "2027-04-11", "kind": "camp", "priority": "A"}, "only a race"),
        ({"name": "X", "date": "2027-04-11", "kind": "party"}, "kind must be one of"),
    ):
        with pytest.raises(race_events.RaceError, match=says):
            race_events.validate(bad)
    # An event needs no sport; a change keeps what it does not set and "" clears.
    camp = race_events.validate(
        {"kind": "camp", "name": "Camp", "date": "2027-03-01", "end_date": "2027-03-07"}
    )
    assert camp["sport"] is None and camp["end_date"] == date(2027, 3, 7)
    changed = race_events.validate({"location": "", "priority": "B"}, {**row, "location": "R"})
    assert changed["location"] is None and changed["priority"] == "B"
    assert changed["name"] == "City marathon"


def test_the_portal_and_the_mcp_refuse_the_same_input_the_same_way(rt) -> None:
    bad = {"name": "Halve", "date": "2027-03-14", "sport": "run", "goal_time": "1:45"}
    with pytest.raises(ValueError, match="goal time looks like 3:30:00"):
        call(rt, "vibesynergy_race_add", reason="test", **bad)
    client = TestClient(create_portal_app(rt), base_url="http://localhost")
    answer = client.post("/api/races", json=bad, headers=HEADERS)
    assert answer.status_code == 400
    assert "goal time looks like 3:30:00" in answer.json()["error"]
    with rt.engine.connect() as conn:
        assert conn.execute(select(race)).first() is None


# MCP: add, change, delete and undo --------------------------------------------------------


def test_mcp_adds_changes_deletes_and_reverts_a_race(rt) -> None:
    day = _today(rt) + timedelta(days=60)
    with pytest.raises(ValueError, match="reason is required"):
        call(rt, "vibesynergy_race_add", reason=" ", name="X", date=day.isoformat(), sport="run")
    added = call(
        rt,
        "vibesynergy_race_add",
        reason="De atleet schrijft zich in",
        name="Zevenheuvelenloop",
        date=day.isoformat(),
        sport="run",
        distance_m=15000,
        priority="B",
        location="Nijmegen",
    )["data"]
    assert added["written"] is True
    ident = added["race"]["id"]
    assert added["race"]["priority"] == "B" and added["race"]["distance_m"] == 15000
    listed = call(rt, "vibesynergy_coach_state")["data"]["races"]
    assert [r["source_id"] for r in listed] == [ident]
    changed = call(
        rt,
        "vibesynergy_race_update",
        reason="Toch A",
        race_id=ident,
        priority="A",
        goal_time="1:05:00",
        location="",
    )["data"]
    assert changed["race"]["priority"] == "A" and changed["race"]["location"] is None
    assert _row(rt, ident)["goal_time"] == "1:05:00"
    gone = call(rt, "vibesynergy_race_delete", reason="Afgemeld", race_id=ident)["data"]
    assert call(rt, "vibesynergy_coach_state")["data"]["races"] == []
    assert _row(rt, ident)["deleted_at"] is not None  # the row stays, so it can come back
    with pytest.raises(ValueError, match="there is no race or event"):
        call(rt, "vibesynergy_race_update", reason="x", race_id=ident, name="Y")
    back = call(rt, "vibesynergy_race_revert", reason="Toch niet", audit_id=gone["audit_id"])
    assert back["data"]["race"]["priority"] == "A"
    assert [r["source_id"] for r in call(rt, "vibesynergy_coach_state")["data"]["races"]] == [ident]
    # Undo the change too: its earlier values come back. An older change cannot be undone
    # over a later one.
    with pytest.raises(ValueError, match="undo the later change first"):
        call(rt, "vibesynergy_race_revert", reason="x", audit_id=added["audit_id"])
    call(rt, "vibesynergy_race_revert", reason="Terug naar B", audit_id=changed["audit_id"])
    row = _row(rt, ident)
    assert (row["priority"], row["location"], row["goal_time"]) == ("B", "Nijmegen", None)
    # Every write is on the audit log with its reason, and on the coach feed.
    with rt.engine.connect() as conn:
        actions = [
            r.action
            for r in conn.execute(
                select(audit_log.c.action).where(audit_log.c.actor == race_events.ACTOR)
            )
        ]
    assert actions == ["race.add", "race.update", "race.delete", "race.revert", "race.revert"]
    history = call(rt, "vibesynergy_races")["data"]["changes"]
    assert history[0]["reason"] == "Terug naar B"


def test_an_event_is_added_with_a_kind_and_shown_in_the_calendar(rt) -> None:
    first = _today(rt) + timedelta(days=10)
    out = call(
        rt,
        "vibesynergy_event_add",
        reason="Zomervakantie",
        kind="holiday",
        name="Vakantie Frankrijk",
        date=first.isoformat(),
        end_date=(first + timedelta(days=13)).isoformat(),
    )["data"]
    assert out["race"]["kind"] == "holiday" and out["race"]["priority"] is None
    with pytest.raises(ValueError, match="group_training"):
        call(rt, "vibesynergy_event_add", reason="x", kind="race", name="R", date=first.isoformat())
    state = call(rt, "vibesynergy_coach_state")["data"]
    assert state["races"] == [] and [e["name"] for e in state["events"]] == ["Vakantie Frankrijk"]
    # The calendar shows it as an event (no race) over its days; the race phase ignores it.
    shown = plan.plan(
        rt.engine, Window(first + timedelta(days=3), first + timedelta(days=20)), first
    )
    events = [i for i in shown["items"] if i["kind"] == "event"]
    assert [(e["title"], e["is_race"]) for e in events] == [("Vakantie Frankrijk", False)]
    assert events[0]["extra"]["end"] == (first + timedelta(days=14)).isoformat()
    assert plan.race_phase(rt.engine, first)["next_race"] is None
    # Changed and deleted with the race tools.
    call(rt, "vibesynergy_race_update", reason="Korter", race_id=out["race"]["id"], end_date="")
    assert _row(rt, out["race"]["id"])["end_date"] is None
    call(rt, "vibesynergy_race_delete", reason="Gaat niet door", race_id=out["race"]["id"])
    assert call(rt, "vibesynergy_coach_state")["data"]["events"] == []


# Dedupe -----------------------------------------------------------------------------------


def test_a_race_already_there_is_refused_and_garmin_and_intervals_copies_are_linked(
    rt,
) -> None:
    day = _today(rt) + timedelta(days=40)
    _garmin(rt, day, "Groet uit Schoorl Run")
    _icu(
        rt,
        [
            {
                "id": 31,
                "category": "RACE_B",
                "type": "Run",
                "start_date_local": f"{day.isoformat()}T00:00:00",
                "name": "Groet uit Schoorl run",
                "updated": "2026-11-01T08:00:00Z",
            }
        ],
    )
    out = race_events.add(
        rt.engine,
        {"name": "Groet uit Schoorl Run", "date": day, "sport": "run", "priority": "B"},
        reason="Ingeschreven",
        author="test",
    )
    assert out["linked"] == {"garmin": "event:g1", "intervals": "31"}
    # Same date, name and sport again: refused, through the MCP and the portal alike.
    with pytest.raises(ValueError, match="already in VibeSynergy"):
        call(
            rt,
            "vibesynergy_race_add",
            reason="nog eens",
            name="groet uit schoorl RUN",
            date=day.isoformat(),
            sport="run",
        )
    answer = TestClient(create_portal_app(rt), base_url="http://localhost").post(
        "/api/races",
        json={"name": "Groet uit Schoorl Run", "date": day.isoformat(), "sport": "run"},
        headers=HEADERS,
    )
    assert answer.status_code == 400 and answer.json()["code"] == "duplicate"
    # Another sport on that day is another race; its copies stay with the first one.
    other = race_events.add(
        rt.engine,
        {"name": "Groet uit Schoorl Run", "date": day, "sport": "ride"},
        reason="Ook de fietstocht",
        author="test",
    )
    assert other["linked"] == {}
    # The calendar shows the race once: the Garmin copy folds into it.
    shown = plan.plan(rt.engine, Window(day, day), _today(rt))
    events = [i for i in shown["items"] if i["kind"] == "event"]
    assert [e["source"] for e in events] == ["vibesynergy", "vibesynergy"]
    assert events[0]["extra"]["garmin"]["source_id"] == "event:g1"
    # The intervals.icu race is the athlete's own: no second copy, and it is left alone.
    client = FakeIcu()
    copied = race_mirror.mirror(rt.engine, client, today=_today(rt))
    assert copied["not_ours"] == [out["race"]["id"]]
    assert copied["created"] == [other["race"]["id"]]
    assert all(ident != "31" for _what, ident in client.calls)


# The copy to intervals.icu -------------------------------------------------------------------


def test_the_mirror_copies_changes_and_removes_and_intervals_edits_never_come_back(
    rt,
) -> None:
    _icu(rt, [])  # intervals.icu connected, nothing on its calendar yet
    today = _today(rt)
    day = today + timedelta(days=30)
    client = FakeIcu()
    added = race_events.add(
        rt.engine,
        {
            "name": "Autumn marathon",
            "date": day,
            "sport": "run",
            "priority": "A",
            "distance_m": 42195,
            "goal_time": "3:45:00",
        },
        reason="Doel",
        author="test",
    )
    ident = added["race"]["id"]
    past = race_events.add(
        rt.engine,
        {"name": "Oude race", "date": today - timedelta(days=5), "sport": "run"},
        reason="Geschiedenis",
        author="test",
    )
    out = race_mirror.mirror(rt.engine, client, today=today)
    assert out["created"] == [ident] and out["errors"] == []
    assert past["race"]["id"] not in out["created"]  # over: never copied
    (copy,) = client.events.values()
    assert copy["category"] == "RACE_A" and copy["name"] == "Autumn marathon"
    assert copy["external_id"] == f"vs-race:{ident}" and copy["type"] == "Run"
    assert "Distance: 42.195 km" in copy["description"]
    assert "Goal time: 3:45:00" in copy["description"]
    assert _row(rt, ident)["links"]["intervals"] == copy["id"]
    # Nothing changed: nothing written.
    client.calls.clear()
    assert race_mirror.mirror(rt.engine, client, today=today)["unchanged"] == 1
    assert client.calls == []
    # A change in VibeSynergy reaches the copy.
    race_events.change(
        rt.engine,
        ident,
        {"priority": "", "name": "Autumn marathon by the sea"},
        reason="x",
        author="t",
    )
    out = race_mirror.mirror(rt.engine, client, today=today)
    assert out["updated"] == [ident]
    assert client.events[copy["id"]]["category"] == "NOTE"  # a race without a priority
    assert client.events[copy["id"]]["name"] == "Autumn marathon by the sea"
    # A change made in intervals.icu (another app, a hand edit) never changes VibeSynergy;
    # the next run puts the copy back.
    with rt.engine.begin() as conn:
        conn.execute(
            update(icu_event)
            .where(icu_event.c.id == copy["id"])
            .values(name="Hacked", category="RACE_C", date=day + timedelta(days=1))
        )
    before = _row(rt, ident)
    assert call(rt, "vibesynergy_races")["data"]["items"][0]["name"] == "Autumn marathon by the sea"
    out = race_mirror.mirror(rt.engine, client, today=today)
    assert out["updated"] == [ident]
    assert client.events[copy["id"]]["name"] == "Autumn marathon by the sea"
    assert client.events[copy["id"]]["start_date_local"].startswith(day.isoformat())
    after = _row(rt, ident)
    assert {k: after[k] for k in race_events.FIELDS} == {k: before[k] for k in race_events.FIELDS}
    # Delete: the copy goes; undo: a new copy comes.
    gone = race_events.delete(rt.engine, ident, reason="Afgezegd", author="t")
    out = race_mirror.mirror(rt.engine, client, today=today)
    assert out["removed"] == [ident] and client.events == {}
    assert "intervals" not in _row(rt, ident)["links"]
    race_events.revert(rt.engine, gone["audit_id"], reason="Toch wel", author="t")
    assert race_mirror.mirror(rt.engine, client, today=today)["created"] == [ident]


def test_every_write_queues_the_mirror_and_it_runs_without_a_plan(rt, monkeypatch) -> None:
    from vibesynergy.query import plan_model
    from vibesynergy.sources.intervals_icu import credentials
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    queued: list[str] = []
    monkeypatch.setattr(plan_model, "mirror_soon", lambda jobs: queued.append("mirror"))
    day = (_today(rt) + timedelta(days=9)).isoformat()
    call(rt, "vibesynergy_race_add", reason="r", name="Cross", date=day, sport="run")
    TestClient(create_portal_app(rt), base_url="http://localhost").post(
        "/api/races", json={"kind": "camp", "name": "Kamp", "date": day}, headers=HEADERS
    )
    assert queued == ["mirror", "mirror"]
    # The mirror job copies races even when there is no plan.
    ran: list[Any] = []
    monkeypatch.setattr(credentials, "load", lambda *a, **k: object())
    monkeypatch.setattr(IntervalsIcuClient, "from_env", classmethod(lambda cls, **k: "client"))
    monkeypatch.setattr(
        race_mirror, "mirror", lambda engine, client, **k: ran.append(client) or {"errors": []}
    )
    assert plan_model.run_mirror(rt.engine, rt.settings) == {"races": {"errors": []}, "drift": []}
    assert ran == ["client"]


# The portal form --------------------------------------------------------------------------


def test_the_portal_adds_changes_deletes_and_undoes(rt) -> None:
    client = TestClient(create_portal_app(rt), base_url="http://localhost")
    day = (_today(rt) + timedelta(days=20)).isoformat()
    form = {
        "kind": "race",
        "name": "Bosloop",
        "date": day,
        "end_date": "",
        "sport": "trail",
        "distance_km": "12.5",
        "priority": "C",
        "start_time": "10:00",
        "goal_time": "",
        "location": "Ede",
        "url": "",
        "notes": "",
    }
    added = client.post("/api/races", json=form, headers=HEADERS)
    assert added.status_code == 200, added.text
    ident = added.json()["data"]["race"]["id"]
    assert added.json()["data"]["race"]["distance_m"] == 12500
    listed = client.get("/api/races").json()["data"]
    assert [i["id"] for i in listed["items"]] == [ident] and "camp" in listed["kinds"]
    changed = client.post(
        f"/api/races/{ident}",
        json={**form, "location": "", "kind": "camp", "priority": ""},
        headers=HEADERS,
    )
    assert changed.status_code == 200, changed.text
    row = _row(rt, ident)
    assert (row["kind"], row["location"], row["priority"]) == ("camp", None, None)
    deleted = client.post(f"/api/races/{ident}/delete", headers=HEADERS)
    assert deleted.status_code == 200 and client.get("/api/races").json()["data"]["items"] == []
    undo = client.post(
        "/api/races/revert", json={"audit_id": deleted.json()["data"]["audit_id"]}, headers=HEADERS
    )
    assert undo.status_code == 200 and undo.json()["data"]["race"]["id"] == ident
    missing = client.post("/api/races/nope/delete", headers=HEADERS)
    assert missing.status_code == 400


# Approval: only the AI coach's own initiative waits for the athlete -------------------------


def test_own_agent_writes_at_once_and_the_coach_runner_proposes(rt) -> None:
    day = (_today(rt) + timedelta(days=50)).isoformat()
    own = _data(
        _call(
            rt,
            OWN_AGENT,
            FULL,
            "vibesynergy_race_add",
            reason="De atleet vraagt het",
            name="Ronde van Ede",
            date=day,
            sport="ride",
        )
    )
    assert own["written"] is True
    text, is_error = ToolBox(rt, RUNNER_SCOPES).call(
        "vibesynergy_race_add",
        {"reason": "Past mooi als C-race", "name": "Veluwecross", "date": day, "sport": "run"},
    )
    assert not is_error, text
    proposal = json.loads(text)["data"]["proposal"]
    assert proposal["summary"]["action"] == "race_add"
    assert [r["name"] for r in race_events.listing(rt.engine)] == ["Ronde van Ede"]
    # The athlete approves: carried out in the proposer's name, still checked and deduped.
    plan_proposals.approve(rt, proposal["id"])
    assert sorted(r["name"] for r in race_events.listing(rt.engine)) == [
        "Ronde van Ede",
        "Veluwecross",
    ]
    text, is_error = ToolBox(rt, RUNNER_SCOPES).call(
        "vibesynergy_race_add",
        {"reason": "nog eens", "name": "Veluwecross", "date": day, "sport": "run"},
    )
    assert is_error and "already" in text  # a proposal that can never be carried out
