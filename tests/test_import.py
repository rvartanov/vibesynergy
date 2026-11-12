"""Garmin account export and FIT folder imports, with synthetic files."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date

from fitgen import activity_fit, utc
from sqlalchemy import select

from vibesynergy import imports
from vibesynergy.store.schema import activity, activity_lap, daily_metric, fit_file, sleep_session

RIDE = activity_fit(utc(2020, 5, 3, 7, 0, 0), 20, serial=1111)
OLD_RUN = activity_fit(utc(2012, 6, 1, 18, 0, 0), 15, sport=1, serial=2222)


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def export_zip(tmp_path):
    uploaded = _zip({"someone_5000000001.fit": RIDE})
    summarized = [
        {
            "summarizedActivitiesExport": [
                {
                    "activityId": 5000000001,
                    "name": "Sunday ride",
                    "activityType": "road_biking",
                    "startTimeGmt": 1588489200000.0,  # 2020-05-03T07:00:00Z
                    "startTimeLocal": 1588496400000.0,  # +2 h
                    "duration": 3600000.0,  # ms
                    "movingDuration": 3500000.0,
                    "distance": 3000000.0,  # cm
                    "elevationGain": 21000.0,  # cm
                    "avgHr": 140.0,
                    "avgPower": 210.0,
                    "someExportOnlyField": "kept",
                }
            ]
        }
    ]
    uds = [{"calendarDate": "2020-05-03", "totalSteps": 12000, "restingHeartRate": 49}]
    sleep = [
        {
            "calendarDate": "2020-05-03",
            "sleepStartTimestampGMT": "2020-05-02T21:30:00.0",
            "sleepEndTimestampGMT": "2020-05-03T05:30:00.0",
            "deepSleepSeconds": 6000.0,
            "lightSleepSeconds": 16000.0,
            "remSleepSeconds": 6000.0,
            "awakeSleepSeconds": 600.0,
            "sleepScores": {"overallScore": 81},
        }
    ]
    data = _zip(
        {
            "DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip": uploaded,
            "DI_CONNECT/DI-Connect-Fitness/someone_0_summarizedActivities.json": json.dumps(
                summarized
            ).encode(),
            "DI_CONNECT/DI-Connect-Aggregator/UDSFile_2020-05-01_2020-08-09.json": json.dumps(
                uds
            ).encode(),
            "DI_CONNECT/DI-Connect-Wellness/2020-05-01_2020-08-09_1_sleepData.json": json.dumps(
                sleep
            ).encode(),
            "DI_CONNECT/DI-Connect-User/user_profile.json": b'{"userName": "someone"}',
            "README.txt": b"export readme",
        }
    )
    path = tmp_path / "export.zip"
    path.write_bytes(data)
    return path


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


def test_garmin_export_import(runtime, tmp_path) -> None:
    results = {r.dataset: r for r in imports.import_garmin_export(runtime, export_zip(tmp_path))}
    assert results["fit"].fetched == 1 and results["files"].fetched == 5
    assert all(r.error is None and r.normalise_errors == 0 for r in results.values())

    act = _rows(runtime, activity)[0]
    assert (act["source"], act["source_id"]) == ("garmin_export", "5000000001")
    assert act["distance_m"] == 30000 and act["moving_s"] == 3500 and act["ascent_m"] == 210
    assert act["local_date"] == date(2020, 5, 3) and act["tz_offset_s"] == 7200
    assert act["extra"]["someExportOnlyField"] == "kept"

    fit = _rows(runtime, fit_file)[0]
    assert fit["source_activity_id"] == "5000000001"  # from the file name
    assert runtime.parquet.read_stream(fit["fit_id"]).num_rows == 20

    metrics = {
        r["metric"]: r["value"]
        for r in _rows(runtime, daily_metric, daily_metric.c.source == "garmin_export")
    }
    assert metrics["daily.steps"] == 12000 and metrics["sleep.score"] == 81
    assert metrics["sleep.duration"] == 28000  # deep + light + rem
    assert _rows(runtime, sleep_session)[0]["awake_s"] == 600

    again = {r.dataset: r for r in imports.import_garmin_export(runtime, export_zip(tmp_path))}
    assert again["fit"].skipped == 1 and again["files"].new_versions == 0


def test_folder_import_creates_activities_and_skips_known_files(runtime, tmp_path) -> None:
    imports.import_garmin_export(runtime, export_zip(tmp_path))
    folder = tmp_path / "old"
    (folder / "2012").mkdir(parents=True)
    (folder / "2012" / "run.FIT").write_bytes(OLD_RUN)
    (folder / "ride-copy.fit").write_bytes(RIDE)  # already archived through the export
    (folder / "notes.txt").write_text("not a fit file")
    (folder / "bundle.zip").write_bytes(_zip({"a.fit": OLD_RUN}))  # same file again

    (result,) = imports.import_files(runtime, folder)
    assert result.dataset == "fit"
    assert (result.fetched, result.skipped, result.normalise_errors) == (1, 2, 0)
    run = _rows(runtime, activity, activity.c.source == "files")[0]
    assert run["sport"] == "running" and run["local_date"] == date(2012, 6, 1)
    assert run["fit_id"] == run["source_id"]
    assert len(_rows(runtime, activity_lap, activity_lap.c.source == "files")) == 1


def test_folder_import_takes_gpx_and_tcx_like_the_upload(runtime, tmp_path) -> None:
    """`vibesynergy import files` takes the same kinds as the upload and the drop folder:
    FIT, GPX and TCX (and zips of them), skips what is archived and counts per kind."""
    from test_dropfolder import TCX, gpx

    folder = tmp_path / "mixed"
    (folder / "sub").mkdir(parents=True)
    (folder / "run.FIT").write_bytes(OLD_RUN)
    (folder / "sub" / "morning.gpx").write_bytes(gpx())
    (folder / "ride.tcx").write_bytes(TCX)
    (folder / "bundle.zip").write_bytes(_zip({"again.gpx": gpx(), "other.gpx": gpx(points=7)}))
    (folder / "broken.gpx").write_bytes(
        b'<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"></gpx>'
    )
    (folder / "notes.txt").write_text("not an activity file")

    results = {r.dataset: r for r in imports.import_files(runtime, folder)}
    assert set(results) == {"fit", "gpx", "tcx"}
    assert (results["fit"].fetched, results["fit"].skipped) == (1, 0)
    # Three GPX files read (one without points: a map error), the zip's copy is skipped.
    assert (results["gpx"].fetched, results["gpx"].skipped) == (3, 1)
    assert results["gpx"].normalise_errors == 1
    assert (results["tcx"].fetched, results["tcx"].skipped) == (1, 0)
    sports = sorted(r["sport"] for r in _rows(runtime, activity, activity.c.source == "files"))
    assert len(sports) == 4  # the FIT run, two GPX runs and the TCX

    # Again: everything is known, nothing is fetched twice.
    again = {r.dataset: r for r in imports.import_files(runtime, folder)}
    assert [again[d].fetched for d in ("fit", "gpx", "tcx")] == [0, 0, 0]
    assert (again["fit"].skipped, again["gpx"].skipped, again["tcx"].skipped) == (1, 4, 1)
    assert len(_rows(runtime, activity, activity.c.source == "files")) == 4

    # One GPX file on its own, as the README says.
    single = tmp_path / "single.gpx"
    single.write_bytes(gpx(points=9))
    only = {r.dataset: r for r in imports.import_files(runtime, single)}
    assert only["gpx"].fetched == 1 and only["fit"].fetched == 0


def test_walk_zip_descends_nested_archives(tmp_path) -> None:
    inner = _zip({"deep.fit": RIDE})
    path = tmp_path / "outer.zip"
    path.write_bytes(
        _zip(
            {
                "a/inner.zip": inner,
                "b.json": b"{}",
                "__MACOSX/a/._inner.zip": b"\x00\x05\x16\x07",  # zipped again on a Mac
                "a/._summarizedActivities.json": b"\x00\x05\x16\x07",
            }
        )
    )
    assert sorted(name for name, _ in imports.walk_zip(path)) == ["a/inner.zip/deep.fit", "b.json"]


def test_walk_zip_reports_how_far_it_is_including_inside_nested_zips(tmp_path) -> None:
    inner = _zip({f"deep{i}.fit": RIDE + bytes([i]) for i in range(4)})
    path = tmp_path / "outer.zip"
    path.write_bytes(_zip({"a.json": b"{}", "b/inner.zip": inner, "c.json": b"[]"}))
    seen: list[float] = []
    names = [name for name, _ in imports.walk_zip(path, on_progress=seen.append)]
    assert len(names) == 6
    assert seen == sorted(seen) and seen[-1] == 1.0
    # moved while still inside the nested zip, not only once it was done
    assert len({round(f, 6) for f in seen}) > 3


def test_garmin_export_reports_progress_as_a_share(runtime, tmp_path) -> None:
    calls: list[tuple] = []
    imports.import_garmin_export(
        runtime, export_zip(tmp_path), progress=lambda *args: calls.append(args)
    )
    assert calls[0][1:] == (0, imports.GARMIN_PROGRESS_SCALE)
    text, done, total = calls[-1]
    assert done == total == imports.GARMIN_PROGRESS_SCALE and text.endswith("files")


def test_archive_kind(tmp_path) -> None:
    garmin, strava, other = tmp_path / "g.zip", tmp_path / "s.zip", tmp_path / "o.zip"
    garmin.write_bytes(_zip({"Garmin Data x/DI_CONNECT/DI-Connect-User/user.json": b"{}"}))
    strava.write_bytes(_zip({"export_1/activities.csv": b"Activity ID\n"}))
    other.write_bytes(_zip({"ride.fit": RIDE}))
    (tmp_path / "not.zip").write_bytes(b"nope")
    assert imports.archive_kind(garmin) == "garmin_export"
    assert imports.archive_kind(strava) == "strava_export"
    assert imports.archive_kind(other) is None
    assert imports.archive_kind(tmp_path / "not.zip") is None


def test_legacy_appledouble_archive_is_reprocessed_without_health_records(runtime):
    from vibesynergy.query import views
    from vibesynergy.store.schema import raw_item

    sidecar = bytes.fromhex("0005160700020000") + bytes(18)
    valid = json.dumps(
        [
            {
                "calendarDate": "2020-05-03",
                "sleepScores": {"overallScore": 80},
                "deepSleepSeconds": 3600,
            }
        ]
    ).encode()
    # Current ZIP imports already skip macOS sidecars. Older archived versions still need
    # to be safely reprocessed; exercise that path without changing the import filter.
    files = {
        "DI_CONNECT/example_sleepData.json": valid,
        "__MACOSX/DI_CONNECT/._example_sleepData.json": sidecar,
        "DI_CONNECT/broken_sleepData.json": b"not JSON",
        "DI_CONNECT/._broken_sleepData.json": b"also not JSON",
    }
    errors = 0
    for name, payload in files.items():
        raw = runtime.raw.put(
            source="garmin_export",
            dataset="files",
            item_key=name,
            payload=payload,
            content_type="application/json",
        )
        runtime.raw.mark_normalised(raw.id, error="JSONDecodeError: legacy attempt")
        result = runtime.normaliser.normalise(raw.id)
        errors += result.error is not None
    assert errors == 2
    rows = _rows(runtime, raw_item)
    assert len(rows) == 4
    metadata = next(r for r in rows if r["item_key"].startswith("__MACOSX/"))
    assert metadata["normalise_error"] is None and metadata["normalised_at"]
    assert len(_rows(runtime, sleep_session)) == 1
    assert views.sync_status(runtime.engine)["problems"][0]["processing"]["errors"] == 2
