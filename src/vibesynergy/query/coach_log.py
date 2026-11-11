"""Change log of what steers the coach: knowledge, rules, sources and the coach's memory.

Read-only over the version tables that already exist (``kb_*_revision`` and
``coach_revision``): who, what and when per version, a readable diff against the version
before, and a revert that copies an older version forward. Nothing is ever deleted.
"""

from __future__ import annotations

import difflib
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from vibesynergy.query import coach, knowledge
from vibesynergy.store.schema import (
    coach_head,
    coach_revision,
    kb_article,
    kb_article_revision,
    kb_rule,
    kb_rule_revision,
    kb_source,
    kb_source_revision,
)

KINDS = ("article", "rule", "source", "policy", "context")
MEMORY_KINDS = ("policy", "context")
_TEXT_FIELDS = {
    "article": ("title", "body_md"),
    "rule": ("text",),
    "source": ("citation",),
    "policy": ("instructions",),
    "context": ("constraints",),
}
_VALUE_FIELDS = {
    "article": ("topic", "evidence_grade", "status"),
    "rule": ("grade", "active", "article"),
    "source": ("doi", "url", "article"),
    "policy": ("language", "detail", "tone"),
    "context": (),
}
_CONTEXT_LINES = 2


def entries(
    engine: Engine,
    *,
    limit: int = 50,
    before: str | None = None,
    kind: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Newest first across every versioned table. ``before`` is the ``next`` of a page.

    ``kind`` narrows to one table; ``memory`` means both coach documents (policy, context).
    """
    limit = min(max(int(limit), 1), 200)
    if kind == "memory":
        kinds: tuple[str, ...] = MEMORY_KINDS
    else:
        kinds = (_kind(kind),) if kind else KINDS
    cutoff = _cursor(before)
    rows: list[dict[str, Any]] = []
    with engine.connect() as conn:
        latest = _latest_versions(conn)
        slugs = _article_slugs(conn)
        for name in kinds:
            rows.extend(_rows(conn, name, target, cutoff, limit + 1, slugs))
        rows.sort(key=lambda row: (row["_at"], row["version"]), reverse=True)
        page = rows[:limit]
        out = []
        for row in page:
            previous = _snapshot(conn, row["kind"], row["target"], row["version"] - 1, slugs)
            current = _snapshot(conn, row["kind"], row["target"], row["version"], slugs)
            out.append(
                {
                    "kind": row["kind"],
                    "target": row["target"],
                    "version": row["version"],
                    "author": row["author"],
                    "at": row["at"],
                    "title": row["title"],
                    "article_slug": row.get("article_slug"),
                    "latest": latest.get((row["kind"], row["target"])) == row["version"],
                    "fields": _changed(row["kind"], previous, current),
                }
            )
    more = len(rows) > limit
    return {"entries": out, "next": page[-1]["at"] if more and page else None}


def diff(engine: Engine, kind: str, target: str, version: int) -> dict[str, Any]:
    """What one version changed against the version before it, field by field."""
    kind = _kind(kind)
    version = _version(version)
    with engine.connect() as conn:
        slugs = _article_slugs(conn)
        current = _snapshot(conn, kind, target, version, slugs)
        if current is None:
            raise LookupError(f"no version {version} of {kind} {target}")
        previous = _snapshot(conn, kind, target, version - 1, slugs)
        latest = _latest_versions(conn).get((kind, target))
    changes = []
    for field in _TEXT_FIELDS[kind]:
        before = _text(previous, field)
        after = _text(current, field)
        if previous is not None and before == after:
            continue
        changes.append({"field": field, "type": "text", "lines": _lines(before, after)})
    for field in _VALUE_FIELDS[kind]:
        before = None if previous is None else previous.get(field)
        after = current.get(field)
        if previous is not None and before == after:
            continue
        changes.append({"field": field, "type": "value", "before": before, "after": after})
    return {
        "kind": kind,
        "target": target,
        "version": version,
        "previous_version": version - 1 if previous is not None else None,
        "latest": latest == version,
        "author": current["_author"],
        "at": current["_at"],
        "changes": changes,
    }


def revert(engine: Engine, kind: str, target: str, version: int, *, actor: str) -> dict[str, Any]:
    """Copy ``version`` forward as the newest version. Returns the new latest version."""
    kind = _kind(kind)
    version = _version(version)
    if kind == "article":
        saved = knowledge.upsert_article(engine, slug=target, author=actor, revert_to=version)
        new_version = saved["version"]
    elif kind == "rule":
        new_version = knowledge.revert_rule(engine, target, version, author=actor)["rule"][
            "version"
        ]
    elif kind == "source":
        new_version = knowledge.revert_source(engine, target, version, author=actor)["version"]
    else:
        if target != kind:
            raise LookupError(f"no {kind} {target}")
        with engine.connect() as conn:
            if _coach_row(conn, kind, version) is None:
                raise LookupError(f"no version {version} of {kind}")
        head = coach.state(engine, kind)
        drafted = coach.restore_draft(
            engine, kind, version, expected_version=head["version"], actor=actor
        )
        activated = coach.activate(engine, kind, expected_version=drafted["version"], actor=actor)
        new_version = activated["active_version"]
    return {"kind": kind, "target": target, "version": new_version, "restored": version}


# Reading the version tables ----------------------------------------------------------------


def _rows(conn: Connection, kind, target, cutoff, limit, slugs) -> list[dict[str, Any]]:
    if kind == "article":
        table = kb_article_revision
        query = select(table)
        if target:
            query = query.where(table.c.slug == target)
    elif kind == "rule":
        table = kb_rule_revision
        query = select(table)
        if target:
            query = query.where(table.c.rule_id == target)
    elif kind == "source":
        table = kb_source_revision
        query = select(table)
        if target:
            query = query.where(table.c.source_id == target)
    else:
        if target and target != kind:
            return []
        table = coach_revision
        query = select(table).where(table.c.subject_id == 1, table.c.kind == kind)
    if cutoff is not None:
        query = query.where(table.c.created_at < cutoff)
    query = query.order_by(table.c.created_at.desc(), table.c.version.desc()).limit(limit)
    out = []
    for row in conn.execute(query).mappings():
        snap = _from_row(kind, row, slugs)
        out.append(
            {
                "kind": kind,
                "target": snap["_target"],
                "version": row["version"],
                "author": snap["_author"],
                "at": snap["_at"],
                "_at": row["created_at"],
                "title": snap["_title"],
                "article_slug": snap.get("article"),
            }
        )
    return out


def _snapshot(conn: Connection, kind, target, version, slugs) -> dict[str, Any] | None:
    if version < 1:
        return None
    if kind == "article":
        article = conn.execute(select(kb_article.c.id).where(kb_article.c.slug == target)).scalar()
        if article is None:
            return None
        row = _one(conn, kb_article_revision, "article_id", article, version)
    elif kind == "rule":
        row = _one(conn, kb_rule_revision, "rule_id", target, version)
    elif kind == "source":
        row = _one(conn, kb_source_revision, "source_id", target, version)
    else:
        if target != kind:
            return None
        row = _coach_row(conn, kind, version)
    return None if row is None else _from_row(kind, row, slugs)


def _from_row(kind: str, row, slugs: dict[str, str]) -> dict[str, Any]:
    at = knowledge._iso(row["created_at"])
    if kind == "article":
        return {
            "_target": row["slug"],
            "_title": row["title"],
            "_author": row["author"],
            "_at": at,
            "title": row["title"],
            "body_md": row["body_md"],
            "topic": row["topic"],
            "evidence_grade": row["evidence_grade"],
            "status": row["status"],
        }
    if kind == "rule":
        return {
            "_target": row["rule_id"],
            "_title": row["rule_id"],
            "_author": row["author"],
            "_at": at,
            "text": row["text"],
            "grade": row["grade"],
            "active": bool(row["active"]),
            "article": slugs.get(row["article_id"]),
        }
    if kind == "source":
        return {
            "_target": row["source_id"],
            "_title": row["citation"],
            "_author": row["author"],
            "_at": at,
            "citation": row["citation"],
            "doi": row["doi"],
            "url": row["url"],
            "article": slugs.get(row["article_id"]),
        }
    payload = row["payload"] if isinstance(row["payload"], dict) else {}
    snap: dict[str, Any] = {"_target": kind, "_title": None, "_author": row["actor"], "_at": at}
    if kind == "policy":
        for key in ("instructions", "language", "detail", "tone"):
            snap[key] = payload.get(key)
    else:
        snap["constraints"] = "\n".join(
            str(item.get("text") or "").replace("\n", " ")
            for item in payload.get("constraints") or []
            if isinstance(item, dict) and item.get("active", True)
        )
    return snap


def _changed(kind: str, previous, current) -> list[str]:
    if current is None:
        return []
    fields = _TEXT_FIELDS[kind] + _VALUE_FIELDS[kind]
    if previous is None:
        return ["created"]
    return [field for field in fields if previous.get(field) != current.get(field)]


def _latest_versions(conn: Connection) -> dict[tuple[str, str], int]:
    latest: dict[tuple[str, str], int] = {}
    for row in conn.execute(select(kb_article.c.slug, kb_article.c.version)):
        latest[("article", row.slug)] = row.version
    for row in conn.execute(select(kb_rule.c.id, kb_rule.c.version)):
        latest[("rule", row.id)] = row.version
    for row in conn.execute(select(kb_source.c.id, kb_source.c.version)):
        latest[("source", row.id)] = row.version
    for row in conn.execute(
        select(coach_head.c.kind, coach_head.c.version).where(coach_head.c.subject_id == 1)
    ):
        latest[(row.kind, row.kind)] = row.version
    return latest


def _article_slugs(conn: Connection) -> dict[str, str]:
    return {row.id: row.slug for row in conn.execute(select(kb_article.c.id, kb_article.c.slug))}


def _one(conn: Connection, table, column: str, key, version: int):
    return (
        conn.execute(select(table).where(table.c[column] == key, table.c.version == version))
        .mappings()
        .first()
    )


def _coach_row(conn: Connection, kind: str, version: int):
    return (
        conn.execute(
            select(coach_revision).where(
                coach_revision.c.subject_id == 1,
                coach_revision.c.kind == kind,
                coach_revision.c.version == version,
            )
        )
        .mappings()
        .first()
    )


def _text(snap: dict[str, Any] | None, field: str) -> str:
    if snap is None:
        return ""
    value = snap.get(field)
    return "" if value is None else str(value)


def _lines(before: str, after: str) -> list[dict[str, str]]:
    """A line diff with a little context around each change; long unchanged runs collapse."""
    old, new = before.splitlines(), after.splitlines()
    out: list[dict[str, str]] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            same = old[i1:i2]
            if len(same) > 2 * _CONTEXT_LINES + 1:
                head = same[:_CONTEXT_LINES] if i1 > 0 else []
                tail = same[-_CONTEXT_LINES:] if i2 < len(old) else []
                out.extend({"op": "same", "text": line} for line in head)
                out.append({"op": "gap", "text": str(len(same) - len(head) - len(tail))})
                out.extend({"op": "same", "text": line} for line in tail)
            else:
                out.extend({"op": "same", "text": line} for line in same)
            continue
        out.extend({"op": "del", "text": line} for line in old[i1:i2])
        out.extend({"op": "add", "text": line} for line in new[j1:j2])
    return out


def _cursor(before: str | None) -> datetime | None:
    if not before:
        return None
    parsed = datetime.fromisoformat(before)
    if parsed.tzinfo is None:
        raise ValueError("before must include a timezone")
    return parsed


def _kind(kind: str | None) -> str:
    if kind not in KINDS:
        raise ValueError("kind must be article, rule, source, policy or context")
    return kind


def _version(version: int) -> int:
    if type(version) is not int or version < 1:
        raise ValueError("version must be a version number")
    return version
