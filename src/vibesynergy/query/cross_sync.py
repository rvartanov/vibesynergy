"""Status of sync between sources, for Settings → Sync.

Two flows write from one service to another: activity publication (the VibeSynergy section
on Strava, copied to Garmin) and the Garmin → CalDAV calendar copy. This reads only
local ledgers and the job queue: no provider call, no titles, descriptions or hrefs.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from vibesynergy.store.schema import calendar_mirror, job, publication_ledger, publication_mirror


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else value


def _last_job(conn, kind: str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(job.c.status, job.c.finished_at, job.c.last_error)
            .where(job.c.kind == kind, job.c.finished_at.is_not(None))
            .order_by(job.c.finished_at.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        "status": row["status"],
        "finished_at": _iso(row["finished_at"]),
        "error": (row["last_error"] or "")[:300] or None,
    }


def overview(engine) -> dict[str, Any]:
    """Last publication per destination, mirror watches, and calendar copy counts."""
    with engine.connect() as conn:
        published = {}
        for destination in ("strava", "garmin"):
            row = conn.execute(
                select(func.count(), func.max(publication_ledger.c.updated_at)).where(
                    publication_ledger.c.source == destination,
                    publication_ledger.c.state == "verified",
                )
            ).one()
            published[destination] = {"count": row[0], "last_at": _iso(row[1])}
        pending = conn.scalar(
            select(func.count()).where(
                publication_ledger.c.state.in_(("pending", "removing", "uncertain"))
            )
        )
        watches = dict(
            conn.execute(
                select(publication_mirror.c.state, func.count()).group_by(
                    publication_mirror.c.state
                )
            ).all()
        )
        problem = conn.scalar(
            select(publication_mirror.c.last_error)
            .where(
                publication_mirror.c.state.in_(("active", "conflict")),
                publication_mirror.c.last_error.is_not(None),
            )
            .order_by(publication_mirror.c.updated_at.desc())
            .limit(1)
        )
        copies = dict(
            conn.execute(
                select(calendar_mirror.c.state, func.count()).group_by(calendar_mirror.c.state)
            ).all()
        )
        return {
            "publication": {
                **published,
                "uncertain": pending or 0,
                "watching": watches.get("active", 0),
                "conflicts": watches.get("conflict", 0),
                "problem": (problem or "")[:300] or None,
                "last_check": _last_job(conn, "publication_mirror"),
            },
            "calendar": {
                "copies": copies.get("verified", 0) + copies.get("held", 0),
                "conflicts": sum(copies.get(k, 0) for k in ("conflict", "uncertain", "reserved")),
                "last_run": _last_job(conn, "calendar_mirror"),
            },
        }
