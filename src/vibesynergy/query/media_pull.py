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

import io
import logging
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import urljoin, urlparse

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
# Videos: at most this many downloads per run (the rest waits for the next one), each within
# one deadline. A streamed video's parts come only from its own host or these (and their
# subdomains).
MAX_VIDEOS_PER_RUN = 3
VIDEO_DEADLINE_S = 300.0
MAX_PLAYLIST_BYTES = 1024 * 1024
HLS_HOSTS: tuple[str, ...] = ("strava.com", "cloudfront.net")
# media_check.error of a copy that only waits (no ffmpeg yet, or the run's video budget):
# that look does not use up the one retry.
WAITING = "waiting: "
_last_run: float | None = None


class Reader(Protocol):
    def photos(self, source_id: str) -> list[dict[str, Any]]:
        """``[{photo_id, url, caption, taken_at}]`` of one activity on the platform."""
        ...

    def download(self, url: str) -> bytes: ...

    def download_video(self, url: str, dest: Path) -> None:
        """A video (``"video": True`` in ``photos``) into the file ``dest``."""
        ...


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


def _host_allowed(url: str, playlist_host: str | None) -> bool:
    """https, and on the playlist's own host or a Strava host or CDN (``HLS_HOSTS``)."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    if playlist_host and host == playlist_host.lower():
        return True
    return any(host == h or host.endswith("." + h) for h in HLS_HOSTS)


class _Fetch:
    """Downloads for one video: verified TLS (requests' default), one overall deadline and
    one size limit for all its parts together."""

    def __init__(self, timeout: float, deadline_s: float, limit: int) -> None:
        self.timeout = timeout
        self.deadline = time.monotonic() + deadline_s
        self.left = limit

    def _remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the video took too long to download")
        return left

    def to(self, url: str, out: IO[bytes], limit: int | None = None) -> str:
        """Write ``url``'s body to ``out``; returns its content type."""
        import requests

        cap = self.left if limit is None else min(limit, self.left)
        timeout = min(self.timeout, self._remaining())
        with requests.get(url, stream=True, timeout=timeout, allow_redirects=True) as response:
            response.raise_for_status()
            if not _host_allowed(response.url, urlparse(url).hostname):
                raise ValueError("the video link went to another place")
            if int(response.headers.get("content-length") or 0) > cap:
                raise workout_media.TooLarge("the video is too large")
            written = 0
            for chunk in response.iter_content(1024 * 1024):
                self._remaining()
                out.write(chunk)
                written += len(chunk)
                if written > cap:
                    raise workout_media.TooLarge("the video is too large")
            self.left -= written
            return response.headers.get("content-type", "")

    def text(self, url: str) -> str:
        buf = io.BytesIO()
        self.to(url, buf, MAX_PLAYLIST_BYTES)
        return buf.getvalue().decode("utf-8", errors="replace")


def download_video(
    url: str, dest: Path, timeout: float, deadline_s: float = VIDEO_DEADLINE_S
) -> None:
    """A video from the platform's own link into ``dest``: https only, at most
    ``MAX_VIDEO_BYTES`` and ``deadline_s`` seconds, no login sent. A streamed video (HLS,
    ``.m3u8``) is downloaded part by part here, then put together by ffmpeg from local files."""
    from vibesynergy.query import video

    if urlparse(url).scheme != "https":
        raise ValueError("video links must be https")
    fetch = _Fetch(timeout, deadline_s, workout_media.MAX_VIDEO_BYTES)
    if urlparse(url).path.lower().endswith(".m3u8"):
        fetch_hls(url, dest, fetch)
        return
    with dest.open("wb") as out:
        kind = fetch.to(url, out)
    if "mpegurl" in kind.lower():
        fetch_hls(url, dest, _Fetch(timeout, deadline_s, workout_media.MAX_VIDEO_BYTES))
        return
    with dest.open("rb") as fh:
        head = fh.read(16)
    if not video.looks_like_video(head):
        raise workout_media.NotAPhoto("the link gave no video")


_URI = re.compile(r'URI="([^"]*)"')
_SEGMENT_SUFFIXES = (".ts", ".m4s", ".mp4", ".aac", ".m4a", ".m4v")


def _variant(text: str, url: str) -> str | None:
    """A master playlist's best stream (highest bandwidth), or None for a media playlist."""
    best: tuple[int, str] | None = None
    lines = [line.strip() for line in text.splitlines()]
    for n, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        found = re.search(r"BANDWIDTH=(\d+)", line)
        rate = int(found.group(1)) if found else 0
        target = next((x for x in lines[n + 1 :] if x and not x.startswith("#")), None)
        if target and (best is None or rate > best[0]):
            best = (rate, urljoin(url, target))
    return best[1] if best else None


def fetch_hls(url: str, dest: Path, fetch: _Fetch) -> None:
    """A streamed video: its playlist, every part and key downloaded with verified TLS (only
    from the playlist's host or a Strava host or CDN), then remuxed from local files."""
    from vibesynergy.query import video

    text = fetch.text(url)
    if not text.lstrip().startswith("#EXTM3U"):
        raise workout_media.NotAPhoto("the link gave no video")
    variant = _variant(text, url)
    if variant is not None:
        if not _host_allowed(variant, urlparse(url).hostname):
            raise ValueError("a part of the video is somewhere else")
        url, text = variant, fetch.text(variant)
        if _variant(text, url) is not None:
            raise workout_media.NotAPhoto("the stream has no video parts")
    host = urlparse(url).hostname
    with tempfile.TemporaryDirectory(prefix="vs-hls-") as tmp:
        folder = Path(tmp)
        lines: list[str] = []
        parts = 0

        def local(target: str, name: str) -> str:
            absolute = urljoin(url, target)
            if not _host_allowed(absolute, host):
                raise ValueError("a part of the video is somewhere else")
            with (folder / name).open("wb") as out:
                fetch.to(absolute, out)
            return name

        for raw in text.splitlines():
            line = raw.strip()
            uri = _URI.search(line) if line.startswith(("#EXT-X-KEY", "#EXT-X-MAP")) else None
            if uri is not None:
                parts += 1
                kind = "key" if line.startswith("#EXT-X-KEY") else "mp4"
                name = local(uri.group(1), f"part{parts:05d}.{kind}")
                line = line[: uri.start(1)] + name + line[uri.end(1) :]
            elif line and not line.startswith("#"):
                parts += 1
                suffix = Path(urlparse(line).path).suffix.lower()
                suffix = suffix if suffix in _SEGMENT_SUFFIXES else ".ts"
                line = local(line, f"part{parts:05d}{suffix}")
            lines.append(line)
        if not parts:
            raise workout_media.NotAPhoto("the stream has no video parts")
        playlist = folder / "index.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        try:
            video.remux_hls(playlist, dest)
        except video.NotAVideo as exc:
            raise workout_media.NotAPhoto(str(exc)) from exc


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
            if not isinstance(photo, dict):
                continue
            urls = photo.get("urls") or {}
            sizes = sorted(urls, key=lambda k: int(k) if str(k).isdigit() else 0)
            # A video's urls are its poster; the video itself is video_url.
            url = photo.get("video_url") or (urls.get(sizes[-1]) if sizes else None)
            photo_id = photo.get("unique_id") or photo.get("id")
            if not url or not photo_id or "placeholder" in str(url):
                continue  # still being processed on Strava: the next look takes it
            out.append(
                {
                    "photo_id": str(photo_id),
                    "url": str(url),
                    "video": bool(photo.get("video_url")),
                    "caption": photo.get("caption"),
                    "taken_at": _when(photo.get("created_at")),
                }
            )
        return out

    def download(self, url: str) -> bytes:
        return download(url, self.timeout)

    def download_video(self, url: str, dest: Path) -> None:
        download_video(url, dest, self.timeout)


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

    def download_video(self, url: str, dest: Path) -> None:
        download_video(url, dest, self.timeout)


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


def pull_one(
    rt, reader: Reader, source: str, source_id: str, videos: list[int] | None = None
) -> dict[str, Any]:
    """Take in the photos and videos of one Strava or Garmin activity VibeSynergy does not
    have yet. ``videos``: how many videos this run may still download (``[n]``, counted
    down); a video beyond it, or one while ffmpeg is missing, only waits (``waiting``)."""
    result: dict[str, Any] = {
        "added": 0,
        "same": 0,
        "skipped": 0,
        "errors": [],
        "listed": 0,
        "waiting": [],
    }
    listed = reader.photos(source_id)
    result["listed"] = len(listed)
    for photo in listed:
        if workout_media.known(rt.engine, source, photo["photo_id"]):
            continue
        if photo.get("video"):
            from vibesynergy.query import video

            if not video.available():
                result["waiting"].append("ffmpeg is not installed")
                continue
            if videos is not None:
                if videos[0] <= 0:
                    result["waiting"].append("the next run takes the next videos")
                    continue
                videos[0] -= 1
            _pull_video(rt, reader, source, source_id, photo, result)
            if result.pop("full", False):
                break
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


def _pull_video(
    rt, reader: Reader, source: str, source_id: str, photo: dict[str, Any], result: dict
) -> None:
    """One video, as ``pull_one`` does a photo: what can never be read is not asked again; a
    download that fails is looked at once more, like a photo's. ffmpeg missing in the middle
    only waits and does not use up that look."""
    with tempfile.TemporaryDirectory(prefix="vs-video-") as tmp:
        path = Path(tmp) / "original"
        try:
            reader.download_video(photo["url"], path)
            done = workout_media.add_video(
                rt.engine,
                rt.raw.blobs,
                f"{source}:{source_id}",
                path,
                source=source,
                origin=(photo["photo_id"], source_id),
                caption=photo.get("caption"),
                taken_at=photo.get("taken_at"),
            )
        except RateLimited:
            raise
        except (workout_media.NotAPhoto, workout_media.TooLarge):
            workout_media.skip(rt.engine, source, photo["photo_id"], source_id)
            result["skipped"] += 1
            return
        except workout_media.TooMany:
            result["errors"].append("the workout has as many photos as it can have")
            result["full"] = True
            return
        except workout_media.NoVideoTool:
            result["waiting"].append("ffmpeg is not installed")
            return
        except Exception as exc:  # an expired link, a network hiccup, the deadline
            result["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
            return
    if done["state"] == "added":
        result["added"] += 1
    elif done["state"] == "same":
        result["same"] += 1


def _retried(check: dict[str, Any] | None, raw_id: str) -> bool:
    """Whether this stored version already had its one more look (a wait does not count)."""
    return bool(
        check
        and check["raw_id"] == raw_id
        and check["next_at"] is not None
        and not str(check["error"] or "").startswith(WAITING)
    )


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
    videos = [MAX_VIDEOS_PER_RUN]
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
            result = pull_one(rt, reader, source, source_id, videos)
        except RateLimited:
            summary["rate_limited"] = True
            break  # the budget is spent: the rest waits for the next run
        except Exception as exc:
            log.warning("photos of %s:%s not taken in: %s", source, source_id, exc)
            summary["failed"] += 1
            retried = _retried(check, raw_id)
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
        # One more look per stored version: what fails twice waits for a new version. What
        # only waits (ffmpeg, the run's video budget) is looked at again without using it up.
        retried = _retried(check, raw_id)
        waiting = result.get("waiting") or []
        if errors:
            next_at, error = (None if retried else now + RETRY), "; ".join(errors)
        elif waiting:
            later = RETRY if any("ffmpeg" in w for w in waiting) else timedelta(0)
            next_at, error = now + later, WAITING + "; ".join(waiting)
        else:
            next_at, error = None, None
        with rt.engine.begin() as conn:
            _save(
                conn,
                source,
                source_id,
                raw_id=raw_id,
                photos=photos,
                checked_at=now,
                next_at=next_at,
                error=error[:500] if error else None,
            )
    return summary
