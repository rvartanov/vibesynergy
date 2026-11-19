"""A race's A/B/C priority, set on VibeSynergy's own race (``races``, #111).

The portal and the MCP name a race by the ``source`` and ``source_id`` of its calendar row:
``vibesynergy`` and the race id. A Garmin or intervals.icu id of a race's copy is accepted
too and resolves to that race. The old ``race_priority`` table is no longer read.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.query import races
from vibesynergy.store.schema import race

PRIORITIES = races.PRIORITIES


def set_priority(
    engine: Engine, source: str, source_id: str, priority: str | None
) -> dict[str, Any]:
    if priority is not None and priority not in PRIORITIES:
        raise ValueError("priority must be A, B, C or null")
    race_id = races.resolve(engine, source, source_id)
    if race_id is None:
        raise LookupError("race not found")
    races.set_priority(engine, race_id, priority)
    with engine.connect() as conn:
        row = conn.execute(select(race.c.name, race.c.date).where(race.c.id == race_id)).first()
    assert row is not None
    return {
        "source": races.SOURCE,
        "source_id": race_id,
        "name": row.name,
        "date": row.date.isoformat(),
        "priority": priority,
    }
