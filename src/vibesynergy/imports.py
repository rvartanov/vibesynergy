"""``vibesynergy import``: archive files from disk through the same path as synced data.

Zips are walked recursively (the Garmin export nests zips of FIT files); nested archives are
spooled to temporary files so multi-gigabyte exports never have to fit in memory.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from sqlalchemy import func, select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.fit.decode import is_fit
from vibesynergy.runtime import Runtime
from vibesynergy.sources.base import FetchedItem
from vibesynergy.sources.garmin.export import item_for
from vibesynergy.sources.strava import export as strava_export
from vibesynergy.sources.tracks import is_gpx, is_tcx
from vibesynergy.store.schema import activity_link
from vibesynergy.sync.engine import DatasetResult

log = logging.getLogger(__name__)

# progress(text, done, total): done and total are given when the import knows how many
# there are, so a page can show "1,240 of 7,300".
Progress = Callable[..., None]
STRAVA_PROGRESS_EVERY = 25
STRAVA_MEDIA_CSV = "media.csv"
# The archive's videos: taken in as videos (MP4 and MOV; another kind is not readable).
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".3gp")


@dataclass
class MediaResult(DatasetResult):
    """The archive's workout photos and videos. ``new_versions`` is the ones added (``videos``
    of them videos) and ``skipped`` the ones VibeSynergy already had (from the API, an
    earlier import, or the same file twice); the rest is counted by why it was left out."""

    listed: int = 0  # photos and videos the activities name
    videos: int = 0  # of the ones added (new_versions), the videos
    missing: int = 0  # named in activities.csv but not in the zip
    unreadable: int = 0  # not a photo VibeSynergy can read, or too large: not tried again
    full: int = 0  # the workout has as many photos as it can hold
    failed: int = 0  # anything else; the other photos still come in


# An export is a zip of zips of small files. These bounds are far above any real export and
# stop an archive built to fill memory or disk (a "zip bomb", or a zip that contains itself).
MAX_ZIP_DEPTH = 4
MAX_MEMBER_BYTES = 2 * 1024**3
MAX_UNPACKED_BYTES = 64 * 1024**3


class ArchiveTooLarge(ValueError):
    """The archive unpacks to more than an export ever does."""


def gunzip(data: bytes, limit: int = MAX_MEMBER_BYTES) -> bytes:
    """``gzip.decompress`` that stops at ``limit`` bytes instead of filling memory."""
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        out = stream.read(limit + 1)
    if len(out) > limit:
        raise ArchiveTooLarge("a compressed file in this archive is too large to import")
    return out


def walk_zip(
    fileobj: IO[bytes] | Path,
    prefix: str = "",
    on_progress: Callable[[float], None] | None = None,
    *,
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(path, bytes)`` for every non-zip file, descending into nested zips. macOS
    metadata (``__MACOSX/``, ``._*`` files, added when a folder is zipped again on a Mac) is
    left out.

    ``on_progress(fraction)`` is told how far through the zip the walk is (0..1, by compressed
    size, so a big nested zip of FIT files moves it gradually rather than in one jump)."""
    if _depth > MAX_ZIP_DEPTH:
        raise ArchiveTooLarge("this archive has zip files nested too deep to import")
    budget = [MAX_UNPACKED_BYTES] if _budget is None else _budget
    with zipfile.ZipFile(fileobj) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and not _mac_metadata(i.filename)]
        total = sum(max(i.compress_size, 1) for i in infos) or 1
        before = 0
        for info in infos:
            budget[0] -= info.file_size
            if info.file_size > MAX_MEMBER_BYTES or budget[0] < 0:
                raise ArchiveTooLarge("this archive unpacks to more than an export can hold")
            weight = max(info.compress_size, 1)
            inner = (
                None
                if on_progress is None
                else lambda f, base=before, w=weight: on_progress((base + w * f) / total)
            )
            path = f"{prefix}{info.filename}"
            if info.filename.lower().endswith(".zip"):
                with tempfile.TemporaryFile() as tmp, zf.open(info) as member:
                    shutil.copyfileobj(member, tmp)
                    tmp.seek(0)
                    if zipfile.is_zipfile(tmp):
                        tmp.seek(0)
                        yield from walk_zip(
                            tmp,
                            prefix=f"{path}/",
                            on_progress=inner,
                            _depth=_depth + 1,
                            _budget=budget,
                        )
                    else:
                        tmp.seek(0)
                        yield path, tmp.read()
            else:
                yield path, zf.read(info)
            before += weight
            if on_progress is not None:
                on_progress(before / total)


def _mac_metadata(name: str) -> bool:
    return name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1].startswith("._")


def archive_kind(path: Path) -> str | None:
    """Which account export a zip is: ``garmin_export`` (Garmin's data export has a
    DI_CONNECT folder), ``strava_export`` (Strava's archive has activities.csv), or None.
    Reads only the zip's table of contents."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return None
    if any(n.startswith("DI_CONNECT/") or "/DI_CONNECT/" in n for n in names):
        return "garmin_export"
    if any(n.rsplit("/", 1)[-1].lower() == strava_export.CSV_NAME for n in names):
        return "strava_export"
    return None


# The Garmin export's progress is a share of the zip (its files aren't known up front: most
# sit in nested zips), reported as done of GARMIN_PROGRESS_SCALE.
GARMIN_PROGRESS_SCALE = 1000
GARMIN_PROGRESS_EVERY = 200


def import_garmin_export(
    rt: Runtime, path: Path, progress: Progress | None = None
) -> list[DatasetResult]:
    """The Garmin Connect data export (DI_CONNECT). Every file is archived by content hash
    (FIT files) or path, so importing the same export again adds nothing."""
    results = {name: DatasetResult("garmin_export", name, None) for name in ("files", "fit")}
    started = utc_now()
    share = [0.0]

    def moved(fraction: float) -> None:
        share[0] = fraction

    def tell(count: int) -> None:
        if progress:
            done = min(GARMIN_PROGRESS_SCALE, int(share[0] * GARMIN_PROGRESS_SCALE))
            progress(f"{count} files", done, GARMIN_PROGRESS_SCALE)

    tell(0)
    count = 0
    for count, (member, data) in enumerate(walk_zip(path, on_progress=moved), start=1):
        sha256 = hashlib.sha256(data).hexdigest()
        dataset, item = item_for(member, data, is_fit=is_fit(data), sha256=sha256)
        rt.sync.ingest("garmin_export", dataset, item, results[dataset])
        if count % GARMIN_PROGRESS_EVERY == 0:
            tell(count)
    share[0] = 1.0
    tell(count)
    for result in results.values():
        rt.sync.record_state(result, started)
    rt.sync.changed(list(results.values()))
    return list(results.values())


def import_strava_export(
    rt: Runtime, path: Path, progress: Progress | None = None
) -> list[DatasetResult]:
    """The Strava account archive: activities.csv, then each activity's original file
    (unzipped from .gz), then the workout photos. Files already archived by another route are
    skipped by hash; a photo VibeSynergy already has (also from the API) is skipped by its
    Strava photo id."""
    source = strava_export.StravaExportSource.id
    results = {name: DatasetResult(source, name, None) for name in ("activities", "files")}
    media = MediaResult(source, "media", None)
    started = utc_now()
    with zipfile.ZipFile(path) as zf:
        members = set(zf.namelist())
        csv_path = next(
            (n for n in sorted(members) if n.rsplit("/", 1)[-1].lower() == strava_export.CSV_NAME),
            None,
        )
        if csv_path is None:
            raise ValueError(
                f"no {strava_export.CSV_NAME} in {path.name}: is it the Strava account archive?"
            )
        base = csv_path[: -len(strava_export.CSV_NAME)]
        table = zf.read(csv_path)
        item = FetchedItem(strava_export.CSV_NAME, table, request={"path": csv_path})
        rt.sync.ingest(source, "activities", item, results["activities"])
        files = strava_export.files_by_activity(table)
        photos = _unique_media(strava_export.media_by_activity(table))
        total = len(files) + len(photos)
        if progress:
            progress(f"0 of {len(files)} files", 0, total)
        for count, (name, activity_id) in enumerate(files.items(), start=1):
            if progress and count % STRAVA_PROGRESS_EVERY == 0:
                progress(f"{count} of {len(files)} files", count, total)
            member = base + name
            if member not in members:
                continue
            data = zf.read(member)
            if member.lower().endswith(".gz"):
                data = gunzip(data)
            sha256 = hashlib.sha256(data).hexdigest()
            if rt.raw.has_content(sha256):  # e.g. the FIT Garmin uploaded to Strava
                results["files"].skipped += 1
                continue
            request = {"path": member, "activity_id": activity_id}
            item = FetchedItem(sha256, data, request=request, immutable=True)
            rt.sync.ingest(source, "files", item, results["files"])
        if progress:
            progress(f"{len(files)} of {len(files)} files", len(files), total)
        for result in results.values():
            rt.sync.record_state(result, started)
        # The photos belong to the workouts, so those must exist first: grouped by derive.
        rt.sync.changed(list(results.values()))
        _import_strava_media(rt, zf, members, base, photos, media, progress, len(files), total)
    return [*results.values(), media]


def _unique_media(listed: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each media file once (activity id, path): the first activity that names it."""
    seen: set[str] = set()
    out = []
    for activity_id, name in listed:
        photo_id = strava_export.photo_id(name)
        if photo_id and photo_id not in seen:
            seen.add(photo_id)
            out.append((activity_id, name))
    return out


def _photo_workout(rt: Runtime, activity_id: str) -> str:
    """The workout a photo goes to: the API's copy when there is one, else the archive's."""
    with rt.engine.connect() as conn:
        linked = conn.scalar(
            select(func.count())
            .select_from(activity_link)
            .where(activity_link.c.source == "strava", activity_link.c.source_id == activity_id)
        )
    return f"strava:{activity_id}" if linked else f"strava_export:{activity_id}"


def _import_strava_media(
    rt: Runtime,
    zf: zipfile.ZipFile,
    members: set[str],
    base: str,
    photos: list[tuple[str, str]],
    result: MediaResult,
    progress: Progress | None,
    offset: int,
    total: int,
) -> None:
    """Take in the archive's photos and videos through the same path as the ones from the
    API (source ``strava``, the photo id as origin), so each one is kept once."""
    from vibesynergy.query import workout_media

    result.listed = len(photos)
    captions: dict[str, str] = {}
    if base + STRAVA_MEDIA_CSV in members:
        captions = strava_export.media_captions(zf.read(base + STRAVA_MEDIA_CSV))
    for count, (activity_id, name) in enumerate(photos, start=1):
        if progress and count % STRAVA_PROGRESS_EVERY == 0:
            progress(f"{count} of {len(photos)} photos", offset + count, total)
        photo_id = strava_export.photo_id(name)
        is_video = name.lower().endswith(VIDEO_SUFFIXES)
        if workout_media.known(rt.engine, "strava", photo_id):
            result.skipped += 1
            continue
        member = base + name
        if member not in members:
            result.missing += 1
            continue
        try:
            limit = workout_media.MAX_VIDEO_BYTES if is_video else workout_media.MAX_BYTES
            if zf.getinfo(member).file_size > limit:
                raise workout_media.TooLarge("the file is too large")
            result.fetched += 1
            options = {
                "source": "strava",
                "origin": (photo_id, activity_id),
                "caption": captions.get(photo_id),
            }
            key = _photo_workout(rt, activity_id)
            if is_video:
                with tempfile.TemporaryDirectory(prefix="vs-video-") as tmp:
                    path = Path(tmp) / "original"
                    with zf.open(member) as src, path.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    done = workout_media.add_video(rt.engine, rt.raw.blobs, key, path, **options)
            else:
                done = workout_media.add(rt.engine, rt.raw.blobs, key, zf.read(member), **options)
        except (workout_media.NotAPhoto, workout_media.TooLarge):
            workout_media.skip(rt.engine, "strava", photo_id, activity_id)
            result.unreadable += 1
            continue
        except workout_media.TooMany:
            result.full += 1
            continue
        except Exception as exc:  # one photo; the others still come in
            log.warning("a photo from the Strava archive was not taken in: %s", type(exc).__name__)
            result.failed += 1
            continue
        if done["state"] == "added":
            result.new_versions += 1
            result.videos += is_video
        else:
            result.skipped += 1
    if progress and photos:
        progress(f"{len(photos)} of {len(photos)} photos", offset + len(photos), total)


def _file_kind(content: bytes) -> str | None:
    """The ``files`` dataset an activity file belongs to, or None when it is no activity file."""
    if is_fit(content):
        return "fit"
    if is_gpx(content):
        return "gpx"
    if is_tcx(content):
        return "tcx"
    return None


def import_files(rt: Runtime, path: Path, progress: Progress | None = None) -> list[DatasetResult]:
    """Import FIT, GPX and TCX files (and zips of them) from a file or directory tree.

    The same kinds the upload and the drop folder take (``import_one``). A file already
    archived is skipped by its content; a file that cannot be read counts as a map error and
    the rest still imports. Returns one result per kind that was seen (FIT always)."""
    results = {ds: DatasetResult("files", ds, None) for ds in ("fit", "gpx", "tcx")}
    started = utc_now()
    candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    for count, file in enumerate(candidates, start=1):
        if file.suffix.lower() == ".zip":
            members = walk_zip(file)
        else:
            members = iter([(str(file), file.read_bytes())])
        for name, data in members:
            dataset = _file_kind(data)
            if dataset is None:
                continue
            sha256 = hashlib.sha256(data).hexdigest()
            if rt.raw.has_content(sha256):  # same file already archived, e.g. from the export
                results[dataset].skipped += 1
                continue
            item = FetchedItem(sha256, data, request={"path": name}, immutable=True)
            rt.sync.ingest("files", dataset, item, results[dataset])
        if progress and count % 200 == 0:
            progress(f"{count} files")
    used = [r for ds, r in results.items() if ds == "fit" or r.fetched or r.skipped]
    for result in used:
        rt.sync.record_state(result, started)
    rt.sync.changed(used)
    return used


def import_one(rt: Runtime, name: str, data: bytes) -> dict[str, int]:
    """One file of any kind the drop folder accepts: FIT, GPX, TCX, or a zip of them.
    Returns counts: imported, skipped (already archived), ignored (not an activity file)."""
    counts = {"imported": 0, "skipped": 0, "ignored": 0}
    members = (
        walk_zip(io.BytesIO(data)) if zipfile.is_zipfile(io.BytesIO(data)) else iter([(name, data)])
    )
    results = {ds: DatasetResult("files", ds, None) for ds in ("fit", "gpx", "tcx")}
    started = utc_now()
    for member, content in members:
        dataset = _file_kind(content)
        if dataset is None:
            counts["ignored"] += 1
            continue
        sha256 = hashlib.sha256(content).hexdigest()
        if rt.raw.has_content(sha256):
            counts["skipped"] += 1
            continue
        item = FetchedItem(sha256, content, request={"path": member}, immutable=True)
        rt.sync.ingest("files", dataset, item, results[dataset])
        if results[dataset].normalise_errors:
            raise ValueError(f"{member} could not be read as {dataset.upper()}")
        counts["imported"] += 1
    used = [r for r in results.values() if r.fetched]
    for result in used:
        rt.sync.record_state(result, started)
    rt.sync.changed(used)
    return counts
