"""Race predictions for 5 km to 50 km (#9)."""

from __future__ import annotations

from datetime import date

import pytest

from vibesynergy.query import race_predictions as rp

TODAY = date(2026, 11, 12)


def test_riegel_with_a_stricter_exponent_beyond_the_half() -> None:
    assert rp.riegel_split(1500, 5000, 10000) == pytest.approx(1500 * 2**1.06)
    # 10 km -> marathon: 1.06 up to the half, 1.08 from there.
    expected = 3000 * (rp.HALF / 10000) ** 1.06 * (42195 / rp.HALF) ** 1.08
    assert rp.riegel_split(3000, 10000, 42195) == pytest.approx(expected)
    # Backwards gives the same pair.
    assert rp.riegel_split(expected, 42195, 10000) == pytest.approx(3000)


def test_one_hour_at_threshold_and_the_fastest_prediction_counts() -> None:
    threshold = {"kind": "threshold", "distance_m": 3600 / 310 * 1000, "seconds": 3600.0,
                 "date": "2026-11-03"}  # fmt: skip
    easy_half = {"kind": "effort", "distance_m": 21097.5, "seconds": 8608.0, "date": "2026-10-20"}
    out = {r["key"]: r for r in rp.predict([threshold, easy_half], TODAY)["predictions"]}
    # The easy long run predicts slower: the threshold hour counts.
    assert out["half"]["from"]["kind"] == "threshold"
    assert out["half"]["seconds"] == round(rp.riegel_split(3600, threshold["distance_m"], 21097.5))
    assert out["10k"]["certainty"] == "high"  # within twice the hour's distance, recent
    assert [r["key"] for r in rp.predict([threshold], TODAY)["predictions"]] == [
        "5k", "10k", "10mi", "half", "marathon", "50k"
    ]  # fmt: skip
    # A race faster than the threshold suggests takes over.
    race = {"kind": "effort", "distance_m": 10000, "seconds": 2700.0, "date": "2026-11-08"}
    out = {r["key"]: r for r in rp.predict([threshold, race], TODAY)["predictions"]}
    assert out["10k"]["seconds"] == 2700 and out["10k"]["from"]["kind"] == "effort"


def test_far_reaches_and_old_evidence_are_less_certain() -> None:
    assert rp.certainty(1.5, 10) == "high"
    assert rp.certainty(3.0, 10) == "medium"
    assert rp.certainty(1.5, 60) == "medium"
    assert rp.certainty(6.0, 10) == "low"
    assert rp.certainty(1 / 6.0, 10) == "low"


def test_no_evidence_no_prediction() -> None:
    assert all(r["seconds"] is None for r in rp.predict([], TODAY)["predictions"])


def test_the_threshold_counts_only_from_the_day_it_was_set(runtime) -> None:
    from vibesynergy.query import thresholds

    thresholds.set_value(
        runtime.engine, sport="run", metric="threshold_pace", value=310, date_set=TODAY
    )
    assert rp._threshold(runtime.engine, TODAY)["pace_s_per_km"] == 310
    assert rp._threshold(runtime.engine, date(2026, 10, 1)) is None
    out = rp.race_predictions(runtime.engine, runtime.parquet, TODAY)
    half = next(r for r in out["predictions"] if r["key"] == "half")
    assert half["seconds"] and half["change_s"] is None  # nothing six weeks ago to compare


def test_portal_and_mcp(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/race-predictions").json()["data"]
    assert [r["key"] for r in data["predictions"]][-1] == "50k"
