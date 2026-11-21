"""#141: the plan mirror's copies to intervals.icu are not plan changes. They go to the
audit log only, the old ones stay in the decision log but are not shown, and a real change
to VibeSynergy's plan is on the coach feed with its reason and what changed."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, insert, select
from test_icu_writes import EVENT, Api, _client, _seed

from vibesynergy.query import icu_plan, plan_model, search
from vibesynergy.query.coach_workouts import coach_feed
from vibesynergy.store.schema import audit_log, coach_decision_log

MONDAY = date(2026, 11, 2)


def _log(runtime) -> list[dict]:
    with runtime.engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(coach_decision_log)).mappings()]


def _plan_changes(runtime) -> list[dict]:
    return [e for e in coach_feed(runtime) if e["entry_type"] == "plan_change"]


def test_a_mirror_copy_is_an_audit_row_and_not_on_the_coach_feed(runtime) -> None:
    version = _seed(runtime)
    api = Api(EVENT)
    saved = icu_plan.update_event(
        runtime.engine,
        _client(api),
        reason="plan mirror: Easy run",
        expected_version=version,
        event_id="1001",
        changes={"name": "Easy run 45'"},
        author=plan_model.MIRROR_AUTHOR,
        mirror_run="run-1",
    )
    with runtime.engine.connect() as conn:
        detail = conn.execute(
            select(audit_log.c.detail).where(audit_log.c.id == saved["audit_id"])
        ).scalar_one()
    assert detail["mirror_run"] == "run-1" and detail["revertable"] is True
    assert _log(runtime) == []
    copy = plan_model.last_copy(runtime.engine)
    assert copy is not None and copy["changes"] == 1
    assert copy["items"][0]["action"] == "icu.update"
    assert copy["items"][0]["name"] == "Easy run 45'"


def test_a_coach_write_to_intervals_is_still_on_the_feed(runtime) -> None:
    version = _seed(runtime)
    icu_plan.update_event(
        runtime.engine,
        _client(Api(EVENT)),
        reason="Make it shorter",
        expected_version=version,
        event_id="1001",
        changes={"name": "Shorter"},
        author="Claude",
    )
    assert [e["text"] for e in _plan_changes(runtime)] == ["Make it shorter"]
    assert plan_model.last_copy(runtime.engine) is None  # not a mirror copy


def test_the_old_mirror_rows_stay_in_the_log_but_are_not_shown(runtime) -> None:
    at = datetime(2026, 11, 19, 17, 16, tzinfo=UTC)
    rows = [
        # The mirror before #141, in both of its texts, and a check that has no text.
        ("VibeSynergy", "plan mirror: fry27:week:23", {"action": "icu.update"}),
        ("VibeSynergy", "plan mirror (#197): Lange rit Z2 1u45", {"action": "icu.create"}),
        ("VibeSynergy", "plan mirror: Lange rit", {}),
        ("Claude", "   ", {"action": "icu.update"}),
        # A real write by the AI coach stays.
        ("Claude", "Zwemrust als herstel, akkoord Robert", {"action": "icu.update"}),
    ]
    with runtime.engine.begin() as conn:
        for author, text, meta in rows:
            conn.execute(
                insert(coach_decision_log).values(
                    subject_id=1,
                    at=at,
                    kind="note",
                    author=author,
                    entry_type="plan_change",
                    for_date=at.date(),
                    text=text,
                    meta=meta,
                )
            )
    shown = _plan_changes(runtime)
    assert [e["text"] for e in shown] == ["Zwemrust als herstel, akkoord Robert"]
    # Nothing was changed or deleted: the rows are only filtered when read.
    assert len(_log(runtime)) == len(rows)
    hits = {g["kind"]: g["hits"] for g in search.search(runtime, "mirror")["groups"]}
    assert not hits.get("coach")
    hits = {g["kind"]: g["hits"] for g in search.search(runtime, "zwemrust")["groups"]}
    assert len(hits["coach"]) == 1


def test_a_real_plan_change_is_on_the_feed_with_reason_and_details(runtime) -> None:
    engine = runtime.engine
    plan_model.create_plan(
        engine, name="Lake 27", start=MONDAY, end=date(2026, 11, 29), reason="new", author="me"
    )
    plan_model.set_week(engine, week=1, note="Rustig.", reason="instapweek", author="Claude")
    plan_model.set_target(
        engine, week=1, sport="run", time_s=3_600, reason="een uur", author="Claude"
    )
    sid = plan_model.set_session(
        engine,
        session_id=None,
        day=MONDAY,
        name="Duurloop Z2 40'",
        sport="Run",
        description="- 40m Z2 HR",
        reason="dag 1",
        author="Claude",
    )["session_id"]
    plan_model.move_session(
        engine, session_id=sid, day=date(2026, 11, 3), reason="regen", author="portal"
    )
    plan_model.skip_session(engine, session_id=sid, reason="ziek", author="Claude")
    shown = _plan_changes(runtime)
    assert [e["text"] for e in shown] == ["ziek", "regen", "dag 1", "een uur", "instapweek"]
    plans = [e["meta"]["plan"] for e in shown]
    assert [p["action"] for p in plans] == [
        "session_skip",
        "session_move",
        "session_add",
        "week_target",
        "week_note",
    ]
    assert plans[1]["from_date"] == "2026-11-02" and plans[1]["date"] == "2026-11-03"
    assert plans[2]["name"] == "Duurloop Z2 40'" and plans[3]["sport"] == "run"
    # A "Plan change" never comes without content.
    for entry in shown:
        assert entry["text"].strip() and entry["meta"]["plan"] and entry["meta"]["version"]
        assert not entry["meta"]["action"].startswith("icu.")


def test_the_mirror_writes_no_plan_change_and_settings_show_its_last_copy(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    _seed(runtime)  # the cached intervals.icu calendar the copies are kept in
    engine = runtime.engine
    plan_model.create_plan(
        engine, name="Lake 27", start=MONDAY, end=date(2026, 11, 29), reason="new", author="me"
    )
    plan_model.set_week(engine, week=1, note="Rustig.", reason="instapweek", author="Claude")
    plan_model.set_session(
        engine,
        session_id=None,
        day=MONDAY,
        name="Duurloop Z2 40'",
        sport="Run",
        description="- 40m Z2 HR",
        reason="dag 1",
        author="Claude",
    )
    before = len(_log(runtime))
    api = Api(EVENT)
    out = plan_model.mirror(engine, _client(api))
    assert out["errors"] == [] and len(out["created"]) == 2
    # The copies are audit rows of one run; the decision log got nothing.
    assert len(_log(runtime)) == before
    with engine.connect() as conn:
        runs = conn.execute(
            select(func.json_extract(audit_log.c.detail, "$.mirror_run")).where(
                audit_log.c.author == plan_model.MIRROR_AUTHOR
            )
        ).scalars()
        assert len(set(runs)) == 1
    copy = plan_model.last_copy(engine)
    assert copy is not None and copy["changes"] == 2
    assert {i["action"] for i in copy["items"]} == {"icu.create"}
    # Nothing changed: the next run writes nothing, and the last copy stays the same.
    assert plan_model.mirror(engine, _client(api))["created"] == []
    assert plan_model.last_copy(engine) == copy
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    sources = client.get("/api/settings").json()["data"]["sources"]
    icu = next(s for s in sources if s["id"] == "intervals_icu")
    assert icu["plan_copy"]["changes"] == 2
