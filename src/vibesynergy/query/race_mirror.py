"""VibeSynergy's races and events, copied one way to intervals.icu (#91).

intervals.icu only mirrors them, so it can pass them on to other services: a race as a
``RACE_A/B/C`` event (a race without a priority, and every other event, as a ``NOTE``).
The copy carries the external id ``vs-race:<id>``; the mirror only changes or removes the
copies it made itself. A race linked to an intervals.icu event it did not make (moved in by
migration 0053 or matched by the dedupe) gets no second copy, and that event is left as it
is. A change made in intervals.icu to one of its own copies is put back at the next run and
never read into VibeSynergy. Garmin is never written.

Only races and events that are not over yet are copied; past ones stay as they are. It runs
with the plan mirror (``plan_model.run_mirror``): after a sync and soon after every write.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine, delete, insert, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.i18n import ENGLISH, Translator
from vibesynergy.store.schema import icu_event, plan_link, race

log = logging.getLogger(__name__)

PREFIX = "vs-race:"
TARGET = "intervals"
AUTHOR = "VibeSynergy"  # the plan mirror's name for its copies (plan_model.MIRROR_AUTHOR)
_ICU_SPORT = {"run": "Run", "trail": "Run", "ride": "Ride", "swim": "Swim"}


def external_id(race_id: str) -> str:
    return f"{PREFIX}{race_id}"


def _link_key(race_id: str) -> str:
    return f"race:{race_id}"


def _km(metres: float) -> str:
    value = round(metres / 1000, 3)
    return f"{value:g} km"


def description(r: dict[str, Any], tr: Translator = ENGLISH) -> str:
    """The copy's text: the facts VibeSynergy holds, one per line, then the notes."""
    lines = []
    if r.get("distance_m"):
        lines.append(tr("Distance: {value}", value=_km(float(r["distance_m"]))))
    if r.get("start_time"):
        lines.append(tr("Start: {time}", time=r["start_time"]))
    if r.get("goal_time"):
        lines.append(tr("Goal time: {time}", time=r["goal_time"]))
    if r.get("location"):
        lines.append(tr("Location: {place}", place=r["location"]))
    if r.get("url"):
        lines.append(str(r["url"]))
    if r.get("notes"):
        lines.append(("\n" if lines else "") + str(r["notes"]))
    return "\n".join(lines).strip()


def desired(r: dict[str, Any], tr: Translator = ENGLISH) -> dict[str, Any]:
    """The intervals.icu event a race or event should have."""
    is_race = (r.get("kind") or "race") == "race"
    letter = r.get("priority") if is_race else None
    out: dict[str, Any] = {
        "category": f"RACE_{letter}" if letter in ("A", "B", "C") else "NOTE",
        "name": r["name"],
        "date": r["date"],
        "end": (r["end_date"] + timedelta(days=1)) if r.get("end_date") else None,
        "type": _ICU_SPORT.get(r.get("sport") or ""),
        "description": description(r, tr),
    }
    return out


def fingerprint(want: dict[str, Any]) -> str:
    body = json.dumps(want, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def _cached_shape(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw") or {}
    end = raw.get("end_date_local")
    return {
        "category": row.get("category"),
        "name": row.get("name"),
        "date": row.get("date"),
        "end": date.fromisoformat(str(end)[:10]) if end else None,
        "type": row.get("type") or None,
        "description": (row.get("description") or "").strip(),
    }


def _changes(want: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "category": want["category"],
        "name": want["name"],
        "description": want["description"],
        "start_date_local": f"{want['date'].isoformat()}T00:00:00",
    }
    if want["type"]:
        out["type"] = want["type"]
    if want["end"]:
        out["end_date_local"] = f"{want['end'].isoformat()}T00:00:00"
    return out


def mirror(
    engine: Engine,
    client: Any,
    *,
    today: date | None = None,
    dry_run: bool = False,
    tr: Translator = ENGLISH,
    run: str | None = None,
    rules: Any = None,
) -> dict[str, Any]:
    """Copy every upcoming race and event whose intervals.icu copy differs; remove the copy
    of one that was deleted. One failure never stops the others.

    A copy another app changed is put back and noted in ``plan_conflict`` (#110), with the
    plan's sessions under the same mirror; while it is held, it is left as it is."""
    from vibesynergy.query import icu_plan, plan_conflict
    from vibesynergy.query.icu_plan import version_token

    today = today or date.today()
    run = run or utc_now().isoformat(timespec="microseconds")
    out: dict[str, Any] = {
        "created": [],
        "updated": [],
        "removed": [],
        "unchanged": 0,
        "not_ours": [],
        "errors": [],
        "put_back": [],
        "held": [],
    }
    stopped = not dry_run and plan_conflict.held(engine, plan_conflict.INTERVALS)
    put_back: list[dict[str, Any]] = []
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(select(race)).mappings()]
        links = {
            r.session_id: r
            for r in conn.execute(
                select(plan_link).where(
                    plan_link.c.target == TARGET, plan_link.c.session_id.like("race:%")
                )
            )
        }
        cached = {
            r["id"]: dict(r)
            for r in conn.execute(
                select(icu_event).where(icu_event.c.external_id.like(f"{PREFIX}%"))
            ).mappings()
        }
        foreign = {
            str((r["links"] or {}).get("intervals"))
            for r in rows
            if (r["links"] or {}).get("intervals")
        }
        foreign_rows = {
            r["id"]: dict(r)
            for r in conn.execute(
                select(icu_event).where(icu_event.c.id.in_(foreign or {""}))
            ).mappings()
        }
    by_external = {r["external_id"]: r for r in cached.values()}
    for r in rows:
        ident = r["id"]
        last = r.get("end_date") or r["date"]
        if last < today:
            continue  # over: its copy stays as it was
        link = links.get(_link_key(ident))
        copy = by_external.get(external_id(ident))
        if copy is None and link is not None and link.remote_id in cached:
            copy = cached[link.remote_id]
        linked = str((r["links"] or {}).get("intervals") or "")
        if copy is None and linked and linked in foreign_rows and linked not in cached:
            # The athlete's own intervals.icu event (migration or dedupe): no second copy,
            # and never changed or removed by VibeSynergy.
            out["not_ours"].append(ident)
            continue
        try:
            if r["deleted_at"] is not None:
                if copy is None:
                    if link is not None:
                        _forget(engine, ident)
                    continue
                if not dry_run:
                    icu_plan.delete_event(
                        engine,
                        client,
                        reason=f"race mirror: {r['name']} deleted",
                        expected_version=version_token(copy["updated"], copy["raw"] or {}),
                        event_id=copy["id"],
                        author=AUTHOR,
                        mirror_run=run,
                    )
                    _forget(engine, ident)
                out["removed"].append(ident)
                continue
            want = desired(r, tr)
            fp = fingerprint(want)
            if copy is not None and _cached_shape(copy) == want:
                if (link is None or link.fingerprint != fp) and not dry_run:
                    _remember(engine, ident, copy["id"], None, fp)
                out["unchanged"] += 1
                continue
            # The copy is as VibeSynergy last wrote it, yet differs: another app changed it.
            restoring = copy is not None and link is not None and link.fingerprint == fp
            if restoring and stopped:
                out["held"].append(ident)
                continue
            if dry_run:
                out["updated" if copy is not None else "created"].append(ident)
                continue
            if copy is not None:
                done = icu_plan.update_event(
                    engine,
                    client,
                    reason=f"race mirror: {r['name']}",
                    expected_version=version_token(copy["updated"], copy["raw"] or {}),
                    changes=_changes(want),
                    event_id=copy["id"],
                    author=AUTHOR,
                    mirror_run=run,
                )
                out["updated"].append(ident)
                if restoring:
                    put_back.append(
                        {
                            "item": _link_key(ident),
                            "name": r["name"],
                            "changed_at": copy.get("updated"),
                            "writer": plan_conflict.icu_writer(copy.get("raw")),
                        }
                    )
            else:
                done = icu_plan.create_event(
                    engine,
                    client,
                    reason=f"race mirror: {r['name']}",
                    category=want["category"],
                    start_date=want["date"],
                    end_date=want["end"],
                    name=want["name"],
                    sport=want["type"],
                    description=want["description"] or None,
                    external_id=external_id(ident),
                    author=AUTHOR,
                    mirror_run=run,
                )
                out["created"].append(ident)
            _remember(engine, ident, str(done["event_id"]), done.get("version"), fp)
        except Exception as exc:  # one failed race must not stop the others
            log.warning("race mirror: %s failed: %s", ident, exc)
            out["errors"].append({"race_id": ident, "error": str(exc)[:300]})
    out["put_back"] = [p["item"].removeprefix("race:") for p in put_back]
    if put_back:
        plan_conflict.record(engine, plan_conflict.INTERVALS, put_back, run=run, rules=rules)
    return out


def _remember(engine: Engine, race_id: str, remote_id: str, version: str | None, fp: str) -> None:
    """The copy's id and fingerprint (``plan_link``), and its id on the race's links."""
    key = _link_key(race_id)
    with engine.begin() as conn:
        conn.execute(
            delete(plan_link).where(plan_link.c.session_id == key, plan_link.c.target == TARGET)
        )
        conn.execute(
            insert(plan_link).values(
                session_id=key,
                target=TARGET,
                remote_id=remote_id,
                remote_version=version,
                fingerprint=fp,
                synced_at=utc_now(),
            )
        )
        links = conn.execute(select(race.c.links).where(race.c.id == race_id)).scalar() or {}
        if links.get("intervals") != remote_id:
            conn.execute(
                update(race)
                .where(race.c.id == race_id)
                .values(links={**links, "intervals": remote_id})
            )


def _forget(engine: Engine, race_id: str) -> None:
    key = _link_key(race_id)
    with engine.begin() as conn:
        conn.execute(
            delete(plan_link).where(plan_link.c.session_id == key, plan_link.c.target == TARGET)
        )
        links = conn.execute(select(race.c.links).where(race.c.id == race_id)).scalar() or {}
        if "intervals" in links:
            conn.execute(
                update(race)
                .where(race.c.id == race_id)
                .values(links={k: v for k, v in links.items() if k != "intervals"})
            )
