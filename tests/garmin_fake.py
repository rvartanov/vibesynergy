"""A fake ``garminconnect.Garmin`` returning synthetic payloads in Garmin Connect's shapes.

Values are made up; only the structure mirrors the real API. Methods the tests do not care
about return a small placeholder so every dataset can be synced.
"""

from __future__ import annotations

import io
import math
import zipfile
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fitgen import activity_fit, monitoring_fit, utc
from garminconnect import Garmin

ACTIVITY_ID = 21000000001


def user_summary(day: str) -> dict:
    return {
        "calendarDate": day,
        "totalSteps": 9876,
        "floorsAscended": 7.0,
        "totalDistanceMeters": 7400,
        "activeKilocalories": 650.0,
        "totalKilocalories": 2600.0,
        "moderateIntensityMinutes": 20,
        "vigorousIntensityMinutes": 35,
        "restingHeartRate": 47,
        "minHeartRate": 44,
        "maxHeartRate": 171,
        "averageStressLevel": 28,
        "restStressDuration": 36000,
        "highStressDuration": 1800,
        "bodyBatteryHighestValue": 91,
        "bodyBatteryLowestValue": 22,
        "averageSpo2": 96.0,
        "lowestSpo2": 89,
        "avgWakingRespirationValue": 14.0,
        "someNewField": 1,
    }


def sleep(day: str) -> dict:
    return {
        "dailySleepDTO": {
            "id": 1789800000000,
            "calendarDate": day,
            "sleepTimeSeconds": 27000,
            "deepSleepSeconds": 5400,
            "lightSleepSeconds": 15000,
            "remSleepSeconds": 6600,
            "awakeSleepSeconds": 900,
            "sleepStartTimestampGMT": 1789848000000,  # 2026-09-19T20:00:00Z
            "sleepEndTimestampGMT": 1789876800000,  # 04:00Z
            "averageSpO2Value": 95.0,
            "averageRespirationValue": 13.5,
            "avgHeartRate": 49.0,
            "avgSleepStress": 12.0,
            "sleepScores": {"overall": {"value": 84, "qualifierKey": "GOOD"}},
        },
        "sleepLevels": [
            {
                "startGMT": "2026-09-19T20:00:00.0",
                "endGMT": "2026-09-19T21:30:00.0",
                "activityLevel": 0.0,
            },
            {
                "startGMT": "2026-09-19T21:30:00.0",
                "endGMT": "2026-09-19T23:00:00.0",
                "activityLevel": 1.0,
            },
            {
                "startGMT": "2026-09-19T23:00:00.0",
                "endGMT": "2026-09-19T23:15:00.0",
                "activityLevel": 3.0,
            },
        ],
        "avgOvernightHrv": 62.0,
        "restlessMomentsCount": 31,
        "restingHeartRate": 46,
        "avgSkinTempDeviationC": -0.3,
        "bodyBatteryChange": 52,
    }


def hrv(day: str) -> dict:
    return {
        "hrvSummary": {
            "calendarDate": day,
            "weeklyAvg": 60,
            "lastNightAvg": 62,
            "lastNight5MinHigh": 88,
            "status": "BALANCED",
            "baseline": {"lowUpper": 50, "balancedLow": 55, "balancedUpper": 70},
        },
        "hrvReadings": [{"hrvValue": 61, "readingTimeGMT": "2026-09-19T21:00:00.0"}],
    }


def _day_start_ms(day: str) -> int:
    """Local midnight in Amsterdam (UTC+2 in summer) as epoch milliseconds."""
    start = datetime.fromisoformat(day).replace(tzinfo=UTC) - timedelta(hours=2)
    return int(start.timestamp() * 1000)


def heart_rates(day: str) -> dict:
    """All-day HR every 2 minutes: low at night, a workout at 08:00-09:00 local."""
    start = _day_start_ms(day)
    values = []
    for i in range(720):
        hour = i * 2 / 60
        hr = 50 + 12 * max(0.0, math.sin((hour - 6) / 18 * math.pi)) + 3 * math.sin(i / 7)
        if 8 <= hour < 9:
            hr = 140 + 10 * math.sin(i / 3)
        values.append([start + i * 120_000, round(hr)])
    values[100][1] = None  # gaps happen
    return {"calendarDate": day, "restingHeartRate": 47, "heartRateValues": values}


def stress(day: str) -> dict:
    """Stress and Body Battery every 3 minutes: charging at night, draining by day."""
    start = _day_start_ms(day)
    stress_values, battery = [], []
    level = 40.0
    for i in range(480):
        hour = i * 3 / 60
        asleep = hour < 6 or hour >= 23
        value = -1 if 8 <= hour < 9 else round(15 + 25 * abs(math.sin(i / 20)))  # -1: active
        stress_values.append([start + i * 180_000, 10 if asleep else value])
        level = min(100.0, level + 0.45) if asleep else max(5.0, level - 0.2)
        battery.append([start + i * 180_000, "MEASURED", round(level), 2.0])
    return {
        "calendarDate": day,
        "avgStressLevel": 28,
        "stressValuesArray": stress_values,
        "bodyBatteryValuesArray": battery,
    }


def activity_summary() -> dict:
    return {
        "activityId": ACTIVITY_ID,
        "activityName": "Morning Ride",
        "startTimeLocal": "2026-09-20 08:00:00",
        "startTimeGMT": "2026-09-20 06:00:00",
        "activityType": {"typeKey": "cycling", "typeId": 2},
        "distance": 30000.0,
        "duration": 3600.0,
        "elapsedDuration": 3700.0,
        "movingDuration": 3550.0,
        "elevationGain": 210.0,
        "averageHR": 138.0,
        "maxHR": 171.0,
        "avgPower": 205.0,
        "normPower": 221.0,
        "averageSpeed": 8.33,
        "calories": 780.0,
        "activityTrainingLoad": 120.5,
        "aerobicTrainingEffect": 3.1,
        "anaerobicTrainingEffect": 1.2,
        "deviceId": 3999999999,
        "manualActivity": False,
    }


def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, data)
    return buf.getvalue()


class FakeGarmin:
    def get_scheduled_workouts(self, *args):
        return {"calendarItems": []}

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.profile_id = 12345
        self.display_name = "athlete"
        self.hr_max = 188

        class _Client:
            def dumps(self_inner) -> str:
                return '{"fake": "tokens"}'

        self.client = _Client()

    def _log(self, name: str) -> None:
        self.calls.append(name)

    def __getattr__(self, name: str) -> Any:
        # Only methods the real client has: a typo in a dataset's call must fail the test.
        if not name.startswith("get_") or not hasattr(Garmin, name):
            raise AttributeError(name)

        def placeholder(*args: Any, **kwargs: Any) -> Any:
            self._log(name)
            return {"placeholder": name, "args": [str(a) for a in args]}

        return placeholder

    def get_user_summary(self, day: str) -> dict:
        self._log("get_user_summary")
        return user_summary(day)

    def get_heart_rates(self, day: str) -> dict:
        self._log("get_heart_rates")
        return heart_rates(day)

    def get_stress_data(self, day: str) -> dict:
        self._log("get_stress_data")
        return stress(day)

    def get_sleep_data(self, day: str) -> dict:
        self._log("get_sleep_data")
        return sleep(day)

    def get_hrv_data(self, day: str) -> dict:
        self._log("get_hrv_data")
        return hrv(day)

    def get_training_readiness(self, day: str) -> list:
        return [
            {"calendarDate": day, "timestamp": f"{day}T05:00:00.0", "score": 55},
            {"calendarDate": day, "timestamp": f"{day}T07:30:00.0", "score": 71},
        ]

    def get_max_metrics(self, day: str) -> list:
        return [{"generic": {"vo2MaxPreciseValue": 54.6}, "cycling": {"vo2MaxValue": 58}}]

    def get_body_composition(self, start: str, end: str) -> dict:
        self._log("get_body_composition")
        return {
            # Newest first, as Garmin returns it; "date" is midnight, timestampGMT the time.
            "dateWeightList": [
                {"samplePk": 2, "date": 1789862400000, "timestampGMT": 1789920000000,
                 "calendarDate": "2026-09-20", "weight": 73900.0, "sourceType": "MFP"},
                {"samplePk": 1, "date": 1789862400000, "timestampGMT": 1789891200000,
                 "calendarDate": "2026-09-20", "weight": 74200.0, "bmi": 22.9,
                 "bodyFat": 14.1, "muscleMass": 35100.0},
            ]
        }  # fmt: skip

    def get_heart_rate_zones(self) -> list:
        return [
            {"sport": "DEFAULT", "trainingMethod": "HR_MAX", "zone1Floor": 94,
             "zone2Floor": 113, "zone3Floor": 132, "zone4Floor": 150, "zone5Floor": 169,
             "maxHeartRateUsed": self.hr_max, "lactateThresholdHeartRateUsed": 172},
        ]  # fmt: skip

    def connectapi(self, path: str) -> Any:
        self._log(f"connectapi {path}")
        if path.startswith("/biometric-service/criticalSwimSpeed/latest/"):
            return {"version": 1703397999852, "criticalSwimSpeed": 1000,
                    "measurementTimestampLocal": "2023-12-24T07:00:23.0",
                    "measurementTimestampGMT": "2023-12-24T06:00:23.0",
                    "biometricSourceType": "CHANGE_LOG"}  # fmt: skip
        raise AttributeError(f"no fake for {path}")

    def get_activities_by_date(self, start, end, activitytype=None, sortorder=None) -> list:
        self._log("get_activities_by_date")
        if start <= "2026-09-20" <= end:
            return [activity_summary()]
        return []

    def get_activity_splits(self, activity_id: str) -> dict:
        return {
            "lapDTOs": [
                {"lapIndex": 1, "startTimeGMT": "2026-09-20T06:00:00.0", "duration": 1800.0,
                 "distance": 15000.0, "averageHR": 135.0, "averagePower": 200.0,
                 "averageCadence": 88},
                {"lapIndex": 2, "startTimeGMT": "2026-09-20T06:30:00.0", "duration": 1800.0,
                 "distance": 15000.0, "averageHR": 141.0, "averagePower": 210.0},
            ]
        }  # fmt: skip

    def get_activity_hr_in_timezones(self, activity_id: str) -> list:
        return [
            {"zoneNumber": 1, "secsInZone": 600.0, "zoneLowBoundary": 94},
            {"zoneNumber": 2, "secsInZone": 3000.0, "zoneLowBoundary": 113},
        ]

    def get_activity_weather(self, activity_id: str) -> Any:
        from garminconnect import GarminConnectConnectionError

        raise GarminConnectConnectionError("404: no weather for this activity")

    def download_activity(self, activity_id: str, dl_fmt: Any = None) -> bytes:
        self._log("download_activity")
        return _zip(f"{activity_id}_ACTIVITY.fit", activity_fit(utc(2026, 9, 20, 6, 0, 0), 30))

    def download_health_snapshot(self, day: str) -> bytes:
        self._log("download_health_snapshot")
        d = date.fromisoformat(day)
        return _zip(f"{day}_WELLNESS.fit", monitoring_fit(utc(d.year, d.month, d.day)))
