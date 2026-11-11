"""The VibeSynergy block on Strava, also in the intervals.icu activity description (1 Nov 2026).

the athlete's decision: the same block (stats, weather, quote and the coach's insight line) that
VibeSynergy publishes on Strava goes into the matching intervals.icu activity too, behind the
same owned-block markers. Whatever else is in that description (the athlete's own text) stays as
it is; a later publication replaces only VibeSynergy's block. Switch: Settings → Sync between
sources → "Also in intervals.icu" (``[publication] icu_description_enabled``, on by default).

When: right after every verified Strava publication or block update (``after_publish``),
and on each automatic-publication run for verified Strava blocks in the last two days that
intervals.icu does not hold yet (``candidates``/``run``), so an activity that reached
intervals.icu later, or a failed write, catches up. The Strava ledger's verified block is
the only text written; never private notes or other coach text.

The matching intervals.icu activity is found as ``activity_effort`` does (Garmin id, Strava
id or start time), first in the archived intervals.icu activities and then in a live list
of the surrounding days. Activities intervals.icu took from Strava are stubs it may not
change (Strava's API terms), so they are skipped. Every write is checkpointed in
``publication_ledger`` (source ``intervals_icu``), read back, and audit-logged
(``publication.icu``, no text). More than one VibeSynergy block or a stray marker is left for
review and not retried.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import activity_effort, evidence, publication_format, views
from vibesynergy.store.schema import activity_unified, audit_log, publication_ledger

log = logging.getLogger(__name__)

SOURCE = "intervals_icu"
LOOKBACK = timedelta(days=2)
RETRY = timedelta(minutes=30)
MAX_TRIES = 5


class Review(ValueError):
    """The intervals.icu description holds VibeSynergy markers that are not one clean block."""


def enabled(rt) -> bool:
    """The switch is on (default) and intervals.icu is connected."""
    from vibesynergy.core.config import load_settings

    pub = load_settings(rt.settings.config_path).publication
    return bool(pub.get("icu_description_enabled", True)) and activity_effort._configured()


def _client(client=None):
    if client is not None:
        return client
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    return IntervalsIcuClient.from_env()


def _group(rt, activity_id: str) -> dict[str, Any]:
    group = views.resolve_activity(rt.engine, activity_id)
    with rt.engine.connect() as conn:
        row = (
            conn.execute(
                select(
                    activity_unified.c.group_id,
                    activity_unified.c.sources,
                    activity_unified.c.start_utc,
                    activity_unified.c.local_date,
                ).where(activity_unified.c.group_id == group)
            )
            .mappings()
            .one()
        )
    return dict(row)


def _strava_ledger(rt, group: dict[str, Any]) -> dict[str, Any] | None:
    strava = [r.split(":", 1)[1] for r in group["sources"] or [] if r.startswith("strava:")]
    if len(strava) != 1:
        return None
    with rt.engine.connect() as conn:
        row = (
            conn.execute(
                select(publication_ledger).where(
                    publication_ledger.c.source == "strava",
                    publication_ledger.c.source_id == strava[0],
                    publication_ledger.c.state == "verified",
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _parse(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict) or not item.get("id"):
        return None
    return {
        "id": str(item["id"]),
        "external_id": str(item.get("external_id") or "") or None,
        "strava_id": str(item.get("strava_id") or "") or None,
        "start": activity_effort._parse_utc(item.get("start_date")),
        "source": str(item.get("source") or "").upper() or None,
    }


def match(rt, group: dict[str, Any], client=None) -> dict[str, Any] | None:
    """The intervals.icu activity of this unified activity: archive first, then live."""
    day = activity_effort._as_date(group["local_date"])
    archived = activity_effort._icu_activities(rt.engine, rt.raw, {day})
    found = activity_effort._match_icu(group, archived)
    if found is None and day is not None:
        live = _client(client).activities(day - timedelta(days=1), day + timedelta(days=1))
        found = activity_effort._match_icu(group, [p for p in map(_parse, live) if p])
    return found


def desired(current: str, owned: str) -> str:
    """The athlete's intervals.icu text with exactly one VibeSynergy block: the old one replaced
    in place, or the new one below the text."""
    blocks = publication_format.find_blocks(current)
    if len(blocks) > 1 or publication_format.orphan_markers(current):
        raise Review("The intervals.icu description has more than one VibeSynergy section")
    if blocks:
        start, end = blocks[0]
        return current[:start] + owned + current[end:]
    text = current.rstrip()
    return f"{text}\n\n{owned}" if text else owned


def _checkpoint(rt, values: dict[str, Any], state: str) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            publication_ledger.delete().where(
                publication_ledger.c.source == SOURCE,
                publication_ledger.c.source_id == values["source_id"],
            )
        )
        conn.execute(
            publication_ledger.insert().values(
                source=SOURCE, state=state, updated_at=datetime.now(UTC), **values
            )
        )


def _audit(rt, outcome: str, target: str, detail: dict[str, Any]) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor="vibesynergy",
                action="publication.icu",
                target=target[:200],
                outcome=outcome,
                detail=detail,
            )
        )


def sync(rt, activity_id: str, *, client=None) -> dict[str, Any]:
    """Put Strava's verified VibeSynergy block into the intervals.icu description, once.

    Returns ``{"state": ...}``: ``updated``, ``unchanged``, ``waiting`` (no matching
    intervals.icu activity yet), ``not_available`` (a Strava stub) or ``no_block``.
    Raises ``Review`` for a description with several blocks; other errors propagate.
    """
    group = _group(rt, activity_id)
    strava = _strava_ledger(rt, group)
    if strava is None:
        return {"state": "no_block"}
    owned = strava["owned_block"]
    client = _client(client)
    found = match(rt, group, client)
    if found is None:
        return {"state": "waiting"}
    if found.get("source") == "STRAVA":
        return {"state": "not_available", "icu_activity_id": found["id"]}
    icu_id = found["id"]
    remote = client.get_activity(icu_id)
    if str(remote.get("id")) != icu_id:
        raise ValueError("intervals.icu activity identity changed")
    if str(remote.get("source") or "").upper() == "STRAVA":
        return {"state": "not_available", "icu_activity_id": icu_id}
    current = remote.get("description") or ""
    if not isinstance(current, str):
        raise ValueError("intervals.icu description is unreadable")
    target = desired(current, owned)
    values = {
        "source_id": icu_id,
        "assessment_id": strava["assessment_id"],
        "preview_version": evidence.digest(
            {"source": SOURCE, "id": icu_id, "base": current, "description": target}
        ),
        "owned_block": owned,
        "base_description": current,
        "description": target,
    }
    if publication_format.same_text(current, target):
        _checkpoint(rt, values, "verified")
        return {"state": "unchanged", "icu_activity_id": icu_id}
    _checkpoint(rt, values, "pending")
    client.update_activity_description(icu_id, target)
    after = client.get_activity(icu_id)
    returned = after.get("description")
    if not publication_format.same_text(returned if isinstance(returned, str) else "", target):
        raise ValueError("intervals.icu did not return the saved description")
    _checkpoint(rt, values, "verified")
    _audit(
        rt,
        "ok",
        icu_id,
        {
            "group_id": group["group_id"],
            "strava_id": strava["source_id"],
            "icu_activity_id": icu_id,
            "replaced_block": bool(publication_format.find_blocks(current)),
            "had_text": bool(current.strip()),
            "insight": _has_insight(owned),
        },
    )
    return {"state": "updated", "icu_activity_id": icu_id}


def _has_insight(owned: str) -> bool:
    return any(
        line.startswith(f"-{labels['insight']}:")
        for line in owned.splitlines()
        for labels in publication_format.LABELS.values()
    )


def after_publish(rt, activity_id: str, *, client=None) -> dict[str, Any]:
    """Called after a verified Strava write; a failure here never undoes it (the next
    automatic run retries)."""
    if not enabled(rt):
        return {"state": "disabled"}
    try:
        return sync(rt, activity_id, client=client)
    except Review as exc:
        _audit(rt, "needs_review", activity_id, {"error": str(exc)})
        return {"state": "needs_review", "error": str(exc)}
    except Exception as exc:
        log.warning("intervals.icu description for %s: %s", activity_id, type(exc).__name__)
        return {"state": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}


def remove(rt, activity_id: str, *, client=None) -> dict[str, Any]:
    """The Strava block was removed: take VibeSynergy's block out of intervals.icu as well,
    keeping the rest of the text. Only a block VibeSynergy wrote there (ledger) is touched."""
    try:
        group = _group(rt, activity_id)
    except (LookupError, ValueError):
        return {"state": "not_published"}
    with rt.engine.connect() as conn:
        written = {
            row.source_id
            for row in conn.execute(
                select(publication_ledger.c.source_id).where(publication_ledger.c.source == SOURCE)
            )
        }
    if not written or (client is None and not activity_effort._configured()):
        return {"state": "not_published"}
    client_ = _client(client)
    found = match(rt, group, client_)
    if found is None or found["id"] not in written:
        return {"state": "not_published"}
    ledger = {"source_id": found["id"]}
    remote = client_.get_activity(ledger["source_id"])
    current = remote.get("description") or ""
    blocks = publication_format.find_blocks(current)
    if len(blocks) > 1:
        return {"state": "needs_review"}
    from vibesynergy.query import publication

    if not blocks:  # already gone by hand: only forget it
        publication._delete_checkpoint(rt, SOURCE, ledger["source_id"])
        return {"state": "removed"}

    target = publication._without_owned(current)
    client_.update_activity_description(ledger["source_id"], target)
    after = client_.get_activity(ledger["source_id"])
    returned = after.get("description")
    if not publication_format.same_text(returned if isinstance(returned, str) else "", target):
        raise ValueError("intervals.icu did not confirm the removal")
    with rt.engine.begin() as conn:
        conn.execute(
            publication_ledger.delete().where(
                publication_ledger.c.source == SOURCE,
                publication_ledger.c.source_id == ledger["source_id"],
            )
        )
    _audit(
        rt,
        "ok",
        ledger["source_id"],
        {"group_id": group["group_id"], "icu_activity_id": ledger["source_id"], "removed": True},
    )
    return {"state": "removed"}


def candidates(rt, *, now: datetime | None = None, attempts: dict | None = None) -> list[dict]:
    """Verified Strava blocks of the last two days that intervals.icu does not hold yet."""
    if not enabled(rt):
        return []
    now = now or datetime.now(UTC)
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(activity_unified.c.group_id, activity_unified.c.sources).where(
                    activity_unified.c.start_utc >= now - LOOKBACK
                )
            )
            .mappings()
            .all()
        )
        verified = {
            row.source_id: row.owned_block
            for row in conn.execute(
                select(publication_ledger.c.source_id, publication_ledger.c.owned_block).where(
                    publication_ledger.c.source == "strava",
                    publication_ledger.c.state == "verified",
                )
            )
        }
        copied = {
            row.owned_block
            for row in conn.execute(
                select(publication_ledger.c.owned_block).where(
                    publication_ledger.c.source == SOURCE,
                    publication_ledger.c.state == "verified",
                )
            )
        }
    tried = attempts or {}
    out = []
    for row in rows:
        strava = [r.split(":", 1)[1] for r in row["sources"] or [] if r.startswith("strava:")]
        if len(strava) != 1 or strava[0] not in verified:
            continue
        owned = verified[strava[0]]
        if owned in copied:
            continue
        digest = evidence.digest({"owned": owned})
        seen = tried.get(f"icu:{strava[0]}") or {}
        if seen.get("block") == digest:
            if seen.get("state") in ("published", "needs_review", "gave_up", "not_available"):
                continue
            retry_at = seen.get("retry_at")
            if retry_at and datetime.fromisoformat(retry_at) > now:
                continue
        out.append({"group_id": row["group_id"], "strava_id": strava[0], "block": digest})
    return out
