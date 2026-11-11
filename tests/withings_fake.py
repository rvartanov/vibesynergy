"""A fake Withings API: routes form POSTs by path and action to synthetic payloads.

Shapes follow developer.withings.com/api-reference; values are made up.
"""

from __future__ import annotations

from typing import Any

# 2026-09-20 07:00:00 UTC
T0 = 1789887600


class FakeWithingsAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.token_requests: list[dict] = []
        self.expire_next_call = False
        self.rotate = 0

    def __call__(self, url: str, data: dict[str, Any], headers: dict[str, str]) -> dict:
        path = url.split("wbsapi.withings.net", 1)[1]
        action = data.get("action")
        if path == "/v2/oauth2":
            self.token_requests.append(dict(data))
            self.rotate += 1
            return {
                "status": 0,
                "body": {
                    "userid": "1234",
                    "access_token": f"access-{self.rotate}",
                    "refresh_token": f"refresh-{self.rotate}",
                    "expires_in": 10800,
                    "scope": "user.info,user.metrics,user.activity,user.sleepevents",
                    "token_type": "Bearer",
                },
            }
        self.calls.append((path, action, dict(data)))
        if self.expire_next_call:
            self.expire_next_call = False
            return {"status": 401, "error": "invalid_token"}
        handler = getattr(self, f"_{action}", None)
        return {"status": 0, "body": handler(data) if handler else {}}

    def _getmeas(self, data: dict) -> dict:
        if data.get("offset") is None:  # two pages
            return {
                "updatetime": T0,
                "timezone": "Europe/Amsterdam",
                "measuregrps": [
                    {"grpid": 101, "attrib": 0, "date": T0, "category": 1, "deviceid": "d1",
                     "model": "Body Cardio", "measures": [
                         {"value": 74250, "type": 1, "unit": -3},
                         {"value": 141, "type": 6, "unit": -1},
                         {"value": 812, "type": 91, "unit": -2},
                         {"value": 5, "type": 999, "unit": 0},
                     ]},
                ],
                "more": 1,
                "offset": 1,
            }  # fmt: skip
        return {
            "measuregrps": [
                {"grpid": 102, "attrib": 0, "date": T0 + 3600, "category": 1,
                 "measures": [{"value": 128, "type": 10, "unit": 0},
                              {"value": 82, "type": 9, "unit": 0},
                              {"value": 61, "type": 11, "unit": 0}]},
                {"grpid": 103, "attrib": 0, "date": T0 + 7200, "category": 1,
                 "measures": [{"value": 73900, "type": 1, "unit": -3}]},
                {"grpid": 104, "attrib": 0, "date": T0, "category": 2,
                 "measures": [{"value": 70000, "type": 1, "unit": -3}]},
            ],
            "more": 0,
            "offset": 0,
        }  # fmt: skip

    def _getactivity(self, data: dict) -> dict:
        return {
            "activities": [
                {"date": "2026-09-20", "steps": 8123, "distance": 6100.5, "elevation": 30,
                 "moderate": 1800, "intense": 600, "calories": 410.2, "totalcalories": 2410,
                 "hr_average": 71, "hr_min": 48, "hr_max": 151},
            ],
            "more": False,
            "offset": 0,
        }  # fmt: skip

    def _getintradayactivity(self, data: dict) -> dict:
        return {
            "series": {
                str(T0): {"heart_rate": 62, "steps": 12},
                str(T0 + 60): {"heart_rate": 64},
            }
        }

    def _getworkouts(self, data: dict) -> dict:
        return {
            "series": [
                {"id": 555, "category": 2, "timezone": "Europe/Amsterdam", "model": 93,
                 "startdate": T0, "enddate": T0 + 1800, "date": "2026-09-20",
                 "data": {"calories": 300, "hr_average": 150, "hr_max": 172,
                          "distance": 5000, "elevation": 12, "steps": 5200}},
            ],
            "more": False,
        }  # fmt: skip

    def _getsummary(self, data: dict) -> dict:
        return {
            "series": [
                {"id": 777, "timezone": "Europe/Amsterdam", "model": 32,
                 "startdate": T0 - 8 * 3600, "enddate": T0, "date": "2026-09-20",
                 "data": {"sleep_score": 82, "total_sleep_time": 26000,
                          "deepsleepduration": 5000, "lightsleepduration": 15000,
                          "remsleepduration": 6000, "wakeupduration": 900,
                          "sleep_efficiency": 0.93, "hr_average": 52, "rr_average": 14,
                          "apnea_hypopnea_index": 3, "snoring": 420}},
            ],
            "more": False,
        }  # fmt: skip

    def _get(self, data: dict) -> dict:
        if "signalid" in data:  # heart v2 get
            return {"signal": [1, 5, -3, 2], "sampling_frequency": 300, "wearposition": 1}
        return {  # sleep v2 get
            "series": [
                {"startdate": T0 - 3600, "enddate": T0 - 1800, "state": 2,
                 "hr": {str(T0 - 3600): 50}, "rr": {str(T0 - 3600): 13}},
                {"startdate": T0 - 1800, "enddate": T0, "state": 0},
            ]
        }  # fmt: skip

    def _list(self, data: dict) -> dict:
        return {
            "series": [
                {"deviceid": "d2", "model": 44, "timestamp": T0,
                 "ecg": {"signalid": 42, "afib": 0}, "heart_rate": 63},
            ],
            "more": False,
        }  # fmt: skip

    def _getdevice(self, data: dict) -> dict:
        return {"devices": [{"type": "Scale", "model": "Body Cardio", "battery": "high"}]}

    def _getgoals(self, data: dict) -> dict:
        return {"goals": {"steps": 10000}}
