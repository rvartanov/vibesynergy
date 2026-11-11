"""Passive, first-observed activity delivery times shared by sync, portal and MCP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import (
    activity,
    activity_latency,
    activity_link,
)

STAGES = (
    "finished_at",
    "source_event_at",
    "received_at",
    "queued_at",
    "fetch_started_at",
    "fetched_at",
    "processed_at",
    "dashboard_at",
)


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def stamp(engine: Engine, source: str, source_id: str, **stages: datetime | str | None) -> None:
    """First value wins, including across retries and corrected uploads."""
    values = {
        key: _utc(value) for key, value in stages.items() if key in STAGES and value is not None
    }
    if not values:
        return
    stmt = insert(activity_latency).values(source=source, source_id=source_id, **values)
    with engine.begin() as conn:
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["source", "source_id"],
                set_={
                    key: func.coalesce(
                        getattr(activity_latency.c, key), getattr(stmt.excluded, key)
                    )
                    for key in values
                },
            )
        )


def fetched(engine: Engine, source: str, source_id: str, at: datetime) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            select(activity.c.start_utc, activity.c.elapsed_s).where(
                activity.c.source == source, activity.c.source_id == source_id
            )
        ).first()
    if row is None:
        return
    finished = (
        row.start_utc + timedelta(seconds=row.elapsed_s) if row.elapsed_s is not None else None
    )
    stamp(engine, source, source_id, finished_at=finished, fetched_at=at)


def processed(engine: Engine) -> None:
    """Stamp newly visible unified rows after a successful derived-table rebuild."""
    with engine.begin() as conn:
        pending = conn.execute(
            select(activity_latency.c.source, activity_latency.c.source_id)
            .join(
                activity_link,
                (activity_link.c.source == activity_latency.c.source)
                & (activity_link.c.source_id == activity_latency.c.source_id),
            )
            .where(
                activity_latency.c.processed_at.is_(None),
                activity_latency.c.fetched_at.is_not(None),
            )
        ).all()
        at = utc_now()
        for source, source_id in pending:
            conn.execute(
                update(activity_latency)
                .where(
                    activity_latency.c.source == source, activity_latency.c.source_id == source_id
                )
                .values(processed_at=at)
            )


def dashboard_visible(engine: Engine, rows: list[dict]) -> None:
    """Record the first successful dashboard query that included each source activity."""
    keys = set()
    for row in rows:
        for ref in row.get("sources") or []:
            keys.add(tuple(ref.split(":", 1)))
    if not keys:
        return
    at = utc_now()
    with engine.begin() as conn:
        for source, source_id in keys:
            conn.execute(
                update(activity_latency)
                .where(
                    activity_latency.c.source == source,
                    activity_latency.c.source_id == source_id,
                    activity_latency.c.dashboard_at.is_(None),
                    activity_latency.c.processed_at.is_not(None),
                )
                .values(dashboard_at=at)
            )


def recent(engine: Engine, limit: int = 20) -> list[dict]:
    """Read-only stage times and intervals. Missing stages remain unknown, never zero."""
    limit = max(1, min(limit, 100))
    with engine.connect() as conn:
        rows = conn.execute(
            select(activity_latency)
            .order_by(
                func.coalesce(
                    activity_latency.c.finished_at,
                    activity_latency.c.received_at,
                    activity_latency.c.fetched_at,
                ).desc()
            )
            .limit(limit)
        ).all()
        result = [dict(row._mapping) for row in rows]
        for item in result:
            item["assessed_at"] = None
    for item in result:
        for key in (*STAGES, "assessed_at"):
            item[key] = _utc(item[key])
        pairs = {
            "provider_upload_s": ("finished_at", "received_at"),
            "source_poll_s": ("finished_at", "fetch_started_at"),
            "notification_s": ("source_event_at", "received_at"),
            "queue_s": ("queued_at", "fetch_started_at"),
            "fetch_s": ("fetch_started_at", "fetched_at"),
            "processing_s": ("fetched_at", "processed_at"),
            "dashboard_s": ("processed_at", "dashboard_at"),
            # Webhook receipt until the activity is in the unified tables. This is the
            # locally observable upload-to-available interval. total_s still starts at
            # workout finish and ends at the first dashboard query.
            "vibesynergy_s": ("received_at", "processed_at"),
            "total_s": ("finished_at", "dashboard_at"),
        }
        item["delays_s"] = {
            name: round((item[end] - item[start]).total_seconds(), 3)
            if item[start] and item[end] and item[end] >= item[start]
            else None
            for name, (start, end) in pairs.items()
        }
        if item["received_at"] is not None:
            item["delays_s"]["source_poll_s"] = None
        for key in (*STAGES, "assessed_at"):
            item[key] = item[key].isoformat() if item[key] else None
    return result
