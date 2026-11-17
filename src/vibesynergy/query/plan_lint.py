"""Plan checks (consistency layer 1, #60): deterministic, read only, no model.

Each check catches a gap that is easy to miss when one session changes and the ones around
it do not. Found on 17-11-2026:

* ``name_duration``: the name says 45' while the steps take 50'.
* ``strength_warmup`` (STR-4): heavy barbell work after one warm-up exercise. The session
  was written to come after the run test; the test moved and the warm-up stayed.
* ``stale_reference``: the text says "na de looptest" while that day has no run test.
* ``review_context``: after a session moves, is skipped or is added, the sessions around it
  whose text mentions it are to be looked at again.

Findings are warnings: the plan write goes through and returns them, the start page and
``vibesynergy_today`` list the ones for the coming days. ``level`` stays "warning" until a
check has proven itself on the live plan (#62 may turn some into refusals).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.store.schema import plan_session

WARNING = "warning"
DAYS = 14
NAME_SLACK_S = 4 * 60
SWIM_SLACK_SHARE = 0.15  # a swim's length is an estimate from its metres
WARMUP_MIN_S = 5 * 60
ACTIVATION_MIN = 2
HEAVY_KG = 20.0  # an empty Olympic bar: from here on a set is barbell work

# Minutes in a name: 45', 45 min, 1u15, 1u. "6x3'" and "20' test" are parts, not the total.
_MINUTES = re.compile(r"(?<![x\u00d7\d])(\d{1,3})\s*(?:'|\u2019|\u2032|\s?min\b)", re.IGNORECASE)
_HOURS = re.compile(r"\b(\d)u(\d{2})?\b", re.IGNORECASE)

# What a text may refer to, and how to recognise that session on the same day.
_REFERENCES: dict[str, tuple[re.Pattern, tuple[str, ...], tuple[str, ...]]] = {
    "looptest": (re.compile(r"\bna de looptest\b", re.I), ("looptest",), ()),
    "ftp": (re.compile(r"\bna de ftp-?test\b", re.I), ("ftp",), ()),
    "css": (re.compile(r"\bna de css-?test\b", re.I), ("css",), ()),
    "groepstraining": (
        re.compile(r"\b(na|vóór|voor) de groepstraining\b", re.I),
        ("training geven", "groepstraining"),
        (),
    ),
    "zwemmen": (re.compile(r"\bna het zwemmen\b", re.I), ("zwem",), ("swim",)),
}
# Which references a session's name answers to (for review_context).
_KEYWORDS = {
    "looptest": ("looptest",),
    "ftp": ("ftp",),
    "css": ("css",),
    "groepstraining": ("training geven", "groepstraining"),
    "zwemmen": ("zwem",),
}


def _finding(
    code: str, row: dict[str, Any], message: str, rule: str | None = None, **params: Any
) -> dict:
    return {
        "level": WARNING,
        "code": code,
        "session_id": row["id"],
        "date": row["date"].isoformat(),
        "name": row["name"],
        "message": message,
        "rule": rule,
        "params": params,
    }


def name_minutes(name: str | None) -> int | None:
    """The total length a session name states, in minutes, or None."""
    text = name or ""
    if "test" in text.casefold():
        return None  # "Looptest 30'", "FTP-test 20'": the test part, not the session
    if (hours := _HOURS.search(text)) is not None:
        return int(hours.group(1)) * 60 + int(hours.group(2) or 0)
    found = _MINUTES.findall(text)
    return int(found[-1]) if len(found) == 1 else None


def _text(row: dict[str, Any]) -> str:
    workout = row.get("workout") or {}
    return f"{row.get('description') or ''}\n{workout.get('garmin_block') or ''}"


def check_name_duration(row: dict[str, Any]) -> list[dict]:
    minutes = name_minutes(row["name"])
    planned = row.get("duration_s")
    if minutes is None or not planned:
        return []
    swim = "swim" in (row.get("sport") or "").casefold()
    slack = max(NAME_SLACK_S, SWIM_SLACK_SHARE * planned) if swim else NAME_SLACK_S
    if abs(minutes * 60 - planned) <= slack:
        return []
    return [
        _finding(
            "name_duration",
            row,
            f"the name says {minutes}' but the steps take {round(planned / 60)}'",
            minutes=minutes,
            planned=round(planned / 60),
        )
    ]


def check_strength_warmup(row: dict[str, Any]) -> list[dict]:
    from vibesynergy.query import garmin_strength

    block_text = (row.get("workout") or {}).get("garmin_block") or ""
    if not block_text:
        return []
    try:
        block = garmin_strength.parse_block(block_text)
    except garmin_strength.BlockError:
        return []
    if block is None:
        return []
    steps = block.steps
    first = next(
        (
            i
            for i, s in enumerate(steps)
            if s.kind == "exercise" and s.weight and _kg(s.weight) >= HEAVY_KG
        ),
        None,
    )
    if first is None:
        return []
    before = steps[:first]
    cardio = sum(s.seconds or 0 for s in before if s.kind == "warmup")
    activation = len(
        {(s.category, s.name) for s in before if s.kind == "exercise" and not s.weight}
    )
    missing = []
    if cardio < WARMUP_MIN_S:
        missing.append(f"{WARMUP_MIN_S // 60}' easy cardio")
    if activation < ACTIVATION_MIN:
        missing.append(f"{ACTIVATION_MIN} activation exercises (has {activation})")
    if not missing:
        return []
    return [
        _finding(
            "strength_warmup",
            row,
            "barbell work starts without a full warm-up: missing " + " and ".join(missing),
            "STR-4",
        )
    ]


def _kg(weight: tuple[float, str]) -> float:
    value, unit = weight
    return value * 0.4536 if unit == "lb" else value


def _matches(row: dict[str, Any], names: tuple[str, ...], sports: tuple[str, ...]) -> bool:
    title = (row["name"] or "").casefold()
    sport = (row["sport"] or "").casefold()
    return any(n in title for n in names) or any(s in sport for s in sports)


_DAY_AFTER = re.compile(r"\bdag\s+$", re.I)  # "de dag na de looptest": the day before


def check_references(row: dict[str, Any], by_day: dict[date, list[dict[str, Any]]]) -> list[dict]:
    """A reference to another session ("na de looptest") needs that session on the day it
    points at: the same day, or the day before for "de dag na …" (the day after for
    "de dag vóór …")."""
    text = _text(row)
    out = []
    for key, (pattern, names, sports) in _REFERENCES.items():
        for match in pattern.finditer(text):
            lead = text[: match.start()][-12:]
            shift = 0
            if _DAY_AFTER.search(lead):
                shift = -1 if match.group(0).casefold().startswith("na") else 1
            day = row["date"] + timedelta(days=shift)
            others = [r for r in by_day.get(day, []) if r["id"] != row["id"]]
            if any(_matches(r, names, sports) for r in others):
                continue
            where = (
                "this day" if not shift else ("the day before" if shift < 0 else "the day after")
            )
            out.append(
                _finding(
                    "stale_reference",
                    row,
                    f"the text refers to the {key} session, but {where} has none",
                    session=key,
                )
            )
            break
    return out


def _rows(engine: Engine, plan_id: str, start: date, end: date) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(plan_session)
                .where(
                    plan_session.c.plan_id == plan_id,
                    plan_session.c.date.between(start, end),
                    plan_session.c.status == "planned",
                )
                .order_by(plan_session.c.date, plan_session.c.id)
            ).mappings()
        ]


def _check(rows: list[dict[str, Any]], only: set[str] | None = None) -> list[dict]:
    by_day: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["date"], []).append(row)
    out: list[dict] = []
    for row in rows:
        if only is not None and row["id"] not in only:
            continue
        out += check_name_duration(row)
        out += check_strength_warmup(row)
        out += check_references(row, by_day)
    return out


def lint(engine: Engine, start: date, end: date, plan_id: str | None = None) -> list[dict]:
    """Every finding for the current plan's planned sessions in the window."""
    from vibesynergy.query import plan_model

    plan_id = plan_id or plan_model.current_plan_id(engine)
    if plan_id is None:
        return []
    # A day on each side, so "de dag na de looptest" on the first day can be checked.
    rows = _rows(engine, plan_id, start - timedelta(days=1), end + timedelta(days=1))
    return _check(rows, only={r["id"] for r in rows if start <= r["date"] <= end})


def upcoming(engine: Engine, today: date, days: int = DAYS) -> list[dict]:
    return lint(engine, today, today + timedelta(days=days))


def _keywords(name: str | None) -> tuple[str, ...]:
    title = (name or "").casefold()
    return tuple(key for key, words in _KEYWORDS.items() if any(word in title for word in words))


def after_change(
    engine: Engine,
    plan_id: str,
    session_id: str,
    days: list[date],
    *,
    changed_name: str | None = None,
) -> list[dict]:
    """The checks for one plan write: the changed session's own findings, any finding on
    the days around it, and ``review_context`` for neighbours whose text mentions it."""
    start, end = min(days) - timedelta(days=1), max(days) + timedelta(days=1)
    rows = _rows(engine, plan_id, start, end)
    near = {r["id"] for r in rows}
    out = _check(rows, only=near)
    keys = _keywords(changed_name)
    for row in rows:
        if row["id"] == session_id or not keys:
            continue
        text = _text(row).casefold()
        mentioned = [k for k in keys if any(w in text for w in _KEYWORDS[k])]
        if mentioned:
            out.append(
                _finding(
                    "review_context",
                    row,
                    f"mentions the {mentioned[0]} session that just changed: look at it again",
                )
            )
    return out
