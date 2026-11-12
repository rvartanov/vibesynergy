"""The coach's public insight line for an activity's VibeSynergy block.

The coach (Claude over MCP) writes an ``activity_assessment`` entry with
``vibesynergy_coach_write``. Only a sentence the coach marked as public is ever published:

- ``public_insight`` with ``activity_id`` on the entry (``meta.public_insight`` and
  ``meta.activity_id``, the group id). An empty ``public_insight`` withdraws the line.
- The earlier convention: ``details`` starting with ``public_insight`` and every activity in
  ``evidence_refs`` being this activity; the entry's summary (first line) is the insight.

Per activity the newest such entry (by ``at``) wins. The summary, caveat, details and other
coach, diary or health text are never published. The line is one paragraph of at most
``MAX_CHARS`` characters without block markers; anything else is left out, not shortened.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select

from vibesynergy.query import views
from vibesynergy.store.schema import activity, activity_unified, coach_decision_log

ENTRY_TYPE = "activity_assessment"
MAX_CHARS = 300
MARKER = re.compile(r"^\s*public_insight\b")
# Brackets and comment tokens could look like the block's own markers.
_FORBIDDEN = re.compile(r"[\[\]]|/\*|\*/")
# Words that mean private health, diary or injury content in a public line (INS-2).
PRIVATE = re.compile(
    r"\b(hrv|readiness|slaap|sleep|rusthartslag|resting heart|rustpols|gewicht|weight|"
    r"bloeddruk|blood pressure|blessure|injur\w*|pijn|pain|ziek\w*|sick\w*|ill(ness)?|"
    r"dagboek|diary|check-?in)\b",
    re.IGNORECASE,
)
# A public line is about the session; it never carries a link.
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
# Entries written this long before the activity day cannot be about it.
_EARLIEST = timedelta(days=1)


class InsightError(ValueError):
    """The public insight was not saved."""


def normalise(text: str | None) -> str | None:
    """One line, single spaces; None when empty or not publishable."""
    if not isinstance(text, str):
        return None
    line = " ".join(text.split())
    if not line or len(line) > MAX_CHARS or _FORBIDDEN.search(line):
        return None
    if PRIVATE.search(line) or _LINK.search(line):
        return None
    return line


def validate(text: str) -> str:
    """The stored form of a public insight; '' withdraws it."""
    if not isinstance(text, str):
        raise InsightError("public_insight must be text")
    if not text.strip():
        return ""
    line = " ".join(text.split())
    if len(line) > MAX_CHARS:
        raise InsightError(f"public_insight must be at most {MAX_CHARS} characters")
    if _FORBIDDEN.search(line):
        raise InsightError("public_insight must not contain [, ], /* or */")
    if PRIVATE.search(line):
        raise InsightError(
            "public_insight mentions private health, diary or injury details (INS-2). "
            "Rewrite it about the session only, or leave the insight out."
        )
    if _LINK.search(line):
        raise InsightError("public_insight must not contain a link")
    return line


def _resolves_to(engine, ref: Any, group: str) -> bool | None:
    """True/False for an activity ref of this/another activity; None for other refs."""
    if not isinstance(ref, str) or ":" not in ref:
        return None
    try:
        return views.resolve_activity(engine, ref) == group
    except ValueError:
        return None


def _from_entry(engine, row, group: str) -> tuple[bool, str | None]:
    """(about this activity, insight or None) for one coach entry."""
    meta = row["meta"] if isinstance(row["meta"], dict) else {}
    if "public_insight" in meta and meta.get("activity_id"):
        # The group id can change after writing: a Strava-only activity becomes the
        # Garmin group once Garmin's copy arrives (1 Nov). Resolve the stored id again.
        if meta["activity_id"] != group and not _resolves_to(engine, meta["activity_id"], group):
            return False, None
        return True, normalise(meta.get("public_insight"))
    details = meta.get("details")
    if isinstance(details, str) and MARKER.match(details):
        refs = meta.get("evidence_refs") or []
        found = [_resolves_to(engine, ref, group) for ref in refs]
        activities = [hit for hit in found if hit is not None]
        if activities and all(activities):
            summary = (row["text"] or "").split("\n", 1)[0]
            return True, normalise(summary)
    return False, None


def _group_day(engine, group: str) -> date | None:
    with engine.connect() as conn:
        return conn.scalar(
            select(activity_unified.c.local_date).where(activity_unified.c.group_id == group)
        )


def for_activity(engine, activity_id: str, *, subject_id: int = 1) -> dict[str, Any] | None:
    """The current public insight: ``{"text", "entry_id", "at"}`` or None."""
    group = views.resolve_activity(engine, activity_id)
    day = _group_day(engine, group)
    if day is None:
        return None
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    coach_decision_log.c.id,
                    coach_decision_log.c.at,
                    coach_decision_log.c.text,
                    coach_decision_log.c.meta,
                )
                .where(
                    coach_decision_log.c.subject_id == subject_id,
                    coach_decision_log.c.entry_type == ENTRY_TYPE,
                    coach_decision_log.c.for_date >= day - _EARLIEST,
                )
                .order_by(desc(coach_decision_log.c.at), desc(coach_decision_log.c.id))
            )
            .mappings()
            .all()
        )
    for row in rows:
        about, text = _from_entry(engine, row, group)
        if about:
            if text is None:
                return None
            at = row["at"]
            return {
                "text": text,
                "entry_id": row["id"],
                "at": at.isoformat() if isinstance(at, datetime) else at,
            }
    return None


def recent(rt, *, days: int = 2, today: date | None = None) -> dict[str, Any]:
    """Activities of the last ``days`` days with a Strava copy, and whether each has an
    insight, so the evening routine knows what to fill. Newest first."""
    from vibesynergy.core.timeutil import today_in
    from vibesynergy.store.schema import publication_ledger

    if not 1 <= days <= 14:
        raise ValueError("days runs from 1 to 14")
    today = today or today_in(rt.settings.timezone)
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    activity_unified.c.group_id,
                    activity_unified.c.local_date,
                    activity_unified.c.sport,
                    activity_unified.c.name,
                    activity_unified.c.sources,
                )
                .where(activity_unified.c.local_date > today - timedelta(days=days))
                .order_by(desc(activity_unified.c.start_utc))
            )
            .mappings()
            .all()
        )
        ledger = {
            (row.source, row.source_id): row.state
            for row in conn.execute(
                select(
                    publication_ledger.c.source,
                    publication_ledger.c.source_id,
                    publication_ledger.c.state,
                )
            )
        }
        strava_ids = [
            str(ref).split(":", 1)[1]
            for row in rows
            for ref in row["sources"] or []
            if str(ref).startswith("strava:")
        ]
        private = {
            source_id: (extra or {}).get("private")
            for source_id, extra in conn.execute(
                select(activity.c.source_id, activity.c.extra).where(
                    activity.c.source == "strava", activity.c.source_id.in_(strava_ids)
                )
            )
        }
    out = []
    for row in rows:
        refs = [str(ref) for ref in row["sources"] or []]
        strava = [ref.split(":", 1)[1] for ref in refs if ref.startswith("strava:")]
        if len(strava) != 1:
            continue
        found = for_activity(rt.engine, row["group_id"])
        out.append(
            {
                "id": row["group_id"],
                "local_date": row["local_date"].isoformat(),
                "sport": row["sport"],
                "name": row["name"],
                "strava": ledger.get(("strava", strava[0])) or "not_published",
                # False while the athlete keeps it private; None when Strava did not say.
                "strava_public": (
                    None if private.get(strava[0]) is None else private[strava[0]] is False
                ),
                "insight": "present" if found else "missing",
                "insight_entry_id": found["entry_id"] if found else None,
            }
        )
    return {
        "activities": out,
        "missing": [item["id"] for item in out if item["insight"] == "missing"],
    }


def resolve_link(engine, activity_id: str) -> str:
    """The group id a new entry links to; a clear error for an unknown activity."""
    try:
        return views.resolve_activity(engine, activity_id)
    except ValueError as exc:
        raise InsightError(str(exc)) from exc
