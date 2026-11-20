# ruff: noqa: F811  (the fixtures rt and sent come from test_coach_continuity)
"""#119: the contact window, when an AI coach may reach the athlete.

Outside the window entries are written but nothing is delivered: the advice notice, push and
mail, report mails, the alert "Messages from your AI coach" and a failed run's notice wait
in the outbox and go out when the window opens; for a day's advice only the version that is
current then. One exception: today's red advice from a hard rule the athlete's own report
set off. The window is in the handbook and in ``vibesynergy_coach_state``.

The model is a scripted fake, mail and push are captured and all data is synthetic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import insert, select
from test_coach_continuity import AMBER, BECAUSE, DAY, _notices, _run, rt, sent  # noqa: F401
from test_coach_runner import WRITE

from vibesynergy import alerts
from vibesynergy.coach_runner import contact, report, runner
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.store.schema import coach_decision_log, coach_run

AMS = ZoneInfo("Europe/Amsterdam")
NIGHT = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)  # 05:00 in Amsterdam, Monday 2-11
MORNING = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)  # 08:30
WINDOW = {"on": True, "start": "07:00", "end": "23:00"}
RED = {"level": "refuse", "colour": "red", "reasons": ["Je meldde dat je ziek bent."]}


@pytest.fixture
def clock(monkeypatch):
    """The contact window's clock; the rest of the app keeps the real one."""
    now = {"at": NIGHT}
    monkeypatch.setattr(contact, "utc_now", lambda: now["at"])
    return now


@pytest.fixture
def window(rt):
    runner_settings.save(rt, {"contact": WINDOW})
    return rt


def _local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 2, hour, minute, tzinfo=AMS)


# --- the window itself -----------------------------------------------------------------------


def test_open_closed_and_across_midnight() -> None:
    day = {"on": True, "start": "07:00", "end": "23:00"}
    assert contact.is_open(day, _local(7)) and contact.is_open(day, _local(22, 59))
    assert not contact.is_open(day, _local(23)) and not contact.is_open(day, _local(6, 59))
    assert contact.opens_at(day, _local(5)) == _local(7)
    assert contact.opens_at(day, _local(23, 30)) == _local(7) + timedelta(days=1)
    assert contact.opens_at(day, _local(12)) is None
    assert contact.closed_at(day, _local(5)) == _local(23) - timedelta(days=1)
    night = {"on": True, "start": "22:00", "end": "06:00"}
    assert contact.is_open(night, _local(23)) and contact.is_open(night, _local(5, 59))
    assert not contact.is_open(night, _local(12))
    assert contact.is_open({**day, "on": False}, _local(3))


def test_the_default_window_opens_with_the_morning_advice() -> None:
    """07:00-23:00: the fixed advice run (07:00), the evening review and the week review
    fall inside, so a default install delivers them the moment they are made."""
    assert contact.DEFAULT_WINDOW == {"on": True, "start": "07:00", "end": "23:00"}
    defaults = runner_settings.default_schedule()
    for job in ("morning", "evening", "weekly"):
        assert contact.is_open(
            contact.DEFAULT_WINDOW, _local(*map(int, defaults[job]["time"].split(":")))
        )


def test_the_setting_is_checked_and_saved(rt) -> None:
    saved = runner_settings.save(rt, {"contact": {"on": True, "start": "08:00", "end": "23:00"}})
    assert saved.contact == {"on": True, "start": "08:00", "end": "23:00"}
    for bad, message in (
        ({"start": "8"}, "Give the time"),
        ({"start": "23:00", "end": "23:00"}, "differ"),
        ({"on": "yes"}, "on or off"),
        ({"quiet": True}, "Unknown"),
    ):
        with pytest.raises(runner_settings.RunnerSettingsError, match=message):
            runner_settings.save(rt, {"contact": bad})
    api = TestClient(create_portal_app(rt), base_url="http://localhost")
    found = api.post(
        "/api/coach-runner/settings", json={"contact": {"end": "22:30"}}, headers=WRITE
    ).json()
    assert found["data"]["contact"]["end"] == "22:30"
    assert found["data"]["contact"]["timezone"] == rt.settings.timezone


# --- holding and releasing --------------------------------------------------------------------


def test_an_advice_at_night_is_written_and_delivered_when_the_window_opens(
    window,
    sent,
    clock,
) -> None:
    first, _ = _run(window, DAY.isoformat(), "Rustpols hoger: rustig aan.", AMBER)
    assert first.status == "done" and first.entry_id
    assert report.stored(window, first.entry_id) is not None  # the entry stands
    assert sent["push"] == [] and sent["mail"] == [] and _notices(window) == []
    with window.engine.connect() as conn:
        row = conn.execute(select(coach_run).where(coach_run.c.entry_id == first.entry_id)).one()
    assert row.mail_error == contact.HELD and row.mailed_at is None
    because = {"previous": first.entry_id, "reason": BECAUSE}
    second, _ = _run(
        window, "event-1", "Rustpols weer normaal: duurloop mag door.", because=because
    )
    assert sent["push"] == [] and len(contact.outbox(window)) == 2
    assert contact.due(window) is False  # still closed

    clock["at"] = MORNING
    assert contact.due(window) is True
    out = contact.release(window)
    assert out["sent"] == [f"advice:{DAY.isoformat()}"]
    # Once, and only the version that is current now: the replaced one never goes out.
    assert len(sent["push"]) == 1 and f"/coach/report/{second.entry_id}" in sent["push"][0][2]
    (notice,) = _notices(window)
    assert notice["entry_id"] == second.entry_id
    assert contact.outbox(window) == [] and contact.due(window) is False
    with window.engine.connect() as conn:
        row = conn.execute(select(coach_run).where(coach_run.c.entry_id == second.entry_id)).one()
    assert row.mailed_at is not None and row.mail_error is None
    with window.engine.connect() as conn:
        row = conn.execute(select(coach_run).where(coach_run.c.entry_id == first.entry_id)).one()
    assert row.mail_error is None and row.mailed_at is None  # replaced: not sent, not waiting


def test_inside_the_window_it_goes_out_at_once(window, sent, clock) -> None:
    clock["at"] = MORNING
    first, _ = _run(window, DAY.isoformat(), "Rustpols hoger: rustig aan.", AMBER)
    assert first.mailed and len(sent["push"]) == 1 and contact.outbox(window) == []


def test_sending_again_by_hand_is_the_athletes_own_request(window, sent, clock) -> None:
    first, _ = _run(window, DAY.isoformat(), "Rustpols hoger: rustig aan.", AMBER)
    entry = report.stored(window, first.entry_id)
    assert report.send(window, "morning", DAY, entry, by_mail=True)
    assert len(sent["mail"]) == 1


def _entry(rt, *, rules, safety=RED, day=None) -> dict:
    meta = {
        "safety": safety,
        "harness": {"rules_floor": {"level": "refuse", "rules": rules}, "task": "injury"},
    }
    return {"id": 1, "meta": meta, "for_date": day or date(2026, 11, 2)}


def test_the_urgent_safety_exception(window, clock) -> None:
    today = date(2026, 11, 2)
    for rule in ("sick", "red_flag_note", "pain_7", "affects_movement"):
        assert contact.urgent(window, _entry(window, rules=[rule]), today), rule
    # A red day from measurements alone waits; so does tomorrow's advice and a milder one.
    assert not contact.urgent(window, _entry(window, rules=["hrv_rhr"]), today)
    assert not contact.urgent(window, _entry(window, rules=["sick"]), today + timedelta(days=1))
    amber = {"level": "flag", "colour": "amber", "reasons": ["Pijn 4 van 10."]}
    assert not contact.urgent(window, _entry(window, rules=["sick"], safety=amber), today)
    assert not contact.urgent(window, _entry(window, rules=["sick"]), None)


def test_an_urgent_advice_is_not_held(window, sent, clock, monkeypatch) -> None:
    monkeypatch.setattr(contact, "urgent", lambda _rt, _entry, _day, now=None: True)
    first, _ = _run(window, DAY.isoformat(), "Ziek: vandaag rust.", AMBER)
    assert first.mailed and len(sent["push"]) == 1 and contact.outbox(window) == []


def test_messages_from_an_ai_coach_wait_for_the_window(window, sent, clock) -> None:
    with window.engine.begin() as conn:
        note_id = conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="note",
                author="client:app",
                entry_type="morning",
                for_date=DAY,
                text="Goedemorgen, vandaag een rustige dag.",
                meta={},
            )
        ).inserted_primary_key[0]
    store = SecretStore(window.settings.secrets_dir, alerts.SECRETS)
    store.set(alerts.STATE, {alerts.CHECKED: (utc_now() - timedelta(hours=1)).isoformat()})
    alerts.check(window)
    assert not [p for p in sent["push"] if f"/coach/report/{note_id}" in p[2] + p[1]]
    clock["at"] = MORNING
    alerts.check(window)
    pushed = [p for p in sent["push"] if f"/coach/report/{note_id}" in p[2] + p[1]]
    assert len(pushed) == 1
    alerts.check(window)  # not twice
    assert len([p for p in sent["push"] if f"/coach/report/{note_id}" in p[2] + p[1]]) == 1


def test_a_failed_runs_notice_waits_too(window, sent, clock) -> None:
    assert runner.notify_failure(window, "morning", DAY, "boom") == []
    assert sent["push"] == [] and contact.outbox(window)[0]["type"] == "message"
    clock["at"] = MORNING
    contact.release(window)
    assert len(sent["push"]) == 1 and contact.outbox(window) == []


# --- what an AI coach reads -------------------------------------------------------------------


def _tool(rt, name: str) -> dict:
    async def go():
        async with Client(build_server(rt)) as client:
            return await client.call_tool(name, {})

    result = anyio.run(go)
    assert not result.is_error
    return result.structured_content["data"]


def test_coach_state_and_the_handbook_show_the_window(window, clock) -> None:
    runner_settings.save(window, {"contact": {"start": "08:00", "end": "22:30"}})
    for data in (
        _tool(window, "vibesynergy_coach_state"),
        _tool(window, "vibesynergy_coach_handbook"),
    ):
        found = data["contact_window"]
        assert (found["start"], found["end"], found["on"]) == ("08:00", "22:30", True)
        assert found["timezone"] == window.settings.timezone
        assert found["open_now"] is False
        assert found["opens_at"].startswith("2026-11-02T08:00")
        assert "refuse" in found["urgent_exception"]
        assert "08:00" in found["rule"]
