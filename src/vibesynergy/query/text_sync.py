"""Two-way sync of a published workout's name and public text: VS ↔ Strava ↔ Garmin (#114).

The same check also keeps the workout's gear in step (``gear_sync``, phase 5): one switch,
one watch, one Strava read per check.

Phase 3 of #114. Off until Settings → Publishing and sync switches it on
(``[publication] text_sync_enabled = true``); while off nothing here reads or writes a
platform, and publication works exactly as before.

Since #175 this is the only path that writes a workout's name and public text to Garmin
and reads Strava's back: the old Strava → Garmin copy (``publication_mirror``) is gone.
Publishing puts VibeSynergy's section on every place under "Publish to"
(``publication.targets``); this sync only works with those places.

**What is synced.** For a *published* workout (a verified VibeSynergy section on its Strava
or Garmin copy): its **name** and the athlete's own **public text**. A description becomes
the public text, then VibeSynergy's block (the AI-coach insight and the statistics, written by
``publication``). The private text and the AI coach's private comments never go out.
intervals.icu takes no part (#160).

**Three-way compare, per platform and field.** The three sides are VibeSynergy's current
text (``workout_texts``), the last text VibeSynergy wrote to that platform or agreed with
(``text_sync.synced``) and what the platform has now (a live read):

- platform = last-written: no outside change; VibeSynergy's text is pushed when it differs;
- platform ≠ last-written: an outside change; it is stored in VibeSynergy as a new version
  (source ``strava`` or ``garmin``) and then pushed to the other platform;
- both changed (or both platforms): the latest change wins; every other text is kept in the
  history as a version flagged "overwritten, from <source>".

The time of an outside change is the first time VibeSynergy saw it: in the copy the normal
sync stored (``noticed``), else the read that found it. Platforms do not say when a text was
typed. On a tie the platform wins over VibeSynergy, and Strava over Garmin.

**First sync with a platform** (no last-written text yet). The public text: the paragraphs
the platform has that VibeSynergy does not are added to VibeSynergy's text (source: that
platform), then the result is pushed, so nothing is lost and nothing doubled. The name: the
name you gave in VibeSynergy wins; without one the Strava name is taken, else the Garmin
name.

**No echo.** A platform text equal to the last-written one is never an outside change, so a
push of VibeSynergy's own comes back as "nothing changed". Compared without differences in
spaces and newlines.

**Guard (#161).** Right before a write the platform is read again; when its name or
description changed since the read the decision was made on, nothing is written and the
check runs again (once now, else on the next run). VibeSynergy only replaces its own block and
only puts the public text above it; a description with a hand-edited, doubled or unverified
VibeSynergy block is left for review. Every write is read back.

**When.** A change in VibeSynergy (``touch``), or a changed copy stored by the normal sync
(Strava activity update or webhook, Garmin activity details), is checked after ``DEBOUNCE``.
Strava does not tell about a changed description, so a published workout is also read
every ``FAST`` for its first 48 hours and every ``SLOW`` until ``WATCH`` after publishing. All
Strava calls go through the same client and request budget as the sync; Strava's rate limit
ends a run and the rest waits.

**Strava visibility.** Strava's API takes ``visibility`` on an update only to make an activity
*more* restrictive (followers only, only me); it cannot make one public. VibeSynergy therefore
never changes the visibility: publishing makes the text public, and who can see the activity
on Strava stays set on Strava (the workout page says so when it is private there).

**Multisport.** A multisport holder is synced with its own Garmin multisport activity only:
Strava gets its legs as separate activities, so which one is "the" Strava copy is not clear.
The legs are workouts of their own and sync like any other. With several Garmin copies
(Zwift's upload and the watch) only the copy publication writes to is synced.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.errors import RateLimited
from vibesynergy.core.secrets import SecretStore
from vibesynergy.store.schema import (
    activity,
    activity_link,
    activity_unified,
    publication_ledger,
    text_sync,
)

log = logging.getLogger(__name__)

JOB = "text_sync"
FIELDS: tuple[str, ...] = ("name", "public")
PLATFORMS: tuple[str, ...] = ("strava", "garmin")
DEBOUNCE = timedelta(minutes=5)  # as agent_events: a few minutes of quiet before a push
WATCH = timedelta(days=7)
FAST = timedelta(hours=2)
FAST_FOR = timedelta(hours=48)
SLOW = timedelta(hours=6)
RETRY = timedelta(minutes=30)
MAX_PER_RUN = 10
# A tie in time goes to the platform (newer to VibeSynergy), and to Strava before Garmin.
_RANK = {"strava": 2, "garmin": 1, "vs": 0, "ai": 0}


class Changed(Exception):
    """The platform changed between the read and the write (#161): nothing was written."""


class Review(Exception):
    """The description needs a look (a hand-edited or unverified VibeSynergy block)."""


# --- setting and small helpers ----------------------------------------------------------


def enabled(rt) -> bool:
    """Settings → Publishing and sync → "Keep name, texts and gear the same everywhere" (the
    gear follows the same switch, ``gear_sync``)."""
    from vibesynergy.core.config import load_settings

    return load_settings(rt.settings.config_path).publication.get("text_sync_enabled") is True


def _key(text: str | None) -> str:
    """Compared without differences in spaces and newlines."""
    return " ".join((text or "").split())


def _hash(text: str | None) -> str:
    return hashlib.sha256(_key(text).encode()).hexdigest()[:16]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _when(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(str(raw)))
    except ValueError:
        return None


def _public(description: Any) -> str:
    """The platform's text outside VibeSynergy's block: the public text it holds."""
    from vibesynergy.query import publication

    return publication.human_text(description if isinstance(description, str) else "")


def _remote(platform: str, detail: dict) -> dict[str, str]:
    name = detail.get("name" if platform == "strava" else "activityName")
    description = detail.get("description")
    description = description if isinstance(description, str) else ""
    return {
        "name": " ".join(str(name or "").split()),
        "public": _public(description),
        "description": description,
    }


def _row(engine: Engine, group: str) -> dict | None:
    with engine.connect() as conn:
        found = (
            conn.execute(select(text_sync).where(text_sync.c.group_id == group)).mappings().first()
        )
    return dict(found) if found else None


def _update(engine: Engine, group: str, **values: Any) -> None:
    with engine.begin() as conn:
        conn.execute(
            text_sync.update()
            .where(text_sync.c.group_id == group)
            .values(**values, updated_at=datetime.now(UTC))
        )


def _published_groups(conn, refs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    if not refs:
        return set()
    rows = conn.execute(
        select(publication_ledger.c.source, publication_ledger.c.source_id).where(
            publication_ledger.c.state == "verified",
            publication_ledger.c.source.in_(PLATFORMS),
            publication_ledger.c.source_id.in_([i for _, i in refs]),
        )
    ).all()
    return {(s, i) for s, i in rows} & set(refs)


# --- which copies ------------------------------------------------------------------------


def targets(rt, group: str) -> dict[str, str]:
    """The one Strava and the one Garmin copy the workout's texts are synced with: only the
    places under "Publish to" (#175)."""
    from vibesynergy.query import multisport, publication, publication_format, source_removed

    chosen = publication.targets(rt)

    with rt.engine.connect() as conn:
        row = (
            conn.execute(
                select(activity_unified.c.sources, activity_unified.c.sport).where(
                    activity_unified.c.group_id == group
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return {}
        refs = source_removed.live(conn, row["sources"])
    out: dict[str, str] = {}
    strava = [r.split(":", 1)[1] for r in refs if r.startswith("strava:")]
    garmin = [r.split(":", 1)[1] for r in refs if r.startswith("garmin:")]
    holder = str(row["sport"] or "").lower() in multisport.MULTI
    if len(strava) == 1 and not holder and "strava" in chosen:
        out["strava"] = strava[0]
    if len(garmin) > 1:  # Zwift's upload and the watch: the copy publication writes to
        garmin = [g for g in [publication_format.copy_id(rt, group, "garmin")] if g]
    if len(garmin) == 1 and "garmin" in chosen:
        out["garmin"] = garmin[0]
    return out


def published(rt, found: dict[str, str]) -> bool:
    """A verified VibeSynergy section on the Strava or the Garmin copy."""
    with rt.engine.connect() as conn:
        return bool(_published_groups(conn, list(found.items())))


# --- scheduling --------------------------------------------------------------------------


def _group_of(engine: Engine, activity_id: str) -> str:
    from vibesynergy.query import views

    return views.resolve_activity(engine, activity_id)


def enroll(rt, activity_id: str, *, now: datetime | None = None) -> dict:
    """Start (or restart) syncing a published workout: checked now, then watched."""
    now = now or datetime.now(UTC)
    group = _group_of(rt.engine, activity_id)
    found = targets(rt, group)
    old = _row(rt.engine, group)
    values = {
        "state": "active",
        "strava_id": found.get("strava"),
        "garmin_id": found.get("garmin"),
        "watch_until": now + WATCH,
        "next_check_at": now,
        "last_error": None,
        "updated_at": now,
    }
    with rt.engine.begin() as conn:
        if old is None:
            conn.execute(
                text_sync.insert().values(
                    group_id=group, synced={}, noticed={}, seen_at=now, created_at=now, **values
                )
            )
        else:
            if old["state"] == "active":
                values["watch_until"] = max(_aware(old["watch_until"]) or now, now + WATCH)
            conn.execute(text_sync.update().where(text_sync.c.group_id == group).values(**values))
    return _row(rt.engine, group) or {}


def touch(engine: Engine, group: str, fields: list[str], now: datetime | None = None) -> None:
    """The name or public text changed in VibeSynergy: check the workout after ``DEBOUNCE``
    (each later change moves it on, so a run of edits is one push). A published workout that
    is not followed yet is added. Never fails the save."""
    if not fields:
        return
    now = now or datetime.now(UTC)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(text_sync.c.state).where(text_sync.c.group_id == group)
            ).first()
            if row is not None:
                if row.state == "active":
                    conn.execute(
                        text_sync.update()
                        .where(text_sync.c.group_id == group)
                        .values(next_check_at=now + DEBOUNCE, updated_at=now)
                    )
                return
            refs = [
                (s, i)
                for s, i in conn.execute(
                    select(activity_link.c.source, activity_link.c.source_id).where(
                        activity_link.c.group_id == group
                    )
                )
                if s in PLATFORMS
            ]
            if not _published_groups(conn, refs):
                return
            conn.execute(
                text_sync.insert().values(
                    group_id=group,
                    state="active",
                    synced={},
                    noticed={},
                    watch_until=now + WATCH,
                    next_check_at=now + DEBOUNCE,
                    seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
    except Exception:
        log.exception("could not schedule the text sync of %s", group)


def _unfollowed(conn, now: datetime, table=text_sync) -> set[str]:
    """Workouts published in the last ``WATCH`` that are not followed yet (the sync was just
    switched on, or the automatic run published them). ``table``: whose followers (the gear
    sync uses the same rule)."""
    ledgers = set(
        conn.execute(
            select(publication_ledger.c.source, publication_ledger.c.source_id).where(
                publication_ledger.c.state == "verified",
                publication_ledger.c.source.in_(PLATFORMS),
                publication_ledger.c.updated_at >= now - WATCH,
            )
        ).all()
    )
    if not ledgers:
        return set()
    groups = {
        g
        for s, i, g in conn.execute(
            select(
                activity_link.c.source, activity_link.c.source_id, activity_link.c.group_id
            ).where(activity_link.c.source_id.in_([i for _, i in ledgers]))
        )
        if (s, i) in ledgers
    }
    found = conn.execute(select(table.c.group_id).where(table.c.group_id.in_(groups)))
    known = set(found.scalars())
    return groups - known


def _enroll_recent(rt, now: datetime) -> int:
    with rt.engine.connect() as conn:
        groups = _unfollowed(conn, now)
    for group in sorted(groups):
        enroll(rt, group, now=now)
    return len(groups)


def _observed(rt, rows: list[dict]) -> dict[str, dict[str, dict[str, Any]]]:
    """Per group the copies the normal sync stored after ``seen_at``: platform -> {name,
    public, at}."""
    pairs = {(p, row[f"{p}_id"]): row for row in rows for p in PLATFORMS if row.get(f"{p}_id")}
    if not pairs:
        return {}
    with rt.engine.connect() as conn:
        found = conn.execute(
            select(
                activity.c.source,
                activity.c.source_id,
                activity.c.name,
                activity.c.extra,
                activity.c.updated_at,
            ).where(
                activity.c.source.in_(PLATFORMS),
                activity.c.source_id.in_([i for _, i in pairs]),
            )
        ).all()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for source, source_id, name, extra, updated in found:
        row = pairs.get((source, source_id))
        if row is None:
            continue
        at = _aware(updated)
        if at is None or at <= (_aware(row["seen_at"]) or at):
            continue
        extra = extra if isinstance(extra, dict) else {}
        out.setdefault(row["group_id"], {})[source] = {
            "name": " ".join(str(name or "").split()),
            "public": _public(extra.get("description")),
            "at": at,
        }
    return out


def _notice(rt, now: datetime) -> int:
    """Outside changes the normal sync brought in: remember when each was first seen and
    check the workout after ``DEBOUNCE``."""
    with rt.engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(select(text_sync).where(text_sync.c.state == "active"))
            .mappings()
            .all()
        ]
    seen = _observed(rt, rows)
    noticed_count = 0
    for row in rows:
        copies = seen.get(row["group_id"])
        if not copies:
            continue
        synced = row["synced"] or {}
        noticed = {p: dict(v) for p, v in (row["noticed"] or {}).items()}
        changed = False
        for platform, copy in copies.items():
            last = synced.get(platform) or {}
            for field in FIELDS:
                if field not in last or _key(copy[field]) == _key(last[field]):
                    continue
                mark = noticed.setdefault(platform, {}).get(field) or {}
                if mark.get("hash") != _hash(copy[field]):
                    noticed[platform][field] = {
                        "hash": _hash(copy[field]),
                        "at": _iso(min(copy["at"], now)),
                    }
                changed = True
        values: dict[str, Any] = {"seen_at": now, "noticed": noticed}
        if changed:
            values["next_check_at"] = now + DEBOUNCE
            noticed_count += 1
        _update(rt.engine, row["group_id"], **values)
    return noticed_count


def due(rt, *, now: datetime | None = None) -> bool:
    """Asked on every scheduler tick; never fails it. False while the setting is off."""
    try:
        if not enabled(rt):
            return False
        now = now or datetime.now(UTC)
        with rt.engine.connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(select(text_sync).where(text_sync.c.state == "active"))
                .mappings()
                .all()
            ]
            if _unfollowed(conn, now):
                return True
        if any((_aware(r["next_check_at"]) or now + WATCH) <= now for r in rows):
            return True
        return bool(_observed(rt, rows))
    except Exception:
        log.exception("checking for texts to sync failed")
        return False


def _next(row: dict, now: datetime) -> datetime | None:
    """The next periodic read: Strava does not tell about a changed description."""
    until = _aware(row["watch_until"])
    if until is None or now >= until:
        return None
    created = _aware(row["created_at"]) or now
    return min(now + (FAST if now - created < FAST_FOR else SLOW), until)


# --- the sync of one workout -------------------------------------------------------------


def _clients(rt, found: dict[str, str], given: dict[str, Any] | None) -> dict[str, Any]:
    from vibesynergy.query import publication

    given = given or {}
    return {p: given.get(p) or publication.provider(rt, p) for p in found}


def _vs_edited(engine: Engine, group: str, field: str) -> bool:
    """The current text was written in VibeSynergy (not only taken over from a platform)."""
    from vibesynergy.query import workout_texts

    for version in workout_texts.history(engine, group, field):
        if version["kind"] == workout_texts.OVERWRITTEN:
            continue
        return version["kind"] != "seed" and version["source"] in ("vs", "ai")
    return False


def _vs_time(current: dict) -> datetime:
    return _when(current.get("at")) or datetime(1970, 1, 1, tzinfo=UTC)


def _check_block(rt, platform: str, source_id: str, description: str) -> tuple[str | None, bool]:
    """VibeSynergy's block in ``description`` (None without one) and whether a write may go
    ahead. A block that is not exactly the verified one is left for review."""
    from vibesynergy.query import publication, publication_format

    blocks = publication_format.find_blocks(description)
    if publication_format.orphan_markers(description) or len(blocks) > 1:
        raise Review(f"The {platform} description has a changed VibeSynergy section")
    ledger = publication._ledger(rt, platform, source_id)
    if ledger and ledger["state"] != "verified":
        return None, False  # a publication write or removal is unfinished: later
    if not blocks:
        return None, True
    block = publication_format.block_text(description, blocks[0])
    if not ledger or block != ledger["owned_block"]:
        raise Review(f"The {platform} description has a changed VibeSynergy section")
    return block, True


def _decide(
    rt,
    group: str,
    field: str,
    current: dict,
    remote: dict[str, dict[str, str]],
    synced: dict,
    noticed: dict,
    now: datetime,
) -> tuple[str, dict[str, Any]]:
    """The text every platform should have for ``field``, after taking outside changes in.

    Returns ``(text, info)``; ``info`` says what was taken over and what lost."""
    from vibesynergy.query import workout_texts

    vs_text = current.get("text") or ""
    base = (synced.get("base") or {}).get(field)
    info: dict[str, Any] = {}
    candidates: list[tuple[datetime, int, str, str]] = []  # (time, rank, source, text)
    first: list[str] = []
    for platform, copy in remote.items():
        last = (synced.get(platform) or {}).get(field)
        text = copy[field]
        if last is None:
            first.append(platform)
            continue
        if _key(text) == _key(last) or (field == "name" and not text):
            continue  # no outside change (VibeSynergy's own write reads back as this)
        mark = (noticed.get(platform) or {}).get(field) or {}
        at = _when(mark.get("at")) if mark.get("hash") == _hash(text) else None
        candidates.append((at or now, _RANK[platform], platform, text))
    if candidates and base is not None and _key(vs_text) != _key(base):
        candidates.append((_vs_time(current), _RANK["vs"], "vs", vs_text))
    target = vs_text
    if candidates:
        winner = max(candidates, key=lambda c: (c[0], c[1]))
        lost: list[tuple[str, str]] = []
        for _at, _rank, source, text in candidates:
            if _key(text) != _key(winner[3]) and all(_key(t) != _key(text) for t, _ in lost):
                lost.append((text, source))
        if winner[2] == "vs":
            if lost:
                workout_texts.take_over(
                    rt.engine, group, field, None, source="vs", overwritten=lost
                )
        else:
            workout_texts.take_over(
                rt.engine,
                group,
                field,
                winner[3] if _key(winner[3]) != _key(vs_text) else None,
                source=winner[2],
                overwritten=lost,
            )
            if _key(winner[3]) != _key(vs_text):
                info["taken_from"] = winner[2]
        if lost:
            info["overwritten"] = [source for _, source in lost]
        target = winner[3]
    if first:
        target = _first_sync(rt, group, field, target, remote, first, info)
    return target, info


def _first_sync(
    rt, group: str, field: str, target: str, remote: dict, first: list[str], info: dict
) -> str:
    """A platform without a last-written text: merge, never drop (see the module text)."""
    from vibesynergy.query import workout_texts

    if field == "public":
        seen: set[str] = set()
        workout_texts.unseen(target, seen)
        merged, source = target, None
        for platform in PLATFORMS:
            if platform not in first:
                continue
            extra = workout_texts.unseen(remote[platform]["public"], seen)
            if extra:
                merged = f"{merged}\n\n{extra}" if merged else extra
                source = source or platform
        if source is not None:
            workout_texts.take_over(rt.engine, group, "public", merged, source=source)
            info["taken_from"] = info.get("taken_from") or source
        return merged
    if len(first) < len(remote):
        return target  # a copy that joins later gets the agreed name
    if not target or not _vs_edited(rt.engine, group, "name"):
        for platform in PLATFORMS:  # the Strava name first
            name = remote.get(platform, {}).get("name") if platform in first else None
            if not name:
                continue
            if _key(name) != _key(target):
                # The platform's own name (the device default): no change of yours, so an
                # automatic name may still replace it (``workout_texts.name_lock``).
                workout_texts.take_over(
                    rt.engine, group, "name", name, source=platform, kind=workout_texts.IMPORT
                )
                info["taken_from"] = info.get("taken_from") or platform
            return name
    return target


def _write(
    rt,
    platform: str,
    source_id: str,
    client,
    before: dict[str, str],
    want: dict[str, str],
) -> dict[str, str]:
    """Guarded write of the name and/or public text to one platform; returns what it holds."""
    from vibesynergy.query import publication, publication_format

    lock = SecretStore(rt.settings.secrets_dir, "publication")
    with lock.lock(f"{platform}-{source_id}"):
        fresh = _remote(platform, client.read(source_id))  # the #161 guard
        if _key(fresh["name"]) != _key(before["name"]) or not publication_format.same_text(
            fresh["description"], before["description"]
        ):
            raise Changed(f"{platform} changed since it was read")
        description = fresh["description"]
        if "public" in want:
            block, ok = _check_block(rt, platform, source_id, description)
            if not ok:
                raise Changed(f"a publication on {platform} is unfinished")
            text = want["public"]
            new = f"{text}\n\n{block}" if text and block else (block or text)
            if not publication_format.same_text(new, description):
                client.write(source_id, new)
                description = new
        if "name" in want and _key(want["name"]) != _key(fresh["name"]):
            client.write_title(source_id, want["name"])
        after = _remote(platform, client.read(source_id))
        for field, text in want.items():
            if _key(after[field]) != _key(text):
                raise publication.Conflict(f"{platform} did not return the synced {field}")
        if "public" in want:
            _ledger_description(rt, platform, source_id, after["description"])
        return after


def _ledger_description(rt, platform: str, source_id: str, description: str) -> None:
    """Keep the publication ledger's description in step, so its checks see the new text."""
    with rt.engine.begin() as conn:
        conn.execute(
            publication_ledger.update()
            .where(
                publication_ledger.c.source == platform,
                publication_ledger.c.source_id == source_id,
                publication_ledger.c.state == "verified",
            )
            .values(description=description, updated_at=datetime.now(UTC))
        )


def sync_one(rt, group: str, *, now: datetime | None = None, clients=None) -> dict[str, Any]:
    """One three-way check of one workout; retries once when a platform changed under it."""
    try:
        return _sync_one(rt, group, now=now, clients=clients)
    except Changed:
        log.info("text sync of %s: a platform changed during the check; checking again", group)
        return _sync_one(rt, group, now=now, clients=clients)


def _sync_one(rt, group: str, *, now: datetime | None = None, clients=None) -> dict[str, Any]:
    from vibesynergy.query import workout_texts

    now = now or datetime.now(UTC)
    row = _row(rt.engine, group)
    if row is None:
        row = enroll(rt, group, now=now)
    found = targets(rt, group)
    _update(rt.engine, group, strava_id=found.get("strava"), garmin_id=found.get("garmin"))
    if not found or not published(rt, found):
        _update(rt.engine, group, state="stopped", next_check_at=None)
        return {"state": "stopped"}
    live = _clients(rt, found, clients)
    details = {p: live[p].read(found[p]) for p in found}
    remote = {p: _remote(p, details[p]) for p in found}
    for platform, copy in remote.items():
        _check_block(rt, platform, found[platform], copy["description"])
    synced = {k: dict(v) for k, v in (row["synced"] or {}).items()}
    noticed = row["noticed"] or {}
    current = workout_texts.texts(rt.engine, group)
    result: dict[str, Any] = {"state": "checked"}
    want: dict[str, dict[str, str]] = {p: {} for p in found}
    for field in FIELDS:
        target, info = _decide(rt, group, field, current[field], remote, synced, noticed, now)
        if info:
            result[field] = info
        if field == "name" and not target:
            continue  # never an empty name on a platform
        for platform in found:
            if _key(remote[platform][field]) != _key(target):
                want[platform][field] = target
            else:
                synced.setdefault(platform, {})[field] = remote[platform][field]
        synced.setdefault("base", {})[field] = target
    # What is agreed already counts as written: no echo of it later.
    _update(rt.engine, group, synced=synced)
    written: dict[str, list[str]] = {}
    for platform, fields in want.items():
        if not fields:
            continue
        after = _write(rt, platform, found[platform], live[platform], remote[platform], fields)
        entry = synced.setdefault(platform, {})
        for field in fields:
            entry[field] = after[field]
        entry["at"] = _iso(now)
        _update(rt.engine, group, synced=synced)
        written[platform] = sorted(fields)
    if written:
        result["written"] = written
    # The gear, on the same check (#114, phase 5): Strava's gear id is in the read above.
    from vibesynergy.query import gear_sync

    try:
        gear = gear_sync.sync(rt, group, found, live, details, synced, now)
    except (Changed, RateLimited):
        raise  # checked again as a whole, or after the wait
    except Exception as exc:  # the texts are synced; the gear is tried on the next check
        log.warning("gear sync of %s failed: %s", group, exc)
        gear = {"error": str(exc)[:300]}
    if gear:
        result["gear"] = gear
    # Changes taken in are handled: forget when they were first seen.
    _update(rt.engine, group, noticed={}, last_error=None, last_checked_at=now)
    return result


# --- entry points ------------------------------------------------------------------------


def after_publish(rt, activity_id: str, *, clients=None) -> dict[str, Any]:
    """Right after a verified publication (the Publish tick or the automatic run): follow the
    workout and sync its texts now. A failure never undoes the publication."""
    if not enabled(rt):
        return {"state": "off"}
    from vibesynergy.query import publication

    now = datetime.now(UTC)
    try:
        row = enroll(rt, activity_id, now=now)
        group = row["group_id"]
        with publication.group_lock(rt, group):
            result = sync_one(rt, group, now=now, clients=clients)
        _update(
            rt.engine,
            group,
            last_result=result,
            next_check_at=_next(_row(rt.engine, group) or row, now),
        )
        return result
    except RateLimited as exc:
        return {"state": "waiting", "error": str(exc)[:300]}
    except Exception as exc:  # recorded; the next run tries again
        log.warning("text sync after publishing %s failed: %s", activity_id, exc)
        try:
            group = _group_of(rt.engine, activity_id)
            _update(rt.engine, group, last_error=str(exc)[:500], next_check_at=now + RETRY)
        except Exception:
            pass
        return {"state": "failed", "error": str(exc)[:300]}


def run(rt, *, now: datetime | None = None, clients=None) -> dict[str, Any]:
    """The scheduled job: follow newly published workouts, notice outside changes the normal
    sync brought in, then check each due workout once, oldest first."""
    summary: dict[str, Any] = {
        "checked": 0,
        "written": 0,
        "taken": 0,
        "review": 0,
        "failed": 0,
        "rate_limited": False,
    }
    if not enabled(rt):
        return {**summary, "state": "off"}
    from vibesynergy.query import publication

    now = now or datetime.now(UTC)
    _enroll_recent(rt, now)
    _notice(rt, now)
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(text_sync)
                .where(text_sync.c.state == "active", text_sync.c.next_check_at <= now)
                .order_by(text_sync.c.next_check_at)
                .limit(MAX_PER_RUN)
            )
            .mappings()
            .all()
        )
    for row in rows:
        group = row["group_id"]
        try:
            with publication.group_lock(rt, group):
                result = sync_one(rt, group, now=now, clients=clients)
        except RateLimited as exc:
            summary["rate_limited"] = True
            retry = _aware(getattr(exc, "retry_at", None))
            _update(rt.engine, group, next_check_at=retry if retry and retry > now else now + RETRY)
            break  # Strava's budget is spent: the rest waits for the next run
        except Review as exc:
            _update(
                rt.engine,
                group,
                state="conflict",
                last_error=str(exc)[:500],
                last_checked_at=now,
                next_check_at=None,
            )
            summary["review"] += 1
            continue
        except Changed as exc:  # changed twice during the check: next run, after the wait
            _update(rt.engine, group, last_error=str(exc)[:500], next_check_at=now + DEBOUNCE)
            summary["failed"] += 1
            continue
        except Exception as exc:  # network or provider trouble: try again later
            log.warning("text sync of %s failed: %s", group, exc)
            _update(
                rt.engine,
                group,
                last_error=str(exc)[:500],
                last_checked_at=now,
                next_check_at=now + RETRY,
            )
            summary["failed"] += 1
            continue
        summary["checked"] += 1
        summary["written"] += len(result.get("written") or {})
        summary["taken"] += sum(1 for f in FIELDS if (result.get(f) or {}).get("taken_from"))
        if result.get("state") == "stopped":
            continue
        current = _row(rt.engine, group) or dict(row)
        _update(rt.engine, group, last_result=result, next_check_at=_next(current, now))
    return summary


def status(rt) -> dict[str, Any]:
    """For Settings: switched on, how many workouts are followed and any that need a look."""
    from sqlalchemy import func

    with rt.engine.connect() as conn:
        counts = dict(
            conn.execute(select(text_sync.c.state, func.count()).group_by(text_sync.c.state)).all()
        )
        problem = conn.scalar(
            select(text_sync.c.last_error)
            .where(text_sync.c.last_error.is_not(None))
            .order_by(text_sync.c.updated_at.desc())
            .limit(1)
        )
    return {
        "enabled": enabled(rt),
        "active": int(counts.get("active", 0)),
        "needs_review": int(counts.get("conflict", 0)),
        "problem": problem,
    }
