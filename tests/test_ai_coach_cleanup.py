"""Leftovers of the internal AI coach: one public snapshot module, and the activity
assessment read comes from the coach feed."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from test_evidence import START, activity, put

from vibesynergy.mcp.server import build_server
from vibesynergy.query import assessments, coach_workouts, snapshot

QUERY = Path(__file__).parents[1] / "src" / "vibesynergy" / "query"


def test_no_module_reaches_into_the_coach_modules_private_helpers() -> None:
    private = re.compile(r"\b(assessments|coach_workouts)\._[a-z]\w*")
    hits = [
        f"{path.name}: {match.group(0)}"
        for path in sorted(QUERY.rglob("*.py"))
        for match in private.finditer(path.read_text())
    ]
    assert hits == []


def test_snapshot_helpers() -> None:
    from datetime import date

    assert snapshot.json_safe({"day": date(2026, 9, 20)}) == {"day": "2026-09-20"}
    assert snapshot.semantic([{"a": 1, "raw_item_id": 3, "b": {"updated_at": "x", "c": 2}}]) == [
        {"a": 1, "b": {"c": 2}}
    ]

    class Conn:
        pass

    conn = Conn()
    with snapshot.SnapshotReader(conn).connect() as got:
        assert got is conn


def _assess(rt, text: str, *, key: str, **extra):
    return coach_workouts.coach_write(
        rt.engine,
        entry_type="activity_assessment",
        summary=text,
        for_date=START.date(),
        author="client:coach",
        idempotency_key=key,
        **extra,
    )


def test_activity_assessment_reads_the_coach_feed(runtime) -> None:
    put(runtime, "garmin", activity())
    other = replace(activity(), source_id="2", start_utc=START + timedelta(hours=5))
    other = replace(other, fit_id="test-evidence-2")
    put(runtime, "garmin", other)
    empty = assessments.read(runtime, "garmin:1")
    assert empty["state"] == "missing"
    assert empty["assessment"] is None
    assert empty["decision_log"] == []

    _assess(runtime, "Easy and steady; well paced.", key="a1", activity_id="garmin:1")
    _assess(
        runtime,
        "Second look: cadence held.",
        key="a2",
        meta={"evidence_refs": ["garmin:1"]},
    )
    _assess(runtime, "About the other run.", key="a3", activity_id="garmin:2")

    ready = assessments.read(runtime, "garmin:1")
    assert ready["state"] == "ready"
    assert ready["source"] == "coach_feed"
    texts = [entry["text"] for entry in ready["decision_log"]]
    assert texts == ["Second look: cadence held.", "Easy and steady; well paced."]
    assert ready["assessment"]["text"] == "Second look: cadence held."
    assert ready["coaching_state"]["reason"] == "retired"

    tool = build_server(runtime)._tool_manager.get_tool("vibesynergy_activity_assessment")
    data = tool.fn("garmin:2")["data"]
    assert [entry["text"] for entry in data["decision_log"]] == ["About the other run."]
    assert assessments.read(runtime, "garmin:unknown")["state"] == "missing"
