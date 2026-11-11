"""GPX/TCX files and the drop folder (and portal upload)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from vibesynergy import dropfolder
from vibesynergy.model import Activity, Stream
from vibesynergy.sources.tracks import map_track

T0 = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)


def gpx(points: int = 5, sport: str = "running") -> bytes:
    ext = "gpxtpx:TrackPointExtension"
    pts = "".join(
        f'<trkpt lat="53.2{i:02d}" lon="5.79"><ele>{i}</ele>'
        f"<time>{(T0 + timedelta(seconds=10 * i)).isoformat()}</time>"
        f"<extensions><{ext}><gpxtpx:hr>{140 + i}</gpxtpx:hr></{ext}></extensions></trkpt>"
        for i in range(points)
    )
    return (
        '<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        f"<trk><name>Test run</name><type>{sport}</type><trkseg>{pts}</trkseg></trk></gpx>"
    ).encode()


TCX = (
    '<?xml version="1.0"?><TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
    '<Activities><Activity Sport="Biking"><Lap><Track>'
    + "".join(
        f"<Trackpoint><Time>{(T0 + timedelta(seconds=i)).isoformat()}</Time>"
        f"<DistanceMeters>{i * 8}</DistanceMeters><HeartRateBpm><Value>130</Value></HeartRateBpm>"
        f"<Extensions><TPX><Watts>{200 + i}</Watts></TPX></Extensions></Trackpoint>"
        for i in range(3)
    )
    + "</Track></Lap></Activity></Activities></TrainingCenterDatabase>"
).encode()


def test_gpx_and_tcx_become_activity_and_stream() -> None:
    activity, stream = map_track(gpx(), "gpx-1", timezone="Europe/Amsterdam")
    assert isinstance(activity, Activity) and isinstance(stream, Stream)
    assert activity.sport == "running" and activity.name == "Test run" and activity.elapsed_s == 40
    assert activity.distance_m == pytest.approx(4 * 111.2, rel=0.01)  # 0.001° latitude steps
    assert activity.max_hr == 144 and activity.ascent_m == 4 and stream.columns["hr"][0] == 140
    ride, _ = map_track(TCX, "tcx-1", timezone="Europe/Amsterdam")
    assert ride.sport == "cycling" and ride.distance_m == 16 and ride.avg_power == 201
    with pytest.raises(ValueError):
        map_track(gpx(points=1), "gpx-2", timezone="UTC")


def test_drop_folder_imports_moves_and_reports(runtime) -> None:
    base = dropfolder.folder(runtime.settings)
    (base / "run.gpx").write_bytes(gpx())
    (base / "broken.gpx").write_bytes(b"<gpx><trk>not closed")
    fresh = base / "still-copying.tcx"
    fresh.write_bytes(TCX)
    old = datetime.now().timestamp() - 60
    for name in ("run.gpx", "broken.gpx"):
        os.utime(base / name, (old, old))
    results = {r["file"]: r for r in dropfolder.scan(runtime)}
    assert results["run.gpx"]["imported"] == 1 and (base / "imported" / "run.gpx").exists()
    assert results["broken.gpx"]["status"] == "failed"
    assert (base / "failed" / "broken.gpx.error.txt").exists()
    assert "still-copying.tcx" not in results and fresh.exists()  # not settled yet
    (base / "again.gpx").write_bytes(gpx())
    again = dropfolder.scan(runtime, settle_s=0)
    assert {r["file"]: r["skipped"] for r in again if r["file"] == "again.gpx"} == {"again.gpx": 1}
    assert dropfolder.history(runtime.settings)[0]["file"] in {"again.gpx", "still-copying.tcx"}
