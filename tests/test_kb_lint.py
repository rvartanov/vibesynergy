"""The knowledge-base lint (#120): retired names, rule ranges, tool names and fixed thresholds.

Every AI coach gets the warnings in the handbook; the athlete sees them on Coach → Methods &
rules. A warning only: the text changes through a proposal the athlete approves.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from vibesynergy.coach_runner.tools import COACH_SCOPES, ToolBox
from vibesynergy.mcp.server import tool_names
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_handbook, coach_prompts, kb_lint, knowledge

FAMILIES = {"FRY": set(range(1, 9)), "ICU": set(range(1, 7)), "HRV": set(range(1, 6))}


def _plan(runtime, body: str = "Weekstructuur: ma zwemmen.", rules=None) -> None:
    knowledge.upsert_article(
        runtime.engine,
        slug="example-plan",
        author="user",
        title="Plan",
        body_md=body,
        topic="example",
        rules=rules
        or [{"id": f"FRY-{n}", "text": f"Planregel {n}.", "grade": "C"} for n in range(1, 9)],
    )


def _loop(runtime, body: str) -> None:
    knowledge.upsert_article(
        runtime.engine,
        slug="ai-coach-loop",
        author="user",
        title="Werkwijze AI-coach",
        body_md=body,
        topic="coach-loop",
        rules=[{"id": f"ICU-{n}", "text": f"Werkregel {n}.", "grade": "C"} for n in range(1, 7)],
    )


def _checks(engine, check: str) -> list[tuple[str, list[str]]]:
    return [(w["ref"], w["names"]) for w in knowledge.lint(engine) if w["check"] == check]


# --- the text checks -------------------------------------------------------------------------


def test_retired_names_are_found_once_in_order() -> None:
    retired = kb_lint.retired_list(knowledge.SLUG_ALIASES)
    text = "Terug naar intervals: `vitality plan source intervals`. Lees claude-coach-loop."
    assert [name for name, _ in kb_lint.retired_names(text, retired)] == [
        "Vitality",
        "plan source",
        "claude-coach-loop",
    ]
    # The plan-source command under the current app name is gone too; the words alone are not.
    assert [n for n, _ in kb_lint.retired_names("vibesynergy plan source", retired)] == [
        "plan source"
    ]
    clean = "VibeSynergy is the only plan source. Lees ai-coach-loop. Vitaliteit telt."
    assert kb_lint.retired_names(clean, retired) == []


def test_a_range_from_one_must_end_at_the_last_rule_in_use() -> None:
    text = "FRY-1..8 en ICU-1..4 zijn leidend, niet alleen FRY-1..5; HRV-1 t/m 5; ICU-1\u20136."
    assert kb_lint.rule_ranges(text, FAMILIES) == [
        ("ICU-1..4", "ICU-1..6"),
        ("FRY-1..5", "FRY-1..8"),
    ]
    # A part of a family is fine while every rule in it exists.
    assert kb_lint.rule_ranges("HRV-4..5 en ICU-2..3", FAMILIES) == []
    assert kb_lint.rule_ranges("HRV-4..7", FAMILIES) == [("HRV-4..7", "HRV-1..5")]
    # A family with no rules in use (or a word like ISO-8601) is no rule reference.
    assert kb_lint.rule_ranges("ABC-1..3, ISO-1..2", FAMILIES) == []


def test_a_single_rule_that_is_not_in_use_is_found() -> None:
    text = "Volg FRY-9 en ICU-2 v3, HRV-* en FRY-9 nog eens; ABC-4 bestaat niet als familie."
    assert kb_lint.unknown_rules(text, FAMILIES) == ["FRY-9"]


def test_tool_names_come_from_the_real_registry() -> None:
    known = tool_names()
    assert {"vibesynergy_coach_write", "vibesynergy_thresholds", "vibesynergy_kb_get"} <= known
    text = (
        "Lees vibesynergy_today.horizon, schrijf met vibesynergy_plan_* en "
        "de vibesynergy_icu_*-tools. Stuur Robert met SendUserMessage via vibesynergy_send. "
        "Gebruik ToolSearch."
    )
    assert kb_lint.unknown_tools(text, known) == [
        "SendUserMessage",
        "vibesynergy_send",
        "ToolSearch",
    ]
    assert kb_lint.unknown_tools("vibesynergy_nothing_*", known) == ["vibesynergy_nothing_*"]


def test_fixed_thresholds_with_units_are_found_and_relative_targets_are_not() -> None:
    text = (
        "Tot de test geldt FTP 265 W (Robert 31-10), fiets LTHR 150, drempeltempo lopen "
        "5:10/km, CSS 2:15/100 m; fiets Z2 ook op HR (Friel Z2 122\u2013133). Zwift 250 W."
    )
    assert kb_lint.fixed_thresholds(text) == [
        "FTP 265 W",
        "LTHR 150",
        "5:10/km",
        "2:15/100 m",
        "Z2 122\u2013133",
        "250 W",
    ]
    relative = (
        "Fiets in %FTP (75-85%, 88-94% sweet spot), loop in %LTHR of HR-zone. FTP 20' test "
        "in wk 27, CSS-test in wk 3. Lees de drempels live met vibesynergy_thresholds. "
        "Deadlift max 60 kg; lange rit 180 km; CSS + 2-5 s/100."
    )
    assert kb_lint.fixed_thresholds(relative) == []


# --- the lint on the knowledge base --------------------------------------------------------


def test_lint_reads_every_article_and_rule_in_use(runtime) -> None:
    engine = runtime.engine
    coach_prompts.seed(engine)
    _plan(runtime)
    _loop(runtime, "Regels FRY-1..8 en ICU-1..6 zijn leidend. Lees vibesynergy_thresholds.")
    assert knowledge.lint(engine) == []  # the seeds and a correct method are clean

    _loop(
        runtime,
        "Regels FRY-1..8 en ICU-1..4 zijn leidend. Terug: `vitality plan source intervals`. "
        "Tot de test: FTP 265 W, loop LTHR 162, drempeltempo 5:10/km.",
    )
    knowledge.upsert_article(
        engine,
        slug="coach-prompt-weekly",
        author="user",
        body_md="Lees frysman (regels FRY-1..5). Stuur Robert met SendUserMessage.",
    )
    found = knowledge.lint(engine)
    assert [(w["check"], w["kind"], w["ref"], w["names"]) for w in found] == [
        ("retired_name", "article", "ai-coach-loop", ["Vitality", "plan source"]),
        ("rule_range", "article", "ai-coach-loop", ["ICU-1..4"]),
        ("fixed_threshold", "article", "ai-coach-loop", ["FTP 265 W", "LTHR 162", "5:10/km"]),
        ("rule_range", "article", "coach-prompt-weekly", ["FRY-1..5"]),
        ("unknown_tool", "article", "coach-prompt-weekly", ["SendUserMessage"]),
    ]
    ranges = [w for w in found if w["check"] == "rule_range"]
    assert [w["expected"] for w in ranges] == [["ICU-1..6"], ["FRY-1..8"]]
    assert all(w["article"] == w["ref"] for w in found)
    assert "ICU-1..6" in ranges[0]["message"] and "vibesynergy_thresholds" in found[2]["message"]

    # Every AI coach is told, in the handbook's warnings.
    _data, warnings = coach_handbook.handbook(engine, scopes=frozenset(COACH_SCOPES))
    assert [w["message"] for w in found] == [w for w in warnings if "scopes" not in w]


def test_fixed_thresholds_only_in_instructions_method_and_plan(runtime) -> None:
    engine = runtime.engine
    _plan(runtime, "Drempels tot de test: FTP **265 W**.")
    knowledge.upsert_article(
        engine,
        slug="evidence-pacing",
        author="user",
        title="Pacing",
        body_md="Een atleet met FTP 300 W reed 250 W.",  # research may quote numbers
        topic="pacing",
        rules=[{"id": "PACE-1", "text": "Rijd op 250 W.", "grade": "B"}],
    )
    knowledge.upsert_article(
        engine,
        slug="example-plan",
        author="user",
        rules=[{"id": "FRY-8", "text": "Fiets Z2 onder LTHR 150.", "grade": "C"}],
    )
    assert _checks(engine, "fixed_threshold") == [
        ("example-plan", ["FTP 265 W"]),
        ("FRY-8", ["LTHR 150"]),
    ]
    rule = next(w for w in knowledge.lint(engine) if w["ref"] == "FRY-8")
    assert rule["kind"] == "rule" and rule["article"] == "example-plan"


def test_rules_of_an_archived_article_and_inactive_rules_are_left_out(runtime) -> None:
    engine = runtime.engine
    _plan(runtime)
    knowledge.upsert_article(
        engine,
        slug="old-loop",
        author="user",
        title="Oud",
        body_md="Vitality.",
        topic="coach-loop",
        status="retired",
        rules=[{"id": "OLD-1", "text": "Gebruik SendUserMessage.", "grade": "C"}],
    )
    knowledge.upsert_article(
        engine,
        slug="example-plan",
        author="user",
        rules=[{"id": "FRY-7", "text": "Zie FRY-12.", "grade": "C", "active": False}],
    )
    assert knowledge.lint(engine) == []


def test_unknown_rules_and_tools_in_a_rule(runtime) -> None:
    engine = runtime.engine
    _plan(
        runtime,
        rules=[
            *({"id": f"FRY-{n}", "text": f"Planregel {n}.", "grade": "C"} for n in range(1, 8)),
            {"id": "FRY-8", "text": "Zie FRY-9; schrijf met vibesynergy_plan_write.", "grade": "C"},
        ],
    )
    assert _checks(engine, "unknown_rule") == [("FRY-8", ["FRY-9"])]
    assert _checks(engine, "unknown_tool") == [("FRY-8", ["vibesynergy_plan_write"])]


def test_a_proposal_gets_the_new_warnings_before_it_is_approved(runtime) -> None:
    coach_prompts.seed(runtime.engine)
    _plan(runtime)
    _loop(runtime, "Werkwijze.")
    box = ToolBox(runtime, frozenset(COACH_SCOPES) | {"plan.propose"})

    def propose(**arguments) -> list[str]:
        answer, is_error = box.call(
            "vibesynergy_kb_propose_change", {"rationale": "test", **arguments}
        )
        assert not is_error, answer
        return json.loads(answer)["meta"]["warnings"]

    stale = propose(
        kind="article",
        target="ai-coach-loop",
        body_md="ICU-1..4 leidend; FTP 265 W; vitality plan source intervals.",
    )
    assert len(stale) == 3
    assert propose(kind="article", target="ai-coach-loop", body_md="ICU-1..6 leidend.") == []
    weekly = propose(
        kind="article", target="coach-prompt-weekly", body_md="Stuur met SendUserMessage."
    )
    assert len(weekly) == 1 and "SendUserMessage" in weekly[0]
    assert len(propose(kind="rule", target="FRY-3", new_text="Planregel 3, FTP 265 W.")) == 1


def test_methods_and_rules_shows_the_warnings(runtime) -> None:
    _loop(runtime, "Regels ICU-1..4 zijn leidend.")
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/knowledge/methods").json()["data"]
    assert [(w["check"], w["article"], w["names"], w["expected"]) for w in data["warnings"]] == [
        ("rule_range", "ai-coach-loop", ["ICU-1..4"], ["ICU-1..6"])
    ]
