"""Another app keeps overwriting the plan in a mirror (#110): each mirror with a simulated
outside writer, the fight found within the window, the alert naming the mirror, the
sessions, since when and the writer, the stop after N put-backs, and the resume after the
athlete confirms. A single put-back is no fight."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_calendar_mirror import BASE, DAY, KEY, MemoryCalendar, puts
from test_calendar_mirror import run as calendar_run
from test_coach_roles import rt as rt
from test_garmin_strength import EVENT, TODAY, Garmin, _delivery, _event
from test_plan_model import EVENTS, _put
from test_race_events import FakeIcu, _today

from vibesynergy import alerts, i18n
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import garmin_strength as gs
from vibesynergy.query import inbox, plan_conflict, plan_model, race_events, race_mirror
from vibesynergy.query.calendar_mirror import Desired
from vibesynergy.sources.caldav.ics import href_for
from vibesynergy.store.schema import dataset_state, icu_event, mirror_putback

HEADERS = {"x-vibesynergy": "portal"}
START = datetime(2026, 11, 18, 19, 46, tzinfo=UTC)
SESSION = "race27:2026-11-17:run-test"
NL = i18n.Translator("nl")


class Clock:
    def __init__(self, monkeypatch, at: datetime = START) -> None:
        self.at = at
        monkeypatch.setattr(plan_conflict, "utc_now", lambda: self.at)

    def later(self, **delta: float) -> datetime:
        self.at += timedelta(**delta)
        return self.at


# intervals.icu: the plan's sessions --------------------------------------------------------


def _outside_edit(runtime, *, when: datetime, app: int | None = 4242) -> None:
    """Another app rewrites the test session in intervals.icu (the next sync caches it)."""
    edited = [dict(e) for e in EVENTS]
    edited[2]["description"] = edited[2]["description"].replace("30m", "25m")
    edited[2]["updated"] = when.isoformat()
    if app is not None:
        edited[2]["oauth_client_id"] = app
    _put(runtime, edited)


@pytest.fixture
def leading(runtime, monkeypatch):
    """A plan VibeSynergy leads; a put-back writes the model's version back to the cache."""
    from vibesynergy.query import icu_plan

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="race27")
    writes: list[str] = []

    def put_back(engine, client, **k):
        writes.append(k["event_id"])
        _put(runtime, EVENTS)
        return {"event_id": k["event_id"], "version": "v2"}

    monkeypatch.setattr(icu_plan, "update_event", put_back)
    return writes


def test_a_single_put_back_is_no_fight(runtime, leading, monkeypatch) -> None:
    clock = Clock(monkeypatch)
    _outside_edit(runtime, when=clock.at)
    out = plan_model.mirror(runtime.engine, None)
    assert out["put_back"] == [SESSION] and leading == ["3"]
    assert plan_conflict.status(runtime.engine) == []
    assert (
        alerts._plan_conflicts(SimpleNamespace(engine=runtime.engine, settings=runtime.settings))
        == []
    )
    # VibeSynergy's own change is no put-back.
    plan_model.set_session(
        runtime.engine,
        session_id=SESSION,
        day=date(2026, 11, 17),
        name="Looptest 30'",
        sport="Run",
        description="Test.\n\nWarmup\n- 15m Z1-Z2 HR\n\nMain set\n- 30m Z4-Z5 HR",
        reason="langere warming-up",
        author="Claude",
    )
    clock.later(minutes=30)
    assert plan_model.mirror(runtime.engine, None)["put_back"] == []


def test_put_backs_further_apart_than_the_window_are_no_fight(runtime, leading, monkeypatch):
    clock = Clock(monkeypatch)
    for _ in range(3):
        _outside_edit(runtime, when=clock.at, app=None)
        plan_model.mirror(runtime.engine, None)
        clock.later(hours=4)
    assert len(leading) == 3
    assert plan_conflict.status(runtime.engine, now=clock.at - timedelta(hours=4)) == []
    assert not plan_conflict.held(runtime.engine, plan_conflict.INTERVALS)


def test_an_outside_writer_in_intervals_is_found_named_and_stopped_until_confirmed(
    runtime, leading, monkeypatch
) -> None:
    clock = Clock(monkeypatch)
    changed = clock.at - timedelta(minutes=5)
    _outside_edit(runtime, when=changed)
    plan_model.mirror(runtime.engine, None)
    clock.later(hours=1)
    _outside_edit(runtime, when=clock.at - timedelta(minutes=5))
    plan_model.mirror(runtime.engine, None)
    # Twice within the window: a fight, still put back.
    (fight,) = plan_conflict.status(runtime.engine)
    assert fight["mirror"] == "intervals" and not fight["held"] and fight["count"] == 2
    assert fight["names"] == ["Looptest 30'"] and fight["writer"] == "icu-app:4242"
    assert datetime.fromisoformat(fight["since"]) == changed
    text = plan_conflict.alert_text(fight, i18n.ENGLISH, "Europe/Amsterdam")
    assert "intervals.icu" in text and "Looptest 30'" in text
    assert "since 18-11-2026 20:41" in text
    assert "the app with number 4242 in intervals.icu" in text
    assert "put your plan back 2 times" in text
    dutch = plan_conflict.alert_text(fight, NL, "Europe/Amsterdam")
    assert "sinds 18-11-2026 20:41" in dutch and "Een andere app" in dutch

    rt_ = SimpleNamespace(engine=runtime.engine, settings=runtime.settings)
    (alert,) = alerts._plan_conflicts(rt_)
    assert (alert.kind, alert.level, alert.key) == (
        "plan_conflict",
        "warn",
        "plan_conflict:intervals",
    )
    assert alerts.KINDS["plan_conflict"][1] == ["portal", "email", "push"]

    # The third put-back in the fight: VibeSynergy stops (N = 3).
    clock.later(hours=1)
    _outside_edit(runtime, when=clock.at)
    plan_model.mirror(runtime.engine, None)
    assert len(leading) == 3 and plan_conflict.held(runtime.engine, "intervals")
    (alert,) = alerts._plan_conflicts(rt_)
    assert alert.level == "stop" and "has now stopped" in alert.text
    # Held: the other app's change stays; VibeSynergy's own changes still go out.
    clock.later(hours=1)
    _outside_edit(runtime, when=clock.at)
    out = plan_model.mirror(runtime.engine, None)
    assert out["held"] == [SESSION] and out["put_back"] == [] and len(leading) == 3
    # Hours later, still waiting for the athlete: the alert stays open.
    clock.later(hours=10)
    assert plan_conflict.status(runtime.engine)[0]["held"] is True

    # Waiting for you shows it, with the plan link and the confirmation.
    (notice,) = [
        n for n in inbox._notices(runtime, date(2026, 11, 19)) if n["kind"] == "plan_conflict"
    ]
    assert notice["held"] and notice["link"] == "#/planning"
    with TestClient(create_portal_app(runtime), base_url="http://localhost") as client:
        done = client.post("/api/plan_conflict/intervals/resume", headers=HEADERS)
        assert done.status_code == 200 and done.json()["data"]["held"] is False
        assert client.post("/api/plan_conflict/nowhere/resume", headers=HEADERS).status_code == 404
    queued = [j for j in runtime.jobs.recent(limit=10) if j["kind"] == plan_model.MIRROR_JOB]
    assert queued  # the plan goes back at once, not at the next sync
    # Confirmed: put back again, and the count starts anew.
    assert plan_conflict.status(runtime.engine) == []
    out = plan_model.mirror(runtime.engine, None)
    assert out["put_back"] == [SESSION] and len(leading) == 4
    assert not plan_conflict.held(runtime.engine, "intervals")


def test_the_same_writer_on_different_sessions_is_a_fight(runtime, monkeypatch) -> None:
    clock = Clock(monkeypatch)
    for item in ("a", "b"):
        plan_conflict.record(
            runtime.engine,
            "intervals",
            [{"item": item, "name": item.upper(), "writer": "icu-app:7"}],
        )
        clock.later(minutes=40)
    (fight,) = plan_conflict.status(runtime.engine)
    assert fight["names"] == ["A", "B"] and fight["writer"] == "icu-app:7"
    # Different sessions without a known writer: a person editing by hand, no fight.
    for item in ("c", "d"):
        plan_conflict.record(runtime.engine, "garmin", [{"item": item, "name": item}])
        clock.later(minutes=40)
    assert [f["mirror"] for f in plan_conflict.status(runtime.engine)] == ["intervals"]


def test_thresholds_are_settings_with_defaults() -> None:
    assert plan_conflict.limits(None) == plan_conflict.Limits(2, 3.0, 3)
    chosen = SimpleNamespace(
        alerts={"plan_conflict_repeats": 3, "plan_conflict_hours": 6, "plan_conflict_stop_after": 5}
    )
    assert plan_conflict.limits(chosen) == plan_conflict.Limits(3, 6.0, 5)
    wrong = SimpleNamespace(alerts={"plan_conflict_repeats": "x", "plan_conflict_stop_after": 1})
    assert plan_conflict.limits(wrong) == plan_conflict.Limits(2, 3.0, 2)  # never below repeats


def test_a_held_mirror_stops_after_the_chosen_number(runtime, leading, monkeypatch) -> None:
    clock = Clock(monkeypatch)
    rules = plan_conflict.Limits(repeats=2, hours=3, stop_after=4)
    for _ in range(3):
        _outside_edit(runtime, when=clock.at)
        plan_model.mirror(runtime.engine, None, rules=rules)
        clock.later(hours=1)
    assert not plan_conflict.held(runtime.engine, "intervals")
    _outside_edit(runtime, when=clock.at)
    plan_model.mirror(runtime.engine, None, rules=rules)
    assert plan_conflict.held(runtime.engine, "intervals") and len(leading) == 4


# intervals.icu: races and events (#91) share the put-back and the hold -----------------------


def test_an_outside_writer_on_a_race_copy_is_put_back_then_held(rt, monkeypatch) -> None:
    from test_race_events import _icu

    _icu(rt, [])
    clock = Clock(monkeypatch)
    client = FakeIcu()
    today = _today(rt)
    added = race_events.add(
        rt.engine,
        {"name": "Autumn marathon", "date": today + timedelta(days=30), "sport": "run"},
        reason="Doel",
        author="test",
    )
    ident = added["race"]["id"]
    race_mirror.mirror(rt.engine, client, today=today)
    (copy,) = client.events.values()

    def hacked() -> None:
        with rt.engine.begin() as conn:
            raw = conn.execute(select(icu_event.c.raw).where(icu_event.c.id == copy["id"])).scalar()
            conn.execute(
                update(icu_event)
                .where(icu_event.c.id == copy["id"])
                .values(name="Hacked", raw={**(raw or {}), "oauth_client_id": 99})
            )

    for _ in range(3):
        hacked()
        assert race_mirror.mirror(rt.engine, client, today=today)["put_back"] == [ident]
        clock.later(hours=1)
    assert plan_conflict.held(rt.engine, "intervals")
    (fight,) = plan_conflict.status(rt.engine)
    assert fight["names"] == ["Autumn marathon"] and fight["writer"] == "icu-app:99"
    hacked()
    out = race_mirror.mirror(rt.engine, client, today=today)
    assert out["held"] == [ident] and out["updated"] == []
    plan_conflict.resume(rt.engine, "intervals")
    assert race_mirror.mirror(rt.engine, client, today=today)["put_back"] == [ident]


# Garmin: a delivered workout another app keeps removing ------------------------------------


def test_a_workout_removed_from_garmin_again_and_again_is_held(runtime, monkeypatch) -> None:
    clock = Clock(monkeypatch)
    _event(runtime, EVENT)
    api = Garmin()
    gs.reconcile(runtime.engine, lambda: api, TODAY)
    sync = [datetime.now(UTC)]

    def removed_on_garmin() -> None:
        # The next calendar sync no longer lists ours.
        sync[0] += timedelta(hours=1)
        with runtime.engine.begin() as conn:
            conn.execute(dataset_state.delete())
            conn.execute(
                dataset_state.insert().values(
                    source="garmin", dataset="scheduled_workouts", last_success_at=sync[0]
                )
            )

    uploads = lambda: [c for c in api.calls if c[0] == "upload"]  # noqa: E731
    removed_on_garmin()
    gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert len(uploads()) == 2 and plan_conflict.status(runtime.engine) == []  # once: no fight
    for _ in range(2):
        clock.later(hours=1)
        removed_on_garmin()
        gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert len(uploads()) == 4 and plan_conflict.held(runtime.engine, "garmin")
    (fight,) = plan_conflict.status(runtime.engine)
    assert fight["mirror"] == "garmin" and fight["names"] == ["⚡ Kracht AA 40'"]
    assert fight["writer"] is None and "Garmin" in plan_conflict.alert_text(fight, i18n.ENGLISH)
    # Held: not delivered again.
    clock.later(hours=1)
    removed_on_garmin()
    out = gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert out["held"] == [str(EVENT["id"])] and len(uploads()) == 4
    # Confirmed: delivered again.
    plan_conflict.resume(runtime.engine, "garmin")
    gs.reconcile(runtime.engine, lambda: api, TODAY)
    assert len(uploads()) == 5 and _delivery(runtime)["state"] == "delivered"


# The calendar: never put back, but the same alert -----------------------------------------


def _edit_on_calendar(calendar: MemoryCalendar, title: str, etag: str) -> None:
    """Another app rewrites our copy, with its own PRODID."""
    obj = calendar.objects[href_for(BASE, KEY)]
    data = obj["data"].replace(
        "PRODID:-//VibeSynergy//Calendar mirror//EN", "PRODID:-//Vitality//Planner 2//EN"
    )
    obj["data"] = re.sub(r"SUMMARY:[^\r\n]*", f"SUMMARY:{title}", data)
    obj["etag"] = etag


def test_an_app_editing_the_calendar_copy_is_named_and_nothing_is_put_back(runtime, monkeypatch):
    clock = Clock(monkeypatch)
    calendar = MemoryCalendar()
    calendar_run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    _edit_on_calendar(calendar, "Tempo (Vitality)", '"v1"')
    calendar.calls.clear()
    calendar_run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    calendar_run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])  # the same edit: once
    assert puts(calendar) == []
    assert plan_conflict.status(runtime.engine) == []
    with runtime.engine.connect() as conn:
        assert conn.execute(select(mirror_putback.c.put_back)).scalars().all() == [False]
    clock.later(minutes=50)
    _edit_on_calendar(calendar, "Tempo (Vitality 2)", '"v2"')
    calendar_run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    assert puts(calendar) == []
    (fight,) = plan_conflict.status(runtime.engine)
    assert fight["mirror"] == "calendar" and fight["names"] == ["Tempo"]
    assert fight["writer"] == "calendar-app:Vitality Planner 2" and fight["put_back"] is False
    text = plan_conflict.alert_text(fight, i18n.ENGLISH)
    assert "your calendar" in text and "Vitality Planner 2" in text
    assert "nothing was put back" in text
    # Never held: there is nothing to stop.
    for n in range(3, 6):
        clock.later(minutes=50)
        _edit_on_calendar(calendar, f"Tempo {n}", f'"v{n}"')
        calendar_run(runtime, calendar, [Desired(KEY, DAY, "Tempo")])
    assert not plan_conflict.held(runtime.engine, "calendar")


def test_the_alert_is_sent_once_and_again_when_it_stops(runtime, monkeypatch) -> None:
    """Through the alert check: the fight is mailed, the stop is mailed again, and quiet
    after the athlete confirms is resolved."""
    clock = Clock(monkeypatch)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        alerts,
        "collect",
        lambda rt, today, since=None: alerts.Collected(alerts._plan_conflicts(rt)),
    )
    monkeypatch.setattr(alerts, "channels", lambda settings, engine=None: {"email": True})
    monkeypatch.setattr("vibesynergy.query.coach_consistency.daily", lambda rt: None)

    def send(subject, body, channel, html=None):
        sent.append((channel, body))
        return [channel]

    for n in range(2):
        plan_conflict.record(
            runtime.engine, "garmin", [{"item": "w", "name": "Kracht"}], run=str(n)
        )
        clock.later(minutes=30)
    alerts.check(runtime, send=send)
    assert len(sent) == 1 and "Garmin" in sent[0][1] and "Kracht" in sent[0][1]
    alerts.check(runtime, send=send)
    assert len(sent) == 1  # still the same fight: not again
    plan_conflict.record(runtime.engine, "garmin", [{"item": "w", "name": "Kracht"}], run="2")
    alerts.check(runtime, send=send)
    assert len(sent) == 2 and "has now stopped" in sent[1][1]
    plan_conflict.resume(runtime.engine, "garmin")
    alerts.check(runtime, send=send)
    assert len(sent) == 3 and "Resolved" in sent[2][1]


def test_the_texts_are_dutch_and_english() -> None:
    item: dict[str, Any] = {
        "mirror": "garmin",
        "held": True,
        "count": 3,
        "since": START.isoformat(),
        "names": ["A", "B", "C", "D", "E", "F"],
        "writer": None,
    }
    text = plan_conflict.alert_text(item, NL)
    assert "A, B, C, D en nog 2" in text and "Wacht op jou" in text
    for words in (text, plan_conflict.alert_text(item, i18n.ENGLISH)):
        for name in ("Claude", "Anthropic", "OpenAI", "GPT"):
            assert name not in words
