"""TSS from power, running best efforts (without non-running stretches), the Calendar API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from vibesynergy.core.timeutil import Window
from vibesynergy.derive import unify
from vibesynergy.model import Activity, Stream, ZoneSet
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import training

DAY = date(2026, 9, 1)
T0 = datetime(2026, 9, 1, 8, tzinfo=UTC)


def _put(rt, source, records, key):
    stored = rt.raw.put(
        source=source, dataset="test", item_key=key, payload=key.encode(), content_type="text/plain"
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)


def test_tss_formula_and_load() -> None:
    assert training.tss(3600, 250, 250) == pytest.approx(100)  # an hour at FTP
    assert training.tss(1800, 200, 250) == pytest.approx(32)  # half an hour at IF 0.8


def test_training_load_on_tss(runtime) -> None:
    _put(
        runtime, "garmin", [ZoneSet("power", "cycling", DAY, (100.0, 200.0), threshold=250.0)], "z"
    )
    _put(
        runtime,
        "garmin",
        [Activity("r1", T0, DAY, "cycling", elapsed_s=3600.0, norm_power=250.0)],
        "r1",
    )
    unify.rebuild(runtime.engine)
    out = training.training_load(runtime.engine, Window(DAY, DAY), load="tss")
    assert out["load"] == "tss" and out["rows"][0][1] == pytest.approx(100)


def test_pace_curve_skips_a_ride_home(runtime) -> None:
    # 3 km at 4:00/km (4.17 m/s), then 60 s at 12 m/s in a car, then 1 km slow.
    t, d, times, dist = 0.0, 0.0, [], []
    for speed, seconds in ((1000 / 240, 720), (12.0, 60), (2.5, 400)):
        for _ in range(int(seconds)):
            t += 1
            d += speed
            times.append(T0 + timedelta(seconds=t))
            dist.append(d)
    runtime.parquet.write_stream(Stream("run-1", {"t": times, "dist": dist}))
    _put(
        runtime,
        "files",
        [Activity("run-1", T0, DAY, "running", elapsed_s=t, fit_id="run-1")],
        "run-1",
    )
    unify.rebuild(runtime.engine)
    out = training.pace_curve(runtime.engine, runtime.parquet, Window(DAY, DAY))
    best = {b["distance_m"]: b["seconds"] for b in out["curve"]}
    assert best[1000] == pytest.approx(240, abs=1)  # the car's kilometre doesn't count
    assert best[400] == pytest.approx(96, abs=1)
    assert 5000 not in best  # never ran 5 km without the car
