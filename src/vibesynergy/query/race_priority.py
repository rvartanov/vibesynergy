"""A race priority VibeSynergy keeps. Calendar sync does not write this table."""

from __future__ import annotations

from sqlalchemy import Engine, delete, or_, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import planned_item, race_priority

PRIORITIES = frozenset({"A", "B", "C"})


def effective_priority(primary: bool, saved: str | None) -> tuple[str | None, str]:
    """A stored choice wins. Otherwise a Garmin primary race is A.

    The second value is ``saved`` when the letter comes from ``race_priority``, and
    ``derived`` when it follows the Garmin primary flag, including when that leaves
    the priority unset.
    """
    if saved in PRIORITIES:
        return saved, "saved"
    return ("A" if primary else None), "derived"


def apply_priority(primary: bool, saved: str | None) -> str | None:
    """A stored choice wins. Otherwise a Garmin primary race is A, and the rest are unset."""
    priority, _origin = effective_priority(primary, saved)
    return priority


def stored_priorities(engine: Engine) -> dict[tuple[str, str], str]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(race_priority.c.source, race_priority.c.source_id, race_priority.c.priority)
        ).all()
    return {(source, source_id): priority for source, source_id, priority in rows}


def set_priority(
    engine: Engine, source: str, source_id: str, priority: str | None
) -> dict[str, str | None]:
    if priority is not None and priority not in PRIORITIES:
        raise ValueError("priority must be A, B, C or null")
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(planned_item).where(
                    planned_item.c.source == source,
                    planned_item.c.source_id == source_id,
                    planned_item.c.kind == "event",
                    or_(planned_item.c.is_race, planned_item.c.primary),
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise LookupError("race not found")
    now = utc_now()
    with engine.begin() as conn:
        if priority is None:
            conn.execute(
                delete(race_priority).where(
                    race_priority.c.source == source,
                    race_priority.c.source_id == source_id,
                )
            )
        else:
            stmt = insert(race_priority).values(
                source=source,
                source_id=source_id,
                priority=priority,
                updated_at=now,
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={"priority": priority, "updated_at": now},
                )
            )
    return {
        "source": source,
        "source_id": source_id,
        "name": row["title"],
        "date": row["date"].isoformat(),
        "priority": apply_priority(bool(row["primary"]), priority),
    }
