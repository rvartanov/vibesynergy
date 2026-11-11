"""Versioned coaching knowledge: articles, rules and sources.

The current row is what search and the portal show. Every change appends a revision, so an
older version can be copied forward. A proposal stores the change only; the current row
moves when the athlete approves it.

The FTS5 table ``kb_fts`` is derived. It is created on use, not in the migration, so the
relational schema stays what Alembic compares.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection, Engine

from vibesynergy.core.ids import new_id
from vibesynergy.core.timeutil import utc_now
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
METHOD_TOPICS = frozenset({"coach-loop", "intervals-icu"})
PLAN_SUFFIX = "-plan"  # the athlete's plan article (phases, weekly structure), whatever its name
EVIDENCE_PREFIX = "evidence-"

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
    """One source nested in an article write. ``id`` is kept when the coach sends it again."""

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
) -> dict[str, Any]:
    """Store a change for the athlete to approve. The current text stays as it is."""
    kind = _kind(kind)
    target = _target(kind, target)
    rationale = _bounded(rationale, "rationale", 2000)
    author = _author(author)
    cleaned = _clean_changes(kind, changes)
    if not cleaned:
        raise ValueError("nothing to change")
    with engine.begin() as conn:
        _check_proposal_shape(conn, kind, target, cleaned)
        base = _version_of(conn, kind, target)
        proposal_id = new_id("kbprop")
        now = utc_now()
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
            )
        )
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


def archived_articles(engine: Engine) -> list[dict[str, Any]]:
    """Articles out of use (draft or retired): hidden from the normal views, never deleted."""
    return [row for row in list_articles(engine) if row["status"] != "active"]


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
    """The coach's prompts, working method and evidence articles in use, with their rules."""
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
    return {"groups": groups}


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


def _apply_proposal(conn: Connection, row: Any) -> None:
    changes = dict(row["changes"])
    author = row["author"]
    kind, target = row["kind"], row["target"]
    if kind == "article":
        _apply_article(conn, target, author, **_article_kwargs(changes), revert_to=None)
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


def _version_of(conn: Connection, kind: str, target: str) -> int:
    if kind == "article":
        row = _one(conn, kb_article, kb_article.c.slug == target)
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
    return view


def _current_snapshot(conn: Connection, kind: str, target: str) -> dict[str, Any] | None:
    if kind == "article":
        row = _one(conn, kb_article, kb_article.c.slug == target)
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
            f"{article['topic']} {article['title']}",
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


def _slug(value: str | None) -> str:
    slug = (value or "").strip()
    if not _SLUG.fullmatch(slug) or len(slug) > 80:
        raise ValueError("slug must be lowercase words separated by hyphens")
    return slug


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
