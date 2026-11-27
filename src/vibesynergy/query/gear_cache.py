"""Gear usage per activity, kept between requests (#203).

The start page, the alerts check and the coach's brief all ask ``workouts.gear()``, and its
expensive part is the same every time until something changes: which gear each activity
counts with (a pass over every activity) and the Garmin gear ids on activities. Those are
kept here and computed again once this process writes one of the tables they come from (a
sync, a history import, a gear change on an activity, a sport's default gear). A write from
another process, such as a CLI command, is seen within ``MAX_AGE_S``.

Only the activity-derived lists are kept; the gear register itself (your settings, services,
own items and Strava's gear list) is read on every call, as before.
"""

from __future__ import annotations

import re
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, event

MAX_AGE_S = 300.0
# The tables the kept lists are computed from; any write that names one drops them.
_WRITES = re.compile(
    r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b.*?\b(?:activity|activity_unified|activity_gear"
    r"|gear_default)\b",
    re.IGNORECASE | re.DOTALL,
)
_DIRTY = "vibesynergy_gear_cache_dirty"


class _State:
    def __init__(self) -> None:
        self.generation = 0
        self.entries: dict[str, tuple[int, float, Any]] = {}
        self.lock = threading.Lock()

    def bump(self) -> None:
        with self.lock:
            self.generation += 1
            self.entries.clear()


_STATES: weakref.WeakKeyDictionary[Engine, _State] = weakref.WeakKeyDictionary()
_STATES_LOCK = threading.Lock()


def _state(engine: Engine) -> _State:
    with _STATES_LOCK:
        state = _STATES.get(engine)
        if state is not None:
            return state
        state = _STATES[engine] = _State()

    def written(conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if _WRITES.match(statement):
            conn.info[_DIRTY] = True
            state.bump()  # a read that started before this write is not kept

    def returned(_dbapi_conn, record) -> None:
        # Back in the pool means committed or rolled back: a read that ran between the
        # write and its commit saw the old rows, so drop once more.
        if record.info.pop(_DIRTY, False):
            state.bump()

    event.listen(engine, "after_cursor_execute", written)
    event.listen(engine.pool, "checkin", returned)
    return state


def cached(engine: Any, name: str, compute: Callable[[], Any]) -> Any:
    """``compute()``, kept per engine until a write to the activity or gear-choice tables.
    Anything that is not an ``Engine`` (a read-only snapshot) computes every time."""
    if not isinstance(engine, Engine):
        return compute()
    state = _state(engine)
    now = time.monotonic()
    with state.lock:
        generation = state.generation
        hit = state.entries.get(name)
    if hit is not None and hit[0] == generation and now - hit[1] < MAX_AGE_S:
        return hit[2]
    value = compute()
    with state.lock:
        if state.generation == generation:
            state.entries[name] = (generation, now, value)
    return value
