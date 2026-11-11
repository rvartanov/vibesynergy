"""The training plan as VibeSynergy's own model (docs/design/plan-model.md).

Step 1 of the change-over: ``import_from_intervals`` builds the model from the intervals.icu
plan cache (``icu_event``): week notes (``<prefix>:week:NN``), week targets
(``<prefix>:target:NN:<sport>``) and workouts (``<prefix>:<date>:<slot>``), each workout's
steps through ``workout_model``. It reads only; nothing is written to intervals.icu and no
reader uses the model yet. A new ``plan_version`` is made only when the content changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, delete, func, insert, or_, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import training_plan, workout_model
from vibesynergy.store.schema import (
    icu_event,
    plan,
    plan_link,
    plan_session,
    plan_target,
    plan_version,
    plan_week,
)

SUBJECT = 1
SHADOW = "shadow"  # intervals.icu leads; the model follows it after every sync
LEADING = "leading"  # VibeSynergy leads; intervals.icu is a one-way mirror
log = logging.getLogger(__name__)


def _week_number(external_id: str, prefix: str, kind: str) -> int | None:
    parts = external_id.split(":")
    if len(parts) >= 3 and parts[0] == prefix and parts[1] == kind and parts[2].isdigit():
        return int(parts[2])
    return None


def _read(engine: Engine, prefix: str) -> dict[str, Any]:
    weeks: dict[int, dict[str, Any]] = {}
    targets: dict[tuple[int, str], dict[str, Any]] = {}
    sessions: list[dict[str, Any]] = []
    unparsed: dict[str, list[str]] = {}
    with engine.connect() as conn:
        rows = list(
            conn.execute(
                select(icu_event).where(icu_event.c.external_id.like(f"{prefix}:%"))
            ).mappings()
        )
        first = min((r["date"] for r in rows), default=None)
        # Workouts in the plan period without the plan's key (group trainings, an optional
        # MTB ride the coach added by hand) belong to the plan too ( step 2).
        if first is not None:
            rows += list(
                conn.execute(
                    select(icu_event).where(
                        icu_event.c.category == "WORKOUT",
                        icu_event.c.date >= first,
                        or_(
                            icu_event.c.external_id.is_(None),
                            icu_event.c.external_id.not_like(f"{prefix}:%"),
                        ),
                    )
                ).mappings()
            )
        for row in rows:
            ext = row["external_id"] or ""
            if not ext.startswith(f"{prefix}:"):
                ext = f"{prefix}:icu:{row['id']}"
            raw = row["raw"] or {}
            if row["category"] == "NOTE" and (n := _week_number(ext, prefix, "week")) is not None:
                title = training_plan.parse_title(row["name"])
                weeks[n] = {
                    "n": n,
                    "monday": row["date"] - timedelta(days=row["date"].weekday()),
                    "phase": title["phase"],
                    "kind": title["kind"],
                    "hours_label": title["hours_label"],
                    "note": row["description"],
                }
            elif (
                row["category"] == "TARGET"
                and (n := _week_number(ext, prefix, "target")) is not None
            ):
                sport = ext.split(":")[3] if len(ext.split(":")) > 3 else (row["type"] or "")
                t = row["targets"] or {}
                targets[(n, sport)] = {
                    "week": n,
                    "sport": sport,
                    "time_s": t.get("time_target"),
                    "distance_m": t.get("distance_target"),
                    "load": t.get("load_target"),
                }
            elif row["category"] == "WORKOUT":
                parsed = workout_model.parse_intervals(row["description"])
                if parsed.unparsed:
                    unparsed[ext] = parsed.unparsed
                parts = ext.split(":")
                slot = parts[2] if len(parts) > 2 and parts[1] != "icu" else None
                sessions.append(
                    {
                        "id": ext,
                        "date": row["date"],
                        "slot": slot,
                        "sport": row["type"],
                        "name": row["name"],
                        "duration_s": raw.get("moving_time"),
                        "distance_m": raw.get("distance"),
                        "workout": {"steps": parsed.steps, "garmin_block": parsed.garmin_block},
                        "description": parsed.intro,
                        "status": "planned",
                        "_remote": (str(row["id"]), row["updated"]),
                        "_fp": fingerprint(
                            row["date"], row["name"], row["type"], _normalised(row["description"])
                        ),
                    }
                )
    return {"weeks": weeks, "targets": targets, "sessions": sessions, "unparsed": unparsed}


def _digest(found: dict[str, Any]) -> str:
    def clean(value: Any) -> Any:
        if isinstance(value, date):
            return value.isoformat()
        return value

    body = {
        # Bump when the import stores more: the next sync then imports once more (v2: the
        # intervals link fingerprints the mirror compares against).
        "format": 2,
        "weeks": [{k: clean(v) for k, v in w.items()} for _, w in sorted(found["weeks"].items())],
        "targets": [found["targets"][k] for k in sorted(found["targets"])],
        "sessions": sorted(
            (
                {k: clean(v) for k, v in s.items() if not k.startswith("_")}
                for s in found["sessions"]
            ),
            key=lambda s: s["id"],
        ),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def import_from_intervals(
    engine: Engine,
    *,
    prefix: str,
    name: str | None = None,
    method: str | None = "friel",
    author: str = "import:intervals.icu",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fill the plan model from intervals.icu (shadow). Returns what was found and done."""
    found = _read(engine, prefix)
    digest = _digest(found)
    dates = [s["date"] for s in found["sessions"]] + [w["monday"] for w in found["weeks"].values()]
    start = min(dates) if dates else None
    end = max(dates) + timedelta(days=6) if dates else None
    report: dict[str, Any] = {
        "plan_id": prefix,
        "weeks": len(found["weeks"]),
        "targets": len(found["targets"]),
        "sessions": len(found["sessions"]),
        "unparsed": found["unparsed"],
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "changed": False,
        "version": None,
    }
    with engine.begin() as conn:
        current = conn.execute(select(plan).where(plan.c.id == prefix)).mappings().first()
        if current and current["status"] == LEADING:
            # VibeSynergy leads: intervals.icu is a mirror and never overwrites the model.
            report["leading"] = True
            report["version"] = current["current_version"]
            return report
        last = (
            conn.execute(
                select(plan_version.c.summary)
                .where(plan_version.c.plan_id == prefix)
                .order_by(plan_version.c.n.desc())
                .limit(1)
            ).scalar()
            if current
            else None
        )
        if last and last.get("hash") == digest:
            report["version"] = current["current_version"] if current else None
            return report
        report["changed"] = True
        if dry_run:
            return report
        n = (current["current_version"] if current else 0) + 1
        now = utc_now()
        summary = {k: report[k] for k in ("weeks", "targets", "sessions")}
        summary["hash"] = digest
        summary["unparsed"] = len(found["unparsed"])
        conn.execute(
            insert(plan_version).values(
                plan_id=prefix,
                n=n,
                created_at=now,
                author=author,
                reason="import from intervals.icu (shadow)",
                summary=summary,
            )
        )
        values = {
            "name": name or (current["name"] if current else prefix),
            "method": method,
            "start": start,
            "end": end,
            "status": SHADOW,
            "current_version": n,
            "updated_at": now,
        }
        if current:
            conn.execute(update(plan).where(plan.c.id == prefix).values(**values))
        else:
            conn.execute(insert(plan).values(id=prefix, subject_id=SUBJECT, **values))
        for table in (plan_week, plan_target, plan_session):
            conn.execute(delete(table).where(table.c.plan_id == prefix))
        ids = [s["id"] for s in found["sessions"]]
        conn.execute(
            delete(plan_link).where(
                plan_link.c.target == "intervals", plan_link.c.session_id.like(f"{prefix}:%")
            )
        )
        for w in found["weeks"].values():
            conn.execute(insert(plan_week).values(plan_id=prefix, version=n, **w))
        for t in found["targets"].values():
            conn.execute(insert(plan_target).values(plan_id=prefix, version=n, **t))
        for s in found["sessions"]:
            remote_id, remote_version = s["_remote"]
            row = {k: v for k, v in s.items() if not k.startswith("_")}
            conn.execute(insert(plan_session).values(plan_id=prefix, version=n, **row))
            conn.execute(
                insert(plan_link).values(
                    session_id=s["id"],
                    target="intervals",
                    remote_id=remote_id,
                    remote_version=remote_version.isoformat() if remote_version else None,
                    fingerprint=s["_fp"],
                    synced_at=now,
                )
            )
        report["version"] = n
        report["linked"] = len(ids)
    return report


def current_plan_id(engine: Engine) -> str | None:
    """The athlete's plan: one per install, the most recently changed one."""
    with engine.connect() as conn:
        return conn.execute(select(plan.c.id).order_by(plan.c.updated_at.desc())).scalar()


def _required_plan(engine: Engine, plan_id: str | None) -> str:
    found = plan_id or current_plan_id(engine)
    if not found:
        raise ValueError("no plan yet: import one with `vibesynergy plan import --prefix <tag>`")
    return found


def summary(engine: Engine, plan_id: str | None = None) -> dict[str, Any] | None:
    """What the model holds for a plan (for checks and the shadow compare)."""
    plan_id = plan_id or current_plan_id(engine) or ""
    with engine.connect() as conn:
        row = conn.execute(select(plan).where(plan.c.id == plan_id)).mappings().first()
        if not row:
            return None
        count = lambda table: conn.execute(  # noqa: E731
            select(func.count()).select_from(table).where(table.c.plan_id == plan_id)
        ).scalar()
        return {
            **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()},
            "weeks": count(plan_week),
            "targets": count(plan_target),
            "sessions": count(plan_session),
        }


def shadow_refresh(engine: Engine) -> dict[str, Any] | None:
    """After a sync: keep the model in step with intervals.icu. Never breaks the sync."""
    try:
        plan_id = current_plan_id(engine)
        if not plan_id:
            return None  # no plan imported yet: nothing to keep in step
        if source(engine, plan_id) == "vibesynergy":
            return run_mirror(engine)
        return import_from_intervals(engine, prefix=plan_id)
    except Exception:  # shadow mode must not stop the sync
        log.warning("plan model shadow refresh failed", exc_info=True)
        return None


MIRROR_JOB = "plan_mirror"


def run_mirror(engine: Engine) -> dict[str, Any] | None:
    """Mirror the current plan to intervals.icu now, if VibeSynergy leads."""
    plan_id = current_plan_id(engine)
    if not plan_id or source(engine, plan_id) != "vibesynergy":
        return None
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    out = mirror(engine, IntervalsIcuClient.from_env(), plan_id=plan_id)
    if out["drift"]:
        log.warning("plan: changed in intervals.icu, put back: %s", out["drift"])
    return out


def mirror_soon(jobs: Any) -> int | None:
    """After a change by the coach: a queued mirror job, so it does not wait for a sync."""
    return jobs.enqueue(MIRROR_JOB, {}, dedupe_key=MIRROR_JOB, priority=5)


# VibeSynergy leads (step 3) -------------------------------------------------------------------


def fingerprint(day: Any, name: Any, sport: Any, text: str) -> str:
    body = json.dumps([str(day), name or "", sport or "", text], ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def _normalised(description: str | None) -> str:
    """intervals.icu text as the model would write it, so formatting is no difference."""
    return workout_model.to_intervals(workout_model.parse_intervals(description))


def session_text(row: dict[str, Any]) -> str:
    workout = row.get("workout") or {}
    return workout_model.to_intervals(
        workout_model.Workout(
            steps=workout.get("steps") or [],
            intro=row.get("description") or "",
            garmin_block=workout.get("garmin_block") or "",
        )
    )


def source(engine: Engine, plan_id: str | None = None) -> str:
    plan_id = plan_id or current_plan_id(engine) or ""
    with engine.connect() as conn:
        status = conn.execute(select(plan.c.status).where(plan.c.id == plan_id)).scalar()
    return "vibesynergy" if status == LEADING else "intervals"


def set_source(engine: Engine, value: str, plan_id: str | None = None) -> str:
    """Switch who leads. Back to intervals.icu: the model follows it again on the next sync."""
    plan_id = _required_plan(engine, plan_id)
    if value not in ("intervals", "vibesynergy"):
        raise ValueError("source is intervals or vibesynergy")
    with engine.begin() as conn:
        done = conn.execute(
            update(plan)
            .where(plan.c.id == plan_id)
            .values(status=LEADING if value == "vibesynergy" else SHADOW, updated_at=utc_now())
        ).rowcount
    if not done:
        raise ValueError(f"no plan {plan_id!r}: run 'vibesynergy plan import' first")
    return value


def set_session(
    engine: Engine,
    *,
    session_id: str | None,
    day: date,
    name: str,
    sport: str | None,
    description: str,
    reason: str,
    author: str,
    plan_id: str | None = None,
    slot: str | None = None,
) -> dict[str, Any]:
    """Create or change one session in the model. ``description`` is intervals.icu text."""
    plan_id = _required_plan(engine, plan_id)
    if not (reason or "").strip():
        raise ValueError("a reason is required")
    parsed = workout_model.parse_intervals(description)
    if parsed.unparsed:
        raise ValueError(f"steps not understood: {parsed.unparsed}")
    from vibesynergy.query import garmin_strength

    garmin_strength.check_description(description)  # a BlockError is a ValueError
    ident = session_id or f"{plan_id}:{day.isoformat()}:{slot or (sport or 'session').lower()}"
    row = {
        "date": day,
        "slot": slot,
        "sport": sport,
        "name": name,
        "workout": {"steps": parsed.steps, "garmin_block": parsed.garmin_block},
        "description": parsed.intro,
        "status": "planned",
    }
    with engine.begin() as conn:
        current = conn.execute(select(plan).where(plan.c.id == plan_id)).mappings().first()
        if not current:
            raise ValueError(f"no plan {plan_id!r}")
        n = current["current_version"] + 1
        conn.execute(
            insert(plan_version).values(
                plan_id=plan_id,
                n=n,
                created_at=utc_now(),
                author=author,
                reason=reason,
                summary={"session": ident},
            )
        )
        conn.execute(update(plan).where(plan.c.id == plan_id).values(current_version=n))
        exists = conn.execute(select(plan_session.c.id).where(plan_session.c.id == ident)).scalar()
        if exists:
            conn.execute(
                update(plan_session).where(plan_session.c.id == ident).values(version=n, **row)
            )
        else:
            conn.execute(insert(plan_session).values(id=ident, plan_id=plan_id, version=n, **row))
    return {"session_id": ident, "version": n}


def drift(engine: Engine, plan_id: str | None = None) -> list[dict[str, Any]]:
    """Sessions whose intervals.icu copy was changed outside VibeSynergy since the last sync."""
    plan_id = plan_id or current_plan_id(engine) or ""
    out = []
    with engine.connect() as conn:
        rows = conn.execute(
            select(plan_link.c.session_id, plan_link.c.fingerprint, icu_event)
            .join(icu_event, icu_event.c.id == plan_link.c.remote_id)
            .where(
                plan_link.c.target == "intervals",
                plan_link.c.session_id.like(f"{plan_id}:%"),
            )
        ).mappings()
        for r in rows:
            now = fingerprint(r["date"], r["name"], r["type"], _normalised(r["description"]))
            if r["fingerprint"] and now != r["fingerprint"]:
                out.append({"session_id": r["session_id"], "event_id": r["id"], "name": r["name"]})
    return out


SKIPPED = "skipped"  # taken out of the plan; the mirror removes its intervals.icu copy


def _planned_session(conn: Any, plan_id: str, session_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            select(plan_session).where(
                plan_session.c.plan_id == plan_id, plan_session.c.id == session_id
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ValueError(f"no session {session_id!r} in plan {plan_id!r}")
    if row["status"] != "planned":
        raise ValueError(f"session {session_id!r} is {row['status']}, not planned")
    return dict(row)


def move_session(
    engine: Engine,
    *,
    session_id: str,
    day: date,
    reason: str,
    author: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Move one planned session to another day; the mirror moves its copy."""
    plan_id = _required_plan(engine, plan_id)
    with engine.begin() as conn:
        row = _planned_session(conn, plan_id, session_id)
        if row["date"] == day:
            raise ValueError("the session is already on that day")
        n = _bump(
            conn, plan_id, author=author, reason=reason, summary={"move": [session_id, str(day)]}
        )
        conn.execute(
            update(plan_session).where(plan_session.c.id == session_id).values(date=day, version=n)
        )
    return {"session_id": session_id, "date": day.isoformat(), "version": n}


def skip_session(
    engine: Engine, *, session_id: str, reason: str, author: str, plan_id: str | None = None
) -> dict[str, Any]:
    """Take one planned session out of the plan; the mirror removes its copy."""
    plan_id = _required_plan(engine, plan_id)
    with engine.begin() as conn:
        _planned_session(conn, plan_id, session_id)
        n = _bump(conn, plan_id, author=author, reason=reason, summary={"skip": session_id})
        conn.execute(
            update(plan_session)
            .where(plan_session.c.id == session_id)
            .values(status=SKIPPED, version=n)
        )
    return {"session_id": session_id, "status": SKIPPED, "version": n}


# Week notes and weekly targets (COACH-5: they change together with the sessions) ----

TARGET_FIELDS = {"time_s": "time_target", "distance_m": "distance_target", "load": "load_target"}
_ICU_SPORT = {"run": "Run", "ride": "Ride", "swim": "Swim", "walk": "Walk"}


def _bump(conn: Any, plan_id: str, *, author: str, reason: str, summary: dict[str, Any]) -> int:
    """A new plan version for one change; returns its number."""
    if not (reason or "").strip():
        raise ValueError("a reason is required")
    current = conn.execute(select(plan).where(plan.c.id == plan_id)).mappings().first()
    if not current:
        raise ValueError(f"no plan {plan_id!r}")
    n = current["current_version"] + 1
    conn.execute(
        insert(plan_version).values(
            plan_id=plan_id,
            n=n,
            created_at=utc_now(),
            author=author,
            reason=reason,
            summary=summary,
        )
    )
    conn.execute(update(plan).where(plan.c.id == plan_id).values(current_version=n))
    return n


def set_week(
    engine: Engine, *, week: int, note: str, reason: str, author: str, plan_id: str | None = None
) -> dict[str, Any]:
    """Change the note of one plan week (the week's intent and structure)."""
    plan_id = _required_plan(engine, plan_id)
    with engine.begin() as conn:
        found = conn.execute(
            select(plan_week.c.n).where(plan_week.c.plan_id == plan_id, plan_week.c.n == week)
        ).scalar()
        if found is None:
            raise ValueError(f"no week {week} in plan {plan_id!r}")
        n = _bump(conn, plan_id, author=author, reason=reason, summary={"week": week})
        conn.execute(
            update(plan_week)
            .where(plan_week.c.plan_id == plan_id, plan_week.c.n == week)
            .values(note=(note or "").strip() or None, version=n)
        )
    return {"week": week, "version": n}


def set_target(
    engine: Engine,
    *,
    week: int,
    sport: str,
    reason: str,
    author: str,
    time_s: float | None = None,
    distance_m: float | None = None,
    load: float | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Set (or with no values remove) one weekly target for a sport."""
    plan_id = _required_plan(engine, plan_id)
    sport = (sport or "").strip().lower()
    if not sport:
        raise ValueError("sport is required")
    values = {"time_s": time_s, "distance_m": distance_m, "load": load}
    if any(v is not None and v <= 0 for v in values.values()):
        raise ValueError("targets are positive numbers")
    with engine.begin() as conn:
        if (
            conn.execute(
                select(plan_week.c.n).where(plan_week.c.plan_id == plan_id, plan_week.c.n == week)
            ).scalar()
            is None
        ):
            raise ValueError(f"no week {week} in plan {plan_id!r}")
        n = _bump(conn, plan_id, author=author, reason=reason, summary={"target": [week, sport]})
        key = (plan_target.c.plan_id == plan_id, plan_target.c.week == week)
        conn.execute(delete(plan_target).where(*key, plan_target.c.sport == sport))
        if any(v is not None for v in values.values()):
            conn.execute(
                insert(plan_target).values(
                    plan_id=plan_id, week=week, sport=sport, version=n, **values
                )
            )
    return {"week": week, "sport": sport, "version": n, **values}


def _icu_rows(conn: Any, plan_id: str, kind: str) -> dict[tuple, dict[str, Any]]:
    """intervals.icu notes (kind week) or targets by (week,) or (week, sport)."""
    out: dict[tuple, dict[str, Any]] = {}
    for row in conn.execute(
        select(icu_event).where(icu_event.c.external_id.like(f"{plan_id}:{kind}:%"))
    ).mappings():
        n = _week_number(row["external_id"], plan_id, kind)
        if n is None:
            continue
        parts = row["external_id"].split(":")
        key = (n, parts[3]) if kind == "target" and len(parts) > 3 else (n,)
        out[key] = dict(row)
    return out


def _mirror_weeks(
    engine: Engine, client: Any, plan_id: str, *, dry_run: bool, author: str
) -> dict[str, list]:
    """Week notes and targets whose intervals.icu copy differs from the model."""
    from vibesynergy.query import icu_plan
    from vibesynergy.query.icu_plan import version_token

    out: dict[str, list] = {"updated": [], "created": [], "errors": []}
    with engine.connect() as conn:
        weeks = {
            r["n"]: dict(r)
            for r in conn.execute(
                select(plan_week).where(plan_week.c.plan_id == plan_id)
            ).mappings()
        }
        targets = [
            dict(r)
            for r in conn.execute(
                select(plan_target).where(plan_target.c.plan_id == plan_id)
            ).mappings()
        ]
        notes = _icu_rows(conn, plan_id, "week")
        goals = _icu_rows(conn, plan_id, "target")
    jobs: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] = []
    for n, w in sorted(weeks.items()):
        row = notes.get((n,))
        note = (w["note"] or "").strip()
        if row is not None and (row["description"] or "").strip() == note:
            continue
        name = " · ".join(x for x in (f"Wk {n}", w["phase"], w["kind"], w["hours_label"]) if x)
        jobs.append(
            (
                f"{plan_id}:week:{n:02d}",
                row,
                {"description": note}
                if row is not None
                else {"category": "NOTE", "name": name, "description": note, "day": w["monday"]},
            )
        )
    for t in targets:
        row = goals.get((t["week"], t["sport"]))
        want = {icu: t[mine] for mine, icu in TARGET_FIELDS.items() if t[mine] is not None}
        if row is not None and all((row["targets"] or {}).get(k) == v for k, v in want.items()):
            continue
        monday = weeks.get(t["week"], {}).get("monday")
        if row is None and monday is None:
            continue
        jobs.append(
            (
                f"{plan_id}:target:{t['week']:02d}:{t['sport']}",
                row,
                want
                if row is not None
                else {
                    "category": "TARGET",
                    "name": f"{t['sport'].capitalize()} target",
                    "sport": _ICU_SPORT.get(t["sport"], t["sport"].capitalize()),
                    "targets": want,
                    "day": monday,
                },
            )
        )
    for external, row, change in jobs:
        if dry_run:
            out["updated" if row is not None else "created"].append(external)
            continue
        why = f"plan mirror: {external}"
        try:
            if row is not None:
                icu_plan.update_event(
                    engine,
                    client,
                    reason=why,
                    expected_version=version_token(row["updated"], row["raw"] or {}),
                    changes=change,
                    event_id=row["id"],
                    author=author,
                )
                out["updated"].append(external)
            else:
                icu_plan.create_event(
                    engine,
                    client,
                    reason=why,
                    category=change["category"],
                    start_date=change["day"],
                    name=change["name"],
                    sport=change.get("sport"),
                    description=change.get("description"),
                    external_id=external,
                    for_week=True,
                    tags=[plan_id],
                    targets=change.get("targets"),
                    author=author,
                )
                out["created"].append(external)
        except Exception as exc:  # one failed week must not stop the others
            out["errors"].append({"session_id": external, "error": str(exc)[:300]})
    return out


def _mirror_skipped(
    engine: Engine, client: Any, plan_id: str, *, dry_run: bool, author: str
) -> list[str]:
    """Delete the intervals.icu copy of each skipped session that still has one."""
    from vibesynergy.query import icu_plan
    from vibesynergy.query.icu_plan import version_token

    with engine.connect() as conn:
        rows = conn.execute(
            select(plan_session.c.id, plan_link.c.remote_id, icu_event.c.updated, icu_event.c.raw)
            .join(plan_link, plan_link.c.session_id == plan_session.c.id)
            .join(icu_event, icu_event.c.id == plan_link.c.remote_id, isouter=True)
            .where(
                plan_session.c.plan_id == plan_id,
                plan_session.c.status == SKIPPED,
                plan_link.c.target == "intervals",
            )
        ).all()
    removed = []
    for session_id, remote_id, updated, raw in rows:
        if dry_run:
            removed.append(session_id)
            continue
        if raw is not None:  # still in the cache: delete it there
            try:
                icu_plan.delete_event(
                    engine,
                    client,
                    reason=f"plan mirror: {session_id} skipped",
                    expected_version=version_token(updated, raw or {}),
                    event_id=remote_id,
                    author=author,
                )
            except Exception as exc:  # retried on the next run
                log.warning("could not remove %s from intervals.icu: %s", session_id, exc)
                continue
        with engine.begin() as conn:
            conn.execute(
                delete(plan_link).where(
                    plan_link.c.session_id == session_id, plan_link.c.target == "intervals"
                )
            )
        removed.append(session_id)
    return removed


def mirror(
    engine: Engine,
    client: Any,
    *,
    plan_id: str | None = None,
    dry_run: bool = False,
    author: str = "VibeSynergy",
) -> dict[str, Any]:
    """One-way: write every session, week note and weekly target whose intervals.icu copy
    differs from the model."""
    plan_id = _required_plan(engine, plan_id)
    from vibesynergy.query import icu_plan

    result: dict[str, Any] = {
        "created": [],
        "updated": [],
        "removed": [],
        "unchanged": 0,
        "errors": [],
    }
    drifted = {d["session_id"] for d in drift(engine, plan_id)}
    with engine.connect() as conn:
        sessions = [
            dict(r)
            for r in conn.execute(
                select(plan_session).where(
                    plan_session.c.plan_id == plan_id, plan_session.c.status == "planned"
                )
            ).mappings()
        ]
        links = {
            r.session_id: r
            for r in conn.execute(select(plan_link).where(plan_link.c.target == "intervals"))
        }
    for s in sessions:
        text = session_text(s)
        fp = fingerprint(s["date"], s["name"], s["sport"], text)
        link = links.get(s["id"])
        if link and link.fingerprint == fp and s["id"] not in drifted:
            result["unchanged"] += 1
            continue
        if dry_run:
            result["updated" if link and link.remote_id else "created"].append(s["id"])
            continue
        why = f"plan mirror: {s['name']}"
        try:
            if link and link.remote_id:
                found = icu_plan.list_events(engine, s["date"], s["date"], "WORKOUT")
                current = next((e for e in found if e["event_id"] == link.remote_id), None)
                if current is None:  # moved: look it up by id across its old date
                    with engine.connect() as conn:
                        day = conn.execute(
                            select(icu_event.c.date).where(icu_event.c.id == link.remote_id)
                        ).scalar()
                    found = icu_plan.list_events(engine, day, day, "WORKOUT") if day else []
                    current = next((e for e in found if e["event_id"] == link.remote_id), None)
                if current is None:
                    raise RuntimeError("intervals.icu event not in the cache")
                done = icu_plan.update_event(
                    engine,
                    client,
                    reason=why,
                    expected_version=current["version"],
                    changes={
                        "name": s["name"],
                        "description": text,
                        "type": s["sport"],
                        "start_date_local": f"{s['date'].isoformat()}T00:00:00",
                    },
                    event_id=link.remote_id,
                    author=author,
                )
                result["updated"].append(s["id"])
            else:
                external = s["id"] if ":icu:" not in s["id"] else None
                done = icu_plan.create_event(
                    engine,
                    client,
                    reason=why,
                    category="WORKOUT",
                    start_date=s["date"],
                    name=s["name"],
                    sport=s["sport"],
                    description=text,
                    external_id=external,
                    tags=[plan_id],
                    author=author,
                )
                result["created"].append(s["id"])
        except Exception as exc:  # one failed session must not stop the others
            result["errors"].append({"session_id": s["id"], "error": str(exc)[:300]})
            continue
        with engine.begin() as conn:
            conn.execute(
                delete(plan_link).where(
                    plan_link.c.session_id == s["id"], plan_link.c.target == "intervals"
                )
            )
            conn.execute(
                insert(plan_link).values(
                    session_id=s["id"],
                    target="intervals",
                    remote_id=str(done.get("event_id")),
                    remote_version=done.get("version"),
                    fingerprint=fp,
                    synced_at=utc_now(),
                )
            )
    result["removed"] = _mirror_skipped(engine, client, plan_id, dry_run=dry_run, author=author)
    weekly = _mirror_weeks(engine, client, plan_id, dry_run=dry_run, author=author)
    result["created"] += weekly["created"]
    result["updated"] += weekly["updated"]
    result["errors"] += weekly["errors"]
    result["drift"] = sorted(drifted)
    return result


def view(engine: Engine, start: date, end: date, plan_id: str | None = None) -> dict[str, Any]:
    """Who leads, the version, and the sessions in the window with their step text."""
    plan_id = plan_id or current_plan_id(engine) or ""
    with engine.connect() as conn:
        row = conn.execute(select(plan).where(plan.c.id == plan_id)).mappings().first()
        sessions = [
            dict(r)
            for r in conn.execute(
                select(plan_session)
                .where(plan_session.c.plan_id == plan_id, plan_session.c.date.between(start, end))
                .order_by(plan_session.c.date, plan_session.c.id)
            ).mappings()
        ]
        weeks = [
            dict(r)
            for r in conn.execute(
                select(plan_week)
                .where(
                    plan_week.c.plan_id == plan_id,
                    plan_week.c.monday.between(start - timedelta(days=6), end),
                )
                .order_by(plan_week.c.n)
            ).mappings()
        ]
        targets = [
            dict(r)
            for r in conn.execute(
                select(plan_target).where(
                    plan_target.c.plan_id == plan_id,
                    plan_target.c.week.in_([w["n"] for w in weeks]),
                )
            ).mappings()
        ]
    return {
        "plan_id": plan_id,
        # The weeks the window touches, with their note and targets (COACH-5).
        "weeks": [
            {
                "week": w["n"],
                "monday": w["monday"].isoformat(),
                "phase": w["phase"],
                "kind": w["kind"],
                "hours": w["hours_label"],
                "note": w["note"],
                "targets": [
                    {k: t[k] for k in ("sport", "time_s", "distance_m", "load")}
                    for t in targets
                    if t["week"] == w["n"]
                ],
            }
            for w in weeks
        ],
        "source": "vibesynergy" if row and row["status"] == LEADING else "intervals",
        "version": row["current_version"] if row else None,
        "sessions": [
            {
                "session_id": s["id"],
                "date": s["date"].isoformat(),
                "name": s["name"],
                "sport": s["sport"],
                "status": s["status"],
                "description": session_text(s),
            }
            for s in sessions
        ],
    }


# Export: the plan as a table, readable without VibeSynergy ----------------------------

EXPORT_COLUMNS: list[str] = ["date", "week", "phase", "week_kind", "sport", "name", "workout"]


def export_rows(engine: Engine, plan_id: str | None = None) -> list[dict[str, Any]]:
    """Every week note and planned session, in date order: one row each. A week note's row
    is on the week's Monday with sport empty; a session's row carries its step text."""
    plan_id = plan_id or current_plan_id(engine) or ""
    with engine.connect() as conn:
        weeks = [
            dict(r)
            for r in conn.execute(
                select(plan_week).where(plan_week.c.plan_id == plan_id).order_by(plan_week.c.n)
            ).mappings()
        ]
        sessions = [
            dict(r)
            for r in conn.execute(
                select(plan_session)
                .where(plan_session.c.plan_id == plan_id, plan_session.c.status == "planned")
                .order_by(plan_session.c.date, plan_session.c.id)
            ).mappings()
        ]
    by_monday = {w["monday"]: w for w in weeks}

    def week_of(day: date) -> dict[str, Any]:
        return by_monday.get(day - timedelta(days=day.weekday()), {})

    rows = [
        {
            "date": w["monday"].isoformat(),
            "week": w["n"],
            "phase": w["phase"] or "",
            "week_kind": w["kind"] or "",
            "sport": "",
            "name": " · ".join(x for x in (f"Wk {w['n']}", w["hours_label"]) if x),
            "workout": w["note"] or "",
        }
        for w in weeks
    ]
    for s in sessions:
        w = week_of(s["date"])
        rows.append(
            {
                "date": s["date"].isoformat(),
                "week": w.get("n", ""),
                "phase": w.get("phase") or "",
                "week_kind": w.get("kind") or "",
                "sport": s["sport"] or "",
                "name": s["name"] or "",
                "workout": session_text(s),
            }
        )
    rows.sort(key=lambda r: (r["date"], r["sport"] != "", r["name"]))
    return rows


def export_csv(engine: Engine, plan_id: str | None = None) -> str:
    """The export as CSV with a byte-order mark, so Excel and Numbers read it as UTF-8."""
    import csv
    import io

    out = io.StringIO()
    out.write("﻿")
    writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(export_rows(engine, plan_id))
    return out.getvalue()
