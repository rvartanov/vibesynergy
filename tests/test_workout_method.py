"""The workout method (#142): the workout library's coaching values come from the knowledge
base, not from the code. An approved change applies to new workouts with no code change, and
to planned sessions on request; done sessions never change."""

from __future__ import annotations

import ast
import json
import re
from datetime import date, timedelta
from pathlib import Path

import anyio
from mcp import Client

from vibesynergy.mcp.server import build_server
from vibesynergy.query import (
    knowledge,
    plan_model,
    plan_proposals,
    workout_library,
    workout_method,
    workout_rerender,
)

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "src" / "vibesynergy" / "query" / "workout_library.py"
Z2_CUE = "cadans 80-95 (eigen keuze binnen die band)"


def _call(runtime, name: str, arguments: dict):
    async def go():
        async with Client(build_server(runtime)) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(go)


def _approve(runtime, body: str) -> None:
    proposal = knowledge.propose_change(
        runtime.engine,
        kind="article",
        target=workout_method.SLUG,
        rationale="cadence band 85-95",
        author="client:coach",
        changes={"body_md": body},
    )
    assert proposal["status"] == "pending"  # a coaching fact waits for the athlete
    knowledge.approve_proposal(runtime.engine, proposal["id"], actor="user")


def _current_body(runtime) -> str:
    return knowledge.get_article(runtime.engine, workout_method.SLUG)["body_md"]


def _z2_band(body: str, band: str, cue: str) -> str:
    return body.replace("cadence = [80, 95]", f"cadence = {band}").replace(Z2_CUE, cue)


# --- the seed and the migration ------------------------------------------------------------


def test_the_seed_covers_every_template_and_reads_without_warnings() -> None:
    seed = workout_method.seed_tables()
    assert set(seed) == set(workout_library.BY_ID)
    assert workout_method.lint_body(workout_method.seed_body()) == []
    for template in workout_library.TEMPLATES:
        params = set(seed[template.id]["params"])
        build = template.garmin or template.build
        if template.garmin is None:  # the structure names the parameters it takes
            names = set(build.__code__.co_varnames[1 : build.__code__.co_argcount])
            assert params == names, template.id


def test_the_migration_puts_the_seed_in_the_knowledge_base_with_todays_values(runtime) -> None:
    article = knowledge.get_article(runtime.engine, workout_method.SLUG)
    assert (article["version"], article["author"], article["status"]) == (
        1,
        workout_method.SEED_AUTHOR,
        "active",
    )
    assert article["body_md"] == workout_method.seed_body()
    assert article["topic"] == workout_method.TOPIC
    method = workout_method.load(runtime.engine)
    assert (method.source, method.version, method.fallback, method.problems) == (
        "knowledge_base",
        1,
        {},
        [],
    )
    # Nothing changes for the athlete: the article gives what the seed (the old code) gave.
    seed = workout_method.seed_method()
    for template in workout_library.TEMPLATES:
        built = workout_library.render(template.id, "nl", method)
        before = workout_library.render(template.id, "nl", seed)
        assert built["text"] == before["text"] and built["name"] == before["name"]
    # The Z2 rides keep the 80-95 rpm band with its cue (#143).
    for template_id in ("ride_endurance", "ride_brick"):
        text = workout_library.render(template_id, "nl", method)["text"]
        assert "80-95rpm" in text and Z2_CUE in text, text
    # The method is part of the working method, and the lint has nothing to say about it.
    assert knowledge.method_group(article) == "method"
    assert not [w for w in knowledge.lint(runtime.engine) if w["ref"] == workout_method.SLUG]


def test_the_migration_leaves_an_existing_article_alone(runtime) -> None:
    from vibesynergy.store import db

    _approve(runtime, _z2_band(_current_body(runtime), "[85, 95]", "cadans 85-95"))
    db.upgrade(runtime.engine, "0057")
    db.upgrade(runtime.engine)
    article = knowledge.get_article(runtime.engine, workout_method.SLUG)
    assert article["version"] == 2 and "cadans 85-95" in article["body_md"]


# --- changing a value ----------------------------------------------------------------------


def test_an_approved_change_applies_to_the_next_workout_without_code(runtime) -> None:
    before = workout_library.render("ride_endurance", "nl", workout_method.load(runtime.engine))
    _approve(runtime, _z2_band(_current_body(runtime), "[85, 95]", "cadans 85-95"))
    method = workout_method.load(runtime.engine)
    after = workout_library.render("ride_endurance", "nl", method)
    assert "80-95rpm" in before["text"] and "85-95rpm" in after["text"]
    assert "cadans 85-95" in after["text"] and Z2_CUE not in after["text"]
    assert after["method"] == {
        "slug": "workout-method",
        "version": 2,
        "source": "knowledge_base",
        "fallback": [],
    }


def test_a_proposal_waits_and_the_coach_cannot_write_the_method_directly(runtime) -> None:
    result = _call(
        runtime,
        "vibesynergy_kb_upsert",
        {"slug": workout_method.SLUG, "body_md": "```toml\n```"},
    )
    assert result.is_error and "kb_propose_change" in str(result.content)
    proposal = knowledge.propose_change(
        runtime.engine,
        kind="article",
        target=workout_method.SLUG,
        rationale="r",
        author="client:coach",
        changes={"body_md": _z2_band(_current_body(runtime), "[85, 95]", "c")},
    )
    assert proposal["status"] == "pending"
    assert workout_method.load(runtime.engine).version == 1


def test_a_missing_or_wrong_value_comes_from_the_seed_and_is_named(runtime) -> None:
    body = _current_body(runtime)
    old_steps = "\n".join(
        (
            "warmup = { minutes = 15, ftp_pct = [50, 65], cadence = [80, 95] }",
            f'work = {{ ftp_pct = [62, 72], cadence = [80, 95], cue = "{Z2_CUE}" }}',
        )
    )
    new_steps = "\n".join(
        (
            "warmup = { minutes = 15, ftp_pct = [50, 65] }",
            'work = { ftp_pct = "fast", cadence = [80, 95] }',
        )
    )
    broken = body.replace(old_steps, new_steps, 1)
    assert broken != body
    warnings = knowledge.change_warnings(
        runtime.engine, "article", workout_method.SLUG, {"body_md": broken}
    )
    assert any("ride_endurance.steps.warmup.cadence" in w for w in warnings)
    _approve(runtime, broken)
    method = workout_method.load(runtime.engine)
    assert method.fallback["ride_endurance"] == [
        "steps.warmup.cadence",
        "steps.work.ftp_pct",
        "steps.work.cue",
    ]
    # Rendered as before: the seed fills what the article lacks.
    text = workout_library.render("ride_endurance", "nl", method)["text"]
    assert text == workout_library.render("ride_endurance", "nl", None)["text"]
    found = [w for w in knowledge.lint(runtime.engine) if w["check"] == "workout_value"]
    assert len(found) == 1 and found[0]["ref"] == workout_method.SLUG
    assert found[0]["names"] == [
        "ride_endurance.steps.warmup.cadence",
        "ride_endurance.steps.work.ftp_pct",
        "ride_endurance.steps.work.cue",
    ]
    explained = workout_method.explain(runtime.engine, "ride_endurance")
    assert (
        explained["templates"]["ride_endurance"]["from_seed"] == method.fallback["ride_endurance"]
    )
    assert "ride_endurance.steps.work.ftp_pct has the wrong form" in explained["problems"]
    result = _call(
        runtime, "vibesynergy_explain", {"metric": "workout:ride_endurance", "date": "2026-11-21"}
    )
    assert not result.is_error
    assert result.structured_content["data"]["version"] == 2


def test_an_article_without_a_block_falls_back_to_the_seed_entirely(runtime) -> None:
    assert workout_method.lint_body("Geen blok meer.") == ["toml"]
    _approve(runtime, "Geen blok meer.")
    method = workout_method.load(runtime.engine)
    assert method.fallback["run_easy"] == ["*"]
    assert (
        workout_library.render("run_easy", "nl", method)["text"]
        == (workout_library.render("run_easy", "nl", None)["text"])
    )


# --- the code holds no coaching value ------------------------------------------------------


def _seed_texts() -> set[str]:
    """Every coaching text of the seed: names, purposes, cues, labels, exercise codes."""
    out: set[str] = set()

    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and key in {"nl", "en", "cue", "label", "code", "time"}:
            out.add(value)

    walk(workout_method.seed_tables())
    return {t for t in out if len(t) >= 3}


def _literal(node: ast.expr) -> bool:
    if isinstance(node, (ast.List, ast.Tuple)):
        return bool(node.elts) and all(_literal(e) for e in node.elts)
    return isinstance(node, ast.Constant)


def test_no_coaching_value_is_hard_coded_in_the_templates() -> None:
    """The library's code is structure only: no numbers but 0 and 1, no rpm, %, zone or
    minutes in a string, and no text the method holds. Put a new value in the workout method
    (docs/workout-method.md for the seed, the knowledge base for the live value) instead."""
    tree = ast.parse(LIBRARY.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    texts = _seed_texts()
    coaching = re.compile(r"\d+\s*(?:-\s*\d+\s*)?(?:rpm|%|'|m\b|s\b|km)|\bZ\d\b|\bRPE\b", re.I)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings:
            continue
        value = node.value
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)) and value not in (0, 1):
            found.append(f"line {node.lineno}: number {value!r}")
        elif isinstance(value, str) and (coaching.search(value) or any(t in value for t in texts)):
            found.append(f"line {node.lineno}: text {value!r}")
    for node in ast.walk(tree):  # a step value given as a literal keyword
        if (
            isinstance(node, ast.keyword)
            and node.arg in {"cadence", "cue", "minutes", "metres", "bounds"}
            and _literal(node.value)
        ):
            found.append(f"line {node.value.lineno}: {node.arg}= literal")
    assert not found, "coaching values in workout_library.py:\n" + "\n".join(found)


# --- planned sessions ----------------------------------------------------------------------


def _plan(runtime) -> None:
    plan_model.create_plan(
        runtime.engine,
        name="P",
        start=date(2026, 11, 2),
        end=date(2027, 3, 28),
        reason="r",
        author="athlete",
    )


def _session(runtime, day: date, slot: str, **template) -> str:
    method = workout_method.load(runtime.engine)
    built = workout_library.render(template["id"], "nl", method, **template.get("params", {}))
    out = plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=day,
        name=template.get("name", built["name"]),
        sport=built["type"],
        description=template.get("text", built["text"]),
        reason="r",
        author="Claude",
        slot=slot,
        template=workout_rerender.stamp(built, method, "nl") if template.get("stamp") else None,
    )
    return out["session_id"]


def _stored(runtime, session_id: str) -> dict:
    from sqlalchemy import select

    from vibesynergy.store.schema import plan_session

    with runtime.engine.connect() as conn:
        row = conn.execute(select(plan_session).where(plan_session.c.id == session_id))
        return dict(row.mappings().one())


def test_a_session_records_the_template_and_method_version(runtime) -> None:
    _plan(runtime)
    today = date(2026, 11, 21)
    marked = _session(runtime, today + timedelta(days=2), "a", id="ride_endurance", stamp=True)
    seen = _session(runtime, today + timedelta(days=3), "b", id="run_easy")
    by_hand = _session(
        runtime, today + timedelta(days=4), "c", id="run_easy", text="Rustig.\n\n- 40m Z1 HR"
    )
    assert _stored(runtime, marked)["workout"]["template"] == {
        "id": "ride_endurance",
        "params": {"minutes": 60},
        "lang": "nl",
        "method_version": 1,
        "source": "knowledge_base",
    }
    # Written as text, but exactly the template's workout under its name: recognised.
    assert _stored(runtime, seen)["workout"]["template"]["id"] == "run_easy"
    assert "template" not in _stored(runtime, by_hand)["workout"]


def test_rerender_rebuilds_planned_template_sessions_only(runtime) -> None:
    _plan(runtime)
    today = date(2026, 11, 21)
    past = _session(runtime, today - timedelta(days=1), "p", id="ride_endurance", stamp=True)
    now = _session(runtime, today, "t", id="ride_endurance", stamp=True)
    ahead = _session(
        runtime, today + timedelta(days=2), "f", id="ride_brick", params={"minutes": 90}
    )
    renamed = _session(
        runtime,
        today + timedelta(days=3),
        "r",
        id="ride_endurance",
        stamp=True,
        name="Rustige rit met de club",
    )
    by_hand = _session(
        runtime,
        today + timedelta(days=4),
        "h",
        id="ride_endurance",
        text="Eigen rit.\n\n- 60m 60-70%",
    )
    skipped = _session(runtime, today + timedelta(days=5), "s", id="ride_endurance", stamp=True)
    plan_model.skip_session(runtime.engine, session_id=skipped, reason="r", author="athlete")
    nothing = workout_rerender.changes(runtime.engine, today - timedelta(days=7), today)
    assert nothing["changes"] == [] and nothing["unchanged"] == 2

    _approve(runtime, _z2_band(_current_body(runtime), "[85, 95]", "cadans 85-95"))
    found = workout_rerender.changes(runtime.engine, today - timedelta(days=7), today)
    assert found["from"] == "2026-11-22"  # never today or earlier
    assert [c["session_id"] for c in found["changes"]] == [ahead, renamed]
    assert found["not_from_a_template"] == 1
    first, second = found["changes"]
    assert "85-95rpm" in first["description"] and "80-95rpm" in first["before"]["text"]
    assert first["name"] == "Lange rit Z2 90' (brick)" and first["built_with"] == 1
    assert second["name"] == "Rustige rit met de club"  # the coach's own name stays
    for change in found["changes"]:
        plan_proposals.execute(
            runtime.engine,
            "vibesynergy_plan_session_set",
            {k: change[k] for k in ("date", "name", "description", "sport", "session_id")}
            | {"template": change["template"]},
            author="user",
            reason="method v2",
        )
    assert _stored(runtime, ahead)["workout"]["template"]["method_version"] == 2
    assert "85-95rpm" in plan_model.session_text(_stored(runtime, ahead))
    for untouched in (past, now, by_hand, skipped):
        assert "85-95rpm" not in plan_model.session_text(_stored(runtime, untouched))
    again = workout_rerender.changes(runtime.engine, today, today)
    assert again["changes"] == [] and again["unchanged"] == 2


def test_rerender_over_mcp_lists_first_and_writes_on_request(runtime) -> None:
    _plan(runtime)
    start = date.today() + timedelta(days=2)
    result = _call(
        runtime,
        "vibesynergy_plan_session_set",
        {
            "reason": "r",
            "date": start.isoformat(),
            "template_id": "ride_endurance",
            "template_params": {"minutes": 75},
        },
    )
    assert not result.is_error, result.content
    session_id = result.structured_content["data"]["session_id"]
    stored = _stored(runtime, session_id)
    assert stored["name"] == "Duurrit Z2 75'" and stored["sport"] == "Ride"
    assert stored["workout"]["template"]["params"] == {"minutes": 75}
    _approve(runtime, _z2_band(_current_body(runtime), "[85, 95]", "cadans 85-95"))
    listed = _call(
        runtime, "vibesynergy_workout_rerender", {"reason": "v2", "from_date": start.isoformat()}
    )
    data = listed.structured_content["data"]
    assert data["dry_run"] and [c["session_id"] for c in data["changes"]] == [session_id]
    assert "85-95rpm" not in plan_model.session_text(_stored(runtime, session_id))
    written = _call(
        runtime,
        "vibesynergy_workout_rerender",
        {"reason": "v2", "from_date": start.isoformat(), "dry_run": False},
    )
    assert not written.is_error, written.content
    assert "85-95rpm" in plan_model.session_text(_stored(runtime, session_id))
    both = _call(
        runtime,
        "vibesynergy_plan_session_set",
        {"reason": "r", "date": start.isoformat(), "description": "x", "template_id": "run_easy"},
    )
    assert both.is_error


def test_the_library_tool_names_the_method_version(runtime) -> None:
    result = _call(runtime, "vibesynergy_workout_library", {"template_id": "ride_endurance"})
    data = result.structured_content["data"]
    assert data["method"]["version"] == 1 and Z2_CUE in data["text"]
    listed = _call(runtime, "vibesynergy_workout_library", {})
    assert listed.structured_content["data"]["method"]["slug"] == "workout-method"
    assert json.dumps(listed.structured_content["data"]["templates"])
