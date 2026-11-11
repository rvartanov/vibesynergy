"""Multisport as a holder: a brick or triathlon is one entry, its parts inside.

Garmin records a multisport as its own activity (``multi_sport``), and the legs come in as
separate activities as well, from Garmin or from Strava (a Zwift ride, the transition and the
run on 8-11). Nothing links them but time: a part starts inside the holder's span. The parts
count for their own discipline (time, distance, load, against the plan); the holder does
not count again, so nothing is counted twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

MULTI = ("multi_sport", "multisport", "triathlon", "duathlon", "brick")
SLACK = timedelta(minutes=2)


def is_multisport(sport: str | None) -> bool:
    return any(word in (sport or "").lower() for word in MULTI)


def _start(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def parts(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Holder id → its parts' ids in order; only holders that have parts.

    Each row needs ``id``, ``sport``, ``start_utc`` and ``elapsed_s`` (or ``moving_s``).
    """
    out: dict[str, list[str]] = {}
    for holder in rows:
        if not is_multisport(holder.get("sport")):
            continue
        begin = _start(holder.get("start_utc"))
        seconds = holder.get("elapsed_s") or holder.get("moving_s")
        if begin is None or not seconds:
            continue
        end = begin + timedelta(seconds=float(seconds))
        inside = []
        for other in rows:
            when = _start(other.get("start_utc"))
            if (
                other is holder
                or is_multisport(other.get("sport"))
                or when is None
                or not begin - SLACK <= when <= end + SLACK
            ):
                continue
            inside.append((when, str(other["id"])))
        if inside:
            out[str(holder["id"])] = [ident for _, ident in sorted(inside)]
    return out


def relatives(engine: Any, ids: list[str]) -> list[str]:
    """``ids`` with, for every one in a multisport, its holder and all its parts (each part gets
    its own insight and the holder one over the whole). Order: as given,
    then the added ones."""
    from sqlalchemy import select

    from vibesynergy.query import series as qs
    from vibesynergy.store.schema import activity_unified as t

    out = list(dict.fromkeys(str(i) for i in ids))
    with engine.connect() as conn:
        days = {row[0] for row in conn.execute(select(t.c.local_date).where(t.c.group_id.in_(out)))}
        if not days:
            return out
        spans = conn.execute(
            select(
                t.c.group_id,
                t.c.sport,
                t.c.start_utc,
                t.c.elapsed_s,
                qs.activity_column("moving_s"),
            ).where(t.c.local_date.in_(days))
        ).all()
    holders = parts(
        [
            {"id": r[0], "sport": r[1], "start_utc": r[2], "elapsed_s": r[3], "moving_s": r[4]}
            for r in spans
        ]
    )
    for holder, legs in holders.items():
        if holder in out or any(leg in out for leg in legs):
            out.extend(i for i in [holder, *legs] if i not in out)
    return out
