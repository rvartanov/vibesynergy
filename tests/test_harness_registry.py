"""The AI harness's task registry."""

from __future__ import annotations

from vibesynergy.coach_runner import JOBS, registry


def test_every_running_job_is_a_registered_task_with_tool_use() -> None:
    for job in JOBS:
        task = registry.BY_ID[job]
        assert task.status == "live"
        assert "tool_use" in task.requires
        assert task.triggers and task.prompt


def test_chain_starts_with_the_fixed_base() -> None:
    chain = registry.chain_for("morning")
    assert chain[: len(registry.BASE_CONTEXT)] == registry.BASE_CONTEXT
    assert "plan_horizon" in chain and len(chain) == len(set(chain))


def test_a_model_without_tools_cannot_run_a_task() -> None:
    assert registry.model_ok("evening", {"tool_use"})
    assert not registry.model_ok("evening", set())


def test_runner_jobs_come_from_the_registry():
    from vibesynergy.coach_runner import JOBS, REPORT_JOBS, runner

    assert JOBS == (
        "morning",
        "evening",
        "weekly",
        "monthly",
        "block",
        "race",
        "test",
        "thresholds",
        "kb",
        "injury",
        "checkin",
        "insight",
    )
    assert JOBS[:-1] == REPORT_JOBS  # every model task but the insight writes a report
    assert runner.ENTRY_TYPES == {
        "morning": "morning",
        "evening": "evening",
        "weekly": "note",
        "monthly": "note",
        "block": "note",
        "race": "note",
        "test": "note",
        "thresholds": "note",
        "kb": "note",
        "injury": "note",
        "checkin": "note",
    }
