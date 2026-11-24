"""The sessions an advice lists are links (#181): done, to the activity; still planned, to
the planned workout's steps; without a match in VibeSynergy's plan, plain text. The links
are made when the entry is read; the stored entry never changes."""

from __future__ import annotations

from datetime import date

from sqlalchemy import insert, select
from test_plan_intervals import MONDAY
from test_week_progress import _more, _seed

from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import advice_view, coach_runner, plan_model
from vibesynergy.store.schema import coach_decision_log

THURSDAY = date(2026, 11, 5)


def _entry(rt, day: date, sessions: list[dict]) -> int:
    with rt.engine.begin() as conn:
        return conn.execute(
            insert(coach_decision_log)
            .values(
                subject_id=1,
                at=utc_now(),
                kind="coach",
                author="client:coach-runner",
                entry_type="morning",
                for_date=day,
                text="Rustig aan vandaag.",
                meta={"sessions": sessions},
            )
            .returning(coach_decision_log.c.id)
        ).scalar_one()


def test_matching_by_title_each_plan_session_once() -> None:
    planned = [
        {"title": "Duurloop Z2", "description": "- 40m Z2", "sport": "Run", "activity_id": None},
        {"title": "Duurloop Z2", "description": "- 30m Z2", "sport": "Run", "activity_id": "a1"},
        {"title": "Kracht", "description": None, "activity_id": None},
    ]
    sessions = [
        {"title": "🏃 Duurloop Z2!", "decision": "keep", "note": "rustig"},
        {"title": "duurloop  z2", "decision": "done"},
        {"title": "Kracht", "decision": "skip"},
        {"title": "Zwemmen", "decision": "proposal"},
        "not a session",
    ]
    out = advice_view.link_sessions(sessions, planned)
    assert out[0] == {
        "title": "🏃 Duurloop Z2!",
        "decision": "keep",
        "note": "rustig",
        "workout": "- 40m Z2",
        "sport": "Run",
    }
    assert out[1] == {"title": "duurloop  z2", "decision": "done", "activity_id": "a1"}
    # A match without a workout text or an activity, and no match at all: plain text.
    assert out[2] == {"title": "Kracht", "decision": "skip"}
    assert out[3] == {"title": "Zwemmen", "decision": "proposal"}
    assert len(out) == 4
    assert "workout" not in sessions[0]  # a copy: what was stored is not touched
    assert advice_view.link_sessions(None, planned) == []


def test_report_links_done_planned_and_unmatched_sessions(runtime) -> None:
    _seed(runtime)
    _more(runtime)
    plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=THURSDAY,
        name="Tempoloop",
        sport="Run",
        description="- 15m Z2 HR\n- 20m Z3 HR\n- 10m Z1 HR",
        reason="test",
        author="test",
    )
    monday = [
        {"title": "Lange rit Z2 1u45", "decision": "done"},
        {"title": "Brickloop Z2 10'", "decision": "missed"},
        {"title": "Iets wat niet gepland is", "decision": "proposal"},
    ]
    thursday = [{"title": "🏃 Tempoloop", "decision": "keep", "note": "Z3 niet harder"}]
    first = _entry(runtime, MONDAY, monday)
    second = _entry(runtime, THURSDAY, thursday)

    linked = coach_runner.report(runtime, first)["entry"]["advice_sessions"]
    ride, brick, other = linked
    # Done: the activity intervals.icu paired with the plan workout, as the week shows it.
    assert ride["activity_id"] and "workout" not in ride
    assert brick == monday[1]  # missed, and the plan has no workout text for it
    assert other == monday[2]

    (tempo,) = coach_runner.report(runtime, second)["entry"]["advice_sessions"]
    assert "activity_id" not in tempo
    assert "20m Z3" in tempo["workout"] and tempo["sport"] == "running"
    assert tempo["note"] == "Z3 niet harder"

    # The coach decision log is append-only: the stored entries still say what they said.
    with runtime.engine.connect() as conn:
        stored = dict(
            conn.execute(
                select(coach_decision_log.c.id, coach_decision_log.c.meta).where(
                    coach_decision_log.c.id.in_([first, second])
                )
            ).all()
        )
    assert stored[first] == {"sessions": monday}
    assert stored[second] == {"sessions": thursday}


def test_coach_feed_entries_get_their_links(runtime) -> None:
    _seed(runtime)
    _more(runtime)
    entries = [
        {
            "id": 1,
            "for_date": MONDAY.isoformat(),
            "meta": {"sessions": [{"title": "Lange rit Z2 1u45", "decision": "done"}]},
        },
        {"id": 2, "for_date": MONDAY.isoformat(), "meta": {}},
        {"id": 3, "for_date": None, "meta": {"sessions": [{"title": "x", "decision": "keep"}]}},
    ]
    advice_view.with_session_links(runtime, entries)
    assert entries[0]["advice_sessions"][0]["activity_id"]
    assert "advice_sessions" not in entries[1] and "advice_sessions" not in entries[2]


def test_a_plan_change_names_its_session_as_a_link(runtime) -> None:
    _seed(runtime)
    _more(runtime)
    plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=THURSDAY,
        name="Tempoloop",
        sport="Run",
        description="- 15m Z2 HR\n- 20m Z3 HR",
        reason="test",
        author="test",
    )

    def change(action: str, name: str, day: date) -> dict:
        plan = {"action": action, "name": name, "sport": "Run", "date": day.isoformat()}
        return {"id": 9, "entry_type": "plan_change", "meta": {"plan": plan}}

    added = change("session_add", "Tempoloop", THURSDAY)
    done = change("session_change", "Lange rit Z2 1u45", MONDAY)
    dropped = change("session_skip", "Tempoloop", THURSDAY)
    unknown = change("session_add", "Niet in het plan", THURSDAY)
    advice_view.with_session_links(runtime, [added, done, dropped, unknown])
    assert "20m Z3" in added["plan_session"]["workout"]
    assert done["plan_session"]["activity_id"]
    assert "plan_session" not in dropped and "plan_session" not in unknown
