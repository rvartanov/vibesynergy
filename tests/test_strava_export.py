"""The Strava account archive: activities.csv, the original files and the workout photos,
with synthetic data."""

from __future__ import annotations

import csv
import gzip
import io
import zipfile
from datetime import UTC, date, datetime

import pytest
from fitgen import activity_fit, utc
from sqlalchemy import func, select
from test_dropfolder import TCX, gpx
from test_workout_media import picture

from vibesynergy import imports
from vibesynergy.derive import unify
from vibesynergy.model import Activity, Stream
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import journal, workout_media
from vibesynergy.sources.strava.export import map_file, parse_when
from vibesynergy.store.schema import (
    activity,
    activity_link,
    activity_unified,
    media_item,
    media_origin,
    note,
)

RIDE = activity_fit(utc(2025, 3, 2, 9, 0, 0), 30, serial=4444)
KNOWN = activity_fit(utc(2025, 3, 1, 9, 0, 0), 30, serial=5555)  # also uploaded to Garmin

# As in real archives: Elapsed Time and Distance twice, rounded (km) and then precise (m).
HEADER = [
    "Activity ID", "Activity Date", "Activity Name", "Activity Type", "Activity Description",
    "Elapsed Time", "Distance", "Max Heart Rate", "Commute", "Activity Private Note",
    "Activity Gear", "Filename", "Elapsed Time", "Moving Time", "Distance", "Average Speed",
    "Elevation Gain", "Average Heart Rate", "Average Watts", "Calories",
]  # fmt: skip
ROWS = [
    ["1001", "Mar 2, 2025, 9:00:00 AM", "Rondje Utrecht", "Ride", "Wind!\nLegs good.",
     "30", "0.2", "150", "false", "Knee felt odd", "Canyon", "activities/1001.fit.gz",
     "30.0", "29.0", "210.5", "7.0", "12.0", "140", "220", "80"],
    ["1002", "3 Mar 2025, 18:30:00", "Evening run", "Trail Run", "", "300", "1.0", "160",
     "false", "", "Hoka", "activities/1002.gpx", "300.0", "290.0", "1000.0", "3.4", "5.0",
     "150", "", "300"],
    ["1003", "Mar 4, 2025, 12:15:00 PM", "Gym", "Weight Training", "", "1800", "0", "",
     "false", "", "", "", "1800.0", "1800.0", "0.0", "", "", "", "", "200"],
    ["1004", "Mar 1, 2025, 9:00:00 AM", "Saturday ride", "Ride", "", "30", "0.2", "",
     "false", "", "", "activities/1004.fit", "30.0", "29.0", "200.0", "", "", "", "", ""],
]  # fmt: skip


def archive(tmp_path, rows=ROWS) -> object:
    table = io.StringIO()
    writer = csv.writer(table)
    writer.writerow(HEADER)
    writer.writerows(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("export_12345/activities.csv", table.getvalue())
        zf.writestr("export_12345/activities/1001.fit.gz", gzip.compress(RIDE))
        zf.writestr("export_12345/activities/1002.gpx", gpx(20))
        zf.writestr("export_12345/activities/1004.fit", KNOWN)
        zf.writestr("export_12345/profile.csv", "Athlete ID\n1\n")
    path = tmp_path / "export_12345.zip"
    path.write_bytes(buf.getvalue())
    return path


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sep 27, 2026, 8:03:21 AM", datetime(2026, 9, 27, 8, 3, 21, tzinfo=UTC)),
        ("Sep 27, 2026, 8:03:21 PM", datetime(2026, 9, 27, 20, 3, 21, tzinfo=UTC)),
        ("Jan 1, 2020, 12:30:00 AM", datetime(2020, 1, 1, 0, 30, tzinfo=UTC)),
        ("27 Sep 2026, 08:03:21", datetime(2026, 9, 27, 8, 3, 21, tzinfo=UTC)),
        ("27 sep. 2026 08:03:21", datetime(2026, 9, 27, 8, 3, 21, tzinfo=UTC)),
        ("3 mrt 2025, 18:30:00", datetime(2025, 3, 3, 18, 30, tzinfo=UTC)),
        ("2026-09-27 08:03:21", datetime(2026, 9, 27, 8, 3, 21, tzinfo=UTC)),
        ("yesterday", None),
    ],
)
def test_activity_dates(text, expected) -> None:
    assert parse_when(text) == expected


def test_strava_quirks_in_original_files() -> None:
    # Strava's TCX files start with spaces before the XML declaration.
    streams = [r for r in map_file(b"          " + TCX, "7", "UTC") if isinstance(r, Stream)]
    assert len(streams) == 1 and streams[0].fit_id == "strava-export-7"
    # A route drawn on a map has no times: nothing to map, and no error.
    route = gpx(5).replace(b"<time>", b"<!--").replace(b"</time>", b"-->")
    assert map_file(route, "8", "UTC") == []


def test_strava_archive_import(runtime, tmp_path) -> None:
    imports.import_files(runtime, _file(tmp_path, KNOWN))  # 1004's FIT, archived earlier
    results = {r.dataset: r for r in imports.import_strava_export(runtime, archive(tmp_path))}
    assert results["activities"].fetched == 1 and results["activities"].normalise_errors == 0
    assert (results["files"].fetched, results["files"].skipped) == (2, 1)
    assert results["files"].normalise_errors == 0

    acts = {
        a["source_id"]: a for a in _rows(runtime, activity, activity.c.source == "strava_export")
    }
    assert set(acts) == {"1001", "1002", "1003", "1004"}
    ride, run, gym = acts["1001"], acts["1002"], acts["1003"]
    assert ride["distance_m"] == 210.5 and ride["elapsed_s"] == 30 and ride["moving_s"] == 29
    assert ride["start_utc"].replace(tzinfo=UTC) == datetime(2025, 3, 2, 9, tzinfo=UTC)
    assert ride["local_date"] == date(2025, 3, 2) and ride["avg_power"] == 220
    assert ride["extra"]["gear"] == "Canyon" and ride["fit_id"] == "strava-export-1001"
    assert run["sport"] == "TrailRun" and run["distance_m"] == 1000
    assert gym["fit_id"] is None  # no file: a manual entry
    assert runtime.parquet.read_stream("strava-export-1001").num_rows == 30
    assert runtime.parquet.read_stream("strava-export-1002").num_rows == 20

    again = {r.dataset: r for r in imports.import_strava_export(runtime, archive(tmp_path))}
    assert again["activities"].new_versions == 0 and again["files"].fetched == 0


def test_the_archive_lines_up_with_the_api_and_garmin(runtime, tmp_path) -> None:
    api_copy = Activity(
        "1001", datetime(2025, 3, 2, 9, tzinfo=UTC), date(2025, 3, 2), "Ride",
        name="Rondje Utrecht", elapsed_s=30.0, fit_id="strava-1001",
        extra={"description": "Wind!\nLegs good."},
    )  # fmt: skip
    stored = runtime.raw.put(
        source="strava", dataset="activities", item_key="1001", payload=b"{}",
        content_type="application/json",
    )  # fmt: skip
    with runtime.engine.begin() as conn:
        write_records(conn, [api_copy], source="strava", raw_item_id=stored.id)
    imports.import_files(runtime, _file(tmp_path, KNOWN))
    imports.import_strava_export(runtime, archive(tmp_path))
    unify.rebuild(runtime.engine)

    links = {(r["source"], r["source_id"]): r for r in _rows(runtime, activity_link)}
    assert links[("strava_export", "1001")]["group_id"] == links[("strava", "1001")]["group_id"]
    assert links[("strava_export", "1001")]["rule"] == "same_id"
    group = _rows(
        runtime,
        activity_unified,
        activity_unified.c.group_id == links[("strava", "1001")]["group_id"],
    )[0]
    assert group["stream_key"] == "strava-export-1001"  # the original file beats the API's
    # 1004's FIT came in as an imported file; the archive's row joins it by time
    assert (
        links[("strava_export", "1004")]["group_id"]
        == links[("files", _fit_id(runtime))]["group_id"]
    )

    journal.import_activity_notes(runtime.engine)
    notes = {(n["source"], n["source_ref"]): n for n in _rows(runtime, note)}
    assert notes[("strava", "1001")]["text"] == "Wind!\nLegs good.\n\nPrivate note: Knee felt odd"
    assert ("strava_export", "1001") not in notes  # one note per Strava activity
    assert journal.import_activity_notes(runtime.engine) == 0  # stable: no flip-flopping


def _file(tmp_path, data: bytes):
    path = tmp_path / f"{len(list(tmp_path.iterdir()))}.fit"
    path.write_bytes(data)
    return path


def _fit_id(rt) -> str:
    return _rows(rt, activity, activity.c.source == "files")[0]["source_id"]


# The workout photos (#114) ------------------------------------------------------------------

HILL, LAKE, API_PHOTO = picture(1, (200, 160)), picture(2, (200, 160)), picture(3, (200, 160))


def media_archive(tmp_path, media: dict[str, str]) -> object:
    """A small archive whose activities name media (``|``-separated), with the media folder:
    two photos, the hill photo again under another id, a file that is no photo, and one named
    photo that is missing from the zip. (Videos: test_workout_videos.py.)"""
    table = io.StringIO()
    writer = csv.writer(table)
    writer.writerow([*HEADER, "Media"])
    writer.writerows([*row, media.get(row[0], "")] for row in ROWS)
    captions = io.StringIO()
    csv.writer(captions).writerows(
        [["Media Filename", "Media Caption"], ["media/hill.jpg", "Top of the dyke"],
         ["media/lake.jpg", "caption"]]
    )  # fmt: skip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("export_1/activities.csv", table.getvalue())
        zf.writestr("export_1/activities/1001.fit.gz", gzip.compress(RIDE))
        zf.writestr("export_1/activities/1002.gpx", gpx(20))
        zf.writestr("export_1/media.csv", captions.getvalue())
        zf.writestr("export_1/media/hill.jpg", HILL)
        zf.writestr("export_1/media/hill-again.jpg", HILL)
        zf.writestr("export_1/media/lake.jpg", LAKE)
        zf.writestr("export_1/media/api.jpg", API_PHOTO)
        zf.writestr("export_1/media/broken.jpg", b"no picture in here")
    path = tmp_path / "export_1.zip"
    path.write_bytes(buf.getvalue())
    return path


MEDIA = {
    "1001": "media/hill.jpg|media/gone.jpg|media/hill-again.jpg",
    "1002": "media/lake.jpg|media/hill.jpg",  # hill.jpg again: one file, one photo
    "1003": "media/broken.jpg",
}


def _photos(rt, activity_id):
    return workout_media.photos(rt.engine, activity_id)["photos"]


def test_the_archive_brings_the_photos(runtime, tmp_path) -> None:
    results = {r.dataset: r for r in imports.import_strava_export(runtime, media_archive(
        tmp_path, MEDIA))}  # fmt: skip
    media = results["media"]
    assert isinstance(media, imports.MediaResult)
    assert media.listed == 5 and media.new_versions == 2
    assert (media.videos, media.missing, media.unreadable, media.failed) == (0, 1, 1, 0)
    assert media.skipped == 1  # hill-again.jpg is the hill photo: kept once
    ride = _photos(runtime, "strava_export:1001")
    assert [p["caption"] for p in ride] == ["Top of the dyke"]
    assert ride[0]["source"] == "strava" and ride[0]["places"] == ["strava"]
    lake = _photos(runtime, "strava_export:1002")
    assert [p["caption"] for p in lake] == [None]  # Strava's placeholder caption is no caption
    with runtime.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(media_item)) == 2
        origins = {r.photo_id: r for r in conn.execute(select(media_origin))}
    assert set(origins) == {"hill", "hill-again", "lake", "broken"}
    assert origins["broken"].media_id == ""  # not a photo: remembered, not tried again
    assert "gone" not in origins

    again = {r.dataset: r for r in imports.import_strava_export(runtime, media_archive(
        tmp_path, MEDIA))}["media"]  # fmt: skip
    assert again.new_versions == 0 and again.fetched == 0 and again.skipped == 4
    with runtime.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(media_item)) == 2


def test_photos_known_from_the_api_are_not_taken_in_again(runtime, tmp_path) -> None:
    api_copy = Activity(
        "1001", datetime(2025, 3, 2, 9, tzinfo=UTC), date(2025, 3, 2), "Ride",
        name="Rondje Utrecht", elapsed_s=30.0, fit_id="strava-1001",
    )  # fmt: skip
    stored = runtime.raw.put(
        source="strava", dataset="activities", item_key="1001", payload=b"{}",
        content_type="application/json",
    )  # fmt: skip
    with runtime.engine.begin() as conn:
        write_records(conn, [api_copy], source="strava", raw_item_id=stored.id)
    unify.rebuild(runtime.engine)
    taken = workout_media.add(
        runtime.engine, runtime.raw.blobs, "strava:1001", API_PHOTO, source="strava",
        origin=("api", "1001"),
    )  # fmt: skip
    assert taken["state"] == "added"

    path = media_archive(tmp_path, {"1001": "media/api.jpg|media/hill.jpg"})
    media = {r.dataset: r for r in imports.import_strava_export(runtime, path)}["media"]
    assert (media.new_versions, media.skipped, media.fetched) == (1, 1, 1)
    # The new photo joins the API's copy of the workout, next to the photo it had.
    assert len(_photos(runtime, "strava:1001")) == 2
    with runtime.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(media_item)) == 2


def test_one_photo_that_fails_does_not_stop_the_others(runtime, tmp_path, monkeypatch) -> None:
    real = workout_media.add

    def flaky(engine, blobs, key, data, **kw):
        if kw["origin"][0] == "hill":
            raise OSError("disk hiccup")
        return real(engine, blobs, key, data, **kw)

    monkeypatch.setattr(workout_media, "add", flaky)
    path = media_archive(tmp_path, {"1001": "media/hill.jpg|media/lake.jpg"})
    media = {r.dataset: r for r in imports.import_strava_export(runtime, path)}["media"]
    assert (media.failed, media.new_versions) == (1, 1)
    monkeypatch.setattr(workout_media, "add", real)
    retried = {r.dataset: r for r in imports.import_strava_export(runtime, path)}["media"]
    assert (retried.new_versions, retried.skipped) == (1, 1)  # the failed one comes in later


def test_a_full_workout_counts_the_rest_and_goes_on(runtime, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workout_media, "MAX_PER_WORKOUT", 1)
    path = media_archive(
        tmp_path, {"1001": "media/hill.jpg|media/lake.jpg", "1002": "media/api.jpg"}
    )
    media = {r.dataset: r for r in imports.import_strava_export(runtime, path)}["media"]
    assert (media.new_versions, media.full) == (2, 1)


def test_the_import_result_counts_files_and_photos_apart(runtime, tmp_path) -> None:
    from test_history import settle

    from vibesynergy import history

    path = media_archive(tmp_path, MEDIA)
    fill_id = history._insert(
        runtime, source="strava_export", method="archive", status="running", file=str(path)
    )
    assert history.resume_archives(runtime) == 1
    fill = settle(runtime, fill_id)
    assert fill["status"] == "done"
    assert fill["counts"] == {"files": 3, "photos": 2, "videos": 0}  # 1001, 1002, 1004
    assert fill["done"] == fill["total"] == 3 + 5
    assert "2 photos added" in fill["message"]
