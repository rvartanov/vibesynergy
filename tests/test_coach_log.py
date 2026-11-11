"""Coach → knowledge, methods, memory and change log: query functions and endpoints."""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach, coach_log, coach_prompts, knowledge

HEADERS = {"x-vibesynergy": "portal"}


def _article(runtime, slug="evidence-hrv", topic="hrv-guided-training", **overrides):
    fields = {
        "slug": slug,
        "title": "HRV-guided training",
        "body_md": "Line one.\nLine two.\nLine three.",
        "topic": topic,
        "evidence_grade": "B",
        "status": "active",
        "author": "client:coach",
        "rules": [{"id": "HRV-4", "text": "Never decide on HRV alone.", "grade": "B"}],
        "sources": [{"id": "plews-2017", "citation": "Plews, 2017.", "url": "https://x.test/a"}],
    }
    fields.update(overrides)
    return knowledge.upsert_article(runtime.engine, **fields)


def _client(runtime) -> TestClient:
    return TestClient(create_portal_app(runtime), base_url="http://localhost")


def test_methods_groups_prompts_method_and_evidence_and_hides_retired(runtime) -> None:
    _article(runtime)
    _article(runtime, slug="claude-coach-loop", topic="coach-loop", rules=[], sources=[])
    _article(runtime, slug="swim-data-quality", topic="swim", rules=[], sources=[])
    # The athlete's plan article counts as method by its slug, whatever the plan is called.
    _article(runtime, slug="example-2027-plan", topic="example-2027", rules=[], sources=[])
    _article(
        runtime, slug="sherpa-loop", topic="sherpa-loop", status="retired", rules=[], sources=[]
    )
    coach_prompts.seed(runtime.engine)

    groups = knowledge.methods_index(runtime.engine)["groups"]
    assert [a["slug"] for a in groups["evidence"]] == ["evidence-hrv"]
    assert groups["evidence"][0]["rules"][0]["id"] == "HRV-4"
    assert groups["evidence"][0]["sources"] == 1
    assert {a["slug"] for a in groups["method"]} == {"claude-coach-loop", "example-2027-plan"}
    assert {a["slug"] for a in groups["prompts"]} == {
        f"coach-prompt-{job}" for job in coach_prompts.JOBS
    }

    index = knowledge.portal_index(runtime.engine)
    assert "sherpa-loop" not in {a["slug"] for a in index["articles"]}
    assert index["archived"] == 1
    assert [a["slug"] for a in knowledge.archived_articles(runtime.engine)] == ["sherpa-loop"]


def test_edit_article_and_rule_save_versions_and_refuse_a_stale_base(runtime) -> None:
    _article(runtime)
    saved = knowledge.edit_article(
        runtime.engine, "evidence-hrv", author="user", expected_version=1, body_md="New text."
    )
    assert saved["version"] == 2
    assert saved["author"] == "user"
    with pytest.raises(knowledge.KnowledgeError) as stale:
        knowledge.edit_article(
            runtime.engine, "evidence-hrv", author="user", expected_version=1, title="X"
        )
    assert stale.value.code == "stale"

    rule = knowledge.edit_rule(
        runtime.engine, "HRV-4", author="user", expected_version=1, active=False
    )["rule"]
    assert rule["version"] == 2
    assert rule["active"] is False
    assert rule["text"] == "Never decide on HRV alone."


def test_log_lists_every_version_with_who_and_what(runtime) -> None:
    _article(runtime)
    knowledge.edit_article(
        runtime.engine,
        "evidence-hrv",
        author="user",
        expected_version=1,
        body_md="Line one.\nLine 2.\nLine three.",
    )
    coach.save_draft(
        runtime.engine, "policy", {"instructions": "Short please."}, expected_version=0
    )
    coach.activate(runtime.engine, "policy", expected_version=1)

    log = coach_log.entries(runtime.engine)["entries"]
    first = log[0]
    assert (first["kind"], first["target"], first["version"]) == ("policy", "policy", 1)
    article = next(e for e in log if e["kind"] == "article" and e["version"] == 2)
    assert article["author"] == "user"
    assert article["fields"] == ["body_md"]
    assert article["latest"] is True
    older = next(e for e in log if e["kind"] == "article" and e["version"] == 1)
    assert older["fields"] == ["created"]
    assert older["latest"] is False
    assert {e["kind"] for e in log} == {"article", "rule", "source", "policy"}

    only = coach_log.entries(runtime.engine, kind="rule", target="HRV-4")["entries"]
    assert [(e["target"], e["version"]) for e in only] == [("HRV-4", 1)]
    page = coach_log.entries(runtime.engine, limit=2)
    assert len(page["entries"]) == 2
    rest = coach_log.entries(runtime.engine, before=page["next"])["entries"]
    assert len(rest) == len(log) - 2


def test_diff_shows_changed_lines_and_values(runtime) -> None:
    _article(runtime, body_md="\n".join(f"line {n}" for n in range(1, 21)))
    knowledge.edit_article(
        runtime.engine,
        "evidence-hrv",
        author="user",
        expected_version=1,
        body_md="\n".join("line ten" if n == 10 else f"line {n}" for n in range(1, 21)),
        status="retired",
    )
    result = coach_log.diff(runtime.engine, "article", "evidence-hrv", 2)
    assert result["previous_version"] == 1
    assert result["author"] == "user"
    body = next(c for c in result["changes"] if c["field"] == "body_md")
    ops = [(line["op"], line["text"]) for line in body["lines"]]
    assert ("del", "line 10") in ops
    assert ("add", "line ten") in ops
    assert ops[0] == ("gap", "7")
    assert ops[-1] == ("gap", "8")
    status = next(c for c in result["changes"] if c["field"] == "status")
    assert (status["before"], status["after"]) == ("active", "retired")
    assert not any(c["field"] == "title" for c in result["changes"])

    created = coach_log.diff(runtime.engine, "rule", "HRV-4", 1)
    assert created["previous_version"] is None
    assert created["changes"][0]["lines"] == [{"op": "add", "text": "Never decide on HRV alone."}]
    with pytest.raises(LookupError):
        coach_log.diff(runtime.engine, "article", "evidence-hrv", 9)


def test_revert_copies_an_older_version_forward_for_kb_and_memory(runtime) -> None:
    _article(runtime)
    knowledge.edit_article(
        runtime.engine, "evidence-hrv", author="user", expected_version=1, body_md="Wrong."
    )
    out = coach_log.revert(runtime.engine, "article", "evidence-hrv", 1, actor="user")
    assert out["version"] == 3
    article = knowledge.get_article(runtime.engine, "evidence-hrv")
    assert article["body_md"].startswith("Line one.")
    assert len(article["history"]) == 3

    knowledge.edit_rule(
        runtime.engine, "HRV-4", author="user", expected_version=1, text_value="Changed."
    )
    coach_log.revert(runtime.engine, "rule", "HRV-4", 1, actor="user")
    assert knowledge.get_rule(runtime.engine, "HRV-4")["rule"]["text"].startswith("Never")

    for version, words in ((1, "First."), (2, "Second.")):
        coach.save_draft(
            runtime.engine, "policy", {"instructions": words}, expected_version=version - 1
        )
        coach.activate(runtime.engine, "policy", expected_version=version)
    out = coach_log.revert(runtime.engine, "policy", "policy", 1, actor="portal")
    assert out["version"] == 3
    active = coach.state(runtime.engine, "policy")["active"]
    assert active["payload"]["instructions"] == "First."
    diffed = coach_log.diff(runtime.engine, "policy", "policy", 3)
    lines = diffed["changes"][0]["lines"]
    assert {"op": "del", "text": "Second."} in lines
    assert {"op": "add", "text": "First."} in lines
    with pytest.raises(LookupError):
        coach_log.revert(runtime.engine, "context", "context", 4, actor="portal")


def test_context_memory_diff_lists_the_notes(runtime) -> None:
    note = {"id": "a", "kind": "availability", "text": "Pilates on Monday.", "days": [1]}
    coach.save_draft(runtime.engine, "context", {"constraints": [note]}, expected_version=0)
    second = {"id": "b", "kind": "preference", "text": "Long ride on Saturday."}
    coach.save_draft(runtime.engine, "context", {"constraints": [note, second]}, expected_version=1)
    lines = coach_log.diff(runtime.engine, "context", "context", 2)["changes"][0]["lines"]
    assert lines == [
        {"op": "same", "text": "Pilates on Monday."},
        {"op": "add", "text": "Long ride on Saturday."},
    ]


def test_seed_prompts_is_idempotent_and_keeps_live_edits(runtime, tmp_path) -> None:
    for job in coach_prompts.JOBS:
        (tmp_path / f"{job}.md").write_text(f"<!-- copy note -->\n\nDo the {job} run.\n")
    first = coach_prompts.seed(runtime.engine, tmp_path)
    assert {row["action"] for row in first} == {"created"}
    morning = knowledge.get_article(runtime.engine, "coach-prompt-morning")
    assert morning["body_md"] == "Do the morning run."
    assert morning["topic"] == "coach-prompts"
    assert {row["action"] for row in coach_prompts.seed(runtime.engine, tmp_path)} == {"unchanged"}

    (tmp_path / "evening.md").write_text("Do the evening run, shorter.\n")
    knowledge.edit_article(
        runtime.engine, "coach-prompt-morning", author="user", expected_version=1, body_md="Mine."
    )
    (tmp_path / "morning.md").write_text("A newer seed.\n")
    again = {row["job"]: row["action"] for row in coach_prompts.seed(runtime.engine, tmp_path)}
    assert again == {
        "morning": "kept",
        "evening": "updated",
        "weekly": "unchanged",
        "monthly": "unchanged",
        "block": "unchanged",
        "race": "unchanged",
        "test": "unchanged",
        "thresholds": "unchanged",
        "kb": "unchanged",
        "injury": "unchanged",
        "checkin": "unchanged",
        "insight": "unchanged",
    }
    assert coach_prompts.prompt(runtime.engine, "morning", tmp_path)["body_md"] == "Mine."
    assert coach_prompts.prompt(runtime.engine, "evening", tmp_path)["version"] == 2


def test_prompt_falls_back_to_the_seed_file_and_the_repo_seed_exists(runtime, tmp_path) -> None:
    (tmp_path / "weekly.md").write_text("Weekly from file.\n")
    fallback = coach_prompts.prompt(runtime.engine, "weekly", tmp_path)
    assert fallback["source"] == "seed_file"
    assert fallback["version"] is None
    with pytest.raises(LookupError):
        coach_prompts.prompt(runtime.engine, "morning", tmp_path)
    with pytest.raises(ValueError):
        coach_prompts.slug_for("lunch")
    for job in coach_prompts.JOBS:
        text = coach_prompts.seed_text(job)
        assert text and not text.startswith("<!--")


def test_mcp_reads_a_routine_prompt(runtime) -> None:
    coach_prompts.seed(runtime.engine)

    async def go():
        async with Client(build_server(runtime)) as client:
            return await client.call_tool("vibesynergy_coach_prompt", {"job": "insight"})

    result = anyio.run(go)
    assert not result.is_error
    data = result.structured_content["data"]
    assert data["slug"] == "coach-prompt-insight"
    assert data["source"] == "knowledge_base"
    assert data["version"] == 1


def test_portal_endpoints_edit_log_diff_revert_archive_and_seed(runtime) -> None:
    _article(runtime)
    client = _client(runtime)

    methods = client.get("/api/knowledge/methods").json()["data"]["groups"]
    assert methods["evidence"][0]["slug"] == "evidence-hrv"

    edited = client.post(
        "/api/knowledge/article/edit",
        json={"slug": "evidence-hrv", "expected_version": 1, "body_md": "Edited here."},
        headers=HEADERS,
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["version"] == 2
    stale = client.post(
        "/api/knowledge/article/edit",
        json={"slug": "evidence-hrv", "expected_version": 1, "body_md": "Again."},
        headers=HEADERS,
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == "stale"
    missing = client.post(
        "/api/knowledge/rule/edit",
        json={"id": "NOPE-1", "expected_version": 1, "text": "x"},
        headers=HEADERS,
    )
    assert missing.status_code == 404
    rule = client.post(
        "/api/knowledge/rule/edit",
        json={"id": "HRV-4", "expected_version": 1, "text": "Look at more than HRV."},
        headers=HEADERS,
    )
    assert rule.json()["data"]["rule"]["version"] == 2

    log = client.get("/api/coach-log").json()["data"]["entries"]
    assert (log[0]["kind"], log[0]["version"]) == ("rule", 2)
    diff = client.get(
        "/api/coach-log/diff", params={"kind": "article", "target": "evidence-hrv", "version": 2}
    ).json()["data"]
    assert {"op": "add", "text": "Edited here."} in diff["changes"][0]["lines"]
    assert (
        client.get(
            "/api/coach-log/diff", params={"kind": "article", "target": "nope", "version": 1}
        ).status_code
        == 404
    )
    assert client.get("/api/coach-log", params={"kind": "bogus"}).status_code == 400

    reverted = client.post(
        "/api/coach-log/revert",
        json={"kind": "article", "target": "evidence-hrv", "version": 1},
        headers=HEADERS,
    )
    assert reverted.status_code == 200
    assert reverted.json()["data"]["version"] == 3
    assert knowledge.get_article(runtime.engine, "evidence-hrv")["author"] == "user"

    archived = client.post(
        "/api/knowledge/article/edit",
        json={"slug": "evidence-hrv", "expected_version": 3, "status": "retired"},
        headers=HEADERS,
    )
    assert archived.status_code == 200
    assert client.get("/api/knowledge").json()["data"]["articles"] == []
    archive = client.get("/api/knowledge/archive").json()["data"]["articles"]
    assert [a["slug"] for a in archive] == ["evidence-hrv"]
    assert client.get("/api/knowledge/methods").json()["data"]["groups"]["evidence"] == []

    seeded = client.post("/api/knowledge/seed-prompts", headers=HEADERS).json()["data"]
    assert {row["action"] for row in seeded["prompts"]} == {"created"}
    no_header = client.post("/api/knowledge/seed-prompts")
    assert no_header.status_code in (400, 403)


def test_an_installs_own_seed_wins_over_the_generic_one(runtime, tmp_path) -> None:
    """the repo holds generic seeds; <data_dir>/coach-prompts holds an install's own."""
    overlay = coach_prompts.overlay_dir(runtime.settings)
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / "morning.md").write_text("<!-- mine -->\n\nMijn eigen ochtend.\n")
    assert coach_prompts.seed_text("morning", overlay=overlay) == "Mijn eigen ochtend."
    assert "de atleet" in coach_prompts.seed_text("evening", overlay=overlay)
    rows = {r["job"]: r["action"] for r in coach_prompts.seed(runtime.engine, overlay=overlay)}
    assert rows["morning"] == "created"
    assert knowledge.get_article(runtime.engine, "coach-prompt-morning")["body_md"] == (
        "Mijn eigen ochtend."
    )


def test_the_repo_seeds_name_no_one() -> None:
    for job in coach_prompts.JOBS:
        text = coach_prompts.seed_text(job) or ""
        assert not any(w in text for w in ("Alex", "Lakeside", "Stad", "PLAN-")), job
