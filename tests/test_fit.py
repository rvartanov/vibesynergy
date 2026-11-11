"""FIT decoding: streams, developer fields, zones, monitoring timestamps, identity."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fitgen import activity_fit, monitoring_fit, utc

from vibesynergy.fit.decode import _pace_zone_times, decode, fit_files_in, map_fit
from vibesynergy.model import (
    Activity,
    ActivityLap,
    ActivityZoneTime,
    FitFile,
    Stream,
    WellnessSamples,
    ZoneSet,
)

START = utc(2026, 9, 20, 6, 0, 0)


def _by_type(records):
    out: dict[type, list] = {}
    for r in records:
        out.setdefault(type(r), []).append(r)
    return out


def test_activity_file_records() -> None:
    records = _by_type(map_fit(activity_fit(START, 10), timezone="UTC", with_activity=True))
    (fit,) = records[FitFile]
    assert fit.file_type == "activity" and fit.sport == "cycling" and fit.start_utc == START
    assert fit.fit_id == f"garmin-3999999999-{int(START.timestamp())}"

    (stream,) = records[Stream]
    cols = stream.columns
    assert cols["t"][0] == START and len(cols["t"]) == 10
    assert abs(cols["lat"][0] - 52.37) < 1e-6 and abs(cols["lon"][0] - 4.89) < 1e-6
    assert cols["alt"][0] == 10 and cols["speed"][0] == 8 and cols["dist"][1] == 8
    assert cols["dev_Form Power"][:2] == [60, 61]
    assert "enhanced_speed" not in cols and "position_lat" not in cols

    hr, power = sorted(records[ZoneSet], key=lambda z: z.kind)
    assert hr.boundaries == (120, 140, 155, 170, 190) and hr.threshold == 172
    assert power.threshold == 250 and power.valid_from == date(2026, 9, 20)

    (act,) = records[Activity]
    assert act.tz_offset_s == 7200 and act.norm_power == 230 and act.fit_id == fit.fit_id
    assert len(records[ActivityLap]) == 1


def test_activity_only_when_asked() -> None:
    types = {type(r) for r in map_fit(activity_fit(START, 5), timezone="UTC")}
    assert Activity not in types and Stream in types


def test_local_date_uses_file_offset() -> None:
    late = datetime(2026, 9, 20, 23, 30, tzinfo=UTC)  # already the 21st at +2 h
    zones = [r for r in map_fit(activity_fit(late, 5), timezone="UTC") if isinstance(r, ZoneSet)]
    assert zones[0].valid_from == date(2026, 9, 21)


def test_monitoring_timestamp_16_and_stress() -> None:
    records = _by_type(map_fit(monitoring_fit(utc(2026, 9, 20)), timezone="UTC"))
    (samples,) = records[WellnessSamples]
    hr = [
        (t.minute, v)
        for t, m, v in zip(samples.ts, samples.metric, samples.value, strict=True)
        if m == "hr.sample"
    ]
    assert hr == [(0, 55), (1, 56), (2, 57), (3, 58), (4, 59)]
    assert "steps.cumulative.walking" in samples.metric
    stress = [v for m, v in zip(samples.metric, samples.value, strict=True) if m == "stress.sample"]
    assert stress == [20, 35]  # -1 (not measurable) dropped


def test_fit_files_in_zip_and_non_fit() -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.fit", activity_fit(START, 3))
        zf.writestr("readme.txt", "hi")
    assert [name for name, _ in fit_files_in(buf.getvalue())] == ["x.fit"]
    assert list(fit_files_in(b"not a fit file")) == []


def test_identity_without_file_id_falls_back_to_hash() -> None:
    fit = decode(activity_fit(START, 3))
    fit.messages["file_id"] = [{}]
    assert fit.fit_id.startswith("sha-")


# Pace zones: real device-recorded boundaries and zone time, never derived from HR/power.


def test_pace_zone_boundaries_and_recorded_time_from_the_device() -> None:
    pace_zones = (2.5, 3.0, 3.5, 4.0, 4.5)
    pace_zone_times = (120.0, 600.0, 900.0, 450.0, 150.0)
    records = _by_type(
        map_fit(
            activity_fit(START, 10, pace_zones=pace_zones, pace_zone_times=pace_zone_times),
            timezone="UTC",
            source_activity_id="garmin-9001",
        )
    )
    zone_sets = {z.kind: z for z in records[ZoneSet]}
    assert set(zone_sets) == {"hr", "power", "pace"}
    assert zone_sets["pace"].boundaries == pace_zones
    assert zone_sets["pace"].threshold is None  # zones_target carries no pace threshold

    zone_times = sorted(records[ActivityZoneTime], key=lambda z: z.zone)
    assert [z.kind for z in zone_times] == ["pace"] * 5
    assert [z.zone for z in zone_times] == [0, 1, 2, 3, 4]
    assert [z.seconds for z in zone_times] == list(pace_zone_times)
    assert [z.activity_source_id for z in zone_times] == ["garmin-9001"] * 5
    assert zone_times[0].low == 0.0  # the slowest zone has no real floor
    assert [z.low for z in zone_times[1:]] == list(pace_zones[:4])  # each floor = previous ceiling


def test_pace_zone_time_without_an_external_activity_id_uses_the_fit_id() -> None:
    data = activity_fit(START, 5, pace_zones=(3.0, 4.0), pace_zone_times=(60.0, 240.0))
    records = _by_type(map_fit(data, timezone="UTC"))
    fit_id = decode(data).fit_id
    assert all(z.activity_source_id == fit_id for z in records[ActivityZoneTime])


def test_pace_zone_time_uses_configured_speed_zones_when_summary_omits_bounds() -> None:
    fit = decode(activity_fit(START, 5, pace_zones=(3.0, 4.0), pace_zone_times=(60.0, 240.0)))
    del fit.messages["time_in_zone"][0]["speed_zone_high_boundary"]
    zones = list(_pace_zone_times(fit, "activity-1"))
    assert [z.low for z in zones] == [0.0, 3.0]


def test_no_pace_zones_without_speed_zone_messages() -> None:
    """Files/devices without pace zones configured stay unzoned: never derived from HR."""
    records = _by_type(map_fit(activity_fit(START, 5), timezone="UTC"))
    assert "pace" not in {z.kind for z in records[ZoneSet]}
    assert ActivityZoneTime not in records
