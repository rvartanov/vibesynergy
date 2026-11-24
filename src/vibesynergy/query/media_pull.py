"""Photos from Strava and Garmin into VibeSynergy (#114, phase 6): read only, never written.

**What the platforms allow** (checked for this phase):

- *Strava*: an activity's detail (``GET /activities/{id}``, already stored by the normal sync)
  says how many photos it has (``photos.count``); ``GET /activities/{id}/photos?size=…``
  lists them with a link per size. The public API has no call to add, change or delete a
  photo.
- *Garmin*: the activity (``get_activity``, already stored by the normal sync) lists its
  photos in ``metadataDTO.activityImages``, each with a link that works for 24 hours
  (``garminconnect``'s ``get_activity_images`` asks for fresh ones). ``garminconnect`` has no
  call to add a photo; the Connect website's own upload is not part of any API VibeSynergy
  uses.

So photos come **in**, and photos added in VibeSynergy stay in VibeSynergy. Nothing here
writes, changes or deletes anything on Strava or Garmin.

**When.** Every ``EVERY`` while Strava or Garmin is connected. A copy is looked at again only
when the normal sync stored a new version of it (a Strava update or webhook, Garmin's
activity details) and the stored copy says it has photos VibeSynergy has not taken in yet:
the count comes from what is already stored, so a workout without photos costs no call. At
most ``MAX_READS`` platform reads and ``MAX_SCAN`` stored copies per run, newest workouts
first; Strava's rate limit ends a run and the rest waits.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy import func, select

from vibesynergy.core.errors import RateLimited
from vibesynergy.core.secrets import SecretStore
from vibesynergy.query import workout_media
from vibesynergy.store.schema import (
    activity_link,
    activity_unified,
    media_check,
    media_origin,
    raw_item,
)

log = logging.getLogger(__name__)

JOB = "media_pull"
PLATFORMS: tuple[str, ...] = ("strava", "garmin")
EVERY = timedelta(minutes=30)
RETRY = timedelta(hours=6)
MAX_SCAN = 200
MAX_READS = 10
STRAVA_SIZE = 2048  # the largest size Strava gives a photo in
_last_run: float | None = None


class Reader(Protocol):
    def photos(self, source_id: str) -> list[dict[str, Any]]:
        """``[{photo_id, url, caption, taken_at}]`` of one activity on the platform."""
        ...

    def download(self, url: str) -> bytes: ...


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def download(url: str, timeout: float) -> bytes:
    """A photo from the platform's own link: https only, at most ``MAX_BYTES``, no login sent."""
    import requests

    if urlparse(url).scheme != "https":
        raise ValueError("photo links must be https")
    limit = workout_media.MAX_BYTES
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=True) as response:
        response.raise_for_status()
        if int(response.headers.get("content-length") or 0) > limit:
            raise workout_media.TooLarge("the photo is too large")
        data = bytearray()
        for chunk in response.iter_content(256 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise workout_media.TooLarge("the photo is too large")
    return bytes(data)


class StravaReader:
    """``GET /activities/{id}/photos`` through the sync's own client and request budget."""

    def __init__(self, rt) -> None:
        from vibesynergy.sources.http_timeout import http_timeout_seconds
        from vibesynergy.sources.strava.client import StravaClient

        self.timeout = http_timeout_seconds(rt.settings, "strava")
        self.client = StravaClient(
            SecretStore(rt.settings.secrets_dir, "strava"), timeout=self.timeout
        )

    def photos(self, source_id: str) -> list[dict[str, Any]]:
        found = self.client.get(
            f"/activities/{source_id}/photos", size=STRAVA_SIZE, photo_sources="true"
        )
        out = []
        for photo in found or []:
            if not isinstance(photo, dict) or photo.get("video_url"):
                continue  # a video: not a photo VibeSynergy keeps
            urls = photo.get("urls") or {}
            sizes = sorted(urls, key=lambda k: int(k) if str(k).isdigit() else 0)
            url = urls.get(sizes[-1]) if sizes else None
            photo_id = photo.get("unique_id") or photo.get("id")
            if not url or not photo_id or "placeholder" in str(url):
                continue  # still being processed on Strava: the next look takes it
            out.append(
                {
                    "photo_id": str(photo_id),
                    "url": str(url),
                    "caption": photo.get("caption"),
                    "taken_at": _when(photo.get("created_at")),
                }
            )
        return out

    def download(self, url: str) -> bytes:
        return download(url, self.timeout)


class GarminReader:
    """``get_activity_images`` through the same Garmin session as publishing."""

    def __init__(self, rt) -> None:
        from vibesynergy.query.publication import GarminProvider
        from vibesynergy.sources.http_timeout import http_timeout_seconds

        self.provider = GarminProvider(rt)
        self.timeout = http_timeout_seconds(rt.settings, "garmin")

    def photos(self, source_id: str) -> list[dict[str, Any]]:
        found = self.provider._call("get_activity_images", source_id)
        out = []
        for image in found or []:
            if not isinstance(image, dict):
                continue
            url = image.get("url")
            photo_id = image.get("imageId")
            if not url or not photo_id:
                continue
            out.append(
                {
                    "photo_id": str(photo_id),
                    "url": str(url),
                    "caption": None,
                    "taken_at": _when(image.get("photoDate")),
                }
            )
        return out

    def download(self, url: str) -> bytes:
        return download(url, self.timeout)


_READERS = {"strava": StravaReader, "garmin": GarminReader}


def readable(rt) -> list[str]:
    """Strava and Garmin, when switched on and signed in."""
    out = []
    for source in PLATFORMS:
        try:
            if not rt.registry.source_enabled(source):
                continue
        except Exception:
            continue
        if SecretStore(rt.settings.secrets_dir, source).get("tokens"):
            out.append(source)
    return out


def photo_count(source: str, payload: Any) -> int:
    """How many photos the stored copy says the activity has (no platform call)."""
    if not isinstance(payload, dict):
        return 0
    if source == "strava":
        detail = payload.get("detail") or {}
        photos = detail.get("photos") if isinstance(detail, dict) else None
        if isinstance(photos, dict) and isinstance(photos.get("count"), int):
            return photos["count"]
        summary = payload.get("summary") or {}
        value = summary.get("total_photo_count") if isinstance(summary, dict) else None
        return value if isinstance(value, int) else 0
    activity = payload.get("activity") or {}
    meta = activity.get("metadataDTO") if isinstance(activity, dict) else None
    images = meta.get("activityImages") if isinstance(meta, dict) else None
    return len(images) if isinstance(images, list) else 0


def due(rt) -> bool:
    """Asked on every scheduler tick; never fails it."""
    try:
        if _last_run is not None and time.monotonic() - _last_run < EVERY.total_seconds():
            return False
        return bool(readable(rt))
    except Exception:
        log.exception("checking for photos to take in failed")
        return False


def _save(conn, source: str, source_id: str, **values: Any) -> None:
    key = (media_check.c.source == source) & (media_check.c.source_id == source_id)
    if conn.scalar(select(func.count()).where(key)):
        conn.execute(media_check.update().where(key).values(**values))
    else:
        conn.execute(media_check.insert().values(source=source, source_id=source_id, **values))


def pull_one(rt, reader: Reader, source: str, source_id: str) -> dict[str, Any]:
    """Take in the photos of one Strava or Garmin activity VibeSynergy does not have yet."""
    result: dict[str, Any] = {"added": 0, "same": 0, "skipped": 0, "errors": [], "listed": 0}
    listed = reader.photos(source_id)
    result["listed"] = len(listed)
    for photo in listed:
        if workout_media.known(rt.engine, source, photo["photo_id"]):
            continue
        try:
            data = reader.download(photo["url"])
        except RateLimited:
            raise
        except Exception as exc:  # an expired link or a network hiccup: the next look
            result["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
            continue
        try:
            done = workout_media.add(
                rt.engine,
                rt.raw.blobs,
                f"{source}:{source_id}",
                data,
                source=source,
                origin=(photo["photo_id"], source_id),
                caption=photo.get("caption"),
                taken_at=photo.get("taken_at"),
            )
        except (workout_media.NotAPhoto, workout_media.TooLarge):
            workout_media.skip(rt.engine, source, photo["photo_id"], source_id)
            result["skipped"] += 1
            continue
        except workout_media.TooMany:
            result["errors"].append("the workout has as many photos as it can have")
            break
        if done["state"] == "added":
            result["added"] += 1
        elif done["state"] == "same":
            result["same"] += 1
    return result


def run(rt, *, now: datetime | None = None, readers: dict[str, Reader] | None = None) -> dict:
    """The scheduled job: look at the stored Strava and Garmin copies, newest first, and take
    in the photos of those that have photos VibeSynergy does not have yet."""
    global _last_run
    _last_run = time.monotonic()
    now = now or datetime.now(UTC)
    sources = list(readers) if readers is not None else readable(rt)
    summary: dict[str, Any] = {
        "looked": 0,
        "read": 0,
        "added": 0,
        "same": 0,
        "failed": 0,
        "rate_limited": False,
    }
    if not sources:
        return {**summary, "state": "off"}
    made: dict[str, Reader] = dict(readers or {})
    with rt.engine.connect() as conn:
        copies = conn.execute(
            select(activity_link.c.source, activity_link.c.source_id)
            .join(activity_unified, activity_unified.c.group_id == activity_link.c.group_id)
            .where(activity_link.c.source.in_(sources))
            .order_by(activity_unified.c.start_utc.desc(), activity_link.c.source_id)
        ).all()
        stored = {
            (r.source, r.item_key): r.id
            for r in conn.execute(
                select(raw_item.c.source, raw_item.c.item_key, raw_item.c.id).where(
                    raw_item.c.source.in_(sources),
                    raw_item.c.dataset == "activities",
                    raw_item.c.is_current,
                )
            )
        }
        checks = {
            (r["source"], r["source_id"]): dict(r)
            for r in conn.execute(select(media_check)).mappings()
        }
        taken = dict(
            conn.execute(
                select(
                    media_origin.c.source + ":" + media_origin.c.source_id, func.count()
                ).group_by(media_origin.c.source, media_origin.c.source_id)
            ).all()
        )
    for source, source_id in copies:
        raw_id = stored.get((source, source_id))
        if raw_id is None:
            continue
        check = checks.get((source, source_id))
        next_at = check["next_at"] if check else None
        if next_at is not None and next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=UTC)
        if check and check["raw_id"] == raw_id and (next_at is None or next_at > now):
            continue
        if summary["looked"] >= MAX_SCAN:
            break
        summary["looked"] += 1
        try:
            photos = photo_count(source, rt.raw.load(raw_id).json())
        except Exception:
            photos = 0
        if photos == 0 or taken.get(f"{source}:{source_id}", 0) >= photos:
            with rt.engine.begin() as conn:
                _save(
                    conn,
                    source,
                    source_id,
                    raw_id=raw_id,
                    photos=photos,
                    checked_at=now,
                    next_at=None,
                    error=None,
                )
            continue
        if summary["read"] >= MAX_READS:
            break
        summary["read"] += 1
        try:
            reader = made.get(source) or made.setdefault(source, _READERS[source](rt))
            result = pull_one(rt, reader, source, source_id)
        except RateLimited:
            summary["rate_limited"] = True
            break  # the budget is spent: the rest waits for the next run
        except Exception as exc:
            log.warning("photos of %s:%s not taken in: %s", source, source_id, exc)
            summary["failed"] += 1
            retried = bool(check and check["raw_id"] == raw_id and check["next_at"] is not None)
            with rt.engine.begin() as conn:
                _save(
                    conn,
                    source,
                    source_id,
                    raw_id=raw_id,
                    photos=photos,
                    checked_at=now,
                    next_at=None if retried else now + RETRY,
                    error=str(exc)[:500],
                )
            continue
        summary["added"] += result["added"]
        summary["same"] += result["same"]
        errors = list(result["errors"])
        if result["listed"] < photos:  # still being processed there, or a video
            errors.append(f"{result['listed']} of {photos} photos listed")
        # One more look per stored version: what fails twice waits for a new version.
        retried = bool(check and check["raw_id"] == raw_id and check["next_at"] is not None)
        with rt.engine.begin() as conn:
            _save(
                conn,
                source,
                source_id,
                raw_id=raw_id,
                photos=photos,
                checked_at=now,
                next_at=now + RETRY if errors and not retried else None,
                error="; ".join(errors)[:500] or None,
            )
    return summary
