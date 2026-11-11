"""The coach handbook tool: protocol text, live KB index and the caller's scopes."""

from __future__ import annotations

import re
from pathlib import Path

import anyio
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
