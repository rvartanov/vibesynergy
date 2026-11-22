"""Mappers for Garmin datasets that were archived raw only (#125, group A), plus workout
steps and rounds on activities (group B).

The fixtures have the field names and nesting of real Garmin Connect replies (checked
read-only against the archive); every value is made up. Unknown fields such as
``someNewField`` must be ignored.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from vibesynergy.derive.unify import source_order
from vibesynergy.model import Activity, DailyMetric
from vibesynergy.query.catalog import KNOWN
from vibesynergy.sources.garmin import mappers
from vibesynergy.store.raw import RawRecord

DEVICE = "1000000001"


def raw(dataset: str, key: str, payload: Any) -> RawRecord:
    return RawRecord(
        "r1",
        "garmin",
        dataset,
        key,
        "x",
        "application/json",
        {},
        None,
        None,
        datetime(2026, 9, 20, 12, tzinfo=UTC),
        None,
        json.dumps(payload).encode(),
    )


def metrics(records) -> dict[tuple[str, str], float]:
    out = {}
    for record in records:
        assert isinstance(record, DailyMetric)
        assert record.metric in KNOWN, record.metric
        assert KNOWN[record.metric].unit == record.unit, record.metric
        out[(record.date.isoformat(), record.metric)] = record.value
    return out


def test_mappers_are_registered() -> None:
    built = mappers.build("Europe/Amsterdam")
    for name in (
        "morning_training_readiness",
        "training_status",
        "respiration",
        "endurance_score",
        "hill_score",
        "running_tolerance",
        "lactate_threshold",
        "cycling_ftp",
    ):
        assert name in built


def test_morning_readiness_and_its_factors() -> None:
    payload = {
        "calendarDate": "2026-09-20",
        "timestamp": "2026-09-20T05:10:00.0",
        "score": 64,
        "level": "MODERATE",
        "sleepScoreFactorPercent": 80,
        "sleepHistoryFactorPercent": 71,
        "recoveryTimeFactorPercent": 55,
        "hrvFactorPercent": 90,
        "acwrFactorPercent": 88,
        "stressHistoryFactorPercent": 66,
        "recoveryTime": 420,
        "feedbackShort": "TEXT",
        "someNewField": 1,
    }
    got = metrics(mappers.map_morning_training_readiness(raw("m", "2026-09-20", payload)))
    assert got == {
        ("2026-09-20", "recovery.readiness.morning"): 64,
        ("2026-09-20", "recovery.readiness.sleep"): 80,
        ("2026-09-20", "recovery.readiness.sleep_history"): 71,
        ("2026-09-20", "recovery.readiness.recovery_time"): 55,
        ("2026-09-20", "recovery.readiness.hrv"): 90,
        ("2026-09-20", "recovery.readiness.acute_load"): 88,
        ("2026-09-20", "recovery.readiness.stress_history"): 66,
        ("2026-09-20", "recovery.recovery_time"): 420,
    }
    # Several readings: the first of the day counts. Nothing scored: nothing mapped.
    later = {**payload, "timestamp": "2026-09-20T09:00:00.0", "score": 30}
    got = metrics(mappers.map_morning_training_readiness(raw("m", "2026-09-20", [later, payload])))
    assert got[("2026-09-20", "recovery.readiness.morning")] == 64
    assert list(mappers.map_morning_training_readiness(raw("m", "2026-09-20", {}))) == []


def _training_status(day: str) -> dict:
    return {
        "userId": 1,
        "mostRecentVO2Max": {
            "generic": {"calendarDate": day, "vo2MaxValue": 50.0},
            "heatAltitudeAcclimation": {
                "calendarDate": day,
                "heatAcclimationPercentage": 35,
                "altitudeAcclimation": 400,
                "currentAltitude": 10,
            },
        },
        "mostRecentTrainingLoadBalance": {
            "metricsTrainingLoadBalanceDTOMap": {
                "999": {"calendarDate": day, "primaryTrainingDevice": False,
                        "monthlyLoadAerobicLow": 1.0},
                DEVICE: {
                    "calendarDate": day,
                    "deviceId": int(DEVICE),
                    "primaryTrainingDevice": True,
                    "monthlyLoadAerobicLow": 410.5,
                    "monthlyLoadAerobicHigh": 300.0,
                    "monthlyLoadAnaerobic": 95.0,
                    "trainingBalanceFeedbackPhrase": "TEXT",
                },
            },
        },
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                DEVICE: {
                    "calendarDate": day,
                    "primaryTrainingDevice": True,
                    "trainingStatus": 7,
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 520,
                        "dailyTrainingLoadChronic": 480,
                        "dailyAcuteChronicWorkloadRatio": 1.1,
                        "acwrStatus": "OPTIMAL",
                    },
                    "someNewField": 1,
                }
            }
        },
    }  # fmt: skip


def test_training_status_loads_balance_and_acclimation() -> None:
    item = raw("t", "2026-09-20", _training_status("2026-09-20"))
    got = metrics(mappers.map_training_status(item))
    assert got == {
        ("2026-09-20", "training.load.acute"): 520,
        ("2026-09-20", "training.load.chronic"): 480,
        ("2026-09-20", "training.load.ratio"): 1.1,
        ("2026-09-20", "training.load_balance.aerobic_low"): 410.5,  # the primary device
        ("2026-09-20", "training.load_balance.aerobic_high"): 300,
        ("2026-09-20", "training.load_balance.anaerobic"): 95,
        ("2026-09-20", "fitness.acclimation.heat"): 35,
        ("2026-09-20", "fitness.acclimation.altitude"): 400,
    }


def test_training_status_from_an_earlier_day_is_not_copied() -> None:
    """Without a watch sync, Garmin repeats the last day's values; they stay on that day."""
    stale = raw("t", "2026-09-21", _training_status("2026-09-20"))
    assert list(mappers.map_training_status(stale)) == []
    assert list(mappers.map_training_status(raw("t", "2026-09-21", {"placeholder": 1}))) == []


def test_respiration_lowest_and_highest() -> None:
    payload = {
        "calendarDate": "2026-09-20",
        "lowestRespirationValue": 9.0,
        "highestRespirationValue": 24.0,
        "avgWakingRespirationValue": 14.0,
        "respirationValuesArray": [[1, 12.0]],
    }
    got = metrics(mappers.map_respiration(raw("respiration", "2026-09-20", payload)))
    assert got == {("2026-09-20", "resp.min"): 9, ("2026-09-20", "resp.max"): 24}


def test_endurance_score_takes_the_daily_score_in_its_month() -> None:
    payload = {
        "startDate": "2026-09-01",
        "endDate": "2026-09-20",
        "avg": 1,
        "max": 2,
        "enduranceScoreDTO": {"calendarDate": "2026-09-20", "overallScore": 6100,
                              "contributors": [{"group": 0, "contribution": 50.0}]},
        "groupMap": {"2026-09-14": {"groupAverage": 6050}},
    }  # fmt: skip
    got = metrics(mappers.map_endurance_score(raw("endurance_score", "2026-09", payload)))
    assert got == {("2026-09-20", "fitness.endurance_score"): 6100}
    # A score dated in another month belongs to that month's item.
    assert list(mappers.map_endurance_score(raw("endurance_score", "2026-10", payload))) == []


def test_hill_score_per_day_with_its_parts() -> None:
    payload = {
        "hillScoreDTOList": [
            {"calendarDate": "2026-09-19", "overallScore": 60, "enduranceScore": 55,
             "strengthScore": 40, "primaryTrainingDevice": True},
            {"calendarDate": "2026-09-20", "overallScore": 10, "primaryTrainingDevice": False},
            {"calendarDate": "2026-09-20", "overallScore": 61, "enduranceScore": 56,
             "strengthScore": 41, "primaryTrainingDevice": True},
            {"calendarDate": "2026-08-31", "overallScore": 1},
        ],
        "maxScore": 61,
    }  # fmt: skip
    got = metrics(mappers.map_hill_score(raw("hill_score", "2026-09", payload)))
    assert got == {
        ("2026-09-19", "fitness.hill_score"): 60,
        ("2026-09-19", "fitness.hill_score.endurance"): 55,
        ("2026-09-19", "fitness.hill_score.strength"): 40,
        ("2026-09-20", "fitness.hill_score"): 61,
        ("2026-09-20", "fitness.hill_score.endurance"): 56,
        ("2026-09-20", "fitness.hill_score.strength"): 41,
    }


def test_running_tolerance_per_week() -> None:
    payload = [
        {"calendarDate": "2026-09-07", "startOfWeek": "2026-09-07", "endOfWeek": "2026-09-13",
         "tolerance": 90000, "totalImpactLoad": 40000, "totalDistance": 30000.0},
        {"calendarDate": "2026-08-31", "startOfWeek": "2026-08-31", "endOfWeek": "2026-09-06",
         "tolerance": 1, "totalImpactLoad": 1},
    ]  # fmt: skip
    got = metrics(mappers.map_running_tolerance(raw("running_tolerance", "2026-09", payload)))
    assert got == {
        ("2026-09-07", "training.running_tolerance"): 90000,
        ("2026-09-07", "training.running_impact_load"): 40000,
    }


def test_lactate_threshold_history() -> None:
    payload = {
        "heart_rate": [
            {"from": "2026-09-03", "until": "2026-09-03", "series": "running", "value": 160.0},
            {"from": "2026-09-05", "until": "2026-09-05", "series": "cycling", "value": 150.0},
        ],
        "speed": [{"from": "2026-09-03", "until": "2026-09-03", "series": "running",
                   "value": 0.3}],
        "power": [{"from": "2026-09-04", "until": "2026-09-04", "series": "running",
                   "value": 400.0}],
    }  # fmt: skip
    got = metrics(mappers.map_lactate_threshold(raw("lactate_threshold", "2026-09", payload)))
    assert got == {
        ("2026-09-03", "fitness.lactate_threshold.hr"): 160,
        ("2026-09-03", "fitness.lactate_threshold.pace"): 300,  # s/km, as the profile's
        ("2026-09-04", "fitness.lactate_threshold.power"): 400,
    }


def test_cycling_ftp_on_the_day_it_was_set() -> None:
    payload = {"calendarDate": "2026-09-02", "functionalThresholdPower": 250, "sport": "CYCLING",
               "isStale": False, "someNewField": 1}  # fmt: skip
    got = metrics(mappers.map_cycling_ftp(raw("cycling_ftp", "current", payload)))
    assert got == {("2026-09-02", "fitness.cycling_ftp"): 250}
    assert list(mappers.map_cycling_ftp(raw("cycling_ftp", "current", None))) == []


@pytest.mark.parametrize(
    "dataset, key",
    [
        ("morning_training_readiness", "2026-09-20"),
        ("training_status", "2026-09-20"),
        ("respiration", "2026-09-20"),
        ("endurance_score", "2026-09"),
        ("hill_score", "2026-09"),
        ("running_tolerance", "2026-09"),
        ("lactate_threshold", "2026-09"),
        ("cycling_ftp", "current"),
    ],
)
@pytest.mark.parametrize("payload", [None, {}, [], {"placeholder": "x"}, "text", [1, "a"]])
def test_unfamiliar_replies_map_to_nothing(dataset: str, key: str, payload: Any) -> None:
    mapper = mappers.build("UTC")[dataset]
    assert list(mapper(raw(dataset, key, payload))) == []


def test_garmin_models_take_garmin_as_their_source() -> None:
    for metric in (
        "training.load.acute",
        "training.running_tolerance",
        "recovery.readiness.hrv",
        "recovery.recovery_time",
        "fitness.acclimation.heat",
        "fitness.endurance_score",
        "fitness.hill_score.strength",
        "fitness.lactate_threshold.pace",
        "fitness.cycling_ftp",
    ):
        assert source_order(metric) == ["garmin"], metric
    # The day's readiness keeps the general order; a VO2max from Withings is unaffected.
    assert source_order("recovery.readiness")[0] == "garmin"
    assert "withings" in source_order("fitness.vo2max.withings")


# Activities: workout steps and rounds ------------------------------------------------------


def _activity(**parts: Any) -> RawRecord:
    payload = {
        "summary": {
            "activityId": 7,
            "startTimeGMT": "2026-09-20 06:00:00",
            "startTimeLocal": "2026-09-20 08:00:00",
            "activityType": {"typeKey": "running"},
            "workoutId": 55,
        },
        **parts,
    }
    return raw("activities", "7", payload)


def test_workout_steps_and_rounds_on_the_activity() -> None:
    steps = {
        "activityId": 7,
        "stepDetailDTOs": [  # made-up wrapper name: any list of objects is read
            {"stepIndex": 0, "intensityType": "WARMUP", "duration": 600.0, "distance": 1800.0,
             "averageHR": 130.0, "startLatitude": 52.0, "laps": [{"lapIndex": 1}],
             "targetType": {"typeKey": "heart.rate.zone", "typeId": 4}, "notes": None},
            {"stepIndex": 1, "intensityType": "INTERVAL", "duration": 240.0,
             "averageSpeed": 4.2},
        ],
    }  # fmt: skip
    rounds = [{"roundIndex": 1, "duration": 180.0, "exerciseSets": [{}, {}]}]
    record = next(mappers.map_activity(_activity(workout_steps=steps, rounds=rounds)))
    assert isinstance(record, Activity)
    assert record.extra["workout_steps"] == [
        {"stepIndex": 0, "intensityType": "WARMUP", "duration": 600.0, "distance": 1800.0,
         "averageHR": 130.0, "laps_count": 1, "targetType": "heart.rate.zone"},
        {"stepIndex": 1, "intensityType": "INTERVAL", "duration": 240.0, "averageSpeed": 4.2},
    ]  # fmt: skip
    assert record.extra["rounds"] == [{"roundIndex": 1, "duration": 180.0, "exerciseSets_count": 2}]


@pytest.mark.parametrize("reply", [None, {}, [], {"activityId": 7}, "x", [1, 2]])
def test_missing_or_unfamiliar_steps_leave_the_activity_as_it_was(reply: Any) -> None:
    record = next(mappers.map_activity(_activity(workout_steps=reply, rounds=reply)))
    assert isinstance(record, Activity)
    assert "workout_steps" not in record.extra and "rounds" not in record.extra


def test_detail_rows_shape() -> None:
    assert mappers.detail_rows([{"a": 1, "b": None, "mapUrl": "x"}]) == [{"a": 1}]
    assert mappers.detail_rows({"x": [{"a": 1}], "y": [{"b": 2}]}) == [{"a": 1}]
    assert mappers.detail_rows({"x": [1, 2]}) == []
