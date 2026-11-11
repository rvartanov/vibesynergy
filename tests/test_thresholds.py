"""Training → Zones & thresholds: zones, platforms, edits, proposals and pushes.

Garmin and intervals.icu are fakes here; nothing reaches the network.
"""

from __future__ import annotations

import json
import re
from datetime import date

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_workouts, threshold_push, thresholds
from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient, IntervalsIcuError
from vibesynergy.store.schema import audit_log, raw_item, zone_set

WRITE = {"x-vibesynergy": "portal", "origin": "http://127.0.0.1:8447"}
TODAY = date(2026, 11, 1)


def _client(runtime) -> TestClient:
    return TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")


def _icu_settings(**bike) -> list[dict]:
    ride = {
        "id": 11,
        "types": ["Ride", "VirtualRide"],
        "ftp": 265,
        "lthr": 150,
        "max_hr": 179,
        "hr_zones": [121, 133, 139, 148, 153, 159, 179],
        "hr_zone_names": ["a", "b", "c", "d", "e", "f", "g"],
        "updated": "2026-10-31T07:40:00Z",
    }
    ride.update(bike)
    return [
        ride,
        {"id": 12, "types": ["Run"], "lthr": 162, "max_hr": 179, "threshold_pace": 3.0, "ftp": 459},
        {"id": 13, "types": ["Swim"], "lthr": 148, "max_hr": 179, "threshold_pace": 0.6667},
    ]


def _garmin_zone(runtime, kind: str, sport: str, boundaries, threshold, max_value=None) -> None:
    now = utc_now()
    raw_id = f"raw-{kind}-{sport}"
    with runtime.engine.begin() as conn:
        conn.execute(
            raw_item.insert().values(
                id=raw_id,
                source="garmin",
                dataset=f"{kind}_zones",
                item_key=raw_id,
                sha256="0" * 64,
                content_type="application/json",
                size=2,
                request={},
                fetched_at=now,
                last_seen_at=now,
                is_current=True,
            )
        )
        conn.execute(
            zone_set.insert().values(
                source="garmin",
                kind=kind,
                sport=sport,
                valid_from=TODAY,
                subject_id=1,
                boundaries=boundaries,
                threshold=threshold,
                max_value=max_value,
                extra={},
                raw_item_id=raw_id,
                updated_at=now,
            )
        )


# Zones --------------------------------------------------------------------------------------


def test_friel_heart_rate_zones_match_the_agreed_plan() -> None:
    bike = thresholds.hr_zones("bike", 150, 179)
    run = thresholds.hr_zones("run", 162, 179)
    assert thresholds.hr_upper_bounds(bike) == [121, 133, 139, 148, 153, 159, 179]
    assert thresholds.hr_upper_bounds(run) == [137, 144, 152, 160, 165, 171, 179]
    assert [z["label"] for z in bike] == ["1", "2", "3", "4", "5a", "5b", "5c"]
    assert bike[1] == {"zone": 2, "label": "2", "low": 122, "high": 133}
    # Garmin has five zones: Friel 5a-5c become its zone 5, starting just above Z4.
    assert thresholds.garmin_hr_floors(bike) == [122, 134, 140, 149]


def test_power_and_pace_zones() -> None:
    power = thresholds.power_zones(265)
    assert [z["high"] for z in power] == [146, 199, 238, 278, 318, 398, None]
    assert thresholds.garmin_power_floors(265) == [0, 146, 199, 238, 278, 318, 398]
    swim = thresholds.pace_zones("swim", 150)
    assert swim[3]["fast"] == 150  # zone 4 ends at CSS
    run = thresholds.pace_zones("run", 330)
    assert run[0] == {"zone": 1, "label": "1", "slow": None, "fast": 426}
    assert run[-1]["fast"] is None


def test_values_in_other_units_are_converted() -> None:
    assert thresholds.canonical("swim", "threshold_pace") == ("swim", "css")
    assert thresholds.to_canonical("swim", "css", 0.6667, "m/s") == 150.0
    assert thresholds.to_canonical("run", "threshold_pace", 5.5, "min/km") == 330.0
    with pytest.raises(ValueError):
        thresholds.validate("bike", "ftp", 2000)
    with pytest.raises(ValueError):
        thresholds.validate("bike", "css", 120)


# Overview -------------------------------------------------------------------------------------


def test_overview_shows_plan_platforms_and_mismatches(runtime) -> None:
    thresholds.set_value(runtime.engine, sport="bike", metric="ftp", value=265, today=TODAY)
    thresholds.set_value(runtime.engine, sport="bike", metric="lthr", value=150, today=TODAY)
    thresholds.set_value(runtime.engine, sport="bike", metric="max_hr", value=179, today=TODAY)
    _garmin_zone(runtime, "hr", "cycling", [98, 119, 134, 143, 151, 178], 151, 178)
    _garmin_zone(runtime, "power", "cycling", [0, 155, 212, 252, 295, 335, 422], 281)
    data = thresholds.overview(
        runtime.engine, today=TODAY, icu_settings=lambda: _icu_settings(lthr=155)
    )
    bike = next(s for s in data["sports"] if s["sport"] == "bike")
    ftp = next(m for m in bike["metrics"] if m["metric"] == "ftp")
    assert ftp["plan"]["value"] == 265 and ftp["plan"]["method"] == "manual"
    assert ftp["garmin"] == 281 and ftp["intervals"] == 265
    found = {(m["platform"], m["metric"]): m for m in bike["mismatches"]}
    assert found[("garmin", "ftp")]["platform_value"] == 281
    assert found[("garmin", "lthr")]["plan_value"] == 150
    assert found[("intervals", "lthr")]["platform_value"] == 155
    assert ("intervals", "hr_zones") not in found  # same zones as the plan
    assert ("garmin", "hr_zones") in found
    assert bike["zones"]["hr"]["model"] == "friel"
    assert data["zwift"]["ftp"] == 265
    assert data["intervals"]["ok"] is True


def test_overview_survives_intervals_icu_being_down(runtime) -> None:
    def down():
        raise IntervalsIcuError("timeout")

    data = thresholds.overview(runtime.engine, today=TODAY, icu_settings=down)
    assert data["intervals"] == {"ok": False, "error": "unavailable"}
    assert [s["sport"] for s in data["sports"]] == ["bike", "run", "swim"]


# Edits, proposals, history --------------------------------------------------------------------


def test_edit_saves_at_once_and_keeps_history(runtime) -> None:
    client = _client(runtime)
    first = client.post(
        "/api/thresholds/set",
        json={"sport": "bike", "metric": "ftp", "value": 265, "date_set": "2026-10-31"},
        headers=WRITE,
    )
    assert first.status_code == 200
    reply = client.post(
        "/api/thresholds/set",
        json={"sport": "bike", "metric": "ftp", "value": 272, "method": "test"},
        headers=WRITE,
    )
    assert reply.status_code == 200
    bike = reply.json()["data"]["sports"][0]
    ftp = bike["metrics"][0]
    assert ftp["plan"]["value"] == 272 and ftp["plan"]["method"] == "test"
    assert [(h["value"], h["previous"], h["status"]) for h in ftp["history"]][:2] == [
        (272, 265, "current"),
        (265, None, "superseded"),
    ]
    assert reply.json()["data"]["push_results"] == []
    assert (
        coach_workouts.workout_generation_thresholds(runtime.engine)["bike"]["ftp"]["value"] == 272
    )
    wrong = client.post(
        "/api/thresholds/set", json={"sport": "bike", "metric": "ftp", "value": 5}, headers=WRITE
    )
    assert wrong.status_code == 400


def test_coach_proposal_with_activity_then_take_over(runtime) -> None:
    row = coach_workouts.propose_threshold_change(
        runtime.engine,
        sport="bike",
        metric="ftp",
        value=270,
        unit="W",
        summary="FTP-test 19-11: 20 min op 284 W.",
        idempotency_key="ftp-test-1",
        method="test",
        activity_id="group-ftp-test",
        date_set=date(2026, 11, 19),
    )
    assert row["meta"]["activity_id"] == "group-ftp-test"
    assert coach_workouts.workout_generation_thresholds(runtime.engine) == {}
    client = _client(runtime)
    data = client.get("/api/thresholds").json()["data"]
    proposal = data["proposals"][0]
    assert proposal["activity_id"] == "group-ftp-test"
    assert proposal["summary"].startswith("FTP-test")
    assert proposal["date_set"] == "2026-11-19"
    taken = client.post(f"/api/thresholds/{proposal['id']}/accept", json={}, headers=WRITE)
    assert taken.status_code == 200
    assert taken.json()["data"]["proposals"] == []
    current = coach_workouts.workout_generation_thresholds(runtime.engine)["bike"]["ftp"]
    assert current["value"] == 270 and current["method"] == "test"
    feed = coach_workouts.pending_workout_regeneration(runtime.engine)["pending_regeneration"]
    assert feed and feed[0]["metric"] == "ftp"
    history = taken.json()["data"]["sports"][0]["metrics"][0]["history"]
    assert history[0]["activity_id"] == "group-ftp-test"


def test_dismissed_proposal_stays_out_of_the_plan(runtime) -> None:
    row = coach_workouts.propose_threshold_change(
        runtime.engine,
        sport="run",
        metric="lthr",
        value=166,
        unit="bpm",
        summary="Race suggests a higher LTHR.",
        idempotency_key="lthr-race-1",
        method="race",
    )
    threshold_id = row["meta"]["athlete_threshold_id"]
    reply = _client(runtime).post(f"/api/thresholds/{threshold_id}/dismiss", headers=WRITE)
    assert reply.status_code == 200
    assert reply.json()["data"]["proposals"] == []
    assert coach_workouts.workout_generation_thresholds(runtime.engine) == {}
    again = _client(runtime).post(f"/api/thresholds/{threshold_id}/accept", headers=WRITE)
    assert again.status_code == 400


def test_old_swim_threshold_pace_proposal_becomes_css(runtime) -> None:
    row = coach_workouts.propose_threshold_change(
        runtime.engine,
        sport="swim",
        metric="threshold_pace",
        value=150,
        unit="s/100m",
        summary="Voorlopige CSS 2:30/100 m.",
        idempotency_key="swim-css-1",
    )
    thresholds.accept(runtime.engine, row["meta"]["athlete_threshold_id"])
    swim = thresholds.current_values(runtime.engine)["swim"]
    assert swim["css"]["value"] == 150 and swim["css"]["unit"] == "s/100m"
    assert "threshold_pace" not in swim


def test_mcp_propose_and_read(runtime) -> None:
    async def go():
        tokens = set_connection("user", "user")
        try:
            async with Client(build_server(runtime)) as client:
                proposed = await client.call_tool(
                    "vibesynergy_coach_threshold_propose",
                    {
                        "sport": "run",
                        "metric": "threshold_pace",
                        "value": 3.03,
                        "unit": "m/s",
                        "summary": "10 km-wedstrijd: drempeltempo 5:30/km.",
                        "idempotency_key": "run-pace-race",
                        "method": "race",
                        "date_set": "2026-11-02",
                    },
                )
                read = await client.call_tool("vibesynergy_thresholds", {})
                return proposed, read
        finally:
            reset_connection(tokens)

    proposed, read = anyio.run(go)
    assert not proposed.is_error, proposed.content
    data = read.structured_content or json.loads(read.content[0].text)
    data = data["data"]
    assert data["proposals"][0]["value"] == 330.0
    assert data["proposals"][0]["method"] == "race"
    assert data["sports"]["run"]["values"] == {}  # not in the plan until taken over


# Pushes ---------------------------------------------------------------------------------------


class FakeIcu:
    def __init__(self, settings, *, keep=False):
        self.settings = settings
        self.keep = keep
        self.sent: list[tuple] = []

    def sport_settings(self):
        return json.loads(json.dumps(self.settings))

    def update_sport_settings(self, settings_id, fields):
        self.sent.append((settings_id, dict(fields)))
        if not self.keep:
            for item in self.settings:
                if item["id"] == settings_id:
                    item.update(fields)


def _bike_plan(runtime) -> None:
    for metric, value in (("ftp", 265), ("lthr", 150), ("max_hr", 179)):
        thresholds.set_value(runtime.engine, sport="bike", metric=metric, value=value, today=TODAY)


def _audit(runtime) -> list[dict]:
    with runtime.engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(audit_log.c.action, audit_log.c.outcome, audit_log.c.detail).where(
                    audit_log.c.action.like("threshold.push.%")
                )
            ).mappings()
        ]


def test_push_to_intervals_sends_the_plan_and_reads_back(runtime) -> None:
    _bike_plan(runtime)
    fake = FakeIcu(_icu_settings(ftp=250, lthr=155, hr_zones=[120, 130, 140, 150, 155, 160, 180]))
    result = threshold_push.push_intervals(runtime, "bike", client_factory=lambda: fake)
    assert result["ok"] is True, result
    assert fake.sent == [
        (
            11,
            {
                "lthr": 150,
                "max_hr": 179,
                "hr_zones": [121, 133, 139, 148, 153, 159, 179],
                "ftp": 265,
            },
        )
    ]
    assert result["before"]["ftp"] == 250
    audit = _audit(runtime)
    assert audit[-1]["action"] == "threshold.push.intervals" and audit[-1]["outcome"] == "ok"
    assert audit[-1]["detail"]["sent"]["ftp"] == 265


def test_push_to_intervals_reports_a_value_it_did_not_take(runtime) -> None:
    _bike_plan(runtime)
    fake = FakeIcu(_icu_settings(ftp=250), keep=True)
    result = threshold_push.push_intervals(runtime, "bike", client_factory=lambda: fake)
    assert result["ok"] is False and result["error"] == "not_applied"
    assert "ftp" in result["not_applied"]
    assert _audit(runtime)[-1]["outcome"] == "failed"


def test_swim_css_goes_to_intervals_as_speed(runtime) -> None:
    thresholds.set_value(runtime.engine, sport="swim", metric="css", value=150, today=TODAY)
    fake = FakeIcu(_icu_settings())
    fake.settings[2]["threshold_pace"] = 0.5
    result = threshold_push.push_intervals(runtime, "swim", client_factory=lambda: fake)
    assert result["ok"] is True
    assert fake.sent == [(13, {"threshold_pace": 0.6667})]


class FakeResponse:
    def json(self):
        return {}


class FakeGarminHttp:
    def __init__(self, api, keep_ftp=False):
        self.api = api
        self.keep_ftp = keep_ftp
        self.puts: list[tuple[str, list]] = []

    def put(self, domain, path, json):
        self.puts.append((path, json))
        if path.endswith("heartRateZones"):
            self.api.hr = json
        elif not self.keep_ftp:  # like Garmin: only the sports sent change
            sent = {row["sport"]: row for row in json}
            self.api.power = [{**row, **sent.get(row["sport"], {})} for row in self.api.power]
        return FakeResponse()


class FakeGarmin:
    def __init__(self, keep_ftp=False):
        self.hr = [
            {"sport": "DEFAULT", "lactateThresholdHeartRateUsed": 158, "maxHeartRateUsed": 178},
            {
                "sport": "CYCLING",
                "lactateThresholdHeartRateUsed": 151,
                "maxHeartRateUsed": 178,
                "zone1Floor": 98,
                "zone2Floor": 119,
                "zone3Floor": 134,
                "zone4Floor": 143,
                "zone5Floor": 151,
                "changeState": "UNCHANGED",
            },
        ]
        self.power = [
            {"sport": "CYCLING", "functionalThresholdPower": 281.0, "zone1Floor": 0.0},
            {"sport": "RUNNING", "functionalThresholdPower": 451.0, "zone1Floor": 300.0},
        ]
        self.client = FakeGarminHttp(self, keep_ftp)

    def get_heart_rate_zones(self):
        return json.loads(json.dumps(self.hr))

    def get_power_zones(self):
        return json.loads(json.dumps(self.power))


def test_push_to_garmin_sets_hr_profile_and_ftp(runtime) -> None:
    _bike_plan(runtime)
    fake = FakeGarmin()
    result = threshold_push.push_garmin(runtime, "bike", api_factory=lambda: fake)
    assert result["ok"] is True, result
    hr_put = dict(fake.client.puts[0][1][1])
    assert hr_put["lactateThresholdHeartRateUsed"] == 150
    assert hr_put["maxHeartRateUsed"] == 179
    assert [hr_put[f"zone{n}Floor"] for n in (2, 3, 4, 5)] == [122, 134, 140, 149]
    assert fake.client.puts[0][1][0]["lactateThresholdHeartRateUsed"] == 158  # DEFAULT kept
    power_put = fake.client.puts[1]
    assert power_put[0].endswith("powerZones/all")
    assert [row["sport"] for row in power_put[1]] == ["CYCLING"]  # only the one sport
    assert power_put[1][0]["functionalThresholdPower"] == 265
    assert "userLocalTime" not in power_put[1][0]
    assert _audit(runtime)[-1]["action"] == "threshold.push.garmin"


def test_garmin_keeping_its_own_ftp_is_reported(runtime) -> None:
    _bike_plan(runtime)
    result = threshold_push.push_garmin(runtime, "bike", api_factory=lambda: FakeGarmin(True))
    assert result["ok"] is False and result["error"] == "not_applied"
    assert result["not_applied"] == ["ftp"]


def test_garmin_not_connected(runtime) -> None:
    _bike_plan(runtime)

    def no_login():
        raise RuntimeError("not logged in")

    result = threshold_push.push_garmin(runtime, "bike", api_factory=no_login)
    assert result["error"] == "not_connected"


class FakeSwimGarmin:
    """Garmin's critical swim speed history: latest/{date} and a POST that adds one."""

    def __init__(self, speed=1000, keep=False):
        self.css = {
            "version": 1703397999852,
            "criticalSwimSpeed": speed,
            "measurementTimestampLocal": "2023-12-24T07:00:23.0",
            "measurementTimestampGMT": "2023-12-24T06:00:23.0",
            "biometricSourceType": "CHANGE_LOG",
        }
        self.keep = keep
        self.posts: list = []
        self.client = self

    def connectapi(self, path):
        assert path.startswith("/biometric-service/criticalSwimSpeed/latest/")
        return dict(self.css)

    def post(self, domain, path, json):
        assert domain == "connectapi" and path == "/biometric-service/criticalSwimSpeed"
        self.posts.append(json)
        if not self.keep:
            self.css = {**json, "version": 1793000000000}
        return FakeResponse()


def _swim_css(runtime, seconds=150) -> None:
    thresholds.set_value(runtime.engine, sport="swim", metric="css", value=seconds, today=TODAY)


def test_push_css_to_garmin_saves_a_manual_measurement(runtime) -> None:
    _swim_css(runtime, 150)  # 2:30 /100 m
    fake = FakeSwimGarmin()
    result = threshold_push.push_garmin(runtime, "swim", api_factory=lambda: fake)
    assert result["ok"] is True, result
    assert result["skipped"] == []
    (body,) = fake.posts
    assert body["criticalSwimSpeed"] == 667 and body["version"] == 0
    assert body["biometricSourceType"] == "MANUAL"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{2,3}", body["measurementTimestampGMT"])
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{2,3}", body["measurementTimestampLocal"]
    )
    assert result["before"]["css"]["mm_per_s"] == 1000
    assert result["after"]["css"]["mm_per_s"] == 667
    audit = _audit(runtime)[-1]
    assert audit["action"] == "threshold.push.garmin" and audit["outcome"] == "ok"


def test_push_css_already_in_garmin_adds_nothing(runtime) -> None:
    _swim_css(runtime, 100)  # 1:40 /100 m = 1000 mm/s
    fake = FakeSwimGarmin(1000)
    result = threshold_push.push_garmin(runtime, "swim", api_factory=lambda: fake)
    assert result["ok"] is True and fake.posts == []


def test_garmin_keeping_its_own_css_is_reported(runtime) -> None:
    _swim_css(runtime, 150)
    fake = FakeSwimGarmin(keep=True)
    result = threshold_push.push_garmin(runtime, "swim", api_factory=lambda: fake)
    assert result["error"] == "not_applied" and result["not_applied"] == ["css"]
    assert _audit(runtime)[-1]["outcome"] == "failed"


def test_css_outside_what_garmin_accepts_is_refused() -> None:
    from vibesynergy.sources.garmin import swim_speed

    now = utc_now()
    assert swim_speed.body(333, now, "Europe/Amsterdam")["criticalSwimSpeed"] == 333
    assert swim_speed.body(2000, now, "Europe/Amsterdam")["criticalSwimSpeed"] == 2000
    for bad in (332, 2001):
        with pytest.raises(ValueError):
            swim_speed.body(bad, now, "Europe/Amsterdam")
    assert swim_speed.mm_per_s(150) == 667 and swim_speed.seconds_per_100m(1000) == 100.0


def test_css_timestamp_keeps_two_millisecond_digits() -> None:
    from datetime import UTC, datetime

    from vibesynergy.sources.garmin import swim_speed

    moment = datetime(2026, 11, 1, 9, 5, 3, 7000, tzinfo=UTC)
    body = swim_speed.body(667, moment, "Europe/Amsterdam")
    assert body["measurementTimestampGMT"] == "2026-11-01T09:05:03.07"
    assert body["measurementTimestampLocal"] == "2026-11-01T10:05:03.07"


def test_garmin_css_shows_as_platform_value_and_mismatch(runtime) -> None:
    _swim_css(runtime, 150)
    _garmin_zone(runtime, "css", "swimming", [], 1000)
    data = thresholds.overview(runtime.engine, today=TODAY, icu_settings=lambda: [])
    swim = next(s for s in data["sports"] if s["sport"] == "swim")
    css = next(m for m in swim["metrics"] if m["metric"] == "css")
    assert css["garmin"] == 100.0
    assert {
        "platform": "garmin",
        "metric": "css",
        "platform_value": 100.0,
        "plan_value": 150.0,
    } in swim["mismatches"]


def test_portal_pushes_only_what_is_ticked(runtime, monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        threshold_push,
        "push_garmin",
        lambda rt, sport: calls.append(("garmin", sport)) or {"platform": "garmin", "ok": True},
    )
    monkeypatch.setattr(
        threshold_push,
        "push_intervals",
        lambda rt, sport: (
            calls.append(("intervals", sport)) or {"platform": "intervals", "ok": True}
        ),
    )
    client = _client(runtime)
    client.post(
        "/api/thresholds/set",
        json={"sport": "run", "metric": "lthr", "value": 162},
        headers=WRITE,
    )
    assert calls == []
    reply = client.post(
        "/api/thresholds/set",
        json={"sport": "run", "metric": "max_hr", "value": 179, "push": {"intervals": True}},
        headers=WRITE,
    )
    assert calls == [("intervals", "run")]
    assert reply.json()["data"]["push_results"] == [{"platform": "intervals", "ok": True}]
    # A coach proposal never pushes by itself, not even when it is taken over.
    row = coach_workouts.propose_threshold_change(
        runtime.engine,
        sport="run",
        metric="lthr",
        value=164,
        unit="bpm",
        summary="x",
        idempotency_key="no-push",
    )
    client.post(f"/api/thresholds/{row['meta']['athlete_threshold_id']}/accept", headers=WRITE)
    assert calls == [("intervals", "run")]


def test_intervals_client_writes_only_sport_settings_fields() -> None:
    seen: list = []

    def exchange(method, url, params, headers, body):
        seen.append((method, url, dict(params), json.loads(body) if body else None))
        return 200, {}, b"{}"

    client = IntervalsIcuClient("key", "i123", exchange=exchange, write_interval=0, interval=0)
    client.update_sport_settings(11, {"ftp": 265})
    assert seen == [
        (
            "PUT",
            "https://intervals.icu/api/v1/athlete/i123/sport-settings/11",
            {"recalcHrZones": "false"},
            {"ftp": 265},
        )
    ]
    with pytest.raises(IntervalsIcuError):
        client.update_sport_settings(11, {"w_prime": 20000})


class FakeRunGarmin:
    """Garmin for a run push: HR profiles incl. DEFAULT, and the profile's user settings."""

    def __init__(self, auto=True):
        self.hr = [
            {
                "sport": "DEFAULT",
                "lactateThresholdHeartRateUsed": 158,
                "maxHeartRateUsed": 178,
                "trainingMethod": "LACTATE_THRESHOLD",
                "zone1Floor": 110,
                "zone2Floor": 123,
                "zone3Floor": 137,
                "zone4Floor": 151,
                "zone5Floor": 164,
            },
            {
                "sport": "RUNNING",
                "lactateThresholdHeartRateUsed": 159,
                "maxHeartRateUsed": 179,
                "trainingMethod": "LACTATE_THRESHOLD",
                "zone1Floor": 102,
                "zone2Floor": 135,
                "zone3Floor": 142,
                "zone4Floor": 150,
                "zone5Floor": 158,
            },
        ]
        self.user = {
            "lactateThresholdHeartRate": 157,
            "lactateThresholdSpeed": 0.319,
            "thresholdHeartRateAutoDetected": auto,
            "ftpAutoDetected": auto,
        }
        api = self

        class Http:
            def put(self, domain, path, json):
                if path.endswith("heartRateZones"):
                    api.hr = json
                else:
                    api.user.update(json["userData"])
                return FakeResponse()

            def connectapi(self, path):
                return {"userData": dict(api.user)}

        self.client = Http()

    def get_heart_rate_zones(self):
        return json.loads(json.dumps(self.hr))

    def get_power_zones(self):
        return []


def test_run_push_writes_running_default_and_the_profile_lactate_threshold(runtime) -> None:
    """DEFAULT = RUNNING, the profile LT (HR and pace) and auto-detection off."""
    thresholds.set_value(runtime.engine, sport="run", metric="lthr", value=162, today=TODAY)
    thresholds.set_value(runtime.engine, sport="run", metric="max_hr", value=179, today=TODAY)
    thresholds.set_value(
        runtime.engine, sport="run", metric="threshold_pace", value=310, today=TODAY
    )
    fake = FakeRunGarmin()
    result = threshold_push.push_garmin(runtime, "run", api_factory=lambda: fake)
    assert result["ok"] is True, result
    run = next(z for z in fake.hr if z["sport"] == "RUNNING")
    default = next(z for z in fake.hr if z["sport"] == "DEFAULT")
    keys = ["lactateThresholdHeartRateUsed", "maxHeartRateUsed"] + [
        f"zone{n}Floor" for n in range(1, 6)
    ]
    assert run["lactateThresholdHeartRateUsed"] == 162
    assert {k: default[k] for k in keys} == {k: run[k] for k in keys}
    assert fake.user["lactateThresholdHeartRate"] == 162
    assert fake.user["lactateThresholdSpeed"] == 0.31
    assert fake.user["thresholdHeartRateAutoDetected"] is False
    assert result["after"]["lactate"] == {"lthr": 162, "pace_s_per_km": 310.0, "auto_detect": False}


def test_garmin_general_profile_unlike_running_is_a_mismatch(runtime) -> None:
    """Garmin's DEFAULT heart-rate profile must equal RUNNING."""
    _garmin_zone(runtime, "hr", "running", [102, 138, 145, 153, 161, 179], 162, 179)
    _garmin_zone(runtime, "hr", "default", [110, 123, 137, 151, 164, 178], 158, 178)
    run = thresholds.garmin_values(runtime.engine)["run"]
    assert run["default_hr"] == {"lthr": 158, "max_hr": 178, "hr_floors": [123, 137, 151, 164]}
    found = thresholds.mismatches("run", {}, {}, {"garmin": run})
    assert [m["metric"] for m in found] == ["hr_default"]


def test_garmin_profile_lactate_threshold_and_auto_detect_are_compared(runtime) -> None:
    """the profile LT and auto-detection come from the Garmin profile snapshot."""
    from vibesynergy.sources.garmin import mappers

    stored = runtime.raw.put(
        source="garmin",
        dataset="profile",
        item_key="snapshot",
        payload=json.dumps(
            {
                "user_profile": {
                    "userData": {
                        "lactateThresholdHeartRate": 157,
                        "lactateThresholdSpeed": 0.319,
                        "thresholdHeartRateAutoDetected": True,
                    }
                }
            }
        ).encode(),
        content_type="application/json",
    )
    raw = runtime.raw.load(stored.id)
    (zone,) = list(mappers.map_profile_threshold(raw, "Europe/Amsterdam"))
    assert (zone.kind, zone.sport, zone.threshold) == ("lt", "running", 157)
    assert zone.extra["pace_s_per_km"] == 319.0 and zone.extra["auto_detect"] is True
    garmin = {"profile_lt": {"lthr": 157, "pace_s_per_km": 319.0, "auto_detect": True}}
    found = thresholds.mismatches(
        "run", {"lthr": 162, "threshold_pace": 310}, {}, {"garmin": garmin}
    )
    assert [m["metric"] for m in found] == ["profile_lthr", "profile_pace", "auto_detect"]
