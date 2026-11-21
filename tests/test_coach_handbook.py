"""The coach handbook tool: protocol text, live KB index and the caller's scopes."""

from __future__ import annotations

import re
from pathlib import Path

import anyio
from conftest import drop_workout_method
from mcp import Client

from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import GUIDE, build_server
from vibesynergy.query import coach_handbook
from vibesynergy.query.knowledge import upsert_article

ROOT = Path(__file__).resolve().parents[1]


def _seed(runtime) -> None:
    upsert_article(
        runtime.engine,
        slug="coach-loop",
        title="Coach loop",
        body_md="Morning, evening and weekly runs.",
        topic="coach-loop",
        evidence_grade="C",
        status="active",
        author="user",
        rules=[
            {"id": "COACH-2", "text": "Cite rules.", "grade": "C", "active": True},
            {"id": "COACH-1", "text": "One coach.", "grade": "C", "active": True},
            {"id": "COACH-9", "text": "Retired rule.", "grade": "C", "active": False},
        ],
    )
    upsert_article(
        runtime.engine,
        slug="old-loop",
        title="Old loop",
        body_md="The former coach's method.",
        topic="coach-loop",
        evidence_grade="C",
        status="retired",
        author="user",
        rules=[{"id": "OLD-1", "text": "Gone.", "grade": "C", "active": True}],
    )


def _call(runtime, arguments: dict, scopes: frozenset[str] | None = None):
    async def go():
        tokens = set_connection("coach-app", scopes=scopes, label="Coach") if scopes else None
        try:
            async with Client(build_server(runtime)) as client:
                return await client.call_tool("vibesynergy_coach_handbook", arguments)
        finally:
            if tokens is not None:
                reset_connection(tokens)

    return anyio.run(go)


def test_handbook_returns_protocol_and_active_kb_index(runtime) -> None:
    drop_workout_method(runtime)
    _seed(runtime)
    result = _call(runtime, {})
    assert not result.is_error
    data = result.structured_content["data"]
    text = (ROOT / "docs" / "COACH_PROTOCOL.md").read_text(encoding="utf-8")
    assert data["protocol_md"] == text
    assert re.fullmatch(r"[0-9a-f]{12}", data["protocol_version"])
    assert data["start_here"][0] == "vibesynergy_coach_handbook"
    knowledge = data["knowledge"]
    assert [a["slug"] for a in knowledge["articles"]] == ["coach-loop"]
    # what each article is, who keeps it, and how many of its sources are checked.
    first = knowledge["articles"][0]
    assert (first["kind"], first["owner"]) == ("routine", "athlete")
    assert first["sources"] == {"total": 0, "verified": 0}
    assert knowledge["rule_ids"] == ["COACH-1", "COACH-2"]
    assert knowledge["rules"][0] == {
        "id": "COACH-1",
        "article": "coach-loop",
        "grade": "C",
        "version": 1,
    }
    assert data["access"]["missing"] == []
    assert result.structured_content["meta"]["warnings"] == []


def test_handbook_without_protocol_and_with_missing_scopes(runtime) -> None:
    drop_workout_method(runtime)
    result = _call(runtime, {"include_protocol": False}, scopes=frozenset({"read"}))
    assert not result.is_error
    data = result.structured_content["data"]
    assert "protocol_md" not in data
    assert data["protocol_version"]
    assert data["knowledge"] == {"articles": [], "rule_ids": [], "rules": []}
    assert data["access"]["scopes"] == ["read"]
    assert data["access"]["missing"] == ["coach.write", "plan.propose", "plan.write"]
    warnings = result.structured_content["meta"]["warnings"]
    assert any("plan.write" in w for w in warnings)


def test_handbook_needs_read_scope(runtime) -> None:
    result = _call(runtime, {}, scopes=frozenset({"coach.write"}))
    assert result.is_error


def test_handbook_resource_and_guide_point_to_it(runtime) -> None:
    async def go():
        async with Client(build_server(runtime)) as client:
            return await client.read_resource("vibesynergy://coach/handbook")

    contents = anyio.run(go).contents
    assert contents[0].text.startswith("# Coach protocol")
    assert GUIDE.startswith("Coaching? Call vibesynergy_coach_handbook first")


def test_handbook_names_only_tools_that_exist(runtime) -> None:
    """Every vibesynergy_* name in the protocol is a real tool, so the handbook can't drift."""
    server = build_server(runtime)
    tools = {tool.name for tool in server._tool_manager.list_tools()}
    named = set(re.findall(r"vibesynergy_[a-z_]+", coach_handbook.protocol_text() or ""))
    # Prefix shorthand in the table ("_update_event") is matched against the full names.
    unknown = {name for name in named if name not in tools and not name.endswith("_")}
    assert not unknown, unknown
    assert set(coach_handbook.COACH_SCOPES_NEEDED) <= {
        "read",
        "coach.write",
        "plan.propose",
        "plan.write",
    }


def test_article_kind_and_owner() -> None:
    assert coach_handbook.article_kind("coach-prompt-morning", "coach-prompts") == (
        "routine",
        "athlete",
    )
    assert coach_handbook.article_kind("evidence-sleep", "sleep") == ("evidence", "coach")
    assert coach_handbook.article_kind("swim-data-quality", "swim") == ("method", "coach")


def test_the_built_wheel_serves_the_protocol(tmp_path) -> None:
    """#119: an installed app served no protocol (docs/ was not in the wheel). Build the
    package the way CI does (sdist, then the wheel from it) and read the protocol from the
    unpacked wheel alone, away from this checkout."""
    import shutil
    import subprocess
    import sys
    import zipfile

    import pytest

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    out = tmp_path / "dist"
    subprocess.run(
        [uv, "build", "--out-dir", str(out), "--quiet"], cwd=ROOT, check=True, timeout=300
    )
    wheel = next(out.glob("vibesynergy-*.whl"))
    unpacked = tmp_path / "site"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(unpacked)
    found = subprocess.run(
        [
            sys.executable,
            "-c",
            "from vibesynergy.query import coach_handbook as h\n"
            "print(h._protocol_path())\n"
            "print(len(h.protocol_text() or ''))",
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(unpacked), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    path, size = found.stdout.split()
    assert path.startswith(str(unpacked)), path
    expected = (ROOT / "docs" / "COACH_PROTOCOL.md").read_text(encoding="utf-8")
    assert int(size) == len(expected) > 1000


def _handbook(runtime) -> dict:
    result = _call(runtime, {"include_protocol": False})
    assert not result.is_error
    return result.structured_content["data"]


def test_the_handbooks_tasks_are_the_registry_and_its_due_times_the_schedule(runtime) -> None:
    """#119: the handbook listed 4 tasks at 11:00 and 23:30; the harness has 14, at the
    times of Coach → Settings. Generated from the registry and the settings, never by hand."""
    from vibesynergy.coach_runner import registry
    from vibesynergy.coach_runner import settings as runner_settings

    def check(data: dict) -> None:
        schedule = runner_settings.load(runtime).schedule
        tasks = {t["id"]: t for t in data["tasks"]}
        assert list(tasks) == [t.id for t in registry.TASKS]
        for task in registry.TASKS:
            row = tasks[task.id]
            spec = schedule.get(task.id) if registry.schedule_trigger(task.id) else None
            if spec is None:
                assert row["due"] is None, task.id
                continue
            assert row["due"] == spec["time"], task.id
            assert row["on"] is spec["on"], task.id
            if "monthday" in spec:
                assert row["monthday"] == spec["monthday"], task.id
            else:
                assert row["days"] == spec["days"], task.id
            (trigger,) = [g for g in row["triggers"] if g["kind"] == "schedule"]
            assert trigger["time"] == spec["time"] and spec["time"] in trigger["when"]
            assert row["context"] == list(registry.chain_for(task.id)) or task.runs == "code"
            assert row["approval"] == list(task.approval)
            assert row["contract"]["entry_type"] == task.contract.entry_type

    data = _handbook(runtime)
    check(data)
    tasks = {t["id"]: t for t in data["tasks"]}
    assert (tasks["morning"]["due"], tasks["evening"]["due"]) == ("07:00", "21:45")
    assert tasks["weekly"]["days"] == [6]

    runner_settings.save(
        runtime,
        {
            "schedule": {
                "morning": {"time": "06:15"},
                "weekly": {"time": "18:30", "days": [5]},
                "kb": {"monthday": 3},
            }
        },
    )
    data = _handbook(runtime)
    check(data)
    tasks = {t["id"]: t for t in data["tasks"]}
    assert tasks["morning"]["due"] == "06:15"
    assert (tasks["weekly"]["due"], tasks["weekly"]["days"]) == ("18:30", [5])
    assert "Saturday 18:30" in tasks["weekly"]["trigger"]
    assert tasks["kb"]["monthday"] == 3
    assert data["timezone"] == runtime.settings.timezone


def test_the_handbook_carries_the_approval_steps_and_the_contact_window(runtime) -> None:
    from vibesynergy.mcp import roles
    from vibesynergy.query import plan_proposals

    data = _handbook(runtime)
    approval = data["approval"]
    assert set(approval["roles"]) == set(roles.ROLES)
    assert approval["roles"]["primary"]["may"] == roles.ROLE_MAY["primary"]
    assert approval["approval_hours"] == plan_proposals.APPROVAL_HOURS
    assert "vibesynergy_plan_session_set" in approval["write_policy"]["plan"]
    listed = {name for names in approval["write_policy"].values() for name in names}
    assert listed == set(roles.WRITE_POLICY)
    window = data["contact_window"]
    assert {"start", "end", "timezone", "open_now", "urgent_exception"} <= set(window)


def test_the_protocol_states_no_times_of_its_own() -> None:
    """Times are the settings' and reach a coach as tasks[].due; a time in the protocol
    text would drift from them (#119)."""
    text = coach_handbook.protocol_text() or ""
    assert not re.findall(r"\b[0-2]?\d:[0-5]\d\b", text)
    assert "contact_window" in text
