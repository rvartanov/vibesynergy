"""Replay the sleep check (#100) over past nights, read-only, to tune its thresholds.

    uv run python tools/sleep_check_replay.py [--days 90] [--end 2026-11-20]
        [--db PATH] [--blobs DIR] [--timezone Europe/Amsterdam] [--versions 2026-11-17]

Opens the database with SQLite's ``mode=ro`` and reads the blob store; it writes nothing (no
migration, no stored check). Prints how many nights get each mark, the rules that fired, and
every night that is not ``ok`` with its reasons. ``--versions DAY`` also replays every
version Garmin delivered of that night, each with the heart rate known at that moment, to see
what the check would have said at the time (17-11-2026: the short first reading).
"""

from __future__ import annotations

import argparse
import collections
import os
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import sleep_quality as sq
from vibesynergy.store.blobs import BlobStore
from vibesynergy.store.raw import RawStore
from vibesynergy.store.schema import checkin, raw_item

DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "vibesynergy"


def _engine(path: Path):
    return create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")


def _quality(engine, day: date):
    with engine.connect() as conn:
        return conn.scalar(select(checkin.c.sleep_quality).where(checkin.c.date == day))


def replay(engine, raw, end: date, days: int, timezone: str) -> list[tuple[date, dict | None]]:
    window = Window(end - timedelta(days=days - 1), end)
    wanted = [
        (ds, d - timedelta(days=back))
        for d in window.days()
        for ds, back in (("sleep", 0), ("heart_rate", 0), ("heart_rate", 1))
    ] + [("training_readiness", d) for d in window.days()]
    current = sq._current_raw(engine, wanted)
    out = []
    for d in window.days():
        if ("sleep", d.isoformat()) not in current:
            out.append((d, None))
            continue
        usual = sq.usual_window(engine, d, timezone)
        out.append((d, sq._compute(engine, raw, d, timezone, current, _quality(engine, d), usual)))
    return out


def versions(engine, raw, day: date, timezone: str) -> None:
    """Every version of the night, with the heart-rate versions fetched before it."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(raw_item.c.id, raw_item.c.dataset, raw_item.c.item_key, raw_item.c.fetched_at)
            .where(
                raw_item.c.source == "garmin",
                raw_item.c.dataset.in_(["sleep", "heart_rate"]),
                raw_item.c.item_key.in_([day.isoformat(), (day - timedelta(days=1)).isoformat()]),
            )
            .order_by(raw_item.c.fetched_at)
        ).all()
    usual = sq.usual_window(engine, day, timezone)
    latest: dict[str, str] = {}
    for rid, dataset, key, fetched in rows:
        if dataset == "heart_rate":
            latest[key] = rid
            continue
        if key != day.isoformat():
            continue
        heart = sq.heart_samples([sq._load(raw, i) for i in latest.values()])
        found = sq.assess(
            sq._load(raw, rid),
            heart if latest else None,
            timezone=timezone,
            usual=usual,
            sleep_quality=_quality(engine, day),
        )
        print(f"  {fetched:%d-%m %H:%M} UTC  {_line(found)}")


def _line(found: dict | None) -> str:
    if not found:
        return "no night"
    reasons = "; ".join(f"{r['state']}: {sq.reason_text(r)}" for r in found["reasons"])
    metrics = ", ".join(f"{m} {s}" for m, s in found["metrics"].items())
    return f"{found['state']:<10} [{metrics}] {reasons}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DATA / "db" / "vibesynergy.sqlite")
    parser.add_argument("--blobs", type=Path, default=DATA / "blobs")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--timezone", default="Europe/Amsterdam")
    parser.add_argument("--versions", type=date.fromisoformat, action="append", default=[])
    args = parser.parse_args()
    engine = _engine(args.db)
    raw = RawStore(engine, BlobStore(args.blobs))
    found = replay(engine, raw, args.end, args.days, args.timezone)
    states = collections.Counter(f["state"] if f else "no night" for _, f in found)
    rules = collections.Counter(
        (r["rule"], r["state"]) for _, f in found if f for r in f["reasons"]
    )
    print(f"{len(found)} nights, {found[0][0]} to {found[-1][0]}")
    for state in (*sq.STATES, "no night"):
        print(f"  {state:<10} {states.get(state, 0)}")
    print("rules:")
    for (rule, state), n in sorted(rules.items()):
        print(f"  {rule:<18} {state:<10} {n}")
    print("nights that are not ok:")
    for day, f in found:
        if f and (f["state"] != "ok" or f["reasons"]):
            print(f"  {day:%d-%m-%Y}  {_line(f)}")
    for day in args.versions:
        print(f"versions of {day:%d-%m-%Y}:")
        versions(engine, raw, day, args.timezone)


if __name__ == "__main__":
    main()
