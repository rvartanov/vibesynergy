"""Status of publishing and sync, for Settings → Publishing and sync.

Two flows write from one service to another: publishing a workout (VibeSynergy's section
on Strava and/or Garmin, kept in step by the text sync) and the Garmin → CalDAV calendar
copy. This reads only local ledgers and the job queue: no provider call, no titles,
descriptions or hrefs.

Per platform (#175): how many workouts are published there, when VibeSynergy last wrote or
checked there, and the workouts that need a look (an unfinished write or removal, a text
sync stopped for review, or an automatic publication left for review), with the first one
so Settings can link to it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from vibesynergy.store.schema import (
    activity_link,
    calendar_mirror,
    job,
    publication_ledger,
    text_sync,
)

PLATFORMS = ("strava", "garmin")


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


def _later(a: Any, b: Any) -> Any:
    return b if a is None or (b is not None and b > a) else a


def overview(engine, auto_attempts: dict[str, dict] | None = None) -> dict[str, Any]:
    """Per platform: published workouts, last synced and what needs a look; calendar copies.

    ``auto_attempts``: the automatic run's attempts (``publication_auto.attempts``), keyed by
    the Strava id or ``garmin:<id>``."""
    with engine.connect() as conn:
        groups = {
            (s, i): g
            for s, i, g in conn.execute(
                select(
                    activity_link.c.source, activity_link.c.source_id, activity_link.c.group_id
                ).where(activity_link.c.source.in_(PLATFORMS))
            )
        }
        published: dict[str, dict[str, Any]] = {}
        review: dict[str, list[str]] = {p: [] for p in PLATFORMS}
        for destination in PLATFORMS:
            row = conn.execute(
                select(func.count(), func.max(publication_ledger.c.updated_at)).where(
                    publication_ledger.c.source == destination,
                    publication_ledger.c.state == "verified",
                )
            ).one()
            checked = conn.scalar(
                select(func.max(text_sync.c.last_checked_at)).where(
                    getattr(text_sync.c, f"{destination}_id").is_not(None)
                )
            )
            published[destination] = {"count": row[0], "last_at": _iso(_later(row[1], checked))}
        unfinished = conn.execute(
            select(publication_ledger.c.source, publication_ledger.c.source_id)
            .where(publication_ledger.c.state.in_(("pending", "removing", "uncertain")))
            .order_by(publication_ledger.c.updated_at.desc())
        ).all()
        for source, source_id in unfinished:
            if source in review:
                review[source].append(groups.get((source, source_id)) or f"{source}:{source_id}")
        stopped = conn.execute(
            select(text_sync.c.group_id, text_sync.c.last_error, text_sync.c.strava_id)
            .where(text_sync.c.state == "conflict")
            .order_by(text_sync.c.updated_at.desc())
        ).all()
        for group, error, strava_id in stopped:
            named = [p for p in PLATFORMS if p in (error or "").lower()]
            for platform in named or (["strava"] if strava_id else ["garmin"]):
                review[platform].append(group)
        for key, entry in (auto_attempts or {}).items():
            if key.startswith("insight:") or entry.get("state") not in ("needs_review", "gave_up"):
                continue
            platform, _, source_id = key.rpartition(":") if ":" in key else ("strava", "", key)
            if platform in review:
                found = groups.get((platform, source_id))
                review[platform].append(found or f"{platform}:{source_id}")
        for platform in PLATFORMS:
            seen = list(dict.fromkeys(review[platform]))
            published[platform]["needs_review"] = len(seen)
            published[platform]["review_activity"] = seen[0] if seen else None
        copies = dict(
            conn.execute(
                select(calendar_mirror.c.state, func.count()).group_by(calendar_mirror.c.state)
            ).all()
        )
        return {
            "publication": {**published, "uncertain": len(unfinished)},
            "calendar": {
                "copies": copies.get("verified", 0) + copies.get("held", 0),
                "conflicts": sum(copies.get(k, 0) for k in ("conflict", "uncertain", "reserved")),
                "last_run": _last_job(conn, "calendar_mirror"),
            },
        }
