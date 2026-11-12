"""Read-only SQL over documented tables (the MCP ``vibesynergy_sql`` escape hatch).

Runs on a separate read-only SQLite connection with an authorizer that allows only SELECT
on the tables listed in ``TABLES`` (no writes, no ATTACH, no PRAGMA), a row limit and a time
limit. The dialect is SQLite's; Parquet streams are not reachable from here.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

TABLES: dict[str, str] = {
    "daily_unified": "One value per (date, metric) from the best source: subject_id, date "
    "(YYYY-MM-DD), metric, value, unit, source.",
    "daily_metric": "Every source's daily values: date, metric, source, value, unit.",
    "activity_unified": "One row per real-world activity: group_id, local_date, start_utc, "
    "sport, name, moving_s, elapsed_s, distance_m, ascent_m, avg_hr, max_hr, avg_power, "
    "norm_power, avg_speed (m/s), calories, training_load, stream_key, primary_source, "
    "sources (JSON).",
    "activity": "Per-source activities (source, source_id, …, extra JSON).",
    "activity_link": "Which per-source activity belongs to which group_id, and the rule.",
    "activity_lap": "Laps per source activity (activity_source_id, lap_index, …).",
    "activity_zone_time": "Seconds per HR/power zone per source activity.",
    "sleep_session": "Nights: source, local_date, start_utc, end_utc, duration_s, deep_s, "
    "light_s, rem_s, awake_s, score.",
    "sleep_stage": "Sleep stages: session_source_id, start_utc, end_utc, stage.",
    "body_measurement": "Timed measurements: measured_at (UTC), metric, value, unit, source.",
    "zone_set": "HR/power zones and Garmin CSS (kind css, mm/s) with valid_from dates (history).",
    "note": "Diary notes: id, start_date, end_date, activity_id, text, tags (JSON list), "
    "source, created_at.",
    "checkin": "Morning check-ins: date, sleep_quality, soreness, mood, stress, motivation "
    "(1-5; soreness and stress 1 = none), note.",
    "injury": "Injuries: id, body_part, side, kind, title, onset, status, resolved_on, notes.",
    "injury_log": "Worst pain per injury and day: injury_id, date, pain (0-10), timing, "
    "affects_movement, note.",
}
MAX_VALUE_BYTES = 20_000_000
_ALLOWED_FUNCTIONS_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION}


def run(db_path: Path, query: str, *, limit: int = 200, timeout_s: float = 10.0) -> dict[str, Any]:
    if ";" in query.strip().rstrip(";"):
        raise ValueError("one statement only")
    limit = max(1, limit)  # fetchmany(0) would return every row
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # No single value larger than this: a query cannot build a huge string in the service.
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + timeout_s
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            cursor = conn.execute(query)
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                f"{exc}. Tables: {', '.join(TABLES)} (see vibesynergy_schema)"
            ) from exc
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(limit + 1)
    finally:
        conn.close()
    return {
        "columns": columns,
        "rows": [list(r) for r in rows[:limit]],
        "truncated": len(rows) > limit,
    }


def _authorizer(action: int, arg1: str | None, arg2: str | None, db: str | None, _: Any) -> int:
    if action in _ALLOWED_FUNCTIONS_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # arg1 = table; allow documented tables and SQLite's own CTE/subquery scratch tables
        return (
            sqlite3.SQLITE_OK if arg1 in TABLES or not db or db == "temp" else sqlite3.SQLITE_DENY
        )
    return sqlite3.SQLITE_DENY
