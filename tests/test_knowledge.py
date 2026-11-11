"""Knowledge base: versions, full-text search, proposals and the portal decision."""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import text

from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query.knowledge import (
    approve_proposal,
    get_article,
    get_rule,
    list_articles,
    pending_proposals,
    propose_change,
    reject_proposal,
    search,
    upsert_article,
)


def _article(runtime, **overrides):
    fields = {
        "slug": "hrv-basics",
        "title": "HRV and easy days",
        "body_md": (
            "A suppressed morning HRV means keep the day easy. Plews described this pattern."
        ),
        "topic": "recovery",
        "evidence_grade": "B",
        "status": "active",
        "author": "client:coach",
        "rules": [
            {
                "id": "HRV-2",
                "text": "Drop the interval if morning HRV is suppressed.",
                "grade": "B",
                "active": True,
            }
        ],
        "sources": [
            {
                "id": "plews-2017",
                "citation": "Plews and others, HRV-guided training, 2017.",
                "doi": "10.1000/synthetic.hrv",
                "url": "https://example.test/hrv",
            }
        ],
    }
    fields.update(overrides)
    return upsert_article(runtime.engine, **fields)


def test_upsert_keeps_every_version_and_ignores_an_identical_write(runtime) -> None:
    first = _article(runtime)
    assert first["version"] == 1
    assert first["author"] == "client:coach"
    assert first["rules"][0]["id"] == "HRV-2"
    assert first["rules"][0]["version"] == 1
    assert first["sources"][0]["version"] == 1
    assert len(first["history"]) == 1

    same = _article(runtime)
    assert same["version"] == 1
    assert len(same["history"]) == 1
    assert same["rules"][0]["version"] == 1

    second = _article(
        runtime,
        body_md="A suppressed morning HRV means keep the day easy, and skip the intervals.",
    )
    assert second["version"] == 2
    assert [row["version"] for row in second["history"]] == [1, 2]
    assert "Plews described this pattern." in second["history"][0]["body_md"]
    assert second["rules"][0]["version"] == 1

    rule = get_rule(runtime.engine, "HRV-2")
    assert rule["article"]["slug"] == "hrv-basics"
    assert rule["rule"]["history"][0]["text"].startswith("Drop the interval")


def test_rule_and_source_versions_are_independent(runtime) -> None:
    _article(runtime)
    updated = upsert_article(
        runtime.engine,
        slug="hrv-basics",
        author="client:coach",
        rules=[
            {
                "id": "HRV-2",
                "text": "If morning HRV is suppressed, run easy instead of intervals.",
                "grade": "A",
                "active": True,
            }
        ],
        sources=[
            {
                "id": "plews-2017",
                "citation": "Plews and others, HRV-guided training, revised note.",
                "doi": "10.1000/synthetic.hrv",
                "url": "https://example.test/hrv",
            }
        ],
    )
    assert updated["version"] == 1
    rule = updated["rules"][0]
    source = updated["sources"][0]
    assert rule["version"] == 2
    assert [row["grade"] for row in rule["history"]] == ["B", "A"]
    assert source["version"] == 2
    assert source["history"][0]["citation"].endswith("2017.")


def test_revert_copies_an_older_article_forward(runtime) -> None:
    _article(runtime)
    _article(runtime, body_md="Temporary wording that should not stay current.")
    restored = upsert_article(runtime.engine, slug="hrv-basics", author="client:coach", revert_to=1)
    assert restored["version"] == 3
    assert "Plews described this pattern." in restored["body_md"]
    assert len(restored["history"]) == 3
    assert "Temporary wording" in restored["history"][1]["body_md"]


def test_search_uses_fts_and_can_filter_by_topic(runtime) -> None:
    _article(runtime)
    _article(
        runtime,
        slug="sleep-window",
        title="Sleep window",
        body_md="A short night is a reason to keep the next day easy.",
        topic="sleep",
        rules=[],
        sources=[],
    )
    found = search(runtime.engine, "Plews")
    by_ref = {hit["ref"]: hit for hit in found}
    assert by_ref["hrv-basics"]["kind"] == "article"
    assert by_ref["plews-2017"]["kind"] == "source"

    rules = search(runtime.engine, "suppressed interval")
    assert any(hit["kind"] == "rule" and hit["ref"] == "HRV-2" for hit in rules)

    assert search(runtime.engine, "Plews", topic="sleep") == []
    assert search(runtime.engine, "short night", topic="sleep")[0]["ref"] == "sleep-window"
    assert search(runtime.engine, "HRV-2")[0]["ref"] in {"HRV-2", "hrv-basics"}

    with runtime.engine.connect() as conn:
        ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'kb_fts'")).scalar()
    assert "fts5" in ddl

    with pytest.raises(ValueError, match="word"):
        search(runtime.engine, "...")


def test_invalid_grade_and_slug_are_refused(runtime) -> None:
    with pytest.raises(ValueError, match="grade"):
        _article(runtime, evidence_grade="Z")
    with pytest.raises(ValueError, match="slug"):
        _article(runtime, slug="HRV Basics")
    assert list_articles(runtime.engine) == []


def test_proposal_waits_until_approval_and_reject_leaves_the_text(runtime) -> None:
    _article(runtime)
    proposal = propose_change(
        runtime.engine,
        kind="rule",
        target="HRV-2",
        rationale="The old sentence named the session type too narrowly.",
        author="client:coach",
        changes={"text": "Keep the day easy when morning HRV is suppressed."},
    )
    assert proposal["status"] == "pending"
    assert get_rule(runtime.engine, "HRV-2")["rule"]["version"] == 1
    assert pending_proposals(runtime.engine)[0]["current"]["text"].startswith("Drop the interval")

    rejected = reject_proposal(runtime.engine, proposal["id"], actor="user")
    assert rejected["status"] == "rejected"
    assert pending_proposals(runtime.engine) == []
    assert get_rule(runtime.engine, "HRV-2")["rule"]["text"].startswith("Drop the interval")

    again = propose_change(
        runtime.engine,
        kind="rule",
        target="HRV-2",
        rationale="Say what to do today.",
        author="client:coach",
        changes={"text": "Keep the day easy when morning HRV is suppressed."},
    )
    approved = approve_proposal(runtime.engine, again["id"], actor="user")
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "user"
    rule = get_rule(runtime.engine, "HRV-2")["rule"]
    assert rule["version"] == 2
    assert rule["text"].startswith("Keep the day easy")
    assert rule["history"][0]["text"].startswith("Drop the interval")
    assert rule["history"][1]["author"] == "client:coach"

    with pytest.raises(Exception, match="not pending"):
        approve_proposal(runtime.engine, again["id"], actor="user")


def test_approve_refuses_a_stale_proposal(runtime) -> None:
    _article(runtime)
    proposal = propose_change(
        runtime.engine,
        kind="rule",
        target="HRV-2",
        rationale="A different sentence.",
        author="client:coach",
        changes={"text": "First suggestion."},
    )
    upsert_article(
        runtime.engine,
        slug="hrv-basics",
        author="client:coach",
        rules=[
            {
                "id": "HRV-2",
                "text": "Already rewritten.",
                "grade": "B",
                "active": True,
            }
        ],
    )
    with pytest.raises(Exception, match="changed"):
        approve_proposal(runtime.engine, proposal["id"], actor="user")
    assert get_rule(runtime.engine, "HRV-2")["rule"]["text"] == "Already rewritten."


def _call(runtime, scopes: frozenset[str] | None, tool: str, arguments: dict):
    async def go():
        tokens = None
        if scopes is not None:
            tokens = set_connection("coach-app", scopes=scopes, label="Coach")
        try:
            async with Client(build_server(runtime)) as client:
                return await client.call_tool(tool, arguments)
        finally:
            if tokens is not None:
                reset_connection(tokens)

    return anyio.run(go)


def test_mcp_tools_follow_read_write_and_propose_scopes(runtime) -> None:
    _article(runtime)
    listed = _call(runtime, None, "vibesynergy_kb_search", {"query": "Plews"})
    assert not listed.is_error
    hits = listed.structured_content["data"]["hits"]
    assert any(hit["ref"] == "hrv-basics" for hit in hits)

    denied = _call(runtime, frozenset({"read"}), "vibesynergy_kb_upsert", {"slug": "hrv-basics"})
    assert denied.is_error
    assert "isn't allowed" in denied.content[0].text

    denied_propose = _call(
        runtime,
        frozenset({"coach.write"}),
        "vibesynergy_kb_propose_change",
        {"kind": "rule", "target": "HRV-2", "rationale": "Because.", "new_text": "Easy day."},
    )
    assert denied_propose.is_error

    written = _call(
        runtime,
        frozenset({"coach.write"}),
        "vibesynergy_kb_upsert",
        {
            "slug": "hrv-basics",
            "body_md": "A suppressed morning HRV means keep the day easy. Updated by the coach.",
        },
    )
    assert not written.is_error
    assert written.structured_content["data"]["version"] == 2
    assert written.structured_content["data"]["author"].startswith("client:")

    fetched = _call(runtime, frozenset({"read"}), "vibesynergy_kb_get", {"rule_id": "HRV-2"})
    assert not fetched.is_error
    assert fetched.structured_content["data"]["rule"]["id"] == "HRV-2"

    proposed = _call(
        runtime,
        frozenset({"plan.propose"}),
        "vibesynergy_kb_propose_change",
        {
            "kind": "rule",
            "target": "HRV-2",
            "rationale": "Name the action for today.",
            "new_text": "Keep today easy when HRV is suppressed.",
        },
    )
    assert not proposed.is_error
    assert proposed.structured_content["data"]["status"] == "pending"
    assert get_rule(runtime.engine, "HRV-2")["rule"]["version"] == 1


def test_portal_lists_pending_proposals_and_decides_them(runtime) -> None:
    _article(runtime)
    proposal = propose_change(
        runtime.engine,
        kind="rule",
        target="HRV-2",
        rationale="Say what to do today.",
        author="client:coach",
        changes={"text": "Keep the day easy when morning HRV is suppressed."},
    )
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    page = client.get("/api/knowledge")
    assert page.status_code == 200
    body = page.json()["data"]
    assert body["articles"][0]["slug"] == "hrv-basics"
    assert body["proposals"][0]["id"] == proposal["id"]
    assert body["proposals"][0]["changes"]["text"].startswith("Keep the day easy")

    article = client.get("/api/knowledge/article", params={"slug": "hrv-basics"})
    assert article.status_code == 200
    assert article.json()["data"]["rules"][0]["text"].startswith("Drop the interval")

    approved = client.post(
        "/api/knowledge/proposal/approve",
        json={"id": proposal["id"]},
        headers={"x-vibesynergy": "portal"},
    )
    assert approved.status_code == 200
    assert get_rule(runtime.engine, "HRV-2")["rule"]["version"] == 2
    assert client.get("/api/knowledge").json()["data"]["proposals"] == []

    second = propose_change(
        runtime.engine,
        kind="article",
        target="hrv-basics",
        rationale="Retire the note.",
        author="client:coach",
        changes={"status": "retired"},
    )
    rejected = client.post(
        "/api/knowledge/proposal/reject",
        json={"id": second["id"]},
        headers={"x-vibesynergy": "portal"},
    )
    assert rejected.status_code == 200
    assert get_article(runtime.engine, "hrv-basics")["status"] == "active"
    again = client.post(
        "/api/knowledge/proposal/reject",
        json={"id": second["id"]},
        headers={"x-vibesynergy": "portal"},
    )
    assert again.status_code == 409
    assert again.json()["error"] == "not_pending"


def test_an_article_cannot_take_another_articles_rule_id(runtime) -> None:
    """9-11: writing SWIM-1..3 for a new article silently took another article's rules."""
    _article(runtime)
    with pytest.raises(ValueError, match="belongs to the article 'hrv-basics'"):
        _article(
            runtime,
            slug="other-article",
            title="Other",
            rules=[{"id": "HRV-2", "text": "Something else.", "grade": "C", "active": True}],
        )
    assert get_rule(runtime.engine, "HRV-2")["article"]["slug"] == "hrv-basics"
