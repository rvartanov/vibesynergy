"""The plan model's shadow import from intervals.icu."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import plan_model
from vibesynergy.sources.intervals_icu.mappers import map_events

EVENTS = [
    {
        "id": 1,
        "category": "NOTE",
        "start_date_local": "2026-11-16T00:00:00",
        "name": "Wk 3 · Base 1 · laadweek · ~8u",
        "description": "Tests: looptest 30'.",
        "external_id": "lake27:week:03",
        "for_week": True,
        "updated": "2026-11-03T08:00:00Z",
    },
    {
        "id": 2,
        "category": "TARGET",
        "type": "Run",
        "start_date_local": "2026-11-16T00:00:00",
        "name": "Loop 4u30",
        "external_id": "lake27:target:03:run",
        "for_week": True,
        "time_target": 16200,
        "updated": "2026-11-03T08:00:00Z",
    },
    {
        "id": 3,
        "category": "WORKOUT",
        "type": "Run",
        "start_date_local": "2026-11-17T00:00:00",
        "name": "Looptest 30'",
        "description": "Test.\n\nWarmup\n- 12m Z1-Z2 HR\n\nMain set\n- 30m Z4-Z5 HR",
        "external_id": "lake27:2026-11-17:run-test",
        "moving_time": 2520,
        "updated": "2026-11-03T08:55:00Z",
    },
]


def _put(runtime, events) -> None:
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


def test_import_builds_weeks_targets_and_sessions_and_versions_only_on_change(runtime) -> None:
    _put(runtime, EVENTS)
    first = plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    assert (first["weeks"], first["targets"], first["sessions"], first["version"]) == (1, 1, 1, 1)
    assert first["unparsed"] == {}
    held = plan_model.summary(runtime.engine)
    assert held and (held["weeks"], held["sessions"], held["status"]) == (1, 1, "shadow")
    again = plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    assert again["changed"] is False and again["version"] == 1
    changed = [dict(e) for e in EVENTS]
    changed[2]["description"] = changed[2]["description"].replace("30m", "35m")
    _put(runtime, changed)
    assert plan_model.import_from_intervals(runtime.engine, prefix="lake27")["version"] == 2


def test_dry_run_writes_nothing(runtime) -> None:
    _put(runtime, EVENTS)
    out = plan_model.import_from_intervals(runtime.engine, prefix="lake27", dry_run=True)
    assert out["changed"] is True and plan_model.summary(runtime.engine) is None


def test_other_workouts_in_the_plan_period_join_and_shadow_refresh_follows(runtime) -> None:
    group = {
        "id": 9,
        "category": "WORKOUT",
        "type": "Run",
        "start_date_local": "2026-11-19T00:00:00",
        "name": "Training geven",
        "description": "- 55m Z1 HR",
        "updated": "2026-11-03T08:00:00Z",
    }
    _put(runtime, EVENTS)
    assert plan_model.shadow_refresh(runtime.engine) is None  # nothing imported yet
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    _put(runtime, [*EVENTS, group])
    out = plan_model.shadow_refresh(runtime.engine)
    assert out and out["sessions"] == 2 and out["version"] == 2


def test_vibesynergy_leads_mirror_writes_changes_one_way_and_puts_drift_back(
    runtime, monkeypatch
) -> None:
    from datetime import date

    from vibesynergy.query import icu_plan

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    assert plan_model.source(runtime.engine) == "intervals"
    calls: list[tuple] = []
    monkeypatch.setattr(
        icu_plan,
        "update_event",
        lambda engine, client, **k: (
            calls.append(("update", k["event_id"], k["changes"]["description"]))
            or {"event_id": k["event_id"], "version": "v2"}
        ),
    )
    monkeypatch.setattr(
        icu_plan,
        "create_event",
        lambda engine, client, **k: (
            calls.append(("create", k["external_id"])) or {"event_id": "77", "version": "v1"}
        ),
    )
    # Nothing changed: the mirror writes nothing.
    assert plan_model.mirror(runtime.engine, None)["unchanged"] == 1 and calls == []
    plan_model.set_source(runtime.engine, "vibesynergy")
    # The coach changes the test in VibeSynergy and adds a session: one update, one create.
    plan_model.set_session(
        runtime.engine,
        session_id="lake27:2026-11-17:run-test",
        day=date(2026, 11, 17),
        name="Looptest 30'",
        sport="Run",
        description="Test.\n\nWarmup\n- 15m Z1-Z2 HR\n\nMain set\n- 30m Z4-Z5 HR",
        reason="langere warming-up",
        author="Claude",
    )
    plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=date(2026, 11, 18),
        name="Duurloop Z2 40'",
        sport="Run",
        description="- 40m Z2 HR",
        reason="dag 9",
        author="Claude",
        slot="run",
    )
    out = plan_model.mirror(runtime.engine, None)
    assert [c[0] for c in calls] == ["update", "create"]
    assert "15m Z1-Z2 HR" in calls[0][2] and calls[1][1] == "lake27:2026-11-18:run"
    assert out["errors"] == []
    # While VibeSynergy leads, an import from intervals.icu never overwrites the model.
    assert plan_model.import_from_intervals(runtime.engine, prefix="lake27").get("leading") is True


def test_a_change_made_in_intervals_is_drift(runtime) -> None:
    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    assert plan_model.drift(runtime.engine) == []
    edited = [dict(e) for e in EVENTS]
    edited[2]["description"] = edited[2]["description"].replace("30m", "25m")
    _put(runtime, edited)
    assert [d["session_id"] for d in plan_model.drift(runtime.engine)] == [
        "lake27:2026-11-17:run-test"
    ]


def test_no_hardcoded_plan(runtime: Any) -> None:
    # without a plan nothing is assumed; the first import names it after its tag.
    _put(runtime, EVENTS)
    assert plan_model.current_plan_id(runtime.engine) is None
    assert plan_model.source(runtime.engine) == "intervals"
    with pytest.raises(ValueError):
        plan_model.set_source(runtime.engine, "vibesynergy")
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    assert plan_model.current_plan_id(runtime.engine) == "lake27"
    held = plan_model.summary(runtime.engine)
    assert held is not None and held["name"] == "lake27"


def test_week_note_and_target_change_with_the_sessions_and_mirror(runtime, monkeypatch) -> None:
    # COACH-5: the week note and the weekly targets are part of the model too.
    from vibesynergy.query import icu_plan

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    plan_model.set_source(runtime.engine, "vibesynergy")
    calls: list[tuple] = []
    monkeypatch.setattr(
        icu_plan,
        "update_event",
        lambda engine, client, **k: (
            calls.append(("update", k["event_id"], k["changes"])) or {"event_id": k["event_id"]}
        ),
    )
    monkeypatch.setattr(
        icu_plan,
        "create_event",
        lambda engine, client, **k: calls.append(("create", k)) or {"event_id": "88"},
    )
    assert plan_model.mirror(runtime.engine, None)["updated"] == [] and calls == []
    with pytest.raises(ValueError, match="no week 9"):
        plan_model.set_week(runtime.engine, week=9, note="x", reason="r", author="Claude")
    with pytest.raises(ValueError, match="reason"):
        plan_model.set_week(runtime.engine, week=3, note="x", reason=" ", author="Claude")
    plan_model.set_week(
        runtime.engine, week=3, note="Rustiger: looptest blijft.", reason="ziek", author="Claude"
    )
    plan_model.set_target(
        runtime.engine, week=3, sport="run", time_s=12_600, reason="ziek", author="Claude"
    )
    plan_model.set_target(
        runtime.engine, week=3, sport="swim", time_s=3_600, reason="ziek", author="Claude"
    )
    out = plan_model.mirror(runtime.engine, None)
    assert out["errors"] == []
    updates = {c[1]: c[2] for c in calls if c[0] == "update"}
    assert updates == {
        "1": {"description": "Rustiger: looptest blijft."},
        "2": {"time_target": 12_600},
    }
    created = [c[1] for c in calls if c[0] == "create"]
    assert len(created) == 1 and created[0]["external_id"] == "lake27:target:03:swim"
    assert created[0]["category"] == "TARGET" and created[0]["sport"] == "Swim"
    assert created[0]["targets"] == {"time_target": 3_600} and created[0]["for_week"] is True
    # No values removes the target.
    plan_model.set_target(runtime.engine, week=3, sport="swim", reason="toch niet", author="Claude")
    assert plan_model.summary(runtime.engine)["targets"] == 1


def test_export_lists_week_notes_and_sessions_in_date_order(runtime) -> None:
    # the plan as a table, readable without VibeSynergy.
    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    rows = plan_model.export_rows(runtime.engine)
    assert rows[0]["sport"] == ""  # the week note first, on its Monday
    assert rows[0]["date"] == "2026-11-16" and rows[0]["workout"] == "Tests: looptest 30'."
    session = rows[1]
    assert session["week"] == 3 and session["sport"] and "30m" in session["workout"]
    text = plan_model.export_csv(runtime.engine)
    assert text.startswith("﻿date,week,phase,week_kind,sport,name,workout\n")
    assert plan_model.export_rows(runtime.engine, plan_id="nope") == []


def test_portal_plan_export_is_a_csv_download(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = client.get("/api/plan/export.csv")
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/csv")
    assert reply.headers["content-disposition"].startswith('attachment; filename="plan-')
    assert "Tests: looptest 30'." in reply.text


def test_move_and_skip_a_session_reach_intervals(runtime, monkeypatch) -> None:
    # step 3: move and skip in the model; the mirror moves or removes the copy.
    from vibesynergy.query import icu_plan

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    plan_model.set_source(runtime.engine, "vibesynergy")
    calls: list[tuple] = []
    monkeypatch.setattr(
        icu_plan,
        "update_event",
        lambda engine, client, **k: (
            calls.append(("update", k["event_id"], k["changes"]["start_date_local"]))
            or {"event_id": k["event_id"], "version": "v2"}
        ),
    )
    monkeypatch.setattr(
        icu_plan,
        "delete_event",
        lambda engine, client, **k: calls.append(("delete", k["event_id"])) or {},
    )
    session = plan_model.view(runtime.engine, date(2026, 11, 16), date(2026, 11, 22))["sessions"][0]
    sid = session["session_id"] if "session_id" in session else session["id"]
    with pytest.raises(ValueError, match="no session"):
        plan_model.move_session(
            runtime.engine, session_id="lake27:nope", day=date(2026, 11, 18), reason="r", author="C"
        )
    plan_model.move_session(
        runtime.engine, session_id=sid, day=date(2026, 11, 18), reason="ziek", author="Claude"
    )
    plan_model.mirror(runtime.engine, None)
    assert calls == [("update", "3", "2026-11-18T00:00:00")]
    calls.clear()
    plan_model.skip_session(runtime.engine, session_id=sid, reason="ziek", author="Claude")
    with pytest.raises(ValueError, match="not planned"):
        plan_model.skip_session(runtime.engine, session_id=sid, reason="again", author="Claude")
    out = plan_model.mirror(runtime.engine, None)
    assert calls == [("delete", "3")] and out["removed"] == [sid]
    calls.clear()
    assert plan_model.mirror(runtime.engine, None)["removed"] == [] and calls == []


def test_a_coach_change_is_mirrored_soon_not_at_the_next_sync(runtime, monkeypatch) -> None:
    # an MCP plan write queues one plan_mirror job; the worker runs the mirror.
    import anyio
    from mcp import Client

    from vibesynergy.mcp.server import build_server

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    plan_model.set_source(runtime.engine, "vibesynergy")

    async def go(name, args):
        async with Client(build_server(runtime)) as client:
            return await client.call_tool(name, args)

    anyio.run(go, "vibesynergy_plan_week_set", {"reason": "ziek", "week": 3, "note": "Rustig."})
    anyio.run(
        go,
        "vibesynergy_plan_target_set",
        {"reason": "ziek", "week": 3, "sport": "run", "time_s": 9000},
    )
    queued = [j for j in runtime.jobs.recent(limit=10) if j["kind"] == plan_model.MIRROR_JOB]
    assert len(queued) == 1  # one pending mirror covers both changes
    ran = []
    monkeypatch.setattr(
        plan_model, "mirror", lambda engine, client, plan_id: ran.append(plan_id) or {"drift": []}
    )
    monkeypatch.setattr(
        "vibesynergy.sources.intervals_icu.client.IntervalsIcuClient.from_env",
        staticmethod(lambda: None),
    )
    assert plan_model.run_mirror(runtime.engine) == {"drift": []} and ran == ["lake27"]
    plan_model.set_source(runtime.engine, "intervals")
    assert plan_model.run_mirror(runtime.engine) is None


def test_view_has_the_weeks_with_note_and_targets(runtime) -> None:
    # the coach sees what it can change: sessions, week notes and weekly targets.
    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    out = plan_model.view(runtime.engine, date(2026, 11, 18), date(2026, 11, 20))
    assert [w["week"] for w in out["weeks"]] == [3]
    assert out["weeks"][0]["note"] == "Tests: looptest 30'."
    assert out["weeks"][0]["targets"] == [
        {"sport": "run", "time_s": 16200, "distance_m": None, "load": None}
    ]


def test_portal_plan_export_as_excel_and_pdf(runtime) -> None:
    """the plan to download as Excel and PDF, in place of the Google Sheet."""
    import io
    import re
    import zipfile
    from xml.dom import minidom

    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = client.get("/api/plan/export.xlsx?lang=nl")
    assert reply.status_code == 200 and "spreadsheetml" in reply.headers["content-type"]
    book = zipfile.ZipFile(io.BytesIO(reply.content))
    for name in book.namelist():
        minidom.parseString(book.read(name))  # every part is well-formed XML
    sheet = book.read("xl/worksheets/sheet1.xml").decode()
    assert ">Datum<" in sheet and "Tests: looptest 30'." in sheet
    reply = client.get("/api/plan/export.pdf?lang=en")
    assert reply.status_code == 200 and reply.headers["content-type"] == "application/pdf"
    pdf = reply.content
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    # Every xref offset points at its object.
    start = int(re.search(rb"startxref\n(\d+)", pdf).group(1))
    entries = re.findall(rb"(\d{10}) 00000 n ", pdf[start:])
    for n, offset in enumerate(entries, start=1):
        assert pdf[int(offset) :].startswith(b"%d 0 obj" % n)
    assert b"Training plan" in pdf and b"Tests: looptest 30'." in pdf


def test_plan_export_carries_what_you_need_without_a_coach(runtime) -> None:
    """Races, thresholds with their zones and the plan's own rules, as in the old Sheet."""
    import io
    import zipfile
    from datetime import date

    from vibesynergy.query import athlete_threshold, knowledge, plan_export

    row = athlete_threshold.propose_threshold(
        runtime.engine,
        sport="run",
        metric="threshold_pace",
        value=310,
        unit="s/km",
        method="manual",
    )
    athlete_threshold.confirm_threshold(runtime.engine, row["id"])
    knowledge.upsert_article(
        runtime.engine,
        slug="test-2027-plan",
        author="Claude",
        title="Plan",
        body_md="Het plan.",
        topic="test-2027",
        rules=[{"id": "TST-1", "text": "Maximaal 2 harde dagen per week."}],
    )
    sections = {s["key"]: s for s in plan_export.extras(runtime.engine, date(2026, 11, 10))}
    assert ["run", "threshold_pace", "5:10/km", ""] in sections["thresholds"]["rows"]
    assert any("pace zone" in r[1] for r in sections["thresholds"]["rows"])
    assert sections["rules"]["rows"] == [["TST-1", "Maximaal 2 harde dagen per week.", "", ""]]
    book = zipfile.ZipFile(io.BytesIO(plan_export.xlsx([], "nl", list(sections.values()))))
    assert "xl/worksheets/sheet3.xml" in book.namelist()
    assert b"TST-1" in plan_export.pdf([], "nl", sections=list(sections.values()))
