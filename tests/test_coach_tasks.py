"""The coach's task register."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

from vibesynergy.query import coach_tasks
from vibesynergy.store.schema import coach_decision_log

PROMPTS = Path(__file__).resolve().parents[1] / "docs" / "coach-prompts"


def _entry(runtime, entry_type: str, day: date, key: str | None = None) -> None:
    with runtime.engine.begin() as conn:
        conn.execute(
            coach_decision_log.insert().values(
                subject_id=1,
                at=datetime.combine(day, datetime.min.time(), UTC),
                kind="note",
                author="Claude",
                entry_type=entry_type,
                idempotency_key=key,
                for_date=day,
                text="x",
                meta={},
            )
        )


def _missing(runtime, now: datetime) -> dict[str, list[str]]:
    rows = coach_tasks.status(runtime.engine, now, "Europe/Amsterdam")
    return {r["id"]: r["missing"] for r in rows}


def test_a_skipped_evening_and_weekly_review_are_missing(runtime) -> None:
    for day in (date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)):
        _entry(runtime, "morning", day, f"morning-{day}")
    _entry(runtime, "evening", date(2026, 10, 30), "evening-2026-10-30")
    _entry(runtime, "evening", date(2026, 11, 1), "evening-2026-11-01")
    _entry(runtime, "note", date(2026, 10, 25), "weekreview:2026-W43")
    # Sunday 1-11, 23:45 local: Saturday's evening is missing and so is the weekly review.
    out = _missing(runtime, datetime(2026, 11, 1, 22, 45, tzinfo=UTC))
    assert out["evening"] == ["2026-10-31"]
    assert out["morning"] == []
    assert out["weekly"] == ["2026-11-01"]
    assert out["insight"] == [] and "fallback_sheet" not in out

    _entry(runtime, "note", date(2026, 11, 1), "weekreview:2026-W44")
    assert _missing(runtime, datetime(2026, 11, 1, 22, 45, tzinfo=UTC))["weekly"] == []


def test_a_task_that_never_ran_is_not_missing(runtime) -> None:
    out = _missing(runtime, datetime(2026, 11, 1, 22, 45, tzinfo=UTC))
    assert all(days == [] for days in out.values())


def test_a_run_not_yet_due_is_not_missing(runtime) -> None:
    _entry(runtime, "morning", date(2026, 11, 1), "morning-2026-11-01")
    # 08:00 local on Monday 2-11: this morning's report is not due until 11:00.
    out = _missing(runtime, datetime(2026, 11, 2, 7, 0, tzinfo=UTC))
    assert "2026-11-02" not in out["morning"]


def test_prompts_match_the_register() -> None:
    for task in coach_tasks.TASKS:
        if task.prompt is None:
            continue
        text = (PROMPTS / f"{task.prompt}.md").read_text(encoding="utf-8")
        assert f'entry_type "{task.entry_type}"' in text, task.id
        if task.key_prefix and task.entry_type != "note":
            assert re.search(rf"idempotency_key=?\s*\"{re.escape(task.key_prefix)}", text), task.id


def test_portal_lists_the_register(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    tasks = api.get("/api/coach/tasks").json()["data"]["tasks"]
    assert [t["id"] for t in tasks] == [t.id for t in coach_tasks.TASKS]
    assert tasks[0]["due"] == "11:00" and tasks[0]["missing"] == []


def test_prompts_and_register_agree(runtime) -> None:
    # one source. Each task's prompt exists, names the task's inputs and its
    # idempotency key, and every tool a prompt names exists.
    import re

    from vibesynergy.mcp.server import build_server
    from vibesynergy.query import coach_prompts, coach_tasks

    tools = set(build_server(runtime).vibesynergy_tool_scopes)
    for task in coach_tasks.TASKS:
        assert set(task.inputs) <= tools, (task.id, set(task.inputs) - tools)
        if not task.prompt:
            continue
        text = coach_prompts.seed_text(task.prompt)
        assert text, task.prompt
        assert [i for i in task.inputs if i not in text] == [], task.id
        assert not task.key_prefix or task.key_prefix in text, task.id
        named = set(re.findall(r"vibesynergy_[a-z_]+", text))
        assert named <= tools, (task.prompt, named - tools)


def test_the_api_runners_week_review_counts(runtime) -> None:
    """8-11: the API coach keys its week review 'weekly-<date>'; it showed as skipped."""
    _entry(runtime, "note", date(2026, 11, 1), "weekly-2026-11-01")
    assert _missing(runtime, datetime(2026, 11, 1, 22, 45, tzinfo=UTC))["weekly"] == []
    _entry(runtime, "note", date(2026, 11, 8), "weekly-2026-11-08")
    assert _missing(runtime, datetime(2026, 11, 8, 22, 45, tzinfo=UTC))["weekly"] == []
