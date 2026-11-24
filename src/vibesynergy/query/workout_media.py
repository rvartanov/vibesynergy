"""Photos of a workout, kept in VibeSynergy (#114, phase 6).

**Where they come from.** You add them on the workout page (source ``vs``), and the photos
on the workout's Strava and Garmin copies are taken in by themselves (``media_pull``, source
``strava`` or ``garmin``). Strava's and Garmin's APIs let an app read an activity's photos
but not add any, so a photo added here stays in VibeSynergy: it is shown on the workout page
and on share links, and nothing is sent anywhere.

**How they are kept.** The original file goes into the content-addressed blob store as it
came. The portal shows two sizes made from it, ``display`` (at most ``DISPLAY_PX``) and
``thumb`` (at most ``THUMB_PX``): turned upright and re-encoded as JPEG without the camera's
metadata, so the place a photo was taken (EXIF GPS) never reaches a share link. A row in
``media_item`` is never deleted. Removing a photo in VibeSynergy only hides it here (it stays
on Strava and Garmin, VibeSynergy never deletes anything there) and can be undone; every
change is kept in ``media_change``, like the texts' and gear's history.

**One photo once.** The same file (SHA-256) or the same picture in another size or
compression (a 64-bit difference hash within ``SAME_PHOTO_BITS`` bits) is one photo of the
workout, whichever place it came from first: Strava and Garmin both having it, or you adding
it again. A photo from Strava or Garmin is known by the platform's own photo id
(``media_origin``), so it is fetched once and does not come back after you removed it.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, func, select, update

from vibesynergy.core.ids import new_id
from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.blobs import BlobStore
from vibesynergy.store.schema import media_change, media_item, media_origin

SOURCES: tuple[str, ...] = ("vs", "strava", "garmin")
# added: a new photo; merged: the same photo also came from another place (or again);
# removed / restored: hidden in VibeSynergy, or back.
ACTIONS: tuple[str, ...] = ("added", "merged", "removed", "restored")
SIZES: tuple[str, ...] = ("display", "thumb")
# Technical limits: what a phone photo is at most, and what a page can show.
MAX_BYTES = 30 * 1024 * 1024
MAX_PIXELS = 100_000_000
MAX_PER_WORKOUT = 60
DISPLAY_PX = 2048
THUMB_PX = 480
# Two difference hashes this close are the same picture (a resize or a recompression).
SAME_PHOTO_BITS = 4
_FORMATS = {
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",  # a phone's JPEG with a second frame
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class NotAPhoto(ValueError):
    """The file is not a photo VibeSynergy can read (JPEG, PNG, WebP or GIF)."""


class TooLarge(ValueError):
    """Larger than ``MAX_BYTES`` or ``MAX_PIXELS``."""


class TooMany(ValueError):
    """The workout already has ``MAX_PER_WORKOUT`` photos."""


@dataclass(frozen=True)
class Processed:
    sha256: str
    content_type: str
    width: int
    height: int
    phash: str
    display: bytes
    thumb: bytes


# --- reading a photo ----------------------------------------------------------------------


def _rgb(img):
    from PIL import Image

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return img.convert("RGB")


def _jpeg(img, px: int, quality: int, icc: bytes | None) -> bytes:
    from PIL import Image

    copy = img.copy()
    copy.thumbnail((px, px), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    # No ``exif``: the camera's metadata (its location above all) is left out.
    options: dict[str, Any] = {"quality": quality, "optimize": True, "progressive": True}
    if icc:
        options["icc_profile"] = icc  # colours only, nothing about the person
    copy.save(out, "JPEG", **options)
    return out.getvalue()


def dhash(img) -> str:
    """A 64-bit difference hash (hex): the same for a photo in another size or compression."""
    from PIL import Image

    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = small.tobytes()  # one byte per pixel in "L"
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def distance(left: str, right: str) -> int:
    """How many of the 64 bits of two difference hashes differ."""
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 64


def process(data: bytes) -> Processed:
    """Check that ``data`` is a photo and make the sizes the portal shows."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(data) > MAX_BYTES:
        raise TooLarge(f"a photo can be at most {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise NotAPhoto("the file is empty")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                kind = _FORMATS.get(str(img.format))
                if kind is None:
                    raise NotAPhoto("choose a JPEG, PNG, WebP or GIF photo")
                if img.width * img.height > MAX_PIXELS:
                    raise TooLarge("the photo has too many pixels")
                icc = img.info.get("icc_profile")
                upright = ImageOps.exif_transpose(img) or img
                rgb = _rgb(upright)
    except (NotAPhoto, TooLarge):
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
        SyntaxError,
    ) as exc:
        raise NotAPhoto("choose a JPEG, PNG, WebP or GIF photo") from exc
    return Processed(
        sha256=hashlib.sha256(data).hexdigest(),
        content_type=kind,
        width=rgb.width,
        height=rgb.height,
        phash=dhash(rgb),
        display=_jpeg(rgb, DISPLAY_PX, 85, icc),
        thumb=_jpeg(rgb, THUMB_PX, 80, icc),
    )


# --- a workout's photos -------------------------------------------------------------------


def _group(engine: Engine, activity_id: str) -> tuple[str, list[str]]:
    from vibesynergy.query import workout_texts

    return workout_texts._group(engine, activity_id)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _rows(conn, keys: list[str]) -> list[dict[str, Any]]:
    order = func.coalesce(media_item.c.taken_at, media_item.c.added_at)
    rows = conn.execute(
        select(media_item)
        .where(media_item.c.group_id.in_(keys))
        .order_by(order, media_item.c.added_at, media_item.c.id)
    ).mappings()
    return [dict(r) for r in rows]


def _change(conn, group: str, media_id: str, action: str, source: str, author, now) -> None:
    conn.execute(
        media_change.insert().values(
            group_id=group, media_id=media_id, action=action, source=source, author=author, at=now
        )
    )


def known(engine: Engine, source: str, photo_id: str) -> bool:
    """Whether the platform's photo was taken in (or skipped) before."""
    with engine.connect() as conn:
        return (
            conn.scalar(
                select(media_origin.c.media_id).where(
                    media_origin.c.source == source, media_origin.c.photo_id == photo_id
                )
            )
            is not None
        )


def skip(engine: Engine, source: str, photo_id: str, source_id: str) -> None:
    """Remember a platform photo that is not a photo VibeSynergy can read: not asked again."""
    with engine.begin() as conn:
        exists = conn.scalar(
            select(func.count()).where(
                media_origin.c.source == source, media_origin.c.photo_id == photo_id
            )
        )
        if not exists:
            conn.execute(
                media_origin.insert().values(
                    source=source, photo_id=photo_id, source_id=source_id, media_id="", at=utc_now()
                )
            )


def add(
    engine: Engine,
    blobs: BlobStore,
    activity_id: str,
    data: bytes,
    *,
    source: str = "vs",
    origin: tuple[str, str] | None = None,
    caption: str | None = None,
    taken_at: datetime | None = None,
    author: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add a photo to the workout. ``origin``: the platform's (photo id, activity id).

    Returns ``{"state": "added" | "same" | "known", "media": id}``: ``same`` when the
    workout already has this photo (from any place), ``known`` when this platform photo was
    taken in before. A photo you add again after removing it comes back (``restored``); one
    from Strava or Garmin does not."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    if (origin is None) != (source == "vs"):
        raise ValueError("a photo from Strava or Garmin needs its photo id, and only then")
    if origin and known(engine, source, origin[0]):
        return {"state": "known", "media": None}
    photo = process(data)
    group, keys = _group(engine, activity_id)
    now = now or utc_now()
    with engine.begin() as conn:
        rows = _rows(conn, keys)
        same = next((r for r in rows if r["sha256"] == photo.sha256), None) or next(
            (r for r in rows if distance(r["phash"], photo.phash) <= SAME_PHOTO_BITS), None
        )
        if same is not None:
            if origin:
                conn.execute(
                    media_origin.insert().values(
                        source=source,
                        photo_id=origin[0],
                        source_id=origin[1],
                        media_id=same["id"],
                        at=now,
                    )
                )
                _change(conn, group, same["id"], "merged", source, author, now)
            restored = source == "vs" and same["removed_at"] is not None
            if restored:
                conn.execute(
                    update(media_item).where(media_item.c.id == same["id"]).values(removed_at=None)
                )
                _change(conn, group, same["id"], "restored", source, author, now)
            return {"state": "same", "media": same["id"], "restored": restored}
        if sum(1 for r in rows if r["removed_at"] is None) >= MAX_PER_WORKOUT:
            raise TooMany(f"a workout can have at most {MAX_PER_WORKOUT} photos")
        # Content-addressed: storing a file that is already there costs nothing.
        blobs.put(data)
        display = blobs.put(photo.display)
        thumb = blobs.put(photo.thumb)
        media_id = new_id("med")
        conn.execute(
            media_item.insert().values(
                id=media_id,
                group_id=group,
                sha256=photo.sha256,
                content_type=photo.content_type,
                size=len(data),
                width=photo.width,
                height=photo.height,
                phash=photo.phash,
                display_sha256=display,
                thumb_sha256=thumb,
                caption=(caption or "").strip()[:500] or None,
                taken_at=taken_at,
                source=source,
                author=author,
                added_at=now,
                removed_at=None,
            )
        )
        if origin:
            conn.execute(
                media_origin.insert().values(
                    source=source,
                    photo_id=origin[0],
                    source_id=origin[1],
                    media_id=media_id,
                    at=now,
                )
            )
        _change(conn, group, media_id, "added", source, author, now)
    return {"state": "added", "media": media_id}


def _own(conn, keys: list[str], media_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            select(media_item).where(media_item.c.id == media_id, media_item.c.group_id.in_(keys))
        )
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError("this workout has no such photo")
    return dict(row)


def remove(
    engine: Engine, activity_id: str, media_id: str, *, author: str | None = None, now=None
) -> None:
    """Hide a photo in VibeSynergy. It stays on Strava and Garmin, and can be restored."""
    group, keys = _group(engine, activity_id)
    now = now or utc_now()
    with engine.begin() as conn:
        row = _own(conn, keys, media_id)
        if row["removed_at"] is not None:
            return
        conn.execute(update(media_item).where(media_item.c.id == media_id).values(removed_at=now))
        _change(conn, group, media_id, "removed", "vs", author, now)


def restore(
    engine: Engine, activity_id: str, media_id: str, *, author: str | None = None, now=None
) -> None:
    """Show a removed photo again."""
    group, keys = _group(engine, activity_id)
    now = now or utc_now()
    with engine.begin() as conn:
        row = _own(conn, keys, media_id)
        if row["removed_at"] is None:
            return
        live = conn.scalar(
            select(func.count()).where(
                media_item.c.group_id.in_(keys), media_item.c.removed_at.is_(None)
            )
        )
        if (live or 0) >= MAX_PER_WORKOUT:
            raise TooMany(f"a workout can have at most {MAX_PER_WORKOUT} photos")
        conn.execute(update(media_item).where(media_item.c.id == media_id).values(removed_at=None))
        _change(conn, group, media_id, "restored", "vs", author, now)


def _public(row: dict[str, Any], places: list[str]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source": row["source"],
        # Every place this photo came from: where it was first, then the others.
        "places": places,
        "caption": row["caption"],
        "width": row["width"],
        "height": row["height"],
        "added_at": _iso(row["added_at"]),
        "taken_at": _iso(row["taken_at"]),
        "removed_at": _iso(row["removed_at"]),
    }


def photos(engine: Engine, activity_id: str, *, shared: bool = False) -> dict[str, Any]:
    """The workout's photos, oldest first, the removed ones and the history (not shared)."""
    group, keys = _group(engine, activity_id)
    with engine.connect() as conn:
        rows = _rows(conn, keys)
        ids = [r["id"] for r in rows]
        merged = conn.execute(
            select(media_change.c.media_id, media_change.c.source)
            .where(media_change.c.media_id.in_(ids), media_change.c.action == "merged")
            .order_by(media_change.c.id)
        ).all()
        changes = (
            []
            if shared
            else [
                dict(r)
                for r in conn.execute(
                    select(media_change)
                    .where(media_change.c.group_id.in_(keys))
                    .order_by(media_change.c.at.desc(), media_change.c.id.desc())
                ).mappings()
            ]
        )
    places: dict[str, list[str]] = {r["id"]: [r["source"]] for r in rows}
    for media_id, source in merged:
        if source not in places[media_id]:
            places[media_id].append(source)
    shown = [_public(r, places[r["id"]]) for r in rows if r["removed_at"] is None]
    out: dict[str, Any] = {"group": group, "photos": shown}
    if not shared:
        number = {r["id"]: n for n, r in enumerate(rows, 1)}
        out["removed"] = [_public(r, places[r["id"]]) for r in rows if r["removed_at"]]
        out["history"] = [
            {
                "at": _iso(c["at"]),
                "action": c["action"],
                "source": c["source"],
                "media": c["media_id"],
                "number": number.get(c["media_id"]),
            }
            for c in changes
        ]
    return out


def file(
    engine: Engine, blobs: BlobStore, media_id: str, size: str, *, shared: bool = False
) -> bytes:
    """The JPEG of one photo in ``size`` (display or thumb). A share link gets no removed
    photo; the original file is never served."""
    if size not in SIZES:
        raise ValueError(f"size must be one of {', '.join(SIZES)}")
    with engine.connect() as conn:
        row = conn.execute(select(media_item).where(media_item.c.id == media_id)).mappings().first()
    if row is None or (shared and row["removed_at"] is not None):
        raise LookupError("no such photo")
    return blobs.get(row["display_sha256" if size == "display" else "thumb_sha256"])


def count(engine: Engine, activity_id: str) -> int:
    """How many photos the workout shows."""
    _, keys = _group(engine, activity_id)
    with engine.connect() as conn:
        return int(
            conn.scalar(
                select(func.count()).where(
                    media_item.c.group_id.in_(keys), media_item.c.removed_at.is_(None)
                )
            )
            or 0
        )
