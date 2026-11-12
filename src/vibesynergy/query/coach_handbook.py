"""The coach handbook: the provider-neutral protocol plus the live knowledge-base index.

Any AI that coaches over MCP reads this first (``vibesynergy_coach_handbook``). The protocol
text is ``docs/COACH_PROTOCOL.md``; the index lists active articles and the active rules
in them, so a coach knows which slugs to read and which rule ids to cite in ``kb_rules``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.store.schema import kb_article, kb_rule, kb_source

PROTOCOL_DOC = "docs/COACH_PROTOCOL.md"
# Enough for the whole coach loop; diary.write and sync are optional extras, and publish is
# only needed to set the public insight line of an activity.
COACH_SCOPES_NEEDED: tuple[str, ...] = ("read", "coach.write", "plan.propose", "plan.write")
COACH_SCOPES_OPTIONAL: tuple[str, ...] = ("diary.write", "sync", "publish")
START_HERE: tuple[str, ...] = (
    "vibesynergy_coach_handbook",
    "vibesynergy_sync_status",
    "vibesynergy_coach_state",
    "vibesynergy_kb_get",
)


def _protocol_path() -> Path | None:
    """The handbook file of this checkout (the service runs from an editable install)."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / PROTOCOL_DOC
        if candidate.is_file():
            return candidate
    return None


def protocol_text() -> str | None:
    path = _protocol_path()
    return path.read_text(encoding="utf-8") if path else None


# What an article is and who keeps it. Routines (the prompts and the coach loop) are
# the athlete's: changed in the portal or with their approval. Evidence and methods the coach
# keeps up, with source corrections proposed (vibesynergy_kb_propose_change), not applied.
ROUTINE_TOPICS = frozenset({"coach-prompts", "coach-loop"})


def article_kind(slug: str, topic: str) -> tuple[str, str]:
    """(kind, owner) of one article: routine/athlete, evidence/coach or method/coach."""
    if topic in ROUTINE_TOPICS:
        return "routine", "athlete"
    if slug.startswith("evidence-"):
        return "evidence", "coach"
    return "method", "coach"


def knowledge_index(engine: Engine) -> dict[str, Any]:
    """Active articles and the active rules in them, in a stable order."""
    with engine.connect() as conn:
        articles = (
            conn.execute(
                select(kb_article)
                .where(kb_article.c.status == "active")
                .order_by(kb_article.c.topic, kb_article.c.slug)
            )
            .mappings()
            .all()
        )
        slug_of = {row["id"]: row["slug"] for row in articles}
        sources = {
            article_id: (total, verified or 0)
            for article_id, total, verified in conn.execute(
                select(
                    kb_source.c.article_id,
                    func.count(),
                    func.sum(kb_source.c.verified_at.is_not(None)),
                ).group_by(kb_source.c.article_id)
            )
        }
        rules = (
            conn.execute(
                select(kb_rule)
                .where(kb_rule.c.active.is_(True), kb_rule.c.article_id.in_(list(slug_of)))
                .order_by(kb_rule.c.id)
            )
            .mappings()
            .all()
            if slug_of
            else []
        )
    return {
        "articles": [
            {
                "slug": row["slug"],
                "title": row["title"],
                "topic": row["topic"],
                "evidence_grade": row["evidence_grade"],
                "version": row["version"],
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                **dict(
                    zip(("kind", "owner"), article_kind(row["slug"], row["topic"]), strict=True)
                ),
                "sources": {
                    "total": sources.get(row["id"], (0, 0))[0],
                    "verified": sources.get(row["id"], (0, 0))[1],
                },
            }
            for row in articles
        ],
        "rule_ids": [row["id"] for row in rules],
        "rules": [
            {
                "id": row["id"],
                "article": slug_of[row["article_id"]],
                "grade": row["grade"],
                "version": row["version"],
            }
            for row in rules
        ],
    }


def _tasks(engine: Engine, timezone: str | None) -> list[dict[str, Any]]:
    """The task register; with a time zone also last runs and missing runs."""
    from dataclasses import asdict

    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.query import coach_tasks

    if timezone is None:
        return [
            {**asdict(t), "due": t.due.strftime("%H:%M") if t.due else None}
            for t in coach_tasks.TASKS
        ]
    return coach_tasks.status(engine, utc_now(), timezone)


def handbook(
    engine: Engine,
    *,
    scopes: frozenset[str] | set[str],
    include_protocol: bool = True,
    timezone: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """The handbook for one connection, and warnings for the envelope."""
    warnings: list[str] = []
    text = protocol_text()
    if text is None:
        warnings.append(f"{PROTOCOL_DOC} is not part of this install; read it in the repository")
    missing = [scope for scope in COACH_SCOPES_NEEDED if scope not in scopes]
    if missing:
        warnings.append("this connection lacks scopes the coach loop needs: " + ", ".join(missing))
    data: dict[str, Any] = {
        "protocol_version": (
            hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text is not None else None
        ),
        "start_here": list(START_HERE),
        "knowledge": knowledge_index(engine),
        "tasks": _tasks(engine, timezone),
        "access": {
            "scopes": sorted(scopes),
            "needed": list(COACH_SCOPES_NEEDED),
            "optional": list(COACH_SCOPES_OPTIONAL),
            "missing": missing,
        },
    }
    if include_protocol:
        data["protocol_md"] = text
    return data, warnings
