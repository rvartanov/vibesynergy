"""Versioned coaching knowledge: articles, rules and sources.

The current row is what search and the portal show. Every change appends a revision, so an
older version can be copied forward. A proposal stores the change only; the current row
moves when the athlete approves it.

A proposal that only fixes ``lint`` warnings mechanically (an old name, a rule range, a
fixed threshold now read live) is applied at once as a version by ``lint/editorial`` (#137):
the server decides, never the caller. Every other proposal waits for the athlete.

The FTS5 table ``kb_fts`` is derived. It is created on use, not in the migration, so the
relational schema stays what Alembic compares.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection, Engine

from vibesynergy.core.ids import new_id
from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import kb_fix, kb_lint, neutral, text_diff
from vibesynergy.store.schema import (
    kb_article,
    kb_article_revision,
    kb_proposal,
    kb_rule,
    kb_rule_revision,
    kb_source,
    kb_source_revision,
)

GRADES = frozenset({"A", "B", "C", "D"})
STATUSES = frozenset({"draft", "active", "retired"})
KINDS = frozenset({"article", "rule", "source"})
# Coach → Methods & rules: the routine prompts, the working method and the evidence.
PROMPT_TOPIC = "coach-prompts"
# The workout method (#142): every coaching value of the workout library, read when a
# workout is built. Changed only through a proposal the athlete approves.
WORKOUT_METHOD_TOPIC = "workout-method"
METHOD_TOPICS = frozenset({"coach-loop", "intervals-icu", WORKOUT_METHOD_TOPIC})
PLAN_SUFFIX = "-plan"  # the athlete's plan article (phases, weekly structure), whatever its name
EVIDENCE_PREFIX = "evidence-"
# Old slug -> the slug an article has now (#96). An old slug keeps working wherever a slug is
# read: kb_get, kb_search, proposals, the portal. It can no longer be taken by a new article.
SLUG_ALIASES = {"claude-coach-loop": "ai-coach-loop"}

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RULE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,39}$")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_WORD = re.compile(r"[0-9A-Za-z]+")
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    kind UNINDEXED,
    ref UNINDEXED,
    topic UNINDEXED,
    title,
    body,
    tokenize = "unicode61 remove_diacritics 2"
)
"""


class KnowledgeError(ValueError):
    """A proposal cannot be applied. ``code`` is for the portal."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class KbRuleInput(BaseModel):
    """One rule nested in an article write."""

    model_config = ConfigDict(extra="ignore")
    id: str
    text: str
    grade: str = "C"
    active: bool = True


class KbSourceInput(BaseModel):
    """One source nested in an article write. ``id`` is kept when the AI coach sends it again."""

    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    citation: str
    doi: str | None = None
    url: str | None = None
    verified_at: str | None = None


def upsert_article(
    engine: Engine,
    *,
    slug: str,
    author: str,
    title: str | None = None,
    body_md: str | None = None,
    topic: str | None = None,
    evidence_grade: str | None = None,
    status: str | None = None,
    rules: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
    revert_to: int | None = None,
) -> dict[str, Any]:
    """Create or update an article. Identical content does not add a version.

    ``revert_to`` copies that article version forward as the next version. Rules and sources
    passed here are versioned on their own and are left alone when omitted.
    """
    slug = _slug(slug)
    author = _author(author)
    if revert_to is not None and revert_to < 1:
        raise ValueError("revert_to must be a version number")
    rule_rows = [_rule_input(item) for item in rules or []]
    source_rows = [_source_input(item) for item in sources or []]
    with engine.begin() as conn:
        _apply_article(
            conn,
            slug,
            author,
            title=title,
            body_md=body_md,
            topic=topic,
            evidence_grade=evidence_grade,
            status=status,
            revert_to=revert_to,
        )
        article = _one(conn, kb_article, kb_article.c.slug == slug)
        for rule in rule_rows:
            _apply_rule(conn, rule["id"], author, {**rule, "article_slug": slug}, move=False)
        for source in source_rows:
            _apply_source(conn, source, author, article["id"])
        _rebuild(conn)
    return get_article(engine, slug)


def search(
    engine: Engine, query: str, *, topic: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Full-text search over current articles, rules and sources."""
    match = _match(query)
    topic_filter = topic.strip() if topic and topic.strip() else None
    if topic_filter and len(topic_filter) > 80:
        raise ValueError("topic is too long")
    limit = min(max(int(limit), 1), 50)
    with engine.begin() as conn:
        _ensure_index(conn)
        rows = (
            conn.execute(
                text(
                    """
                    SELECT kind, ref, snippet(kb_fts, 4, '', '', ' … ', 12) AS snippet
                    FROM kb_fts
                    WHERE kb_fts MATCH :match AND (:topic IS NULL OR topic = :topic)
                    ORDER BY bm25(kb_fts)
                    LIMIT :limit
                    """
                ),
                {"match": match, "topic": topic_filter, "limit": limit},
            )
            .mappings()
            .all()
        )
        return [hit for row in rows if (hit := _hit(conn, row))]


def get_article(engine: Engine, slug: str) -> dict[str, Any]:
    """The current article, its rules and sources, and every stored version."""
    slug = _slug(slug)
    with engine.connect() as conn:
        article = _one(conn, kb_article, kb_article.c.slug == slug)
        if article is None:
            raise LookupError(f"no article {slug}")
        return _article_view(conn, article)


def get_rule(engine: Engine, rule_id: str) -> dict[str, Any]:
    """One rule, its older versions, and the article it belongs to."""
    rule_id = _rule_id(rule_id)
    with engine.connect() as conn:
        rule = _one(conn, kb_rule, kb_rule.c.id == rule_id)
        if rule is None:
            raise LookupError(f"no rule {rule_id}")
        article = _one(conn, kb_article, kb_article.c.id == rule["article_id"])
        return {
            "rule": _rule_view(conn, rule),
            "article": {
                "slug": article["slug"],
                "title": article["title"],
                "topic": article["topic"],
                "status": article["status"],
                "version": article["version"],
            },
        }


def list_articles(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = (
            conn.execute(select(kb_article).order_by(kb_article.c.topic, kb_article.c.title))
            .mappings()
            .all()
        )
        return [
            {
                "slug": row["slug"],
                "title": row["title"],
                "topic": row["topic"],
                "status": row["status"],
                "evidence_grade": row["evidence_grade"],
                "version": row["version"],
                "updated_at": _iso(row["updated_at"]),
            }
            for row in rows
        ]


def propose_change(
    engine: Engine,
    *,
    kind: str,
    target: str,
    rationale: str,
    author: str,
    changes: dict[str, Any],
    replace: bool = False,
    editorial: bool = False,
) -> dict[str, Any]:
    """Store a change for the athlete to approve. The current text stays as it is.

    Unless it is a pure lint fix (#137, ``_editorial_check``): then it is applied at once as
    a new version by ``lint/editorial`` (status ``applied``), and the athlete sees it
    afterwards, with an undo. ``editorial`` is the caller's request only; the server
    decides. The check's outcome is stored with the proposal either way.

    A new proposal replaces the same author's proposal for the same target that is still
    waiting: the older one becomes ``superseded``, so the athlete only sees the latest
    wording. Proposals from other authors stay.

    A rule text that has little in common with the rule it would replace is refused unless
    ``replace`` is set (#60): a new rule proposed under an id that is already taken would
    otherwise overwrite an unrelated rule once approved."""
    kind = _kind(kind)
    target = _target(kind, target)
    rationale = _bounded(rationale, "rationale", 2000)
    author = _author(author)
    cleaned = _clean_changes(kind, changes)
    if not cleaned:
        raise ValueError("nothing to change")
    trusted = _trusted_author(engine, author)
    with engine.begin() as conn:
        _check_proposal_shape(conn, kind, target, cleaned)
        base = _version_of(conn, kind, target)
        if kind == "rule" and base and "text" in cleaned and not replace:
            _refuse_unrelated_rule(conn, target, str(cleaned["text"]))
        proposal_id = new_id("kbprop")
        now = utc_now()
        check = _editorial_check(
            conn, _lint_context(conn), kind, target, cleaned, trusted=trusted, author=author
        )
        check["requested"] = bool(editorial)
        conn.execute(
            kb_proposal.update()
            .where(
                kb_proposal.c.kind == kind,
                kb_proposal.c.target == target,
                kb_proposal.c.author == author,
                kb_proposal.c.status == "pending",
            )
            .values(status="superseded", decided_at=now, decided_by=f"superseded:{proposal_id}")
        )
        conn.execute(
            kb_proposal.insert().values(
                id=proposal_id,
                kind=kind,
                target=target,
                base_version=base,
                changes=cleaned,
                rationale=rationale,
                status="pending",
                author=author,
                created_at=now,
                decided_at=None,
                decided_by=None,
                editorial=check,
            )
        )
        if check["applied"]:
            row = {"kind": kind, "target": target, "changes": cleaned, "author": author}
            _apply_proposal(conn, row, author=EDITORIAL_AUTHOR)
            conn.execute(
                kb_proposal.update()
                .where(kb_proposal.c.id == proposal_id)
                .values(status="applied", decided_at=now, decided_by=EDITORIAL_AUTHOR)
            )
            _rebuild(conn)
    return get_proposal(engine, proposal_id)


def pending_proposals(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(kb_proposal)
                .where(kb_proposal.c.status == "pending")
                .order_by(kb_proposal.c.created_at)
            )
            .mappings()
            .all()
        )
        return [_proposal_view(conn, row) for row in rows]


def portal_index(engine: Engine) -> dict[str, Any]:
    """Pending changes and the articles in use. Drafts and retired ones live in the archive."""
    articles = list_articles(engine)
    return {
        "proposals": pending_proposals(engine),
        "articles": [row for row in articles if row["status"] == "active"],
        "archived": sum(1 for row in articles if row["status"] != "active"),
    }


# Rules about how the AI coach works: their ids start with this.
COACH_RULE_PREFIX = "COACH-"


def model_name_warning(kind: str, ref: str, *texts: str | None) -> dict[str, Any] | None:
    """A lint warning when a text the AI coach is told to follow names a model or provider."""
    names = neutral.named_models(" ".join(t for t in texts if t))
    if not names:
        return None
    return {
        "kind": kind,
        "ref": ref,
        "check": "model_name",
        "names": names,
        "message": (
            f"{kind} {ref} names a model or provider ({', '.join(names)}). Write 'AI coach' "
            "(Dutch: 'AI-coach') instead, so any model can follow it."
        ),
    }


_MESSAGES = {
    "retired_name": "{kind} {ref} uses a retired name: {found}. Remove it or write the current "
    "name.",
    "rule_range": "{kind} {ref} refers to {found}, but the rules in use are {expected}. "
    "Write the real range.",
    "unknown_rule": "{kind} {ref} refers to {found}, which is not a rule in use.",
    "unknown_tool": "{kind} {ref} names a tool that does not exist: {found}. Name a "
    "VibeSynergy tool, or remove it.",
    "fixed_threshold": "{kind} {ref} writes thresholds as fixed numbers ({found}). Thresholds "
    "change after every test: write 'read live with " + kb_lint.THRESHOLD_TOOL + "' instead.",
    "workout_value": "{kind} {ref}: workout values missing, in the wrong form or unknown "
    "({found}); the workout library uses its built-in value for each missing or wrong one.",
}


def _warning(
    kind: str,
    ref: str,
    article: str | None,
    check: str,
    names: list[str],
    *,
    expected: list[str] | None = None,
    hints: list[str] | None = None,
) -> dict[str, Any]:
    found = (
        ", ".join(f"{name} ({hint})" for name, hint in zip(names, hints, strict=True))
        if hints
        else ", ".join(names)
    )
    item: dict[str, Any] = {
        "kind": kind,
        "ref": ref,
        "article": article,
        "check": check,
        "names": names,
        "message": _MESSAGES[check].format(
            kind=kind, ref=ref, found=found, expected=", ".join(expected or [])
        ),
    }
    if expected is not None:
        item["expected"] = expected
    return item


def in_threshold_scope(slug: str | None, topic: str | None) -> bool:
    """Does the AI coach follow this article as an instruction or as its working method
    (Coach → Methods & rules: routine instructions, working method, plan)? Such a text holds
    no threshold as a number (#120): thresholds change after every test, and the AI coach
    reads them live with vibesynergy_thresholds."""
    return method_group({"topic": topic, "slug": slug or ""}) in {"prompts", "method"}


def _lint_context(conn: Connection) -> dict[str, Any]:
    from vibesynergy.mcp.server import tool_names  # the real registry; mcp imports query

    rule_ids = conn.execute(select(kb_rule.c.id).where(kb_rule.c.active.is_(True))).scalars()
    return {
        "families": kb_lint.rule_families(rule_ids),
        "tools": tool_names(),
        "retired": kb_lint.retired_list(SLUG_ALIASES),
    }


def _text_warnings(
    ctx: Mapping[str, Any],
    kind: str,
    ref: str,
    article: str | None,
    texts: tuple[str | None, ...],
    *,
    prompt: bool,
    thresholds: bool,
) -> list[dict[str, Any]]:
    """Every check on one article or rule, in a fixed order."""
    text = "\n".join(t for t in texts if t)
    out: list[dict[str, Any]] = []
    if prompt:
        found = model_name_warning(kind, ref, text)
        if found:
            found["article"] = article
            out.append(found)
    retired = kb_lint.retired_names(text, ctx["retired"])
    if retired:
        out.append(
            _warning(
                kind,
                ref,
                article,
                "retired_name",
                [name for name, _ in retired],
                hints=[hint for _, hint in retired],
            )
        )
    for found_range, expected in kb_lint.rule_ranges(text, ctx["families"]):
        out.append(_warning(kind, ref, article, "rule_range", [found_range], expected=[expected]))
    unknown = kb_lint.unknown_rules(text, ctx["families"])
    if unknown:
        out.append(_warning(kind, ref, article, "unknown_rule", unknown))
    tools = kb_lint.unknown_tools(text, ctx["tools"])
    if tools:
        out.append(_warning(kind, ref, article, "unknown_tool", tools))
    if thresholds:
        values = kb_lint.fixed_thresholds(text)
        if values:
            out.append(_warning(kind, ref, article, "fixed_threshold", values))
    return out


def _workout_warnings(kind: str, slug: str, body_md: str | None) -> list[dict[str, Any]]:
    """The workout method's own check (#142): values the article lacks or holds in the wrong
    form, which the library then takes from its built-in seed."""
    from vibesynergy.query import workout_method

    if canonical_slug(slug) != workout_method.SLUG:
        return []
    found = workout_method.lint_body(body_md)
    return [_warning(kind, slug, slug, "workout_value", found)] if found else []


def lint(engine: Engine) -> list[dict[str, Any]]:
    """Warnings about the knowledge base's own text, for every article and rule in use.

    - ``model_name`` (#96): an instruction article (topic ``coach-prompts``) or a COACH rule
      names a model or provider;
    - ``retired_name``, ``rule_range``, ``unknown_rule``, ``unknown_tool`` (#120): any text;
    - ``fixed_threshold`` (#120): an instruction, working-method or plan article, or a rule
      of one, or a COACH rule, writes a threshold as a number;
    - ``workout_value`` (#142): the workout method lacks a value, or holds it in the wrong
      form, so the library uses its built-in value.

    Articles first (by slug), then rules (by id). A warning only; the athlete decides about
    the text."""
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        ctx = _lint_context(conn)
        articles = conn.execute(
            select(
                kb_article.c.id,
                kb_article.c.slug,
                kb_article.c.title,
                kb_article.c.body_md,
                kb_article.c.topic,
            )
            .where(kb_article.c.status == "active")
            .order_by(kb_article.c.slug)
        ).all()
        by_id = {row.id: row for row in articles}
        for row in articles:
            out.extend(
                _text_warnings(
                    ctx,
                    "article",
                    row.slug,
                    row.slug,
                    (row.title, row.body_md),
                    prompt=row.topic == PROMPT_TOPIC,
                    thresholds=in_threshold_scope(row.slug, row.topic),
                )
            )
            out.extend(_workout_warnings("article", row.slug, row.body_md))
        rules = conn.execute(
            select(kb_rule.c.id, kb_rule.c.text, kb_rule.c.article_id)
            .where(kb_rule.c.active.is_(True))
            .order_by(kb_rule.c.id)
        ).all()
        for row in rules:
            article = by_id.get(row.article_id)
            if article is None:
                continue  # a rule of an archived article is out of use with it
            coach = row.id.upper().startswith(COACH_RULE_PREFIX)
            out.extend(
                _text_warnings(
                    ctx,
                    "rule",
                    row.id,
                    article.slug,
                    (row.text,),
                    prompt=coach,
                    thresholds=coach or in_threshold_scope(article.slug, article.topic),
                )
            )
    return out


def change_warnings(
    engine: Engine, kind: str, target: str, changes: Mapping[str, Any]
) -> list[str]:
    """Lint messages for one write or proposal, before it is the current text."""
    with engine.connect() as conn:
        ctx = _lint_context(conn)
        if kind == "article":
            slug = canonical_slug(str(target).strip())
            current = _one(conn, kb_article, kb_article.c.slug == slug)
            topic = changes.get("topic") or (current["topic"] if current is not None else None)
            found = _text_warnings(
                ctx,
                "article",
                slug,
                slug,
                (changes.get("title"), changes.get("body_md")),
                prompt=topic == PROMPT_TOPIC,
                thresholds=in_threshold_scope(slug, topic),
            )
            if changes.get("body_md") is not None:
                found += _workout_warnings("article", slug, changes["body_md"])
        elif kind == "rule":
            rule_id = str(target).strip()
            current = _one(conn, kb_rule, kb_rule.c.id == rule_id)
            article = None
            if current is not None:
                article = _one(conn, kb_article, kb_article.c.id == current["article_id"])
            elif changes.get("article_slug"):
                article = _one(
                    conn,
                    kb_article,
                    kb_article.c.slug == canonical_slug(str(changes["article_slug"])),
                )
            coach = rule_id.upper().startswith(COACH_RULE_PREFIX)
            found = _text_warnings(
                ctx,
                "rule",
                rule_id,
                article["slug"] if article is not None else None,
                (changes.get("text"),),
                prompt=coach,
                thresholds=coach
                or (article is not None and in_threshold_scope(article["slug"], article["topic"])),
            )
        else:
            return []
    return [item["message"] for item in found]


# --- editorial fixes without approval (#137) ---------------------------------------------

# The author of a version an editorial fix writes, and of the proposals the daily fix job
# makes. The proposal keeps who asked for the fix.
EDITORIAL_AUTHOR = "lint/editorial"
EDITORIAL_DAYS = 30  # Coach → Methods & rules lists the fixes of this many days
NOTICE_DAYS = 7  # Waiting for you → Notices has one line for the fixes of this many days
# Why a proposal is not an editorial fix, for the AI coach (the portal has its own words).
EDITORIAL_REASONS = {
    "source": "a source is never an editorial fix",
    "advisory_author": "a proposal from an advisory AI coach always waits",
    "new": "it adds something new",
    "not_text": "it changes more than the text (evidence, status, topic or the article)",
    "content": "a changed part is not a mechanical fix of a lint warning",
    "no_change": "it fixes no lint warning",
    "new_warning": "the new text has a lint warning the current one does not",
    "undone": "the athlete undid this fix before",
    "other_pending": "another proposal for this text waits",
}


def _trusted_author(engine: Engine, author: str) -> bool:
    """May this author's lint fix go through without approval? VibeSynergy's own fix job,
    the athlete's own agent and the primary AI coach may; an advisory AI coach never."""
    if author == EDITORIAL_AUTHOR:
        return True
    from vibesynergy.mcp import roles  # mcp imports query

    return roles.author_role(engine, author) in (roles.OWN, roles.PRIMARY)


def _editorial_subject(conn: Connection, kind: str, target: str) -> dict[str, Any] | None:
    """The current texts and other fields of an article or rule, and how the lint reads
    them. ``None`` when it does not exist yet."""
    if kind == "article":
        row = _one(conn, kb_article, kb_article.c.slug == canonical_slug(target))
        if row is None:
            return None
        return {
            "ref": row["slug"],
            "article": row["slug"],
            "texts": {"title": row["title"], "body_md": row["body_md"]},
            "values": {
                "topic": row["topic"],
                "evidence_grade": row["evidence_grade"],
                "status": row["status"],
            },
            "prompt": row["topic"] == PROMPT_TOPIC,
            "thresholds": in_threshold_scope(row["slug"], row["topic"]),
        }
    if kind == "rule":
        row = _one(conn, kb_rule, kb_rule.c.id == target)
        if row is None:
            return None
        article = _one(conn, kb_article, kb_article.c.id == row["article_id"])
        if article is None:
            return None
        coach = row["id"].upper().startswith(COACH_RULE_PREFIX)
        return {
            "ref": row["id"],
            "article": article["slug"],
            "texts": {"text": row["text"]},
            "values": {
                "grade": row["grade"],
                "active": bool(row["active"]),
                "article_slug": article["slug"],
            },
            "prompt": coach,
            "thresholds": coach or in_threshold_scope(article["slug"], article["topic"]),
        }
    return None


def _fix_slots(ctx: Mapping[str, Any], text_value: str, thresholds: bool) -> list[kb_fix.Slot]:
    return kb_fix.slots(
        text_value,
        slug_aliases=SLUG_ALIASES,
        families=ctx["families"],
        tools=ctx["tools"],
        thresholds=thresholds,
    )


def _finding_names(ctx, kind: str, subject: Mapping[str, Any], texts) -> set[tuple[str, str]]:
    found = _text_warnings(
        ctx,
        kind,
        subject["ref"],
        subject["article"],
        tuple(texts.values()),
        prompt=subject["prompt"],
        thresholds=subject["thresholds"],
    )
    return {(item["check"], name) for item in found for name in item["names"]}


def _editorial_check(
    conn: Connection,
    ctx: Mapping[str, Any],
    kind: str,
    target: str,
    changes: Mapping[str, Any],
    *,
    trusted: bool,
    author: str,
) -> dict[str, Any]:
    """Is this proposal a pure lint fix? ``{"applied", "reason", "fixes"}``.

    Applied only when all of these hold: the author may (``_trusted_author``); the article
    or rule exists; no field other than its text changes; every changed part of every text
    is a mechanical fix of a lint finding (``kb_fix.explain``: per hunk, so one content
    change anywhere sends the whole proposal to the athlete); the result has no lint
    finding the current text did not have; the same fix was not undone before; and no other
    proposal for it waits (applying would make that one stale)."""

    def wait(reason: str, fixes: list[dict[str, str]] | None = None) -> dict[str, Any]:
        return {"applied": False, "reason": reason, "fixes": fixes or []}

    if kind == "source":
        return wait("source")
    if not trusted:
        return wait("advisory_author")
    subject = _editorial_subject(conn, kind, target)
    if subject is None:
        return wait("new")
    for field, value in changes.items():
        if field in subject["values"] and value != subject["values"][field]:
            return wait("not_text")
    fixes: list[dict[str, str]] = []
    after = dict(subject["texts"])
    for field, before in subject["texts"].items():
        if field not in changes:
            continue
        old, new = kb_fix.normal(before), kb_fix.normal(changes[field])
        if old == new:
            continue
        explained = kb_fix.explain(old, new, _fix_slots(ctx, old, subject["thresholds"]))
        if explained is None:
            return wait("content")
        fixes += [{"field": field, **fix.as_dict()} for fix in explained]
        after[field] = new
    if not fixes:
        return wait("no_change")
    if _finding_names(ctx, kind, subject, after) - _finding_names(
        ctx, kind, subject, subject["texts"]
    ):
        return wait("new_warning", fixes)
    earlier = conn.execute(
        select(kb_proposal.c.changes).where(
            kb_proposal.c.kind == kind,
            kb_proposal.c.target.in_([target, subject["ref"]]),
            kb_proposal.c.status == "applied",
        )
    ).scalars()
    wanted = {
        field: kb_fix.normal(changes[field]) for field in subject["texts"] if field in changes
    }
    for old_changes in earlier:
        if {f: kb_fix.normal((old_changes or {}).get(f)) for f in wanted} == wanted:
            return wait("undone", fixes)  # applied once and taken back: the athlete decides
    others = conn.execute(
        select(func.count())
        .select_from(kb_proposal)
        .where(
            kb_proposal.c.kind == kind,
            kb_proposal.c.target.in_([target, subject["ref"]]),
            kb_proposal.c.status == "pending",
            kb_proposal.c.author != author,
        )
    ).scalar()
    if others:
        return wait("other_pending", fixes)
    return {"applied": True, "reason": None, "fixes": fixes}


def _fix_rationale(fixes: list[dict[str, str]]) -> str:
    shown = "; ".join(f"{fix['found']} → {fix['fixed']}" for fix in fixes[:8])
    more = f" (and {len(fixes) - 8} more)" if len(fixes) > 8 else ""
    return f"Editorial fix from the check of the texts (#137): {shown}{more}. Nothing else changes."


def editorial_sweep(engine: Engine) -> dict[str, Any]:
    """Fix every article and rule in use whose lint findings have a mechanical fix, through
    the same path as a proposal (``propose_change`` by ``lint/editorial``). A text whose fix
    would wait (undone before, another proposal waiting) is left alone: its warning stays on
    Coach → Methods & rules, where the athlete decides."""
    todo: list[tuple[str, str, dict[str, str], list[dict[str, str]]]] = []
    with engine.connect() as conn:
        ctx = _lint_context(conn)
        targets = [
            ("article", slug)
            for slug in conn.execute(
                select(kb_article.c.slug)
                .where(kb_article.c.status == "active")
                .order_by(kb_article.c.slug)
            ).scalars()
        ]
        targets += [
            ("rule", rule_id)
            for rule_id in conn.execute(
                select(kb_rule.c.id)
                .join(kb_article, kb_article.c.id == kb_rule.c.article_id)
                .where(kb_rule.c.active.is_(True), kb_article.c.status == "active")
                .order_by(kb_rule.c.id)
            ).scalars()
        ]
        for kind, target in targets:
            subject = _editorial_subject(conn, kind, target)
            if subject is None:
                continue
            changes: dict[str, str] = {}
            fixes: list[dict[str, str]] = []
            for field, value in subject["texts"].items():
                old = kb_fix.normal(value)
                fixed, found = kb_fix.autofix(old, _fix_slots(ctx, old, subject["thresholds"]))
                if found:
                    changes[field] = fixed
                    fixes += [{"field": field, **fix.as_dict()} for fix in found]
            if changes:
                check = _editorial_check(
                    conn, ctx, kind, target, changes, trusted=True, author=EDITORIAL_AUTHOR
                )
                if check["applied"]:
                    todo.append((kind, target, changes, fixes))
    applied = []
    for kind, target, changes, fixes in todo:
        proposal = propose_change(
            engine,
            kind=kind,
            target=target,
            rationale=_fix_rationale(fixes),
            author=EDITORIAL_AUTHOR,
            changes=changes,
            editorial=True,
        )
        if proposal["status"] == "applied":
            applied.append({"kind": kind, "target": target, "fixes": fixes})
    return {"applied": applied}


def editorial_changes(
    engine: Engine, *, days: int = EDITORIAL_DAYS, limit: int = 20
) -> list[dict[str, Any]]:
    """Editorial fixes applied without approval in the last ``days``, newest first, each
    with whether its version is still the current one (then one click undoes it)."""
    since = utc_now() - timedelta(days=days)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(kb_proposal)
                .where(kb_proposal.c.status == "applied", kb_proposal.c.decided_at >= since)
                .order_by(kb_proposal.c.decided_at.desc())
                .limit(limit)
            )
            .mappings()
            .all()
        )
        out = []
        for row in rows:
            current = _current_snapshot(conn, row["kind"], row["target"])
            if current is None:
                continue
            version = int(row["base_version"]) + 1
            if int(current["version"]) == version:
                state = "current"
            elif _same_as_version(conn, row["kind"], row["target"], int(row["base_version"])):
                state = "undone"
            else:
                state = "changed"
            out.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "target": current.get("slug") or row["target"],
                    "title": current.get("title") or row["target"],
                    "article": current.get("slug") or current.get("article_slug"),
                    "author": row["author"],
                    "at": _iso(row["decided_at"]),
                    "version": version,
                    "base_version": int(row["base_version"]),
                    "state": state,
                    "fixes": list((row["editorial"] or {}).get("fixes") or []),
                }
            )
        return out


def _same_as_version(conn: Connection, kind: str, target: str, version: int) -> bool:
    """Is the current text that of ``version`` again (an undo)?"""
    if kind == "article":
        row = _one(conn, kb_article, kb_article.c.slug == canonical_slug(target))
        if row is None:
            return False
        old = _revision(conn, kb_article_revision, row["id"], version, "article_id")
        return old is not None and (old["title"], old["body_md"]) == (row["title"], row["body_md"])
    row = _one(conn, kb_rule, kb_rule.c.id == target)
    if row is None:
        return False
    old = _revision(conn, kb_rule_revision, target, version, "rule_id")
    return old is not None and old["text"] == row["text"]


def archived_articles(engine: Engine) -> list[dict[str, Any]]:
    """Articles out of use (draft or retired): hidden from the normal views, never deleted."""
    return [row for row in list_articles(engine) if row["status"] != "active"]


def is_prompt_article(engine: Engine, slug: str, topic: str | None = None) -> bool:
    """Is this (existing or about to be created) article one of the coach's instructions?"""
    if topic == PROMPT_TOPIC:
        return True
    with engine.connect() as conn:
        article = _one(conn, kb_article, kb_article.c.slug == canonical_slug(str(slug).strip()))
    return article is not None and article.topic == PROMPT_TOPIC


def is_workout_method(engine: Engine, slug: str, topic: str | None = None) -> bool:
    """Is this (existing or about to be created) article the workout method (#142)? Its
    values are coaching facts: they change only through a proposal the athlete approves."""
    from vibesynergy.query import workout_method

    if topic == WORKOUT_METHOD_TOPIC or canonical_slug(str(slug).strip()) == workout_method.SLUG:
        return True
    with engine.connect() as conn:
        article = _one(conn, kb_article, kb_article.c.slug == canonical_slug(str(slug).strip()))
    return article is not None and article.topic == WORKOUT_METHOD_TOPIC


def method_group(article: Mapping[Any, Any]) -> str | None:
    """Which part of Coach → Methods & rules an article belongs to, if any."""
    if article["topic"] == PROMPT_TOPIC:
        return "prompts"
    if article["topic"] in METHOD_TOPICS or str(article["slug"]).endswith(PLAN_SUFFIX):
        return "method"
    if str(article["slug"]).startswith(EVIDENCE_PREFIX):
        return "evidence"
    return None


def methods_index(engine: Engine) -> dict[str, Any]:
    """The coach's prompts, working method and evidence articles in use, with their rules,
    and the lint warnings about the knowledge base's text."""
    groups: dict[str, list[dict[str, Any]]] = {"prompts": [], "method": [], "evidence": []}
    with engine.connect() as conn:
        articles = (
            conn.execute(
                select(kb_article)
                .where(kb_article.c.status == "active")
                .order_by(kb_article.c.topic, kb_article.c.title)
            )
            .mappings()
            .all()
        )
        for article in articles:
            group = method_group(article)
            if group is None:
                continue
            rules = (
                conn.execute(
                    select(kb_rule)
                    .where(kb_rule.c.article_id == article["id"])
                    .order_by(kb_rule.c.id)
                )
                .mappings()
                .all()
            )
            sources = conn.execute(
                select(func.count())
                .select_from(kb_source)
                .where(kb_source.c.article_id == article["id"])
            ).scalar()
            groups[group].append(
                {
                    "slug": article["slug"],
                    "title": article["title"],
                    "topic": article["topic"],
                    "evidence_grade": article["evidence_grade"],
                    "version": article["version"],
                    "updated_at": _iso(article["updated_at"]),
                    "sources": int(sources or 0),
                    "rules": [
                        {
                            "id": rule["id"],
                            "text": rule["text"],
                            "grade": rule["grade"],
                            "active": bool(rule["active"]),
                            "version": rule["version"],
                        }
                        for rule in rules
                    ],
                }
            )
    # The knowledge-base lint (#96, #120), shown above the groups: the same warnings every
    # AI coach gets in the handbook, with the fixes applied without approval (#137) and the
    # changes to these texts that wait for the athlete.
    slugs = {article["slug"] for group in groups.values() for article in group}
    waiting = [
        proposal
        for proposal in pending_proposals(engine)
        if (proposal.get("current") or {}).get("slug") in slugs
        or (proposal.get("current") or {}).get("article_slug") in slugs
        or (proposal["kind"] == "rule" and proposal["changes"].get("article_slug") in slugs)
    ]
    return {
        "groups": groups,
        "warnings": lint(engine),
        "editorial": editorial_changes(engine),
        "proposals": waiting,
    }


def edit_article(
    engine: Engine,
    slug: str,
    *,
    author: str,
    expected_version: int,
    title: str | None = None,
    body_md: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """The athlete's own edit, saved at once as a new version.

    ``expected_version`` is the version the edit started from; when the coach wrote a newer
    one in between, nothing is saved (``stale``).
    """
    slug = _slug(slug)
    author = _author(author)
    with engine.begin() as conn:
        current = _article_by_slug(conn, slug)
        if int(current["version"]) != int(expected_version):
            raise KnowledgeError("the article changed since you opened it", code="stale")
        _apply_article(
            conn,
            slug,
            author,
            title=title,
            body_md=body_md,
            topic=None,
            evidence_grade=None,
            status=status,
            revert_to=None,
        )
        _rebuild(conn)
    return get_article(engine, slug)


def edit_rule(
    engine: Engine,
    rule_id: str,
    *,
    author: str,
    expected_version: int,
    text_value: str | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    """The athlete's own edit of one rule, saved at once as a new version."""
    rule_id = _rule_id(rule_id)
    author = _author(author)
    with engine.begin() as conn:
        current = _one(conn, kb_rule, kb_rule.c.id == rule_id)
        if current is None:
            raise LookupError(f"no rule {rule_id}")
        if int(current["version"]) != int(expected_version):
            raise KnowledgeError("the rule changed since you opened it", code="stale")
        _apply_rule(conn, rule_id, author, {"text": text_value, "active": active})
        _rebuild(conn)
    return get_rule(engine, rule_id)


def revert_rule(engine: Engine, rule_id: str, version: int, *, author: str) -> dict[str, Any]:
    """Copy an older rule version forward as the next version."""
    rule_id = _rule_id(rule_id)
    author = _author(author)
    with engine.begin() as conn:
        old = _revision(conn, kb_rule_revision, rule_id, int(version), "rule_id")
        if old is None:
            raise LookupError(f"rule {rule_id} has no version {version}")
        article = _one(conn, kb_article, kb_article.c.id == old["article_id"])
        _apply_rule(
            conn,
            rule_id,
            author,
            {
                "text": old["text"],
                "grade": old["grade"],
                "active": bool(old["active"]),
                "article_slug": article["slug"] if article else None,
            },
        )
        _rebuild(conn)
    return get_rule(engine, rule_id)


def revert_source(engine: Engine, source_id: str, version: int, *, author: str) -> dict[str, Any]:
    """Copy an older source version forward as the next version."""
    source_id = _source_id(source_id)
    author = _author(author)
    with engine.begin() as conn:
        old = _revision(conn, kb_source_revision, source_id, int(version), "source_id")
        if old is None:
            raise LookupError(f"source {source_id} has no version {version}")
        _apply_source(
            conn,
            {
                "id": source_id,
                "citation": old["citation"],
                "doi": old["doi"],
                "url": old["url"],
                "verified_at": old["verified_at"],
            },
            author,
            old["article_id"],
        )
        _rebuild(conn)
        row = _one(conn, kb_source, kb_source.c.id == source_id)
        return _source_view(conn, row)


def get_proposal(engine: Engine, proposal_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = _one(conn, kb_proposal, kb_proposal.c.id == proposal_id)
        if row is None:
            raise LookupError(f"no proposal {proposal_id}")
        return _proposal_view(conn, row)


def approve_proposal(engine: Engine, proposal_id: str, *, actor: str) -> dict[str, Any]:
    """Apply a pending proposal as a new version. The approver is recorded separately."""
    actor = _author(actor)
    with engine.begin() as conn:
        row = _lock_proposal(conn, proposal_id)
        if row["status"] != "pending":
            raise KnowledgeError("proposal is not pending", code="not_pending")
        current = _version_of(conn, row["kind"], row["target"])
        if current != row["base_version"]:
            raise KnowledgeError("the current version changed since this proposal", code="stale")
        _apply_proposal(conn, row)
        now = utc_now()
        conn.execute(
            kb_proposal.update()
            .where(kb_proposal.c.id == proposal_id)
            .values(status="approved", decided_at=now, decided_by=actor)
        )
        _rebuild(conn)
    return get_proposal(engine, proposal_id)


def reject_proposal(engine: Engine, proposal_id: str, *, actor: str) -> dict[str, Any]:
    actor = _author(actor)
    with engine.begin() as conn:
        row = _lock_proposal(conn, proposal_id)
        if row["status"] != "pending":
            raise KnowledgeError("proposal is not pending", code="not_pending")
        conn.execute(
            kb_proposal.update()
            .where(kb_proposal.c.id == proposal_id)
            .values(status="rejected", decided_at=utc_now(), decided_by=actor)
        )
    return get_proposal(engine, proposal_id)


def _apply_proposal(conn: Connection, row: Any, *, author: str | None = None) -> None:
    """Write a proposal's change. The new version's author is the proposal's, or ``author``
    (``lint/editorial`` for an editorial fix)."""
    changes = dict(row["changes"])
    author = str(author or row["author"])
    kind, target = row["kind"], row["target"]
    if kind == "article":
        # A proposal made for an old slug (#96) applies to the article under its new one.
        slug = canonical_slug(target)
        _apply_article(conn, slug, author, **_article_kwargs(changes), revert_to=None)
    elif kind == "rule":
        _apply_rule(conn, target, author, changes)
    else:
        article_id = None
        if changes.get("article_slug"):
            article_id = _article_by_slug(conn, changes["article_slug"])["id"]
        payload = {"id": target}
        for key in ("citation", "doi", "url", "verified_at"):
            if key in changes:
                payload[key] = changes[key]
        _apply_source(conn, payload, author, article_id, partial=True)


def _article_kwargs(changes: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": changes.get("title"),
        "body_md": changes.get("body_md"),
        "topic": changes.get("topic"),
        "evidence_grade": changes.get("evidence_grade"),
        "status": changes.get("status"),
    }


def _apply_article(
    conn: Connection,
    slug: str,
    author: str,
    *,
    title: str | None,
    body_md: str | None,
    topic: str | None,
    evidence_grade: str | None,
    status: str | None,
    revert_to: int | None,
) -> None:
    current = _one(conn, kb_article, kb_article.c.slug == slug)
    if revert_to is not None:
        if current is None:
            raise LookupError(f"no article {slug}")
        previous = _revision(conn, kb_article_revision, current["id"], revert_to, "article_id")
        if previous is None:
            raise ValueError(f"article {slug} has no version {revert_to}")
        title, body_md, topic = previous["title"], previous["body_md"], previous["topic"]
        evidence_grade, status = previous["evidence_grade"], previous["status"]
    if current is None:
        title = _bounded(title, "title", 200)
        body_md = _bounded(body_md, "body_md", 50_000)
        topic = _bounded(topic, "topic", 80)
        grade = _grade(evidence_grade or "C")
        state = _status(status or "active")
        now = utc_now()
        article_id = new_id("kbart")
        conn.execute(
            kb_article.insert().values(
                id=article_id,
                slug=slug,
                topic=topic,
                title=title,
                body_md=body_md,
                evidence_grade=grade,
                status=state,
                version=1,
                created_at=now,
                updated_at=now,
                author=author,
            )
        )
        _insert_article_revision(
            conn, article_id, 1, slug, topic, title, body_md, grade, state, author, now
        )
        return
    title = current["title"] if title is None else _bounded(title, "title", 200)
    body_md = current["body_md"] if body_md is None else _bounded(body_md, "body_md", 50_000)
    topic = current["topic"] if topic is None else _bounded(topic, "topic", 80)
    grade = current["evidence_grade"] if evidence_grade is None else _grade(evidence_grade)
    state = current["status"] if status is None else _status(status)
    if (
        title == current["title"]
        and body_md == current["body_md"]
        and topic == current["topic"]
        and grade == current["evidence_grade"]
        and state == current["status"]
    ):
        return
    version = current["version"] + 1
    now = utc_now()
    conn.execute(
        kb_article.update()
        .where(kb_article.c.id == current["id"])
        .values(
            topic=topic,
            title=title,
            body_md=body_md,
            evidence_grade=grade,
            status=state,
            version=version,
            updated_at=now,
            author=author,
        )
    )
    _insert_article_revision(
        conn, current["id"], version, slug, topic, title, body_md, grade, state, author, now
    )


def _insert_article_revision(
    conn: Connection,
    article_id: str,
    version: int,
    slug: str,
    topic: str,
    title: str,
    body_md: str,
    grade: str,
    status: str,
    author: str,
    now: datetime,
) -> None:
    conn.execute(
        kb_article_revision.insert().values(
            article_id=article_id,
            version=version,
            slug=slug,
            topic=topic,
            title=title,
            body_md=body_md,
            evidence_grade=grade,
            status=status,
            author=author,
            created_at=now,
        )
    )


def _apply_rule(
    conn: Connection, rule_id: str, author: str, changes: dict[str, Any], *, move: bool = True
) -> None:
    rule_id = _rule_id(rule_id)
    current = _one(conn, kb_rule, kb_rule.c.id == rule_id)
    article_id = None
    if changes.get("article_slug"):
        article_id = _article_by_slug(conn, _slug(changes["article_slug"]))["id"]
    # Writing an article's rules must not take a rule id that another article owns (9-11:
    # writing SWIM-1..3 for a new article silently overwrote another article's SWIM rules).
    if (
        not move
        and current is not None
        and article_id is not None
        and current["article_id"] != article_id
    ):
        owner = conn.execute(
            select(kb_article.c.slug).where(kb_article.c.id == current["article_id"])
        ).scalar()
        raise ValueError(
            f"rule {rule_id} belongs to the article {owner!r}; choose another id for this article"
        )
    if current is None:
        if article_id is None:
            raise ValueError("a new rule needs article_slug")
        text_value = _bounded(changes.get("text"), "text", 4000)
        grade = _grade(changes.get("grade") or "C")
        active = (
            True
            if "active" not in changes or changes["active"] is None
            else bool(changes["active"])
        )
        now = utc_now()
        conn.execute(
            kb_rule.insert().values(
                id=rule_id,
                article_id=article_id,
                text=text_value,
                grade=grade,
                active=active,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        _insert_rule_revision(conn, rule_id, 1, article_id, text_value, grade, active, author, now)
        return
    if article_id is None:
        article_id = current["article_id"]
    text_value = (
        current["text"] if changes.get("text") is None else _bounded(changes["text"], "text", 4000)
    )
    grade = current["grade"] if changes.get("grade") is None else _grade(changes["grade"])
    if "active" not in changes or changes.get("active") is None:
        active = bool(current["active"])
    else:
        active = bool(changes["active"])
    if (
        text_value == current["text"]
        and grade == current["grade"]
        and active == bool(current["active"])
        and article_id == current["article_id"]
    ):
        return
    version = current["version"] + 1
    now = utc_now()
    conn.execute(
        kb_rule.update()
        .where(kb_rule.c.id == rule_id)
        .values(
            article_id=article_id,
            text=text_value,
            grade=grade,
            active=active,
            version=version,
            updated_at=now,
        )
    )
    _insert_rule_revision(
        conn, rule_id, version, article_id, text_value, grade, active, author, now
    )


def _insert_rule_revision(
    conn: Connection,
    rule_id: str,
    version: int,
    article_id: str,
    text_value: str,
    grade: str,
    active: bool,
    author: str,
    now: datetime,
) -> None:
    conn.execute(
        kb_rule_revision.insert().values(
            rule_id=rule_id,
            version=version,
            article_id=article_id,
            text=text_value,
            grade=grade,
            active=active,
            author=author,
            created_at=now,
        )
    )


def _apply_source(
    conn: Connection,
    source: dict[str, Any],
    author: str,
    article_id: str | None,
    *,
    partial: bool = False,
) -> None:
    source_id = source.get("id")
    current = _one(conn, kb_source, kb_source.c.id == source_id) if source_id else None
    if current is None and article_id and not source_id:
        current = _match_source(conn, article_id, source.get("doi"), source.get("citation"))
        if current is not None:
            source_id = current["id"]
    if current is None:
        if article_id is None:
            raise ValueError("a new source needs article_slug")
        citation = _bounded(source.get("citation"), "citation", 500)
        doi = _doi(source.get("doi"))
        url = _url(source.get("url"))
        verified = _verified(source.get("verified_at"))
        source_id = _source_id(source_id) if source_id else new_id("kbsrc")
        now = utc_now()
        conn.execute(
            kb_source.insert().values(
                id=source_id,
                article_id=article_id,
                citation=citation,
                doi=doi,
                url=url,
                verified_at=verified,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        _insert_source_revision(
            conn, source_id, 1, article_id, citation, doi, url, verified, author, now
        )
        return
    source_id = current["id"]
    if article_id is None:
        article_id = current["article_id"]
    if partial:
        citation = (
            current["citation"]
            if "citation" not in source
            else _bounded(source.get("citation"), "citation", 500)
        )
        doi = current["doi"] if "doi" not in source else _doi(source.get("doi"))
        url = current["url"] if "url" not in source else _url(source.get("url"))
        verified = (
            current["verified_at"]
            if "verified_at" not in source
            else _verified(source.get("verified_at"))
        )
    else:
        citation = _bounded(source.get("citation"), "citation", 500)
        doi = _doi(source.get("doi"))
        url = _url(source.get("url"))
        verified = _verified(source.get("verified_at"))
    if (
        citation == current["citation"]
        and doi == current["doi"]
        and url == current["url"]
        and verified == current["verified_at"]
        and article_id == current["article_id"]
    ):
        return
    version = current["version"] + 1
    now = utc_now()
    conn.execute(
        kb_source.update()
        .where(kb_source.c.id == source_id)
        .values(
            article_id=article_id,
            citation=citation,
            doi=doi,
            url=url,
            verified_at=verified,
            version=version,
            updated_at=now,
        )
    )
    _insert_source_revision(
        conn, source_id, version, article_id, citation, doi, url, verified, author, now
    )


def _insert_source_revision(
    conn: Connection,
    source_id: str,
    version: int,
    article_id: str,
    citation: str,
    doi: str | None,
    url: str | None,
    verified: datetime | None,
    author: str,
    now: datetime,
) -> None:
    conn.execute(
        kb_source_revision.insert().values(
            source_id=source_id,
            version=version,
            article_id=article_id,
            citation=citation,
            doi=doi,
            url=url,
            verified_at=verified,
            author=author,
            created_at=now,
        )
    )


def _match_source(conn: Connection, article_id: str, doi: str | None, citation: str | None):
    rows = (
        conn.execute(select(kb_source).where(kb_source.c.article_id == article_id)).mappings().all()
    )
    wanted_doi = _doi(doi) if doi else None
    if wanted_doi:
        for row in rows:
            if row["doi"] == wanted_doi:
                return row
    if citation and citation.strip():
        for row in rows:
            if row["citation"] == citation.strip():
                return row
    return None


def _check_proposal_shape(
    conn: Connection, kind: str, target: str, changes: dict[str, Any]
) -> None:
    """Refuse a proposal that could never be approved."""
    if kind == "article":
        if _one(conn, kb_article, kb_article.c.slug == target) is None:
            for field in ("title", "body_md", "topic"):
                if not changes.get(field):
                    raise ValueError(f"a new article needs {field}")
        return
    if kind == "rule":
        if _one(conn, kb_rule, kb_rule.c.id == target) is None:
            if not changes.get("text"):
                raise ValueError("a new rule needs text")
            if not changes.get("article_slug"):
                raise ValueError("a new rule needs article_slug")
            _article_by_slug(conn, _slug(changes["article_slug"]))
        return
    if _one(conn, kb_source, kb_source.c.id == target) is None:
        if not changes.get("citation"):
            raise ValueError("a new source needs citation")
        if not changes.get("article_slug"):
            raise ValueError("a new source needs article_slug")
        _article_by_slug(conn, _slug(changes["article_slug"]))


RELATED_MIN = 0.2  # share of words two versions of one rule have in common at least
_OVERLAP_WORD = re.compile(r"[\w%]{3,}", re.UNICODE)


def _words(text: str) -> set[str]:
    return {w.casefold() for w in _OVERLAP_WORD.findall(text or "")}


def text_overlap(old: str, new: str) -> float:
    """Words two texts share, as a share of the smaller one (0 to 1)."""
    a, b = _words(old), _words(new)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def next_rule_id(conn: Connection, target: str) -> str | None:
    """The next free id with the same prefix: STR-3 taken, STR-4 free."""
    match = re.fullmatch(r"([A-Z][A-Z0-9]*)-(\d+)", target)
    if not match:
        return None
    prefix = match.group(1)
    taken = set(
        conn.execute(select(kb_rule.c.id).where(kb_rule.c.id.like(f"{prefix}-%"))).scalars()
    )
    pending = set(
        conn.execute(
            select(kb_proposal.c.target).where(
                kb_proposal.c.kind == "rule", kb_proposal.c.status == "pending"
            )
        ).scalars()
    )
    numbers = [
        int(i.rsplit("-", 1)[1]) for i in taken | pending if re.fullmatch(rf"{prefix}-\d+", i)
    ]
    # After the highest, never in a gap: a gap can be a retired rule that entries still name.
    return f"{prefix}-{max(numbers, default=0) + 1}"


def _refuse_unrelated_rule(conn: Connection, target: str, new_text: str) -> None:
    current = _one(conn, kb_rule, kb_rule.c.id == target)
    if current is None or text_overlap(current["text"], new_text) >= RELATED_MIN:
        return
    free = next_rule_id(conn, target)
    hint = f" Use a new id ({free})" if free else " Use a new id"
    raise ValueError(
        f"{target} already exists and says something else: "
        f'"{current["text"][:120]}…". This proposal would replace it.{hint}, '
        "or pass replace=true if this rule is meant to be replaced."
    )


def _version_of(conn: Connection, kind: str, target: str) -> int:
    if kind == "article":
        row = _one(conn, kb_article, kb_article.c.slug == canonical_slug(target))
    elif kind == "rule":
        row = _one(conn, kb_rule, kb_rule.c.id == target)
    else:
        row = _one(conn, kb_source, kb_source.c.id == target)
    return 0 if row is None else int(row["version"])


def _lock_proposal(conn: Connection, proposal_id: str):
    row = _one(conn, kb_proposal, kb_proposal.c.id == proposal_id)
    if row is None:
        raise LookupError(f"no proposal {proposal_id}")
    return row


def _article_view(conn: Connection, article) -> dict[str, Any]:
    rules = (
        conn.execute(
            select(kb_rule).where(kb_rule.c.article_id == article["id"]).order_by(kb_rule.c.id)
        )
        .mappings()
        .all()
    )
    sources = (
        conn.execute(
            select(kb_source)
            .where(kb_source.c.article_id == article["id"])
            .order_by(kb_source.c.id)
        )
        .mappings()
        .all()
    )
    view = _public(article)
    view["history"] = _history(
        conn, kb_article_revision, kb_article_revision.c.article_id == article["id"]
    )
    view["rules"] = [_rule_view(conn, rule) for rule in rules]
    view["sources"] = [_source_view(conn, source) for source in sources]
    return view


def _rule_view(conn: Connection, rule) -> dict[str, Any]:
    view = _public(rule)
    view["active"] = bool(rule["active"])
    view["history"] = _history(conn, kb_rule_revision, kb_rule_revision.c.rule_id == rule["id"])
    return view


def _source_view(conn: Connection, source) -> dict[str, Any]:
    view = _public(source)
    view["history"] = _history(
        conn, kb_source_revision, kb_source_revision.c.source_id == source["id"]
    )
    return view


def _history(conn: Connection, table, where) -> list[dict[str, Any]]:
    rows = conn.execute(select(table).where(where).order_by(table.c.version)).mappings().all()
    return [_public(row) for row in rows]


def _proposal_view(conn: Connection, row) -> dict[str, Any]:
    view = _public(row)
    view["current"] = _current_snapshot(conn, row["kind"], row["target"])
    view["diff"] = proposal_diff(row["kind"], view["current"], dict(row["changes"] or {}))
    return view


# What a proposal changes, field by field: text fields as a line diff with a little context,
# other fields as before -> after (#137). A new article, rule or source has no diff.
_DIFF_TEXT = {"article": ("title", "body_md"), "rule": ("text",), "source": ("citation",)}
_DIFF_VALUE = {
    "article": ("topic", "evidence_grade", "status"),
    "rule": ("grade", "active", "article_slug"),
    "source": ("doi", "url", "verified_at", "article_slug"),
}


def proposal_diff(
    kind: str, current: Mapping[str, Any] | None, changes: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if current is None:
        return []
    out: list[dict[str, Any]] = []
    for field in _DIFF_TEXT[kind]:
        if field not in changes:
            continue
        before, after = str(current.get(field) or ""), str(changes[field] or "")
        if kb_fix.normal(before) != kb_fix.normal(after):
            out.append({"field": field, "type": "text", "lines": text_diff.lines(before, after)})
    for field in _DIFF_VALUE[kind]:
        if field in changes and changes[field] != current.get(field):
            out.append(
                {
                    "field": field,
                    "type": "value",
                    "before": current.get(field),
                    "after": changes[field],
                }
            )
    return out


def _current_snapshot(conn: Connection, kind: str, target: str) -> dict[str, Any] | None:
    if kind == "article":
        row = _one(conn, kb_article, kb_article.c.slug == canonical_slug(target))
        if row is None:
            return None
        return {
            "version": row["version"],
            "title": row["title"],
            "body_md": row["body_md"],
            "topic": row["topic"],
            "evidence_grade": row["evidence_grade"],
            "status": row["status"],
            "slug": row["slug"],
        }
    if kind == "rule":
        row = _one(conn, kb_rule, kb_rule.c.id == target)
        if row is None:
            return None
        article = _one(conn, kb_article, kb_article.c.id == row["article_id"])
        return {
            "version": row["version"],
            "text": row["text"],
            "grade": row["grade"],
            "active": bool(row["active"]),
            "article_slug": article["slug"],
            "article_title": article["title"],
        }
    row = _one(conn, kb_source, kb_source.c.id == target)
    if row is None:
        return None
    article = _one(conn, kb_article, kb_article.c.id == row["article_id"])
    return {
        "version": row["version"],
        "citation": row["citation"],
        "doi": row["doi"],
        "url": row["url"],
        "article_slug": article["slug"],
        "article_title": article["title"],
    }


def _hit(conn: Connection, row) -> dict[str, Any] | None:
    kind, ref, snippet = row["kind"], row["ref"], row["snippet"] or ""
    if kind == "article":
        article = _one(conn, kb_article, kb_article.c.slug == ref)
        if article is None:
            return None
        return {
            "kind": "article",
            "ref": ref,
            "title": article["title"],
            "topic": article["topic"],
            "snippet": snippet,
            "article_slug": article["slug"],
            "status": article["status"],
            "grade": article["evidence_grade"],
        }
    if kind == "rule":
        rule = _one(conn, kb_rule, kb_rule.c.id == ref)
        if rule is None:
            return None
        article = _one(conn, kb_article, kb_article.c.id == rule["article_id"])
        return {
            "kind": "rule",
            "ref": ref,
            "title": ref,
            "topic": article["topic"],
            "snippet": snippet,
            "article_slug": article["slug"],
            "grade": rule["grade"],
            "active": bool(rule["active"]),
        }
    source = _one(conn, kb_source, kb_source.c.id == ref)
    if source is None:
        return None
    article = _one(conn, kb_article, kb_article.c.id == source["article_id"])
    return {
        "kind": "source",
        "ref": ref,
        "title": source["citation"],
        "topic": article["topic"],
        "snippet": snippet,
        "article_slug": article["slug"],
        "url": source["url"],
    }


def _ensure_index(conn: Connection) -> None:
    conn.execute(text(_FTS_DDL))
    indexed = conn.execute(text("SELECT count(*) FROM kb_fts")).scalar() or 0
    if indexed != _live_count(conn):
        _rebuild(conn)


def _rebuild(conn: Connection) -> None:
    conn.execute(text(_FTS_DDL))
    conn.execute(text("DELETE FROM kb_fts"))
    articles = conn.execute(select(kb_article)).mappings().all()
    by_id = {row["id"]: row for row in articles}
    for article in articles:
        _put_fts(
            conn,
            "article",
            article["slug"],
            article["topic"],
            # The slug and its old names are searchable, so a search for either finds it.
            " ".join(
                [
                    article["topic"],
                    article["slug"],
                    *slug_aliases(article["slug"]),
                    article["title"],
                ]
            ),
            article["body_md"],
        )
    for rule in conn.execute(select(kb_rule)).mappings():
        article = by_id[rule["article_id"]]
        _put_fts(
            conn,
            "rule",
            rule["id"],
            article["topic"],
            f"{article['topic']} {rule['id']}",
            rule["text"],
        )
    for source in conn.execute(select(kb_source)).mappings():
        article = by_id[source["article_id"]]
        body = " ".join(part for part in (source["citation"], source["doi"], source["url"]) if part)
        _put_fts(
            conn,
            "source",
            source["id"],
            article["topic"],
            f"{article['topic']} {source['citation']}",
            body,
        )


def _put_fts(conn: Connection, kind: str, ref: str, topic: str, title: str, body: str) -> None:
    conn.execute(
        text(
            "INSERT INTO kb_fts (kind, ref, topic, title, body) "
            "VALUES (:kind, :ref, :topic, :title, :body)"
        ),
        {"kind": kind, "ref": ref, "topic": topic, "title": title, "body": body or ""},
    )


def _live_count(conn: Connection) -> int:
    total = 0
    for table in (kb_article, kb_rule, kb_source):
        total += conn.execute(select(func.count()).select_from(table)).scalar() or 0
    return total


def _one(conn: Connection, table, where):
    return conn.execute(select(table).where(where)).mappings().first()


def _revision(conn: Connection, table, parent_id: str, version: int, column: str):
    return (
        conn.execute(select(table).where(table.c[column] == parent_id, table.c.version == version))
        .mappings()
        .first()
    )


def _article_by_slug(conn: Connection, slug: str):
    slug = canonical_slug(slug)
    row = _one(conn, kb_article, kb_article.c.slug == slug)
    if row is None:
        raise LookupError(f"no article {slug}")
    return row


def _public(row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        out[key] = _iso(value) if isinstance(value, datetime) else value
    return out


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _clean_changes(kind: str, changes: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "article": {"title", "body_md", "topic", "evidence_grade", "status"},
        "rule": {"text", "grade", "active", "article_slug"},
        "source": {"citation", "doi", "url", "verified_at", "article_slug"},
    }[kind]
    cleaned: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in allowed or value is None:
            continue
        if key == "evidence_grade" or key == "grade":
            cleaned[key] = _grade(value)
        elif key == "status":
            cleaned[key] = _status(value)
        elif key == "active":
            cleaned[key] = bool(value)
        elif key == "article_slug":
            cleaned[key] = _slug(value)
        elif key == "url":
            cleaned[key] = _url(value)
        elif key == "doi":
            cleaned[key] = _doi(value)
        elif key == "verified_at":
            verified = _verified(value)
            cleaned[key] = _iso(verified) if verified else None
        elif key == "title":
            cleaned[key] = _bounded(value, "title", 200)
        elif key == "body_md":
            cleaned[key] = _bounded(value, "body_md", 50_000)
        elif key == "topic":
            cleaned[key] = _bounded(value, "topic", 80)
        elif key == "text":
            cleaned[key] = _bounded(value, "text", 4000)
        elif key == "citation":
            cleaned[key] = _bounded(value, "citation", 500)
    return {key: value for key, value in cleaned.items() if value is not None or key == "active"}


def _rule_input(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _rule_id(item.get("id", "")),
        "text": _bounded(item.get("text"), "text", 4000),
        "grade": _grade(item.get("grade") or "C"),
        "active": bool(item.get("active", True)),
    }


def _source_input(item: dict[str, Any]) -> dict[str, Any]:
    source_id = item.get("id")
    return {
        "id": _source_id(source_id) if source_id else None,
        "citation": item.get("citation"),
        "doi": item.get("doi"),
        "url": item.get("url"),
        "verified_at": item.get("verified_at"),
    }


def _match(query: str) -> str:
    if len(query or "") > 200:
        raise ValueError("query is too long")
    tokens = _WORD.findall(query or "")
    if not tokens:
        raise ValueError("query needs a word to search for")
    return " AND ".join(f'"{token}"' for token in tokens)


def canonical_slug(slug: str) -> str:
    """The slug an article has now: an old slug (``SLUG_ALIASES``) gives the new one."""
    return SLUG_ALIASES.get(slug, slug)


def slug_aliases(slug: str) -> list[str]:
    """The old slugs that still lead to this article."""
    return sorted(old for old, new in SLUG_ALIASES.items() if new == slug)


def _slug(value: str | None) -> str:
    slug = (value or "").strip()
    if not _SLUG.fullmatch(slug) or len(slug) > 80:
        raise ValueError("slug must be lowercase words separated by hyphens")
    return canonical_slug(slug)


def _rule_id(value: str | None) -> str:
    rule_id = (value or "").strip()
    if not _RULE_ID.fullmatch(rule_id):
        raise ValueError("rule id must look like HRV-2")
    return rule_id


def _source_id(value: str | None) -> str:
    source_id = (value or "").strip()
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError("source id must be letters, digits or . _ : -")
    return source_id


def _target(kind: str, value: str) -> str:
    if kind == "article":
        return _slug(value)
    if kind == "rule":
        return _rule_id(value)
    return _source_id(value)


def _kind(value: str) -> str:
    if value not in KINDS:
        raise ValueError("kind must be article, rule or source")
    return value


def _grade(value: str | None) -> str:
    grade = (value or "").strip().upper()
    if grade not in GRADES:
        raise ValueError("evidence grade must be A, B, C or D")
    return grade


def _status(value: str | None) -> str:
    status = (value or "").strip().lower()
    if status not in STATUSES:
        raise ValueError("status must be draft, active or retired")
    return status


def _author(value: str | None) -> str:
    author = (value or "").strip()
    if not author or len(author) > 200:
        raise ValueError("author is required")
    return author


def _bounded(value: str | None, field: str, limit: int) -> str:
    text_value = (value or "").strip()
    if not text_value:
        raise ValueError(f"{field} is required")
    if len(text_value) > limit:
        raise ValueError(f"{field} is too long")
    return text_value


def _doi(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    doi = str(value).strip()
    if len(doi) > 200:
        raise ValueError("doi is too long")
    return doi


def _url(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    url = str(value).strip()
    if len(url) > 500 or not url.startswith(("https://", "http://")):
        raise ValueError("url must be an http(s) address")
    return url


def _verified(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("verified_at must include a timezone")
        return value.astimezone(UTC)
    text_value = str(value).strip()
    if len(text_value) == 10:
        return datetime.fromisoformat(text_value).replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(text_value)
    if parsed.tzinfo is None:
        raise ValueError("verified_at must include a timezone")
    return parsed.astimezone(UTC)
