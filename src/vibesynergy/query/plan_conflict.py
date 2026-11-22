"""Another app keeps overwriting the plan in a mirror (#110).

VibeSynergy is the only source of the plan; intervals.icu, Garmin and the calendar only
copy it. When another app or device changes a copy, the mirror puts VibeSynergy's version
back (intervals.icu: the sessions and the races and events; Garmin: a delivered workout
that was removed there). The calendar is never put back: an outside edit stays as it is
(``calendar_mirror``), but it is recorded here too, so the same alert names it.

Each put-back is a row in ``mirror_putback`` (``record``). A **fight** is a streak of
put-backs in one mirror, each within ``hours`` of the one before, in which the same item was
put back ``repeats`` times or more, or the same known writer was behind ``repeats`` of them.
A single put-back (the athlete changing a session by hand in intervals.icu) is no fight.

After ``stop_after`` put-backs in such a streak the mirror stops putting back
(``mirror_hold``): VibeSynergy's own changes still go out, but what the other app changed is
left until the athlete confirms (``resume``) under Waiting for you. Their confirmation also
starts a new count.

The numbers are operational settings, not coaching values: ``[alerts]
plan_conflict_repeats``, ``plan_conflict_hours`` and ``plan_conflict_stop_after``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, insert, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import mirror_hold, mirror_putback

log = logging.getLogger(__name__)

INTERVALS = "intervals"
GARMIN = "garmin"
CALENDAR = "calendar"
MIRRORS = (INTERVALS, GARMIN, CALENDAR)
# The mirror's name as the athlete knows it (translated where it is shown).
LABELS = {INTERVALS: "intervals.icu", GARMIN: "Garmin", CALENDAR: "your calendar"}
KEEP_DAYS = 30  # put-backs older than this are dropped
NAMES_SHOWN = 4  # sessions named in the alert; the rest as "and N more"

REPEATS = 2  # the same session put back this often in one streak is a fight
HOURS = 3.0  # put-backs further apart than this start a new streak
STOP_AFTER = 3  # after this many put-backs in a fight, stop until the athlete confirms


@dataclass(frozen=True)
class Limits:
    repeats: int = REPEATS
    hours: float = HOURS
    stop_after: int = STOP_AFTER

    @property
    def window(self) -> timedelta:
        return timedelta(hours=self.hours)


def _number(value: Any, default: float, low: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= low else default


def limits(settings: Any = None) -> Limits:
    """The thresholds from ``[alerts]``, each with its default when absent or invalid."""
    table = getattr(settings, "alerts", None) or {}
    repeats = int(_number(table.get("plan_conflict_repeats"), REPEATS, 2))
    hours = _number(table.get("plan_conflict_hours"), HOURS, 0.25)
    stop_after = int(_number(table.get("plan_conflict_stop_after"), STOP_AFTER, 1))
    return Limits(repeats=repeats, hours=hours, stop_after=max(stop_after, repeats))


def _aware(moment: datetime | None) -> datetime | None:
    from datetime import UTC

    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _iso(moment: datetime | None) -> str | None:
    found = _aware(moment)
    return found.isoformat() if found else None


def held(engine: Engine, mirror: str) -> bool:
    """The mirror stopped putting back and waits for the athlete."""
    with engine.connect() as conn:
        return (
            conn.execute(
                select(mirror_hold.c.held_at).where(mirror_hold.c.mirror == mirror)
            ).scalar()
            is not None
        )


def _reset_at(conn: Any, mirror: str) -> datetime | None:
    return _aware(
        conn.execute(select(mirror_hold.c.reset_at).where(mirror_hold.c.mirror == mirror)).scalar()
    )


def _streak(rows: list[dict[str, Any]], window: timedelta) -> list[list[dict[str, Any]]]:
    """The latest chain of put-back runs, newest first, each within ``window`` of the next."""
    runs: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        runs.setdefault(row["run"], []).append(row)
    ordered = sorted(runs.values(), key=lambda items: max(r["at"] for r in items), reverse=True)
    chain: list[list[dict[str, Any]]] = []
    for items in ordered:
        at = max(r["at"] for r in items)
        if chain and min(r["at"] for r in chain[-1]) - at > window:
            break
        chain.append(items)
    return chain


def _is_fight(chain: list[list[dict[str, Any]]], repeats: int) -> bool:
    per_item: dict[str, int] = {}
    per_writer: dict[str, int] = {}
    for items in chain:
        for item in {r["item"] for r in items}:
            per_item[item] = per_item.get(item, 0) + 1
        for writer in {r["writer"] for r in items if r["writer"]}:
            per_writer[writer] = per_writer.get(writer, 0) + 1
    return any(n >= repeats for n in [*per_item.values(), *per_writer.values()])


def _rows(conn: Any, mirror: str) -> list[dict[str, Any]]:
    reset = _reset_at(conn, mirror)
    query = select(mirror_putback).where(mirror_putback.c.mirror == mirror)
    if reset is not None:
        query = query.where(mirror_putback.c.at > reset)
    out = []
    for r in conn.execute(query.order_by(mirror_putback.c.id)).mappings():
        row = dict(r)
        row["at"] = _aware(row["at"])
        row["changed_at"] = _aware(row["changed_at"])
        out.append(row)
    return out


def record(
    engine: Engine,
    mirror: str,
    items: list[dict[str, Any]],
    *,
    run: str | None = None,
    put_back: bool = True,
    rules: Limits | None = None,
    now: datetime | None = None,
) -> bool:
    """Note the copies another app changed in one mirror run (``item``, ``name``, and where
    known ``changed_at`` and ``writer``). Returns whether the mirror is held now: after
    ``stop_after`` put-backs in a fight it stops putting back. Never raises."""
    if not items:
        return False
    rules = rules or Limits()
    now = now or utc_now()
    run = run or now.isoformat(timespec="microseconds")
    try:
        with engine.begin() as conn:
            conn.execute(
                delete(mirror_putback).where(mirror_putback.c.at < now - timedelta(days=KEEP_DAYS))
            )
            for item in items:
                conn.execute(
                    insert(mirror_putback).values(
                        mirror=mirror,
                        item=str(item["item"]),
                        name=item.get("name"),
                        run=run,
                        at=now,
                        changed_at=item.get("changed_at"),
                        writer=item.get("writer"),
                        put_back=put_back,
                    )
                )
            if not put_back:
                return False
            hold = conn.execute(select(mirror_hold).where(mirror_hold.c.mirror == mirror)).first()
            if hold is not None and hold.held_at is not None:
                return True
            chain = _streak(_rows(conn, mirror), rules.window)
            if len(chain) < rules.stop_after or not _is_fight(chain, rules.repeats):
                return False
            if hold is None:
                conn.execute(insert(mirror_hold).values(mirror=mirror, held_at=now))
            else:
                conn.execute(
                    update(mirror_hold).where(mirror_hold.c.mirror == mirror).values(held_at=now)
                )
            log.warning(
                "plan: %s keeps being changed by another app; stopped putting it back after %s",
                mirror,
                len(chain),
            )
            return True
    except Exception:  # noting a fight must never stop a mirror
        log.warning("recording a put-back in %s failed", mirror, exc_info=True)
        return False


def resume(engine: Engine, mirror: str, *, by: str = "athlete") -> dict[str, Any]:
    """The athlete confirms: the mirror puts the plan back again, and counts anew."""
    if mirror not in MIRRORS:
        raise ValueError(f"unknown mirror {mirror!r}")
    now = utc_now()
    with engine.begin() as conn:
        found = conn.execute(select(mirror_hold).where(mirror_hold.c.mirror == mirror)).first()
        values = {"held_at": None, "reset_at": now, "reset_by": by}
        if found is None:
            conn.execute(insert(mirror_hold).values(mirror=mirror, **values))
        else:
            conn.execute(update(mirror_hold).where(mirror_hold.c.mirror == mirror).values(**values))
    return {"mirror": mirror, "held": False, "resumed_at": now.isoformat()}


def status(
    engine: Engine, rules: Limits | None = None, now: datetime | None = None
) -> list[dict[str, Any]]:
    """The mirrors in a fight now (the latest put-back within ``hours``) or held: which
    sessions, since when, how many put-backs, and the writer when known."""
    rules = rules or Limits()
    now = now or utc_now()
    out = []
    with engine.connect() as conn:
        holds = {r.mirror: r.held_at for r in conn.execute(select(mirror_hold)) if r.held_at}
        for mirror in MIRRORS:
            chain = _streak(_rows(conn, mirror), rules.window)
            is_held = mirror in holds
            if not chain:
                continue
            last = max(r["at"] for r in chain[0])
            if not is_held and (now - last > rules.window or not _is_fight(chain, rules.repeats)):
                continue
            rows = [r for items in reversed(chain) for r in items]
            names: list[str] = []
            for r in rows:
                name = r["name"] or r["item"]
                if name not in names:
                    names.append(name)
            writers = [r["writer"] for r in rows if r["writer"]]
            since = min((r["changed_at"] or r["at"]) for r in rows)
            out.append(
                {
                    "mirror": mirror,
                    "held": is_held,
                    "held_at": _iso(holds.get(mirror)),
                    "put_back": any(r["put_back"] for r in rows),
                    "since": since.isoformat(),
                    "last": last.isoformat(),
                    "count": len(chain),
                    "names": names,
                    "writer": max(set(writers), key=writers.count) if writers else None,
                }
            )
    return out


ICU_APP = "icu-app:"  # an app connected to intervals.icu, by its number there
ICU_USER = "icu-user:"  # another intervals.icu account (a coach), by its id
CALENDAR_APP = "calendar-app:"  # the app that wrote a calendar item (its PRODID)


def calendar_writer(prodid: Any, own: str) -> str | None:
    """The app named in a calendar item's PRODID, unless it is VibeSynergy's own."""
    text = str(prodid or "").strip()
    if not text or text == own:
        return None
    parts = [p.strip() for p in text.split("//") if p.strip() not in ("", "-", "EN", "NL")]
    name = " ".join(parts)[:80]
    return f"{CALENDAR_APP}{name}" if name else None


def icu_writer(raw: dict[str, Any] | None) -> str | None:
    """Who changed an intervals.icu event, where the event says: an app connected over
    OAuth, or another account (a coach). VibeSynergy itself writes with the API key."""
    raw = raw or {}
    if raw.get("oauth_client_id"):
        return f"{ICU_APP}{raw['oauth_client_id']}"
    creator, athlete = raw.get("created_by_id"), raw.get("athlete_id")
    if creator and athlete and str(creator) != str(athlete):
        return f"{ICU_USER}{creator}"
    return None


def writer_text(writer: str, tr: Any) -> str:
    if writer.startswith(ICU_APP):
        return tr("the app with number {id} in intervals.icu", id=writer.removeprefix(ICU_APP))
    if writer.startswith(ICU_USER):
        return tr("the intervals.icu account {id}", id=writer.removeprefix(ICU_USER))
    return writer.removeprefix(CALENDAR_APP)


def names_text(names: list[str], tr: Any) -> str:
    """The first sessions by name, then "and N more"."""
    shown = ", ".join(names[:NAMES_SHOWN])
    rest = len(names) - NAMES_SHOWN
    return tr("{names} and {n} more", names=shown, n=rest) if rest > 0 else shown


def alert_text(item: dict[str, Any], tr: Any, timezone: str = "UTC") -> str:
    """What the alert says: the mirror, the sessions, since when, the writer when known, and
    whether VibeSynergy stopped putting the plan back."""
    from vibesynergy.core.user_datetime import format_user_datetime

    since = format_user_datetime(datetime.fromisoformat(item["since"]), timezone)
    where = tr(LABELS.get(item["mirror"], item["mirror"]))
    names = names_text(item["names"], tr)
    head = tr(
        "Another app or device keeps changing your plan in {mirror} (since {since}): {names}.",
        mirror=where,
        since=since,
        names=names,
    )
    parts = [head]
    if item.get("writer"):
        parts.append(tr("It was written by {writer}.", writer=writer_text(item["writer"], tr)))
    if item["mirror"] == CALENDAR:
        parts.append(tr("VibeSynergy leaves calendar items as they are, so nothing was put back."))
    elif item["held"]:
        parts.append(
            tr(
                "VibeSynergy put your plan back {n} times and has now stopped. Stop the other "
                "app, then confirm under Waiting for you to put your plan back.",
                n=item["count"],
            )
        )
    else:
        parts.append(
            tr(
                "VibeSynergy put your plan back {n} times. Check which app writes there.",
                n=item["count"],
            )
        )
    return " ".join(parts)
