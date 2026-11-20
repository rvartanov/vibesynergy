"""Model-neutral harness (#96): no model or provider name where the role is meant.

- The guard: every text a model reads (harness rules, seed prompts, the handbook, the
  contract help, context labels, MCP tool descriptions) and the portal's texts name no
  model or provider, and call the role "AI coach" (Dutch: "AI-coach").
- The slug ``ai-coach-loop`` with its old name as an alias, and migration 0052.
- The knowledge-base lint.
- A coach run through the OpenAI-format client against a stub server that passes the
  output contract, and what the request leaves out for a provider without it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
from dataclasses import replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import pytest
from sqlalchemy import select, text, update

from vibesynergy import alerts, mailer
from vibesynergy.coach_runner import context, contract, prompts, registry, runner
from vibesynergy.coach_runner import model as runner_model
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.coach_runner.tools import COACH_SCOPES, ToolBox
from vibesynergy.core.timeutil import Window
from vibesynergy.mcp import server as mcp_server
from vibesynergy.query import coach, coach_handbook, coach_log, coach_prompts, journal, knowledge
from vibesynergy.query.neutral import MODEL_NAMES, named_models
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log, kb_article, kb_proposal, note

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "vibesynergy"
I18N = SRC / "portal" / "static" / "i18n"
DAY = date(2026, 11, 1)

# The role is "AI coach" / "AI-coach". These flag the bare forms where the role is meant;
# a compound that names a thing ("the coach feed", "the coach loop") is not the role.
_THINGS = (
    "feed|entry|entries|task|tasks|loop|run|runs|runner|page|pages|log|protocol|handbook|"
    "link|links|routine|routines|settings|prompt|prompts|app|apps|insight|passphrase|token|"
    "write|state|language|register|booklet"
)
ROLE_EN = re.compile(
    rf"\b(?:the|your|a new|one active|every|previous) coach\b(?![ -](?:{_THINGS})\b)"
    r"|\bthe AI\b(?![ -](?:coach|app|harness|module)\b)",
    re.IGNORECASE,
)
# Dutch: "de Coach-pagina" is the page and "coach-apps" a thing; lower-case "coach" alone is
# the role.
ROLE_NL = re.compile(r"\b(?i:de|je|jouw) coach\b(?!-)|\b(?i:de) AI\b(?!-)")


def role_words(text_: str) -> list[str]:
    return [m.group(0) for m in ROLE_EN.finditer(text_)] + [
        m.group(0) for m in ROLE_NL.finditer(text_)
    ]


# --- the name check itself ---------------------------------------------------------------


def test_named_models_finds_names_and_only_names() -> None:
    assert named_models("Je bent Claude, de coach") == ["Claude"]
    assert named_models("use gpt-5 or claude-opus-5-5") == ["GPT", "Claude", "Opus"]
    assert named_models("lees claude-coach-loop") == ["Claude"]
    assert named_models("ChatGPT, OpenAI and xAI's Grok") == ["ChatGPT", "OpenAI", "xAI", "Grok"]
    assert named_models("Je bent de AI-coach. Schrijf sonnetten in opusvorm.") == []
    assert named_models(None) == []
    assert {"Claude", "Anthropic", "Opus", "Sonnet", "Haiku", "Fable", "GPT", "Gemini"} <= set(
        MODEL_NAMES
    )


# --- the guard: model-facing text --------------------------------------------------------

# Modules whose whole source is model-facing or steers it: no model or provider name at
# all, comments included. The provider adapters, the model list and prices, the provider
# settings and the routine adapter are not in this list (coach_runner/model.py,
# settings.py, pricing.py, __init__.py, runner.py, query/agent_events.py): they have to
# name what they talk to. What runner.py sends is checked on the wire, in the run below.
NEUTRAL_MODULES = (
    "coach_runner/prompts.py",
    "coach_runner/contract.py",
    "coach_runner/context.py",
    "coach_runner/registry.py",
    "coach_runner/report.py",
    "coach_runner/tools.py",
    "query/coach.py",
    "query/coach_handbook.py",
    "query/coach_prompts.py",
    "query/coach_tasks.py",
    "query/neutral.py",
    "mcp/server.py",
    *(f"mcp/tools/{p.name}" for p in sorted((SRC / "mcp" / "tools").glob("*.py"))),
)
# The one module that has to list the names it looks for.
NAME_LIST_MODULE = "query/neutral.py"


def _model_facing(rt) -> dict[str, str]:
    """Every text a model is given, by where it comes from."""
    texts: dict[str, str] = {"mcp guide": mcp_server.GUIDE, "safety help": contract.SAFETY_HELP}
    for job in coach_prompts.JOBS:
        for via in ("runner", "mcp"):
            texts[f"rules {job} via {via}"] = prompts.rules_for(job, via=via)
        texts[f"seed {job}"] = coach_prompts.seed_text(job) or ""
        texts[f"prompt {job}"] = prompts.job_prompt(rt.settings, job)
    texts["rewrites"] = "\n".join(replacement for _, replacement in prompts.REWRITES)
    texts["protocol"] = coach_handbook.protocol_text() or ""
    texts["coach state note"] = coach.COACH_STATE_NOTE
    handbook, _warnings = coach_handbook.handbook(
        rt.engine, scopes=frozenset(COACH_SCOPES), include_protocol=False
    )
    texts["handbook"] = json.dumps(handbook, ensure_ascii=False, default=str)
    texts["harness tasks"] = json.dumps(
        [(t.id, t.title, t.contract.entry_type) for t in registry.TASKS], ensure_ascii=False
    )
    server = mcp_server.build_server(rt)
    for tool in anyio.run(server.list_tools):
        texts[f"tool {tool.name}"] = json.dumps(
            {"description": tool.description, "schema": tool.input_schema}, ensure_ascii=False
        )
    # What vibesynergy_coach_prompt hands an AI app: prompt, rules, contract and the
    # preloaded context with its block labels.
    box = ToolBox(rt, frozenset(COACH_SCOPES))
    for job in ("morning", "evening", "weekly", "insight"):
        answer, is_error = box.call(
            "vibesynergy_coach_prompt",
            {"job": job, "include_context": True, "date": DAY.isoformat()},
        )
        assert not is_error, answer
        texts[f"coach_prompt {job}"] = answer
    texts["context labels"] = "\n".join(
        f"{block.name}: {block.title}" for block in context.BLOCKS.values()
    )
    return texts


def test_no_model_or_provider_name_in_model_facing_text(runtime) -> None:
    found = {
        where: names
        for where, body in _model_facing(runtime).items()
        if (names := named_models(body))
    }
    assert not found, f"write 'AI coach', not a model or provider name: {found}"


def test_model_facing_modules_name_no_model() -> None:
    found = {}
    for name in NEUTRAL_MODULES:
        if name == NAME_LIST_MODULE:
            continue
        names = named_models((SRC / name).read_text(encoding="utf-8"))
        if names:
            found[name] = names
    assert not found, found
    assert named_models((SRC / NAME_LIST_MODULE).read_text(encoding="utf-8"))


def test_the_role_is_called_ai_coach_in_model_facing_text(runtime) -> None:
    texts = _model_facing(runtime)
    found = {where: words for where, body in texts.items() if (words := role_words(body))}
    assert not found, f"the role is 'AI coach' (Dutch 'AI-coach'): {found}"
    # And the term is really there, in the rules of both languages' readers.
    assert "AI-coach" in texts["rules morning via mcp"]
    assert "AI coach" in texts["protocol"] and "AI app" in texts["protocol"]
    assert all("AI-coach" in texts[f"seed {job}"] for job in coach_prompts.JOBS)


def test_role_check_knows_role_from_thing() -> None:
    assert role_words("Ask the coach.") and role_words("Vraag het de coach.")
    assert role_words("the AI reads it") and role_words("de AI leest het")
    assert not role_words("Read the coach feed, then the coach loop article.")
    assert not role_words("Your AI coach reads it. Je AI-coach leest het. De AI-app ook.")
    assert not role_words("Schrijf als een coach die even appt.")


# --- the guard: the portal's texts --------------------------------------------------------

# Portal texts that may name a provider: where the athlete picks one. Nothing else.
PROVIDER_SETTINGS_TEXTS = {
    # The kind of link for one vendor's routine API, and where its address comes from.
    "Claude Code routine",
    "Enter the routine's API address "
    "(https://api.anthropic.com/v1/claude_code/routines/<id>/fire) or its id.",
    # Coach → Settings → AI providers: why two of them take a key.
    "Keys are kept encrypted and never shown again. OpenAI and xAI do not let other apps "
    "sign in with your ChatGPT or Grok account, so those take an API key.",
    # What a key of each provider starts with.
    "This does not look like a key for this provider (it should start with xai-).",
}
# "Your coach" as a person: the calendar's own appointments and who a share link is for.
HUMAN_COACH_TEXTS = {"Sessions with your coach", "Sharing with your coach or an app"}


def _catalogue(lang: str) -> dict[str, str]:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def test_portal_texts_name_no_model_or_provider() -> None:
    nl, en = _catalogue("nl"), _catalogue("en")
    assert set(nl) >= PROVIDER_SETTINGS_TEXTS, "an exemption that no longer exists"
    found = {}
    for key in en:
        if key in PROVIDER_SETTINGS_TEXTS:
            continue
        names = named_models(f"{key}\n{en[key]}\n{nl[key]}")
        if names:
            found[key] = names
    assert not found, found
    assert len(PROVIDER_SETTINGS_TEXTS) <= 5  # exemptions stay few


def test_portal_texts_call_the_role_ai_coach() -> None:
    nl = _catalogue("nl")
    assert set(nl) >= HUMAN_COACH_TEXTS
    found = {
        key: words
        for key, value in nl.items()
        if key not in HUMAN_COACH_TEXTS and (words := role_words(f"{key}\n{value}"))
    }
    assert not found, found
    assert nl["Reply to your AI coach"] == "Antwoord je AI-coach"
    assert (
        "AI-app"
        in nl[
            "Connect an AI app here. It uses this address and a passphrase you "
            "choose. No command is needed."
        ]
    )


def test_provider_names_in_the_portal_stay_in_the_provider_settings() -> None:
    """In the pages' own code a provider is named only where one is picked."""
    allowed = {"coach_runner_settings.js", "agent_links.js"}
    found = {}
    for path in sorted((SRC / "portal" / "static" / "js").rglob("*.js")):
        if "vendor" in path.parts or path.name in allowed:
            continue
        names = named_models(path.read_text(encoding="utf-8"))
        if names:
            found[path.name] = names
    assert not found, found


# --- the slug and its alias ----------------------------------------------------------------


def _loop_article(rt, slug: str = "ai-coach-loop") -> dict:
    return knowledge.upsert_article(
        rt.engine,
        slug=slug,
        author="user",
        title="Werkwijze AI-coach",
        body_md="De werkwijze: ochtend, avond, week.",
        topic="coach-loop",
        rules=[{"id": "COACH-1", "text": "Er is één actieve AI-coach.", "grade": "C"}],
    )


def test_old_slug_is_an_alias_everywhere_a_slug_is_read(runtime) -> None:
    _loop_article(runtime)
    assert knowledge.SLUG_ALIASES == {"claude-coach-loop": "ai-coach-loop"}
    old = knowledge.get_article(runtime.engine, "claude-coach-loop")
    assert old["slug"] == "ai-coach-loop" and old["title"] == "Werkwijze AI-coach"
    # Search finds it by the old slug, by the new one and by its words.
    for query in ("claude-coach-loop", "ai-coach-loop", "werkwijze"):
        hits = knowledge.search(runtime.engine, query)
        assert [h["ref"] for h in hits if h["kind"] == "article"] == ["ai-coach-loop"], query
    # A write or a proposal under the old slug lands on the same article, never a second one.
    again = _loop_article(runtime, slug="claude-coach-loop")
    assert again["slug"] == "ai-coach-loop" and again["version"] == 1
    proposal = knowledge.propose_change(
        runtime.engine,
        kind="article",
        target="claude-coach-loop",
        rationale="neutral",
        author="client:app",
        changes={"body_md": "Nieuwe werkwijze."},
    )
    assert proposal["target"] == "ai-coach-loop" and proposal["base_version"] == 1
    knowledge.approve_proposal(runtime.engine, proposal["id"], actor="user")
    assert knowledge.get_article(runtime.engine, "claude-coach-loop")["body_md"] == (
        "Nieuwe werkwijze."
    )
    with runtime.engine.connect() as conn:
        assert conn.execute(select(kb_article.c.slug)).scalars().all() == ["ai-coach-loop"]
    # The change log shows one history under either name.
    assert coach_log.diff(runtime.engine, "article", "claude-coach-loop", 2)["target"] == (
        "ai-coach-loop"
    )


def test_old_slug_over_mcp(runtime) -> None:
    _loop_article(runtime)
    box = ToolBox(runtime, frozenset(COACH_SCOPES))
    got, is_error = box.call("vibesynergy_kb_get", {"slug": "claude-coach-loop"})
    assert not is_error and json.loads(got)["data"]["slug"] == "ai-coach-loop"
    found, is_error = box.call("vibesynergy_kb_search", {"query": "claude-coach-loop"})
    assert not is_error
    assert [h["ref"] for h in json.loads(found)["data"]["hits"]][:1] == ["ai-coach-loop"]
    assert "ai-coach-loop" in coach.COACH_STATE_NOTE


def _migration():
    path = SRC / "store" / "migrations" / "versions" / "0052_model_neutral_names.py"
    spec = importlib.util.spec_from_file_location("migration_0052", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(rt, direction: str = "upgrade") -> None:
    migration = _migration()

    class _Op:
        @staticmethod
        def get_bind():
            return bind

    with rt.engine.begin() as bind:
        migration.op = _Op
        getattr(migration, direction)()


def _as_before_the_rename(rt) -> dict[str, str]:
    """The live shape before 0052: the article under its old slug, with proposals waiting
    for it (an article change, a rule change and a new rule for it), and an old AI note."""
    _loop_article(rt)
    ids = {}
    ids["article"] = knowledge.propose_change(
        rt.engine,
        kind="article",
        target="ai-coach-loop",
        rationale="AI-coach wording",
        author="client:app",
        changes={"title": "Werkwijze AI-coach (v2)", "body_md": "Lees claude-coach-loop niet."},
    )["id"]
    ids["rule"] = knowledge.propose_change(
        rt.engine,
        kind="rule",
        target="COACH-1",
        rationale="AI-coach wording",
        author="client:app",
        changes={"text": "De AI-coach is de enige coach; er is één actieve AI-coach."},
    )["id"]
    ids["new_rule"] = knowledge.propose_change(
        rt.engine,
        kind="rule",
        target="COACH-9",
        rationale="new",
        author="client:app",
        changes={"text": "Een nieuwe regel.", "article_slug": "ai-coach-loop"},
    )["id"]
    journal.add_note(rt.engine, text="[Coach] werklog", start=DAY, source="portal")
    with rt.engine.begin() as conn:
        conn.execute(update(kb_article).values(slug="claude-coach-loop"))
        conn.execute(text("UPDATE kb_article_revision SET slug = 'claude-coach-loop'"))
        conn.execute(
            update(kb_proposal)
            .where(kb_proposal.c.id == ids["article"])
            .values(target="claude-coach-loop")
        )
        conn.execute(
            update(kb_proposal)
            .where(kb_proposal.c.id == ids["new_rule"])
            .values(changes={"text": "Een nieuwe regel.", "article_slug": "claude-coach-loop"})
        )
        conn.execute(update(note).values(source="claude"))
    return ids


def test_migration_renames_the_slug_and_waiting_proposals_still_apply(runtime) -> None:
    ids = _as_before_the_rename(runtime)
    _run_migration(runtime)
    _run_migration(runtime)  # a second run changes nothing
    with runtime.engine.connect() as conn:
        assert conn.execute(select(kb_article.c.slug)).scalars().all() == ["ai-coach-loop"]
        revisions = conn.execute(text("SELECT DISTINCT slug FROM kb_article_revision")).scalars()
        assert revisions.all() == ["ai-coach-loop"]
        targets = dict(conn.execute(select(kb_proposal.c.id, kb_proposal.c.target)).all())
        assert conn.execute(select(note.c.source)).scalars().all() == ["ai"]
    assert targets[ids["article"]] == "ai-coach-loop"
    waiting = {p["id"]: p for p in knowledge.pending_proposals(runtime.engine)}
    assert set(waiting) == set(ids.values())
    assert waiting[ids["new_rule"]]["changes"]["article_slug"] == "ai-coach-loop"
    # Every proposal that waited still applies, to the renamed article. Its text is the
    # proposal's own, old slug in the body included: the migration does not edit text.
    for proposal_id in ids.values():
        assert knowledge.approve_proposal(runtime.engine, proposal_id, actor="user")["status"] == (
            "approved"
        )
    article = knowledge.get_article(runtime.engine, "ai-coach-loop")
    assert article["title"] == "Werkwijze AI-coach (v2)" and article["version"] == 2
    assert article["body_md"] == "Lees claude-coach-loop niet."
    rules = {rule["id"]: rule["text"] for rule in article["rules"]}
    assert rules["COACH-1"].startswith("De AI-coach is de enige coach")
    assert rules["COACH-9"] == "Een nieuwe regel."
    assert [h["ref"] for h in knowledge.search(runtime.engine, "claude-coach-loop")][:1] == [
        "ai-coach-loop"
    ]
    # The old AI note is still a working log, not the athlete's own words.
    stored = journal.notes(runtime.engine, Window(DAY, DAY))
    assert [journal.own_words(item) for item in stored] == [False]


def test_waiting_proposal_under_the_old_slug_applies_without_the_migration_too(runtime) -> None:
    """A proposal that still names the old slug (a restored backup, a proposal made between
    the deploy and the migration) is resolved through the alias when it is approved."""
    ids = _as_before_the_rename(runtime)
    with runtime.engine.begin() as conn:  # only the article was renamed
        conn.execute(update(kb_article).values(slug="ai-coach-loop"))
    for proposal_id in ids.values():
        knowledge.approve_proposal(runtime.engine, proposal_id, actor="user")
    article = knowledge.get_article(runtime.engine, "claude-coach-loop")
    assert article["slug"] == "ai-coach-loop" and article["title"] == "Werkwijze AI-coach (v2)"
    assert {rule["id"] for rule in article["rules"]} == {"COACH-1", "COACH-9"}


def test_migration_leaves_an_existing_new_article_alone_and_goes_back(runtime) -> None:
    _as_before_the_rename(runtime)
    _run_migration(runtime)
    _run_migration(runtime, "downgrade")
    with runtime.engine.connect() as conn:
        assert conn.execute(select(kb_article.c.slug)).scalars().all() == ["claude-coach-loop"]
        assert conn.execute(select(note.c.source)).scalars().all() == ["claude"]
    # With an ai-coach-loop article already there, the old one is not renamed over it.
    with runtime.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO kb_article (id, slug, topic, title, body_md, evidence_grade, "
                "status, version, created_at, updated_at, author) SELECT 'kbart_new', "
                "'ai-coach-loop', topic, title, body_md, evidence_grade, status, 1, "
                "created_at, updated_at, author FROM kb_article"
            )
        )
    _run_migration(runtime)
    with runtime.engine.connect() as conn:
        slugs = conn.execute(select(kb_article.c.slug).order_by(kb_article.c.slug)).scalars()
        assert slugs.all() == ["ai-coach-loop", "claude-coach-loop"]


# --- neutral sources -----------------------------------------------------------------------


def test_agent_notes_have_a_neutral_source_and_the_old_one_still_reads() -> None:
    from vibesynergy.mcp.identity import note_source, reset_connection, set_connection

    assert journal.AGENT_SOURCE == "ai"
    for source in ("ai", "claude", "client:oa-1"):
        assert journal.own_words({"source": source}) is False
    assert journal.own_words({"source": "portal"}) is True
    for label in ("Claude", "ChatGPT", "My own agent"):
        tokens = set_connection("oa-1", label=label)
        try:
            assert note_source() == "client:oa-1"  # the app's id, never its name
        finally:
            reset_connection(tokens)


# --- the knowledge-base lint ---------------------------------------------------------------


def test_kb_lint_warns_when_an_instruction_or_coach_rule_names_a_model(runtime) -> None:
    engine = runtime.engine
    coach_prompts.seed(engine)
    _loop_article(runtime)
    # The seeds and a neutral rule name no model (the seeds cite COACH-5, not in use here).
    assert [w for w in knowledge.lint(engine) if w["check"] == "model_name"] == []
    knowledge.upsert_article(
        engine,
        slug="coach-prompt-morning",
        author="user",
        body_md="Je bent Claude, de coach. Gebruik Opus.",
    )
    knowledge.upsert_article(
        engine,
        slug="ai-coach-loop",
        author="user",
        rules=[
            {"id": "COACH-1", "text": "Claude is de enige coach.", "grade": "C"},
            {"id": "COACH-2", "text": "GPT mag niet.", "grade": "C", "active": False},
            {"id": "HRV-1", "text": "Volgens Claude is HRV nuttig.", "grade": "C"},
        ],
    )
    # An evidence article may quote whatever it wants: only instructions and COACH rules.
    knowledge.upsert_article(
        engine,
        slug="evidence-llm",
        author="user",
        title="Modellen",
        body_md="Claude en GPT vergeleken.",
        topic="evidence",
    )
    found = [w for w in knowledge.lint(engine) if w["check"] == "model_name"]
    assert [(w["kind"], w["ref"], w["names"]) for w in found] == [
        ("article", "coach-prompt-morning", ["Claude", "Opus"]),
        ("rule", "COACH-1", ["Claude"]),
    ]
    assert all(w["check"] == "model_name" and "AI coach" in w["message"] for w in found)
    # Every AI coach is told, in the handbook's warnings.
    _data, warnings = coach_handbook.handbook(engine, scopes=frozenset(COACH_SCOPES))
    assert [w for w in warnings if "names a model" in w] == [w["message"] for w in found]


def test_a_proposal_that_names_a_model_gets_the_warning(runtime) -> None:
    coach_prompts.seed(runtime.engine)
    _loop_article(runtime)
    box = ToolBox(runtime, frozenset(COACH_SCOPES) | {"plan.propose"})

    def propose(**arguments) -> list[str]:
        answer, is_error = box.call(
            "vibesynergy_kb_propose_change", {"rationale": "test", **arguments}
        )
        assert not is_error, answer
        return json.loads(answer)["meta"]["warnings"]

    named = propose(kind="article", target="coach-prompt-evening", body_md="Je bent Claude.")
    assert len(named) == 1 and "Claude" in named[0] and "AI coach" in named[0]
    assert (
        propose(kind="article", target="coach-prompt-evening", body_md="Je bent de AI-coach.") == []
    )
    assert len(propose(kind="rule", target="COACH-1", new_text="Claude is de enige AI-coach.")) == 1
    assert propose(kind="article", target="ai-coach-loop", body_md="Claude schreef dit ooit.") == []


# --- any provider: the request, the key, a whole run ----------------------------------------


def test_request_options_follow_what_the_provider_can_do() -> None:
    full = runner_model.request_options("anthropic", "claude-opus-5-5", "high")
    assert full == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "cache_control": {"type": "ephemeral"},
    }
    budget = runner_model.request_options("anthropic", "claude-haiku-4-5-20251001", "high")
    assert budget == {
        "thinking": {"type": "enabled", "budget_tokens": 4000},
        "cache_control": {"type": "ephemeral"},
    }
    for provider, name in (("openai", "gpt-5"), ("xai", "grok-4"), ("local", "qwen2.5")):
        assert runner_model.request_options(provider, name, "high") == {}
        can = runner_model.capabilities(provider, name)
        assert can == {"tool_use"} and not can & set(runner_model.OPTIONAL)
        # Tool use is all a task requires, so every provider may run every task.
        assert all(registry.model_ok(task, can) for task in registry.live())


def test_api_key_check_is_per_provider(runtime) -> None:
    good = {
        "anthropic": "sk-ant-api03-SYNTHETIC-never-shown",
        "openai": "sk-proj-SYNTHETIC-never-shown",
        "xai": "xai-SYNTHETIC-never-shown",
        "local": "anything-goes-here",
    }
    for provider, key in good.items():
        assert runner_settings.set_api_key(runtime, key, provider=provider).keys[provider]
    wrong = {
        "anthropic": good["openai"],  # another provider's key
        "openai": good["anthropic"],
        "xai": good["openai"],
    }
    for provider, key in wrong.items():
        with pytest.raises(runner_settings.RunnerSettingsError) as refused:
            runner_settings.set_api_key(runtime, key, provider=provider)
        assert str(refused.value) == runner_settings.KEY_ERRORS[provider]
        assert not named_models(str(refused.value).replace("xai-", ""))
        assert str(refused.value) in _catalogue("nl")
    with pytest.raises(runner_settings.RunnerSettingsError):
        runner_settings.set_api_key(runtime, "short", provider="local")


class _Stub:
    """A Chat Completions server on localhost that plays a scripted model."""

    def __init__(self, script) -> None:
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                stub.headers.append({k.lower(): v for k, v in self.headers.items()})
                assert self.path == "/v1/chat/completions"
                answer = json.dumps(script(len(stub.requests), body)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _Stub:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 80},
    }


SUMMARY = "Alex, loop vandaag rustig: een uur in een tempo waarop je kunt praten."


def _script(turn: int, body: dict) -> dict:
    """Reads the day, writes an entry that misses the contract, reads the refusal and
    writes it again as the contract asks: what any model with tool use has to manage."""
    entry = {
        "entry_type": "morning",
        "summary": SUMMARY,
        "idempotency_key": f"morning-{DAY.isoformat()}",
        "for_date": DAY.isoformat(),
        "details": "Check-in en metingen wijzen dezelfde kant op.",
    }
    last = body["messages"][-1]
    if turn == 1:
        return _call("call_1", "vibesynergy_today", {"date": DAY.isoformat()})
    if turn == 2:
        assert last["role"] == "tool" and last["tool_call_id"] == "call_1"
        return _call("call_2", "vibesynergy_coach_write", entry)  # no safety: refused
    if turn == 3:
        assert last["role"] == "tool" and "safety" in last["content"]
        floor = json.loads(next(m for m in body["messages"] if m["role"] == "tool")["content"])[
            "data"
        ]["safety"]["level"]
        colour = {"ok": "green", "ease": "amber", "flag": "amber", "refuse": "red"}[floor]
        safety = {"level": floor, "colour": colour, "reasons": ["Niets wijst op terughouden."]}
        return _call("call_3", "vibesynergy_coach_write", {**entry, "safety": safety})
    assert last["role"] == "tool" and "safety" not in json.loads(last["content"]).get("error", "")
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "Klaar."}}
        ],
        "usage": {"prompt_tokens": 1500, "completion_tokens": 5},
    }


def _keys_in(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys_in(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys_in(v)}
    return set()


def test_a_coach_run_through_a_second_provider_passes_the_contract(runtime, monkeypatch) -> None:
    """End to end over HTTP with the OpenAI-format client: no key of the default provider,
    no thinking, effort or cache settings on the wire, and the entry meets the contract."""
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda settings: True)
    # The daily advice is a push and a portal notice by default; read it here as a mail.
    routes = alerts.routes
    monkeypatch.setattr(
        alerts, "routes", lambda settings: {**routes(settings), "advice": ["portal", "email"]}
    )
    monkeypatch.setattr(
        mailer, "send", lambda settings, to, subject, body, html=None: sent.append((subject, body))
    )
    rt = Runtime.open(
        replace(
            runtime.settings,
            server={"public_url": "https://server.example.ts.net"},
            alerts={"email_to": "athlete@example.com"},
        )
    )
    key = "sk-proj-SYNTHETIC-second-provider-never-shown"
    try:
        with _Stub(_script) as stub:
            monkeypatch.setitem(runner_settings.PROVIDER_URLS, "openai", stub.url)
            runner_settings.set_api_key(rt, key, provider="openai")
            runner_settings.save(
                rt,
                {"enabled": True, "tasks": {"morning": {"provider": "openai", "model": "gpt-5"}}},
            )
            assert not runner_settings.load(rt).keys["anthropic"]
            result = runner.run(rt, "morning", DAY.isoformat(), DAY)
        assert result.status == "done", result
        assert len(stub.requests) == 4
        with rt.engine.connect() as conn:
            entries = conn.execute(select(coach_decision_log)).mappings().all()
        assert len(entries) == 1 and entries[0]["text"] == SUMMARY
        harness = entries[0]["meta"]["harness"]
        assert harness["via"] == "runner" and harness["task"] == "morning"
        assert contract.parse_stamp(harness["stamp"])["rules"] == prompts.RULES_VERSION
        assert entries[0]["meta"]["safety"]["reasons"] == ["Niets wijst op terughouden."]
        assert any(SUMMARY in body for _subject, body in sent)  # the report was mailed
        row = runner.get_run(rt, "morning", DAY.isoformat())
        assert row["model"] == "gpt-5" and row["turns"] == 4
        assert row["input_tokens"] == 1200 * 3 + 1500 and row["cache_read_tokens"] == 0
        # What went over the wire: the OpenAI format, with the tools, and nothing the
        # provider does not have.
        for body, headers in zip(stub.requests, stub.headers, strict=True):
            assert headers["authorization"] == f"Bearer {key}"
            assert body["model"] == "gpt-5" and body["messages"][0]["role"] == "system"
            assert {t["type"] for t in body["tools"]} == {"function"}
            assert not _keys_in(body) & {
                "thinking",
                "output_config",
                "cache_control",
                "budget_tokens",
                "effort",
                "signature",
            }
            # And no model or provider name in anything the model was told: the system
            # text, the start message with its context, and every tool description.
            told = {**body, "model": "", "messages": body["messages"][:2]}
            assert named_models(json.dumps(told, ensure_ascii=False)) == []
        assert "AI-coach" in stub.requests[0]["messages"][0]["content"]
    finally:
        rt.close()
