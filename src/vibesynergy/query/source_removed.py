"""Copies removed at their source (#151).

A Garmin copy of an activity can disappear on Garmin (deleted by hand, or Garmin's own
dedupe of a Zwift upload next to the watch recording). VibeSynergy keeps the ``activity``
row and its raw history, and records the removal in ``source_removed``. A removed copy
leaves the group's *live* sources: the copy choice of #146 (``publication_format._choose``),
the publication target and the text sync then use a copy that still exists.

Found in two ways:

- a Garmin read or write answers 404, and one re-check answers 404 again
  (``publication.GarminProvider``); publishing to the other targets then tries the copy
  that is left in the same call;
- the light check after a Garmin sync (``check_recent``): a recent activity that VS has but
  that the fresh Garmin activity list no longer holds is asked for once; 404 marks it.

A copy that appears in the Garmin list again loses its mark.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.store.schema import activity, source_removed

log = logging.getLogger(__name__)

# The sync check looks this far back, and asks Garmin about at most this many copies.
RECENT = timedelta(days=14)
MAX_CHECKS = 5


def _split(ref: str) -> tuple[str, str] | None:
    source, sep, source_id = str(ref).partition(":")
    return (source, source_id) if sep else None


def removed(conn, refs: Iterable[str]) -> dict[str, datetime]:
    """``{"garmin:123": removed_at}`` for the refs that were removed at their source."""
    pairs = {p for p in (_split(r) for r in refs) if p}
    if not pairs:
        return {}
    ids = {source_id for _, source_id in pairs}
    rows = conn.execute(
        select(
            source_removed.c.source, source_removed.c.source_id, source_removed.c.removed_at
        ).where(source_removed.c.source_id.in_(ids))
    ).all()
    return {f"{s}:{i}": at for s, i, at in rows if (s, i) in pairs}


def live(conn, refs: Sequence[str] | None) -> list[str]:
    """``refs`` without the copies removed at their source, in the same order."""
    refs = list(refs or [])
    gone = removed(conn, refs)
    return [r for r in refs if r not in gone]


def mark(engine, source: str, source_id: str, *, reason: str, now: datetime | None = None) -> None:
    """Record the removal once; a later mark keeps the first time and reason."""
    stmt = insert(source_removed).values(
        source=source,
        source_id=str(source_id),
        removed_at=now or datetime.now(UTC),
        reason=reason,
    )
    with engine.begin() as conn:
        conn.execute(stmt.on_conflict_do_nothing(index_elements=["source", "source_id"]))
    log.info("%s:%s removed at the source (%s)", source, source_id, reason)


def clear(engine, source: str, source_ids: Iterable[str]) -> int:
    """The copies are there again (listed by the source): drop their marks."""
    ids = [str(i) for i in source_ids]
    if not ids:
        return 0
    with engine.begin() as conn:
        return conn.execute(
            delete(source_removed).where(
                source_removed.c.source == source, source_removed.c.source_id.in_(ids)
            )
        ).rowcount


def check_recent(engine, source, results, *, today: date) -> dict:
    """After a Garmin sync: find recent copies Garmin no longer has (light, bounded).

    Uses the activity list the sync just fetched, so it costs no extra list call; only a
    copy missing from that list is asked for, once, and only 404 marks it."""
    from garminconnect import GarminConnectNotFoundError

    summary = {"checked": 0, "removed": 0, "restored": 0}
    for result in results:
        if result.dataset != "activities" or result.error or result.window is None:
            continue
        listed = source.listed(result.window)
        if listed is None:
            continue
        start = max(result.window.start, today - RECENT)
        if start > result.window.end:
            continue
        with engine.connect() as conn:
            known = set(
                conn.execute(
                    select(activity.c.source_id).where(
                        activity.c.source == "garmin",
                        activity.c.local_date >= start,
                        activity.c.local_date <= result.window.end,
                    )
                ).scalars()
            )
            marked = set(
                conn.execute(
                    select(source_removed.c.source_id).where(source_removed.c.source == "garmin")
                ).scalars()
            )
        summary["restored"] += clear(engine, "garmin", sorted(marked & listed))
        for source_id in sorted(known - listed - marked)[:MAX_CHECKS]:
            summary["checked"] += 1
            try:
                source.api.get_activity(source_id)
            except GarminConnectNotFoundError:
                mark(engine, "garmin", source_id, reason="sync")
                summary["removed"] += 1
            except Exception as exc:  # network or rate limit: the next sync looks again
                log.warning("Garmin removal check for %s failed: %s", source_id, exc)
                break
    return summary
