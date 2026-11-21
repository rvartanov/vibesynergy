"""Editorial knowledge-base fixes without approval, and a diff instead of whole articles (#137).

A proposal whose every changed part is a mechanical fix of a lint warning is applied at once
as a version by ``lint/editorial``; anything more, and every proposal from an advisory AI
coach, waits for the athlete. A daily job makes the same fixes itself. Every fix is listed
afterwards with an undo.
"""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from vibesynergy.coach_runner.tools import COACH_SCOPES, ToolBox
from vibesynergy.mcp import roles
from vibesynergy.mcp.server import tool_names
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_log, inbox, kb_editorial, kb_fix, knowledge
from vibesynergy.store.schema import job, kb_proposal

LOOP = (
    "# Werkwijze\n\n"
    "Lees eerst claude-coach-loop.\n"
    "Regels ICU-1..4 zijn leidend.\n"
    "Daarna de dag plannen met Robert."
)
ADVISORY = "client:second-opinion"
HEADERS = {"x-vibesynergy": "portal"}


def _loop(runtime, body: str = LOOP) -> None:
    knowledge.upsert_article(
        runtime.engine,
        slug="ai-coach-loop",
        author="user",
        title="Werkwijze AI-coach",
        body_md=body,
        topic="coach-loop",
        rules=[{"id": f"ICU-{n}", "text": f"Werkregel {n}.", "grade": "C"} for n in range(1, 7)],
    )


def _propose(runtime, body: str, *, author: str = "client:coach-runner", **extra):
    return knowledge.propose_change(
        runtime.engine,
        kind="article",
        target="ai-coach-loop",
        rationale="test",
        author=author,
        changes={"body_md": body, **extra},
    )


def _fixed(body: str = LOOP) -> str:
    return body.replace("claude-coach-loop", "ai-coach-loop").replace("ICU-1..4", "ICU-1..6")


# --- the text-level check --------------------------------------------------------------------


def _slots(text: str, *, thresholds: bool = False) -> list[kb_fix.Slot]:
    return kb_fix.slots(
        text,
        slug_aliases=knowledge.SLUG_ALIASES,
        families={"ICU": set(range(1, 7)), "FRY": set(range(1, 9))},
        tools=tool_names(),
        thresholds=thresholds,
    )


def test_only_the_lint_substitution_is_explained() -> None:
    old = "Regels ICU-1..4 en FRY-1 t/m 5. Zie claude-coach-loop."
    found = _slots(old)
    assert [(s.check, s.found) for s in found] == [
        ("rule_range", "ICU-1..4"),
        ("rule_range", "FRY-1..5"),
        ("retired_name", "claude-coach-loop"),
    ]
    fixed, fixes = kb_fix.autofix(old, found)
    assert fixed == "Regels ICU-1..6 en FRY-1 t/m 8. Zie ai-coach-loop."
    assert [f.fixed for f in fixes] == ["ICU-1..6", "FRY-1 t/m 8", "ai-coach-loop"]
    assert kb_fix.explain(old, fixed, found) == fixes
    # Fixing only one of them is fine, and so is the canonical form of a range.
    assert len(kb_fix.explain(old, old.replace("FRY-1 t/m 5", "FRY-1..8"), found) or []) == 1
    # One other word anywhere, and it is a content change.
    assert kb_fix.explain(old, fixed.replace("Zie", "Lees"), found) is None
    assert kb_fix.explain(old, fixed.replace("ICU-1..6", "ICU-1..5"), found) is None
    assert kb_fix.explain(old, old, found) == []


def test_no_mechanical_fix_for_what_needs_a_decision() -> None:
    text = (
        "Gebruik `vitality plan source intervals`. Sherpa is weg. Stuur met SendUserMessage. "
        "Zie FRY-9 en ICU-2..7."
    )
    assert _slots(text) == []
    # The old app name and its tools have one replacement.
    assert [s.options[0] for s in _slots("Vitality: vitality_today.")] == [
        "VibeSynergy",
        "vibesynergy_today",
    ]


def test_a_threshold_is_replaced_only_on_a_plain_instruction_line() -> None:
    text = (
        "Rijd de blokken op FTP 265 W.\n"
        "Tot de test op 19-11 geldt LTHR 162.\n"
        "Voorbeeld: Z2 122-133.\n"
        "Fiets: FTP 265 W, LTHR 150.\n"
        "| FTP | 265 W |\n"
        "## Drempels (31-10-2026)\n"
        "Loop op 5:10/km."
    )
    found = _slots(text, thresholds=True)
    assert [s.found for s in found] == ["FTP 265 W"]
    fixed, _ = kb_fix.autofix(text, found)
    assert fixed.startswith("Rijd de blokken op FTP (lees live met vibesynergy_thresholds).\n")
    assert _slots("Ride at FTP 265 W for the blocks.", thresholds=True)[0].options[0] == (
        "FTP (read live with vibesynergy_thresholds)"
    )
    assert _slots(text, thresholds=False) == []


# --- a proposal -----------------------------------------------------------------------------


def test_a_lint_only_proposal_is_applied_as_an_editorial_version(runtime) -> None:
    _loop(runtime)
    proposal = _propose(runtime, _fixed())
    assert proposal["status"] == "applied"
    assert proposal["decided_by"] == knowledge.EDITORIAL_AUTHOR
    assert proposal["editorial"]["applied"] is True
    assert {f["fixed"] for f in proposal["editorial"]["fixes"]} == {"ai-coach-loop", "ICU-1..6"}
    article = knowledge.get_article(runtime.engine, "ai-coach-loop")
    assert article["body_md"] == _fixed()
    assert article["history"][-1]["author"] == "lint/editorial"
    assert knowledge.pending_proposals(runtime.engine) == []


def test_a_lint_fix_mixed_with_a_content_change_waits(runtime) -> None:
    _loop(runtime)
    proposal = _propose(runtime, _fixed().replace("met Robert", "met Robert, pilates ma en vr"))
    assert proposal["status"] == "pending"
    assert proposal["editorial"] == {
        "applied": False,
        "reason": "content",
        "fixes": [],
        "requested": False,
    }
    assert knowledge.get_article(runtime.engine, "ai-coach-loop")["body_md"] == LOOP
    # The proposal shows what changes, not the whole article.
    texts = [c for c in proposal["diff"] if c["type"] == "text"]
    assert [c["field"] for c in texts] == ["body_md"]
    ops = [(line["op"], line["text"]) for line in texts[0]["lines"]]
    assert ("del", "Lees eerst claude-coach-loop.") in ops
    assert ("add", "Daarna de dag plannen met Robert, pilates ma en vr.") in ops


def test_another_field_or_a_new_rule_is_never_editorial(runtime) -> None:
    _loop(runtime)
    assert _propose(runtime, _fixed(), evidence_grade="A")["status"] == "pending"
    new_rule = knowledge.propose_change(
        runtime.engine,
        kind="rule",
        target="ICU-7",
        rationale="test",
        author="client:coach-runner",
        changes={"text": "Zie ICU-1..6.", "article_slug": "ai-coach-loop"},
    )
    assert new_rule["status"] == "pending" and new_rule["editorial"]["reason"] == "new"


def test_an_advisory_author_always_waits(runtime) -> None:
    _loop(runtime)
    proposal = _propose(runtime, _fixed(), author=ADVISORY)
    assert proposal["status"] == "pending"
    assert proposal["editorial"]["reason"] == "advisory_author"
    # The athlete's own agent and the primary AI coach may.
    roles.set_role(runtime.engine, "own-agent", roles.OWN)
    assert _propose(runtime, _fixed(), author="client:own-agent")["status"] == "pending"  # waits
    knowledge.reject_proposal(runtime.engine, proposal["id"], actor="user")
    assert _propose(runtime, _fixed(), author="client:own-agent")["status"] == "applied"


def test_undo_restores_the_previous_version_and_the_fix_is_not_made_again(runtime) -> None:
    _loop(runtime)
    applied = _propose(runtime, _fixed())
    version = applied["base_version"] + 1
    coach_log.revert(
        runtime.engine, "article", "ai-coach-loop", applied["base_version"], actor="user"
    )
    assert knowledge.get_article(runtime.engine, "ai-coach-loop")["body_md"] == LOOP
    [item] = knowledge.editorial_changes(runtime.engine)
    assert item["state"] == "undone" and item["version"] == version
    again = _propose(runtime, _fixed())
    assert again["status"] == "pending" and again["editorial"]["reason"] == "undone"
    assert knowledge.editorial_sweep(runtime.engine)["applied"] == []


def test_a_rule_text_fix_and_a_rule_grade_change(runtime) -> None:
    _loop(runtime)
    knowledge.upsert_article(
        runtime.engine,
        slug="ai-coach-loop",
        author="user",
        rules=[{"id": "ICU-6", "text": "Zie ICU-1..4 in claude-coach-loop.", "grade": "C"}],
    )

    def rule(changes):
        return knowledge.propose_change(
            runtime.engine,
            kind="rule",
            target="ICU-6",
            rationale="test",
            author="client:coach-runner",
            changes=changes,
        )

    graded = rule({"text": "Zie ICU-1..6 in ai-coach-loop.", "grade": "A"})
    assert graded["status"] == "pending" and graded["editorial"]["reason"] == "not_text"
    # The same author's new proposal replaces the one that waited, and is a pure fix.
    assert rule({"text": "Zie ICU-1..6 in ai-coach-loop."})["status"] == "applied"
    assert knowledge.get_proposal(runtime.engine, graded["id"])["status"] == "superseded"
    assert knowledge.get_rule(runtime.engine, "ICU-6")["rule"]["text"] == (
        "Zie ICU-1..6 in ai-coach-loop."
    )


# --- the daily job --------------------------------------------------------------------------


def test_the_daily_job_fixes_what_it_can_and_leaves_the_rest(runtime) -> None:
    _loop(runtime, LOOP + "\nStuur met SendUserMessage.")
    assert kb_editorial.due(runtime)
    result = kb_editorial.run(runtime)
    assert [(item["kind"], item["target"]) for item in result["applied"]] == [
        ("article", "ai-coach-loop")
    ]
    body = knowledge.get_article(runtime.engine, "ai-coach-loop")["body_md"]
    assert body == _fixed() + "\nStuur met SendUserMessage."
    # The tool of another app needs a decision: its warning stays.
    assert [w["check"] for w in knowledge.lint(runtime.engine)] == ["unknown_tool"]
    assert kb_editorial.run(runtime)["applied"] == []
    with runtime.engine.begin() as conn:
        conn.execute(
            job.insert().values(
                kind=kb_editorial.JOB,
                payload={},
                status="done",
                priority=9,
                run_after=knowledge.utc_now(),
                attempts=1,
                max_attempts=3,
                created_at=knowledge.utc_now(),
                finished_at=knowledge.utc_now(),
            )
        )
    assert not kb_editorial.due(runtime)


def test_the_job_leaves_a_text_with_another_proposal_waiting(runtime) -> None:
    _loop(runtime)
    _propose(runtime, "Een heel andere tekst.", author=ADVISORY)
    assert knowledge.editorial_sweep(runtime.engine)["applied"] == []
    assert knowledge.get_article(runtime.engine, "ai-coach-loop")["body_md"] == LOOP


# --- what the athlete sees ------------------------------------------------------------------


def test_methods_and_rules_and_the_notices_show_the_fixes(runtime) -> None:
    _loop(runtime)
    knowledge.editorial_sweep(runtime.engine)
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/knowledge/methods").json()["data"]
    [item] = data["editorial"]
    assert item["state"] == "current" and item["article"] == "ai-coach-loop"
    assert {f["check"] for f in item["fixes"]} == {"retired_name", "rule_range"}
    assert data["warnings"] == []
    notices = inbox._editorial(runtime.engine)
    assert notices == [
        {
            "kind": "kb_editorial",
            "count": 1,
            "checks": ["retired_name", "rule_range"],
            "at": item["at"],
            "link": "#/coach/methods",
        }
    ]
    # One click undoes it, through the change log's revert.
    reply = client.post(
        "/api/coach-log/revert",
        json={"kind": "article", "target": "ai-coach-loop", "version": item["base_version"]},
        headers=HEADERS,
    )
    assert reply.status_code == 200, reply.text
    assert client.get("/api/knowledge/methods").json()["data"]["editorial"][0]["state"] == "undone"
    # The notice is for the last week only.
    with runtime.engine.begin() as conn:
        conn.execute(update(kb_proposal).values(decided_at=knowledge.utc_now() - timedelta(days=8)))
    assert inbox._editorial(runtime.engine) == []


def test_waiting_proposals_on_methods_and_in_the_inbox_carry_a_diff(runtime) -> None:
    _loop(runtime)
    _propose(runtime, LOOP.replace("met Robert", "met Robert en de AI-coach"), author=ADVISORY)
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    [waiting] = client.get("/api/knowledge/methods").json()["data"]["proposals"]
    lines = waiting["diff"][0]["lines"]
    assert [(line["op"], line["text"]) for line in lines[-2:]] == [
        ("del", "Daarna de dag plannen met Robert."),
        ("add", "Daarna de dag plannen met Robert en de AI-coach."),
    ]
    [decision] = inbox.inbox(runtime, knowledge.utc_now().date())["decisions"]
    assert decision["diff"] == waiting["diff"]


def test_the_mcp_tool_says_why_an_editorial_request_waits(runtime) -> None:
    _loop(runtime)
    box = ToolBox(runtime, frozenset(COACH_SCOPES) | {"plan.propose"})
    answer, is_error = box.call(
        "vibesynergy_kb_propose_change",
        {
            "kind": "article",
            "target": "ai-coach-loop",
            "rationale": "test",
            "body_md": _fixed() + " En meer.",
            "editorial": True,
        },
    )
    assert not is_error, answer
    reply = json.loads(answer)
    assert reply["data"]["status"] == "pending"
    assert any("Not applied as an editorial fix" in w for w in reply["meta"]["warnings"])
    answer, _ = box.call(
        "vibesynergy_kb_propose_change",
        {"kind": "article", "target": "ai-coach-loop", "rationale": "test", "body_md": _fixed()},
    )
    assert json.loads(answer)["data"]["status"] == "applied"


def test_proposals_made_before_keep_their_rows(runtime) -> None:
    """The check runs on new proposals only; nothing rewrites what is stored."""
    _loop(runtime)
    with runtime.engine.connect() as conn:
        before = conn.execute(select(kb_proposal)).all()
    knowledge.editorial_sweep(runtime.engine)
    with runtime.engine.connect() as conn:
        rows = conn.execute(select(kb_proposal)).mappings().all()
    assert before == [] and [r["author"] for r in rows] == ["lint/editorial"]
