"""A workout's own texts in VibeSynergy, with their history (#114, phase 1).

Every workout (an activity group) has three texts of its own: its **name**, its **public**
text and its **private** text. Each change is stored as a new version in ``workout_text``:
when, the source (``vs``, ``strava``, ``garmin`` or ``ai``), the text before and after. Old
versions are never changed or removed; restoring one adds a new version that copies it.

Until a workout has a version of its own, its texts are derived from what is already there
(``derive``):

- name: the merged activity's name;
- public: what the athlete wrote in the Strava and Garmin descriptions, without
  VibeSynergy's block and the generated parts (``own_text``);
- private: the notes linked to the activity, without the paragraphs that are already in the
  public text (the Strava description the diary imported as a note is given once).

The first write to a workout stores these derived texts as ``seed`` versions, so the history
starts with what was there. A later Strava or Garmin text changes a stored text only through
the two-way sync of a published workout (``text_sync``, phase 3), which stores it as a version
with its platform as the source.

The private text is the athlete's alone. It is never part of anything published to Strava or
Garmin, or of an activity view a share link shows.

**Correction of the AI-coach insight** (#114, phase 4). The athlete's comment on the
insight ("wasn't with the group") is kept in the same history as a fourth field,
``correction``: private like the private text, never synced or published. It is not one of
the workout's texts (``FIELDS``); the AI coach reads it when it rewrites the insight
(``coach_runner.insight_rewrite``).
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import activity, activity_link, activity_unified, note, workout_text

FIELDS: tuple[str, ...] = ("name", "public", "private")
# The athlete's correction of the AI-coach insight (#114, phase 4): private, never synced.
CORRECTION = "correction"
ALL_FIELDS: tuple[str, ...] = (*FIELDS, CORRECTION)
# Where a version came from. A seed carries the platform its text was taken from.
SOURCES: tuple[str, ...] = ("vs", "strava", "garmin", "ai")
# ``overwritten``: a version that lost to a later change in the two-way sync (phase 3): kept
# in the history, never the current text. ``auto``: a name VibeSynergy's AI coach gave the
# workout with its insight (``auto_names``, phase 7a, #188). ``import``: a platform's name
# taken in on the first sync (the device's own name, such as "Utrecht Hardlopen"), not a
# change of yours.
KINDS: tuple[str, ...] = ("seed", "edit", "restore", "overwritten", "auto", "import")
OVERWRITTEN = "overwritten"
AUTO = "auto"
IMPORT = "import"
# A name version that locks the name (``name_lock``): a change you made, in VibeSynergy or
# on Strava or Garmin (pulled in by the two-way sync), or a version you restored.
_LOCKING_KINDS: tuple[str, ...] = ("edit", "restore")
_LOCKING_SOURCES: tuple[str, ...] = ("vs", "strava", "garmin")
# Technical limits, the size a description field takes on the platforms.
MAX_NAME = 255
MAX_TEXT = 20000
MAX_CORRECTION = 2000
# The platforms whose description is the athlete's public text, in order of preference.
_PUBLIC_FROM: tuple[str, ...] = ("strava", "garmin")
_PARAGRAPH = re.compile(r"\n\s*\n")


def own_text(description: Any) -> str | None:
    """What the athlete wrote in a provider description: every VibeSynergy block
    (``[/*]`` … ``*/]``, found as publication finds it) taken out, and the generated parts
    and app texts the diary import leaves out (``journal.personal_text``) too."""
    from vibesynergy.query import journal, publication_format

    if not isinstance(description, str) or not description.strip():
        return None
    text = publication_format.normalize_newlines(description)
    for start, end in reversed(publication_format.find_blocks(text)):
        text = text[:start] + "\n" + text[end:]
    return journal.personal_text(text)


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def paragraphs(text: str | None) -> list[str]:
    """The paragraphs of a text (split on blank lines), trimmed, empty ones left out."""
    from vibesynergy.query import publication_format

    found = _PARAGRAPH.split(publication_format.normalize_newlines(text or ""))
    return [p.strip() for p in found if p.strip()]


def unseen(text: str | None, seen: set[str]) -> str | None:
    """``text`` without the paragraphs already in ``seen`` (normalized); adds the rest to
    ``seen``. None when nothing new is left."""
    kept = []
    for para in paragraphs(text):
        key = _norm(para)
        if key not in seen:
            seen.add(key)
            kept.append(para)
    return "\n\n".join(kept) or None


def _platform(source: str | None) -> str:
    """The version source for a text taken from ``source`` (an activity or note source)."""
    name = str(source or "")
    for platform in ("strava", "garmin"):
        if name == platform or name.startswith(f"{platform}_"):
            return platform
    return "ai" if name == "ai" else "vs"


def _group(engine: Engine, activity_id: str) -> tuple[str, list[str]]:
    """The group id of an activity id, and every member key (``source:source_id``).

    Versions are found by any member key, so they survive a change of primary source."""
    from vibesynergy.query import views

    group = views.resolve_activity(engine, activity_id)
    with engine.connect() as conn:
        keys = [
            f"{s}:{i}"
            for s, i in conn.execute(
                select(activity_link.c.source, activity_link.c.source_id).where(
                    activity_link.c.group_id == group
                )
            )
        ]
    return group, sorted(set(keys) | {group})


def derive(engine: Engine, activity_id: str) -> dict[str, dict[str, Any]]:
    """The texts as they follow from what is already there, per field ``{text, source}``.

    A field without any text is left out."""
    from vibesynergy.query import source_removed

    group, keys = _group(engine, activity_id)
    with engine.connect() as conn:
        unified = (
            conn.execute(
                select(
                    activity_unified.c.name,
                    activity_unified.c.primary_source,
                    activity_unified.c.sources,
                ).where(activity_unified.c.group_id == group)
            )
            .mappings()
            .first()
        )
        members = list((unified or {}).get("sources") or [])
        gone = source_removed.removed(conn, members)
        rows = {
            f"{r.source}:{r.source_id}": r
            for r in conn.execute(
                select(activity.c.source, activity.c.source_id, activity.c.extra)
                .where(activity.c.source.in_(_PUBLIC_FROM))
                .where(activity.c.source_id.in_([m.split(":", 1)[1] for m in members]))
            )
        }
        notes = conn.execute(
            select(note.c.text, note.c.source)
            .where(note.c.activity_id.in_(keys))
            .order_by(note.c.start_date, note.c.created_at)
        ).all()
    out: dict[str, dict[str, Any]] = {}
    name = str((unified or {}).get("name") or "").strip()
    if name:
        out["name"] = {"text": name, "source": _platform((unified or {}).get("primary_source"))}
    seen: set[str] = set()
    public: list[str] = []
    public_source = None
    for platform in _PUBLIC_FROM:
        for key in members:
            row = rows.get(key)
            if row is None or row.source != platform or key in gone:
                continue
            extra = row.extra if isinstance(row.extra, dict) else {}
            new = unseen(own_text(extra.get("description")), seen)
            if new:
                public.append(new)
                public_source = public_source or platform
    if public:
        out["public"] = {"text": "\n\n".join(public), "source": public_source}
    private: list[str] = []
    private_source = None
    for text, source in notes:
        new = unseen(text, seen)
        if new:
            private.append(new)
            private_source = private_source or _platform(source)
    if private:
        out["private"] = {"text": "\n\n".join(private), "source": private_source}
    return out


def _version(row: Any) -> dict[str, Any]:
    at = row.at
    return {
        "version": row.id,
        "field": row.field,
        "text": row.text,
        "old_text": row.old_text,
        "source": row.source,
        "kind": row.kind,
        "author": row.author,
        "restored_from": row.restored_from,
        "at": at.isoformat() if at is not None else None,
    }


def _latest(conn, keys: list[str], fields: tuple[str, ...] = FIELDS) -> dict[str, Any]:
    rows = conn.execute(
        select(workout_text)
        .where(workout_text.c.group_id.in_(keys), workout_text.c.field.in_(fields))
        .order_by(workout_text.c.id)
    ).all()
    latest: dict[str, Any] = {}
    for row in rows:
        if row.kind != OVERWRITTEN:
            latest[row.field] = row
    return latest


def name_lock(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Whether the workout's name is locked against automatic names, from its history.

    Locked once any name version is a change of yours: an edit (kind ``edit``) or a restore
    in VibeSynergy, or a change made on Strava or Garmin that the two-way sync pulled in.
    Not locking: the ``seed`` (what was there), ``import`` (the platform's own name on the
    first sync), ``auto`` (VibeSynergy's own name) and ``overwritten`` versions, and
    versions from the AI coach. Returns the newest locking version
    ``{version, source, at}``, or None. No separate flag: the history is the record."""
    for v in versions:  # newest first
        if (
            v.get("field") == "name"
            and v.get("kind") in _LOCKING_KINDS
            and v.get("source") in _LOCKING_SOURCES
        ):
            return {"version": v["version"], "source": v["source"], "at": v["at"]}
    return None


def stored(engine: Engine, activity_id: str) -> dict[str, str]:
    """Only the texts VibeSynergy holds a version of: field -> current text. Empty when the
    workout has no version of its own yet (then ``derive`` is what there is)."""
    _group_id, keys = _group(engine, activity_id)
    with engine.connect() as conn:
        return {field: row.text for field, row in _latest(conn, keys).items()}


def texts(engine: Engine, activity_id: str) -> dict[str, Any]:
    """The workout's current name, public and private text.

    Per field: ``text``, ``source``, ``at``, ``version`` (None while derived) and ``stored``
    (False while the text still follows from the notes and descriptions)."""
    group, keys = _group(engine, activity_id)
    with engine.connect() as conn:
        latest = _latest(conn, keys)
    derived = derive(engine, group) if len(latest) < len(FIELDS) else {}
    out: dict[str, Any] = {"activity_id": group}
    for field in FIELDS:
        row = latest.get(field)
        if row is not None:
            out[field] = {
                "text": row.text,
                "source": row.source,
                "at": row.at.isoformat() if row.at else None,
                "version": row.id,
                "stored": True,
            }
        else:
            found = derived.get(field) or {}
            out[field] = {
                "text": found.get("text") or "",
                "source": found.get("source"),
                "at": None,
                "version": None,
                "stored": False,
            }
    out["name"]["lock"] = name_lock(history(engine, group, "name"))
    return out


def history(engine: Engine, activity_id: str, field: str | None = None) -> list[dict[str, Any]]:
    """Every stored version of the workout's texts, newest first (one field with ``field``).
    The insight corrections (``correction``) are in it too: this is the owner's history."""
    if field is not None and field not in ALL_FIELDS:
        raise ValueError(f"field must be one of {', '.join(ALL_FIELDS)}")
    _group_id, keys = _group(engine, activity_id)
    query = select(workout_text).where(workout_text.c.group_id.in_(keys))
    if field is not None:
        query = query.where(workout_text.c.field == field)
    with engine.connect() as conn:
        rows = conn.execute(query.order_by(workout_text.c.id.desc())).all()
    return [_version(r) for r in rows]


def _clean(field: str, text: Any) -> str:
    if not isinstance(text, str):
        raise ValueError(f"{field} must be text")
    from vibesynergy.query import publication_format

    if field == "name":
        clean = " ".join(text.split())
        if len(clean) > MAX_NAME:
            raise ValueError(f"the name is longer than {MAX_NAME} characters")
        return clean
    clean = publication_format.normalize_newlines(text).strip()
    if field == CORRECTION and len(clean) > MAX_CORRECTION:
        raise ValueError(f"the correction is longer than {MAX_CORRECTION} characters")
    if len(clean) > MAX_TEXT:
        raise ValueError(f"the {field} text is longer than {MAX_TEXT} characters")
    if publication_format.find_blocks(clean) or publication_format.orphan_markers(clean):
        raise ValueError(
            f"the {field} text holds VibeSynergy's block markers; write only your own text"
        )
    return clean


def _write(
    engine: Engine,
    activity_id: str,
    changes: dict[str, str],
    *,
    source: str,
    kind: str,
    author: str | None,
    restored_from: int | None = None,
    overwritten: list[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    group, keys = _group(engine, activity_id)
    derived = derive(engine, group)
    now = utc_now()
    changed: list[str] = []
    with engine.begin() as conn:
        latest = _latest(conn, keys)
        if len(latest) < len(FIELDS):
            # The first versions: what was there before, so the history starts with it.
            for field, found in derived.items():
                if field in latest or not found.get("text"):
                    continue
                conn.execute(
                    workout_text.insert().values(
                        group_id=group,
                        field=field,
                        text=found["text"],
                        old_text=None,
                        source=found["source"] or "vs",
                        kind="seed",
                        author=None,
                        at=now,
                    )
                )
            latest = _latest(conn, keys)
        for field, text, lost_source in overwritten or ():
            # Lost to a later change (two-way sync): stored first, so it is never current.
            conn.execute(
                workout_text.insert().values(
                    group_id=group,
                    field=field,
                    text=text,
                    old_text=latest[field].text if field in latest else None,
                    source=lost_source,
                    kind=OVERWRITTEN,
                    author=author,
                    at=now,
                )
            )
        if CORRECTION in changes:
            latest = {**latest, **_latest(conn, keys, (CORRECTION,))}
        for field, text in changes.items():
            current = latest[field].text if field in latest else ""
            if text == current:
                continue
            conn.execute(
                workout_text.insert().values(
                    group_id=group,
                    field=field,
                    text=text,
                    old_text=current if field in latest else None,
                    source=source,
                    kind=kind,
                    author=author,
                    restored_from=restored_from,
                    at=now,
                )
            )
            changed.append(field)
    if changed and source in ("vs", "ai"):
        # A published workout's name or public text changed in VibeSynergy: the two-way
        # sync passes it on after its short wait (a no-op while the sync is off).
        from vibesynergy.query import text_sync

        text_sync.touch(engine, group, [f for f in changed if f in text_sync.FIELDS], now)
    return {"activity_id": group, "changed": changed, "texts": texts(engine, group)}


def set_texts(
    engine: Engine,
    activity_id: str,
    *,
    name: str | None = None,
    public: str | None = None,
    private: str | None = None,
    source: str = "vs",
    author: str | None = None,
) -> dict[str, Any]:
    """Change one or more of the workout's texts; a field left out (None) stays as it is,
    an empty string clears it. Each changed field gets a new version; an unchanged one
    none. Returns ``{activity_id, changed, texts}``."""
    given = {"name": name, "public": public, "private": private}
    changes = {f: _clean(f, t) for f, t in given.items() if t is not None}
    if not changes:
        raise ValueError("give a name, a public text or a private text")
    return _write(engine, activity_id, changes, source=source, kind="edit", author=author)


def corrections(engine: Engine, activity_id: str) -> list[dict[str, Any]]:
    """The athlete's corrections of the AI-coach insight, newest first (private)."""
    return [v for v in history(engine, activity_id, CORRECTION) if v["text"]]


def set_correction(
    engine: Engine, activity_id: str, text: str, *, author: str | None = None
) -> dict[str, Any]:
    """Store the athlete's comment on the AI-coach insight as a new version (source ``vs``).
    It is private: never synced, published or shown on a shared page. Returns
    ``{activity_id, changed, version}``; ``changed`` is empty when it is the same text as the
    last correction."""
    clean = _clean(CORRECTION, text)
    if not clean:
        raise ValueError("write what is not right in the insight")
    done = _write(engine, activity_id, {CORRECTION: clean}, source="vs", kind="edit", author=author)
    found = corrections(engine, done["activity_id"])
    return {
        "activity_id": done["activity_id"],
        "changed": done["changed"],
        "version": found[0]["version"] if found else None,
    }


def take_over(
    engine: Engine,
    activity_id: str,
    field: str,
    text: str | None,
    *,
    source: str,
    overwritten: list[tuple[str, str]] | None = None,
    kind: str = "edit",
) -> dict[str, Any]:
    """The two-way sync's write (``text_sync``): ``text`` (None: leave the current text) from
    ``source`` becomes the current version, and each ``(text, source)`` in ``overwritten``
    is kept in the history as a version that lost to it ("overwritten, from <source>").
    ``kind`` is ``import`` for a platform's name taken in on the first sync."""
    if kind not in ("edit", IMPORT):
        raise ValueError("the sync writes an edit or an import")
    if field not in ("name", "public"):
        raise ValueError("only the name and the public text are synced")
    changes = {field: _clean(field, text)} if text is not None else {}
    lost = [(field, _clean(field, t), s) for t, s in overwritten or () if s in SOURCES]
    return _write(
        engine, activity_id, changes, source=source, kind=kind, author="sync", overwritten=lost
    )


def set_auto_name(
    engine: Engine, activity_id: str, name: str, *, source: str = "ai", author: str = "ai-coach"
) -> dict[str, Any]:
    """Store a name VibeSynergy gave the workout by itself (``auto_names``: the AI coach's
    name from its insight, source ``ai``), kind ``auto``. Refused when the name is locked
    (``name_lock``). The two-way sync passes it on like any VibeSynergy change (published
    workouts only)."""
    if source not in ("vs", "ai"):
        raise ValueError("an automatic name comes from VibeSynergy or its AI coach")
    clean = _clean("name", name)
    if not clean:
        raise ValueError("an automatic name cannot be empty")
    if name_lock(history(engine, activity_id, "name")) is not None:
        raise ValueError("the name is locked: you changed it yourself")
    return _write(engine, activity_id, {"name": clean}, source=source, kind=AUTO, author=author)


def restore(
    engine: Engine, version: int, *, activity_id: str | None = None, author: str | None = None
) -> dict[str, Any]:
    """Make an old version the current text again: a new version (kind ``restore``, source
    ``vs``) that copies it. The old versions all stay."""
    with engine.connect() as conn:
        row = conn.execute(select(workout_text).where(workout_text.c.id == int(version))).first()
    if row is None:
        raise ValueError(f"no text version {version}")
    if activity_id is not None:
        _group_id, keys = _group(engine, activity_id)
        if row.group_id not in keys:
            raise ValueError(f"text version {version} belongs to another workout")
    out = _write(
        engine,
        row.group_id,
        {row.field: row.text},
        source="vs",
        kind="restore",
        author=author,
        restored_from=row.id,
    )
    out["restored"] = _version(row)
    return out
