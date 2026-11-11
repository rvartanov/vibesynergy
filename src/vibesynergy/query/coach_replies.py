"""The athlete's replies to coach reports (Coach page), read by the next coach run.

A reply belongs to one coach entry. It saves as typed (no reason field) and can be changed
until the coach has read it; the next API coach run gets every unread reply in its start
message and marks them read with its run id once it finished. Each add and change is in the
audit log (who, what, when: field names and lengths, never the text).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Engine, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import audit_log, coach_decision_log, coach_reply

MAX_CHARS = 4000
AUTHOR = "user"


class ReplyError(ValueError):
    """A reply was not saved; plain English for the page to translate."""


def _clean(text: str) -> str:
    value = (text or "").strip()
    if not value:
        raise ReplyError("Write a reply first.")
    if len(value) > MAX_CHARS:
        raise ReplyError("Keep a reply within 4,000 characters.")
    return value


def _row(row) -> dict[str, Any]:
    def iso(value):
        return value.isoformat() if value else None

    return {
        "id": row["id"],
        "entry_id": row["entry_id"],
        "at": iso(row["at"]),
        "updated_at": iso(row["updated_at"]),
        "author": row["author"],
        "text": row["text"],
        "read": row["read_at"] is not None,
        "read_at": iso(row["read_at"]),
    }


def _audit(conn, action: str, reply_id: int, entry_id: int, chars: int, actor: str) -> None:
    conn.execute(
        audit_log.insert().values(
            at=utc_now(),
            actor=actor,
            action=action,
            detail={"entry_id": entry_id, "chars": chars},
            target=f"coach_reply:{reply_id}",
            outcome="ok",
        )
    )


def add(engine: Engine, entry_id: int, text: str, *, actor: str = "portal") -> dict[str, Any]:
    value = _clean(text)
    now = utc_now()
    with engine.begin() as conn:
        exists = conn.scalar(
            select(coach_decision_log.c.id).where(coach_decision_log.c.id == entry_id)
        )
        if exists is None:
            raise LookupError("no such coach entry")
        reply_id = conn.execute(
            coach_reply.insert()
            .values(entry_id=entry_id, at=now, updated_at=now, author=AUTHOR, text=value)
            .returning(coach_reply.c.id)
        ).scalar_one()
        _audit(conn, "coach.reply.add", reply_id, entry_id, len(value), actor)
        row = conn.execute(select(coach_reply).where(coach_reply.c.id == reply_id)).mappings()
        return _row(row.one())


def edit(engine: Engine, reply_id: int, text: str, *, actor: str = "portal") -> dict[str, Any]:
    value = _clean(text)
    with engine.begin() as conn:
        row = (
            conn.execute(select(coach_reply).where(coach_reply.c.id == reply_id)).mappings().first()
        )
        if row is None:
            raise LookupError("no such reply")
        if row["read_at"] is not None:
            raise ReplyError("Your coach has already read this reply. Add a new one instead.")
        conn.execute(
            update(coach_reply)
            .where(coach_reply.c.id == reply_id)
            .values(text=value, updated_at=utc_now())
        )
        _audit(conn, "coach.reply.edit", reply_id, row["entry_id"], len(value), actor)
        fresh = conn.execute(select(coach_reply).where(coach_reply.c.id == reply_id)).mappings()
        return _row(fresh.one())


def for_entries(engine: Engine, entry_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    ids = sorted({int(i) for i in entry_ids})
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    if not ids:
        return out
    with engine.connect() as conn:
        for row in conn.execute(
            select(coach_reply)
            .where(coach_reply.c.entry_id.in_(ids))
            .order_by(coach_reply.c.at, coach_reply.c.id)
        ).mappings():
            out[row["entry_id"]].append(_row(row))
    return out


def unread(engine: Engine, *, limit: int = 30) -> list[dict[str, Any]]:
    """Unread replies, oldest first, with the entry they answer (type, date, first line)."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                coach_reply,
                coach_decision_log.c.entry_type,
                coach_decision_log.c.for_date,
                coach_decision_log.c.text.label("entry_text"),
            )
            .join(coach_decision_log, coach_decision_log.c.id == coach_reply.c.entry_id)
            .where(coach_reply.c.read_at.is_(None))
            .order_by(coach_reply.c.at, coach_reply.c.id)
            .limit(limit)
        ).mappings()
        out = []
        for row in rows:
            item = _row(row)
            item["entry_type"] = row["entry_type"]
            item["for_date"] = row["for_date"].isoformat() if row["for_date"] else None
            item["entry_first_line"] = (row["entry_text"] or "").split("\n", 1)[0][:200]
            out.append(item)
        return out


def mark_read(engine: Engine, reply_ids: Iterable[int], run_id: int) -> int:
    ids = [int(i) for i in reply_ids]
    if not ids:
        return 0
    with engine.begin() as conn:
        return conn.execute(
            update(coach_reply)
            .where(coach_reply.c.id.in_(ids), coach_reply.c.read_at.is_(None))
            .values(read_at=utc_now(), read_run_id=run_id)
        ).rowcount
