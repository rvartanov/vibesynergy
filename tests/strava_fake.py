"""A fake Strava API v3 with synthetic payloads in Strava's shapes."""

from __future__ import annotations

from typing import Any

from vibesynergy.sources.strava.client import Response

RIDE = {
    "id": 9001,
    "name": "Zwift - Watopia",
    "sport_type": "VirtualRide",
    "type": "VirtualRide",
    "start_date": "2026-09-20T17:00:00Z",
    "start_date_local": "2026-09-20T19:00:00Z",
    "utc_offset": 7200.0,
    "elapsed_time": 3600,
    "moving_time": 3590,
    "distance": 35000.0,
    "total_elevation_gain": 320.0,
    "average_heartrate": 140.0,
    "max_heartrate": 168.0,
    "average_watts": 215.0,
    "weighted_average_watts": 228,
    "average_speed": 9.7,
    "device_name": "Zwift",
    "external_id": "zwift-activity-123.fit",
    "trainer": True,
    "manual": False,
}
MANUAL = {
    "id": 9002,
    "name": "Yoga",
    "sport_type": "Yoga",
    "start_date": "2026-09-20T06:00:00Z",
    "start_date_local": "2026-09-20T08:00:00Z",
    "elapsed_time": 1800,
    "moving_time": 1800,
    "distance": 0.0,
    "manual": True,
}


class FakeStravaAPI:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.token_requests: list[dict] = []
        self.usage = "10,100"  # 15-minute, daily
        self.limit = "200,2000"
        self.status_for: dict[str, int] = {}
        self.push_calls: list[tuple[str, str, dict]] = []
        self.detail: dict[str, dict] = {}  # extra fields per activity id, e.g. {"private": False}

    def __call__(self, method: str, url: str, params: dict[str, Any], headers: dict) -> Response:
        if url.endswith("/oauth/token"):
            self.token_requests.append(dict(params))
            body = {
                "token_type": "Bearer",
                "access_token": f"s-access-{len(self.token_requests)}",
                "refresh_token": f"s-refresh-{len(self.token_requests)}",
                "expires_in": 21600,
            }
            if params.get("grant_type") == "authorization_code":
                body["athlete"] = {"id": 77}
            return Response(200, {}, body)
        path = url.split("/api/v3", 1)[1]
        self.calls.append(path)
        if path.startswith("/push_subscriptions"):
            self.push_calls.append((method, path, dict(params)))
            if method == "POST":
                return Response(201, {}, {"id": 5})
            if method == "DELETE":
                return Response(204, {}, "")
            return Response(200, {}, [{"id": 5, "callback_url": "x"}])
        rate = {"x-ratelimit-limit": self.limit, "x-ratelimit-usage": self.usage}
        if path in self.status_for:
            return Response(self.status_for[path], rate, {"message": "error"})
        return Response(200, rate, self._body(path, params))

    def _body(self, path: str, params: dict) -> Any:
        if path == "/athlete":
            return {"id": 77, "firstname": "Test", "bikes": [{"id": "b1"}], "shoes": []}
        if path == "/athlete/activities":
            return [RIDE, MANUAL] if params.get("page") == 1 else []
        if path == "/activities/9001":
            return {
                **RIDE,
                "calories": 800.0,
                "description": "Easy spin",
                **self.detail.get("9001", {}),
            }
        if path == "/activities/9002":
            return MANUAL
        if path.endswith("/laps"):
            return [
                {"lap_index": 1, "start_date": "2026-09-20T17:00:00Z", "elapsed_time": 1800,
                 "moving_time": 1795, "distance": 17500.0, "average_watts": 210.0},
                {"lap_index": 2, "start_date": "2026-09-20T17:30:00Z", "elapsed_time": 1800,
                 "moving_time": 1795, "distance": 17500.0, "average_watts": 220.0},
            ]  # fmt: skip
        if path.endswith("/zones"):
            return [
                {"type": "heartrate", "distribution_buckets": [
                    {"min": 0, "max": 120, "time": 600}, {"min": 120, "max": 150, "time": 3000}]},
            ]  # fmt: skip
        if path.endswith("/streams"):
            return {
                "time": {"data": [0, 1, 2]},
                "latlng": {"data": [[52.1, 4.3], [52.1001, 4.3], [52.1002, 4.3]]},
                "watts": {"data": [200, 210, None]},
                "heartrate": {"data": [120, 121, 122]},
                "moving": {"data": [True, True, False]},
            }
        if path.startswith("/gear/"):
            return {"id": path.rsplit("/", 1)[1], "distance": 1234567.0}
        return {}
