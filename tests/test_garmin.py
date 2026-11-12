"""Garmin source against a fake API: every dataset syncs, known payloads map correctly."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from garmin_fake import ACTIVITY_ID, FakeGarmin, activity_summary
from sqlalchemy import select

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import Window
from vibesynergy.portal import settings_api
from vibesynergy.runtime import Runtime
from vibesynergy.sources.garmin import client
from vibesynergy.sources.garmin.source import Throttled
from vibesynergy.store.schema import (
    activity,
    activity_lap,
    activity_zone_time,
    body_measurement,
    daily_metric,
    fit_file,
    sleep_session,
    sleep_stage,
    zone_set,
)

DAY = date(2026, 9, 20)


@pytest.fixture
def garmin(runtime):
    settings = replace(
        runtime.settings, sources={"garmin": {"enabled": True, "request_interval": 0}}
    )
    rt = Runtime.open(settings)
    source = rt.registry.get("garmin")
    fake = FakeGarmin()
    source._api = Throttled(fake, 0)
    yield rt, fake
    rt.close()


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


def _metric(rt, metric, day=DAY):
    rows = _rows(rt, daily_metric, daily_metric.c.metric == metric, daily_metric.c.date == day)
    return rows[0]["value"] if rows else None


def test_every_default_dataset_syncs(garmin) -> None:
    rt, _ = garmin
    results = rt.sync.sync("garmin", window=Window(DAY, DAY))
    failed = {r.dataset: r.error for r in results if r.error or r.normalise_errors}
    assert failed == {}
    names = {r.dataset for r in results}
    assert "nutrition_food_log" not in names and "menstrual" not in names  # off by default
    assert {"activities", "activity_files", "wellness_files", "sleep", "profile"} <= names


def test_daily_wellness_mapping(garmin) -> None:
    rt, _ = garmin
    rt.sync.sync(
        "garmin",
        ["user_summary", "sleep", "hrv", "training_readiness", "max_metrics"],
        Window(DAY, DAY),
    )
    assert _metric(rt, "daily.steps") == 9876
    assert _metric(rt, "hr.resting") == 47
    assert _metric(rt, "recovery.stress.high_minutes") == 30
    assert _metric(rt, "sleep.score") == 84
    assert _metric(rt, "sleep.deep") == 5400
    assert _metric(rt, "sleep.avg_hr") == 49
    assert _metric(rt, "sleep.skin_temp_deviation") == -0.3
    assert _metric(rt, "hrv.night_avg") == 62
    assert _metric(rt, "hrv.baseline_high") == 70
    assert _metric(rt, "recovery.readiness") == 71  # latest reading of the day
    assert _metric(rt, "fitness.vo2max.run") == 54.6
    assert _metric(rt, "fitness.vo2max.ride") == 58

    session = _rows(rt, sleep_session)[0]
    assert session["local_date"] == DAY and session["duration_s"] == 27000
    assert session["start_utc"].isoformat() == "2026-09-19T20:00:00+00:00"
    stages = [r["stage"] for r in _rows(rt, sleep_stage)]
    assert sorted(stages) == ["awake", "deep", "light"]  # activityLevel 0.0 is deep sleep


def test_body_composition_per_measurement_and_last_of_day(garmin) -> None:
    rt, _ = garmin
    rt.sync.sync("garmin", ["body_composition"], Window(DAY, DAY))
    weights = _rows(rt, body_measurement, body_measurement.c.metric == "body.weight")
    assert sorted(r["value"] for r in weights) == [73.9, 74.2]
    times = {r["value"]: r["measured_at"].isoformat() for r in weights}
    assert times[73.9] == "2026-09-20T16:00:00+00:00"  # the actual time, not midnight
    assert _metric(rt, "body.weight") == 73.9  # the later weigh-in
    assert _metric(rt, "body.muscle_mass") == 35.1


def test_range_datasets_use_month_items(garmin) -> None:
    rt, _ = garmin
    rt.sync.sync("garmin", ["body_composition"], Window(date(2026, 8, 30), DAY))
    keys = sorted(rt.raw.load(i).item_key for i in rt.raw.current_ids("garmin", "body_composition"))
    assert keys == ["2026-08", "2026-09"]


def test_activity_summary_laps_zones_and_detail_errors(garmin) -> None:
    rt, _ = garmin
    result = rt.sync.sync("garmin", ["activities"], Window(DAY, DAY))[0]
    assert result.error is None and result.fetched == 1
    act = _rows(rt, activity)[0]
    assert act["source_id"] == str(ACTIVITY_ID)
    assert act["sport"] == "cycling" and act["name"] == "Morning Ride"
    assert act["tz_offset_s"] == 7200 and act["local_date"] == DAY
    assert act["distance_m"] == 30000 and act["norm_power"] == 221
    assert "weather" in act["extra"]["detail_errors"]  # one failing call, activity still stored
    assert [lap["avg_power"] for lap in _rows(rt, activity_lap)] == [200, 210]
    assert _rows(rt, activity_lap)[0]["extra"]["averageCadence"] == 88
    assert {z["zone"]: z["seconds"] for z in _rows(rt, activity_zone_time)} == {1: 600, 2: 3000}

    from vibesynergy.derive import unify
    from vibesynergy.query import views

    unify.rebuild(rt.engine)
    detail = views.activity_detail(rt.engine, rt.parquet, f"garmin:{ACTIVITY_ID}")
    assert detail["laps"][0]["extra"]["averageCadence"] == 88
    assert all(z["source"] == "garmin" for z in detail["zones"])
    assert all(z["activity_source_id"] == str(ACTIVITY_ID) for z in detail["zones"])
    assert all(z["updated_at"] is not None for z in detail["zones"])


def test_typed_swim_sets_preserve_lengths_swolf_and_rest(garmin) -> None:
    from vibesynergy.query.units import presentation

    rt, fake = garmin
    summary = activity_summary()
    summary.update(
        activityType={"typeKey": "lap_swimming"},
        distance=100.0,
        averageSpeed=1.0,
        poolLength=25.0,
    )
    fake.get_activities_by_date = lambda *_args: [summary]
    fake.get_activity_splits = lambda _id: {"lapDTOs": []}
    fake.get_activity_typed_splits = lambda _id: {
        "swimSplits": [
            {
                "lapIndex": 1,
                "duration": 100.0,
                "distance": 100.0,
                "averageSpeed": 1.0,
                "averageSWOLF": 42,
                "averageSwimCadence": 26,
                "lengthDTOs": [
                    {
                        "lengthIndex": n,
                        "distance": 25.0,
                        "duration": 25.0,
                        "swimStroke": "FREESTYLE",
                    }
                    for n in range(1, 5)
                ],
            },
            {"lapIndex": 2, "duration": 30.0, "intensityType": "REST"},
        ]
    }
    result = rt.sync.sync("garmin", ["activities"], Window(DAY, DAY))[0]
    assert result.error is None and result.normalise_errors == 0
    rows = _rows(rt, activity_lap)
    assert len(rows) == 2
    work = presentation("lap_swimming", rows[0])
    rest = presentation("lap_swimming", rows[1])
    assert work["pace_or_speed"] == "1:40/100m"
    assert (work["lengths"], work["swolf"], work["stroke_rate"]) == (4, 42, 26)
    assert rest["rest_s"] == 30
    assert presentation("lap_swimming", _rows(rt, activity)[0])["pool_length_m"] == 25


def test_activity_files_are_downloaded_once(garmin) -> None:
    rt, fake = garmin
    first = rt.sync.sync("garmin", ["activity_files"], Window(DAY, DAY))[0]
    assert (first.fetched, first.new_versions, first.normalise_errors) == (1, 1, 0)
    fit = _rows(rt, fit_file)[0]
    assert fit["source_activity_id"] == str(ACTIVITY_ID) and fit["file_type"] == "activity"
    stream = rt.parquet.read_stream(fit["fit_id"])
    assert stream.num_rows == 30
    assert {"hr", "power", "lat", "lon", "speed", "dev_Form Power"} <= set(stream.column_names)

    again = rt.sync.sync("garmin", ["activity_files"], Window(DAY, DAY))[0]
    assert (again.fetched, again.skipped) == (0, 1)
    assert fake.calls.count("download_activity") == 1


def test_new_activity_shows_up_on_the_next_sync(garmin) -> None:
    """The source outlives a sync; a workout uploaded after the first sync of the day must
    not wait for a restart (1 Nov: a run only reached Strava)."""
    rt, fake = garmin
    listed: list[dict] = []
    fake.get_activities_by_date = lambda *_args: list(listed)
    assert rt.sync.sync("garmin", ["activities"], Window(DAY, DAY))[0].fetched == 0
    listed.append(activity_summary())
    assert rt.sync.sync("garmin", ["activities"], Window(DAY, DAY))[0].fetched == 1
    assert _rows(rt, activity, activity.c.source == "garmin")


def test_wellness_files_to_parquet(garmin) -> None:
    rt, _ = garmin
    result = rt.sync.sync("garmin", ["wellness_files"], Window(DAY, DAY))[0]
    assert result.error is None and result.normalise_errors == 0
    files = list((rt.settings.parquet_dir / "wellness" / "source=garmin").glob("*.parquet"))
    assert len(files) == 1
    import pyarrow.parquet as pq

    table = pq.read_table(files[0]).to_pydict()
    assert table["metric"].count("hr.sample") == 5
    assert "stress.sample" in table["metric"]


def test_zone_changes_keep_history(garmin, monkeypatch) -> None:
    rt, fake = garmin
    rt.sync.sync("garmin", ["heart_rate_zones"])
    fake.hr_max = 185  # the athlete edits max HR later
    from vibesynergy.store import raw as raw_module

    later = raw_module.utc_now().replace(year=2027)
    monkeypatch.setattr(raw_module, "utc_now", lambda: later)
    rt.sync.sync("garmin", ["heart_rate_zones"])
    rows = sorted(_rows(rt, zone_set), key=lambda r: r["valid_from"])
    assert [r["max_value"] for r in rows] == [188, 185]
    assert rows[0]["boundaries"][-1] == 188 and rows[0]["threshold"] == 172

    # A rebuild re-maps every version, so the history survives re-normalisation.
    with rt.engine.begin() as conn:
        conn.execute(zone_set.delete())
    assert rt.normaliser.rebuild("garmin", "heart_rate_zones") == (2, 0)
    assert len(_rows(rt, zone_set)) == 2


def test_critical_swim_speed_is_a_zone_set(garmin) -> None:
    rt, _ = garmin
    rt.sync.sync("garmin", ["critical_swim_speed"])
    (row,) = [r for r in _rows(rt, zone_set) if r["kind"] == "css"]
    assert row["sport"] == "swimming" and row["threshold"] == 1000
    assert row["valid_from"] == date(2023, 12, 24) and row["boundaries"] == []


def test_extra_and_limited_datasets(runtime) -> None:
    settings = replace(
        runtime.settings,
        sources={"garmin": {"enabled": True, "extra_datasets": ["nutrition_food_log"]}},
    )
    names = [s.name for s in Runtime.open(settings).registry.get("garmin").sync_datasets()]
    assert "nutrition_food_log" in names and "menstrual" not in names
    settings = replace(settings, sources={"garmin": {"enabled": True, "datasets": ["sleep"]}})
    assert [s.name for s in Runtime.open(settings).registry.get("garmin").sync_datasets()] == [
        "sleep"
    ]


def test_not_logged_in_is_a_dataset_error(runtime) -> None:
    settings = replace(runtime.settings, sources={"garmin": {"enabled": True}})
    rt = Runtime.open(settings)
    result = rt.sync.sync("garmin", ["sleep"], Window(DAY, DAY))[0]
    assert "not logged in to Garmin Connect; connect Garmin in Settings → Sources" in result.error


def test_tokens_saved_only_when_changed(tmp_path) -> None:
    from vibesynergy.core.secrets import SecretStore

    secrets = SecretStore(tmp_path, "garmin")
    fake = FakeGarmin()
    client.save_tokens(secrets, fake)
    assert secrets.get(client.TOKENS) == {"tokenstore": '{"fake": "tokens"}'}
    assert (tmp_path / "garmin" / "tokens.enc").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    ("prior", "profile_id", "expected_state", "expected_account"),
    [
        ({}, 123, "connected", {"name": "Runner", "id": 123}),
        ({"name": "Runner"}, 123, "connected", {"name": "Runner", "id": 123}),
        ({"name": "Runner", "id": 456}, 123, "failed", {"name": "Runner", "id": 456}),
    ],
)
def test_portal_login_saves_profile_id_and_rejects_different_id(
    runtime, monkeypatch, prior, profile_id, expected_state, expected_account
):
    store = SecretStore(runtime.settings.secrets_dir, "garmin")
    if prior:
        store.set("account", prior)

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    api = SimpleNamespace(display_name="Runner", profile_id=profile_id, login=lambda: None)
    monkeypatch.setattr(settings_api.threading, "Thread", FakeThread)
    monkeypatch.setattr("vibesynergy.sources.garmin.client._garmin_class", lambda: lambda **_: api)
    monkeypatch.setattr("vibesynergy.sources.garmin.client.save_tokens", lambda *_: None)
    session = settings_api._start_garmin_login(runtime.settings, "runner@example.test", "synthetic")
    assert settings_api._garmin_logins.pop(session)["state"] == expected_state
    assert store.get("account") == expected_account


def test_throttle_spaces_calls(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("vibesynergy.sources.garmin.source.time.sleep", sleeps.append)
    api = Throttled(FakeGarmin(), 10.0)
    api.get_user_summary("2026-09-20")
    api.get_user_summary("2026-09-21")
    assert len(sleeps) == 1 and sleeps[0] > 9
    assert api.profile_id == 12345  # attributes pass through


def test_failed_login_is_not_retried_per_dataset(runtime, monkeypatch) -> None:
    attempts = []

    def failing_connect(secrets, **kwargs):
        attempts.append(1)
        raise client.GarminAuthError("not logged in")

    monkeypatch.setattr(client, "connect", failing_connect)
    settings = replace(runtime.settings, sources={"garmin": {"enabled": True}})
    rt = Runtime.open(settings)
    results = rt.sync.sync("garmin", ["sleep", "hrv", "profile"], Window(DAY, DAY))
    assert all("not logged in" in r.error for r in results)
    assert len(attempts) == 1


def test_wellness_file_for_today_is_skipped(garmin) -> None:
    from vibesynergy.core.timeutil import today_in

    rt, fake = garmin
    today = today_in(rt.settings.timezone)
    rt.sync.sync("garmin", ["wellness_files"], Window(today, today))
    assert "download_health_snapshot" not in fake.calls


def test_fake_only_implements_real_client_methods() -> None:
    from garminconnect import Garmin

    own = [n for n, v in vars(FakeGarmin).items() if callable(v) and not n.startswith("_")]
    assert own and all(hasattr(Garmin, name) for name in own)


def test_challenge_pages_start_at_one() -> None:
    from vibesynergy.sources.garmin.datasets import _challenges

    starts: list[int] = []

    class Api:
        def __getattr__(self, name):
            def page(start, limit):
                starts.append(start)
                if start < 1:
                    raise ValueError("start should > 0")
                return [{"id": i} for i in range(limit if start == 1 else 3)]

            return page

    result = _challenges(Api())
    assert min(starts) == 1 and len(result["adhoc"]) == 103
    assert len(result["available_badge"]) == 103
    assert len(result["non_completed_badge"]) == 103
    assert "errors" not in result


def test_challenge_endpoint_failure_stays_in_the_payload() -> None:
    from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

    from vibesynergy.sources.garmin.datasets import _challenges

    class Api:
        def __getattr__(self, name):
            def page(start, limit):
                if name == "get_available_badge_challenges":
                    raise GarminConnectConnectionError("badge list unavailable")
                return [{"id": start}]

            return page

    result = _challenges(Api())
    assert result["adhoc"] == [{"id": 1}]
    assert "available_badge" not in result
    assert result["errors"]["available_badge"].startswith("GarminConnectConnectionError")

    class Fatal:
        def __getattr__(self, name):
            def page(start, limit):
                raise GarminConnectAuthenticationError("signed out")

            return page

    with pytest.raises(GarminConnectAuthenticationError):
        _challenges(Fatal())


def test_all_day_heart_rate_stress_and_body_battery(garmin) -> None:
    rt, _ = garmin
    results = rt.sync.sync("garmin", ["heart_rate", "stress"], Window(DAY, DAY))
    assert all(r.error is None and r.normalise_errors == 0 for r in results)
    from vibesynergy.query import training

    day = training.intraday(rt.parquet, DAY, "Europe/Amsterdam", resample_min=60)
    assert day["columns"] == ["time", "bodybattery.sample", "hr.sample", "stress.sample"]
    assert len(day["rows"]) == 24  # the whole local day, hour by hour
    by_hour = {row[0]: dict(zip(day["columns"][1:], row[1:], strict=True)) for row in day["rows"]}
    assert by_hour["08:00:00"]["hr.sample"] > 130  # the workout
    assert by_hour["08:00:00"]["stress.sample"] is None  # -1 ("active") is not a stress level
    assert by_hour["05:00:00"]["bodybattery.sample"] > by_hour["00:00:00"]["bodybattery.sample"]


def test_intraday_mappers_skip_bad_rows() -> None:
    from datetime import UTC, datetime

    from vibesynergy.sources.garmin.mappers import map_heart_rate, map_stress
    from vibesynergy.store.raw import RawRecord

    def raw(dataset, payload):
        import json

        return RawRecord(
            "r1",
            "garmin",
            dataset,
            "2026-09-20",
            "x",
            "application/json",
            {},
            DAY,
            DAY,
            datetime.now(UTC),
            None,
            json.dumps(payload).encode(),
        )

    t = 1789900000000
    hr = list(
        map_heart_rate(
            raw("heart_rate", {"heartRateValues": [[t, 60], [t + 1, None], "x", [t + 2]]})
        )
    )
    assert len(hr) == 1 and hr[0].value == [60.0] and hr[0].key == "heart_rate-2026-09-20"
    assert list(map_heart_rate(raw("heart_rate", {"heartRateValues": None}))) == []
    stress = next(
        map_stress(
            raw(
                "stress",
                {
                    "stressValuesArray": [[t, -2], [t, 30]],
                    "bodyBatteryValuesArray": [[t, "MEASURED", 55, 2.0], [t, "MEASURED"]],
                },
            )
        )
    )
    assert sorted(zip(stress.metric, stress.value, strict=True)) == [
        ("bodybattery.sample", 55.0),
        ("stress.sample", 30.0),
    ]


def test_map_activity_extracts_gear_and_exercise_identity() -> None:
    """Gear use (per-activity, by uuid) and strength-exercise identity (category + specific
    variant) are kept for the query layer (V-C6); unfamiliar/empty shapes are ignored,
    never an error, matching the rest of this mapper."""
    from datetime import UTC, datetime

    from vibesynergy.sources.garmin.mappers import map_activity
    from vibesynergy.store.raw import RawRecord

    def raw(payload):
        return RawRecord(
            "r1",
            "garmin",
            "activities",
            "1",
            "x",
            "application/json",
            {},
            DAY,
            DAY,
            datetime.now(UTC),
            None,
            __import__("json").dumps(payload).encode(),
        )

    payload = {
        "summary": {
            "activityId": 1,
            "startTimeGMT": "2026-09-20 08:00:00",
            "activityType": {"typeKey": "strength_training"},
        },
        "gear": [{"uuid": "g-1", "displayName": "Lifting shoes"}, {"uuid": "g-1"}, {}],
        "exercise_sets": {
            "exerciseSets": [
                {
                    "setType": "ACTIVE",
                    "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"}],
                },
                {
                    "setType": "ACTIVE",
                    "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"}],
                },
                {
                    "setType": "ACTIVE",
                    "exercises": [{"category": "SQUAT"}],
                },  # name=None: category only
                {"setType": "REST"},
            ]
        },
    }
    activities = list(map_activity(raw(payload)))
    extra = activities[0].extra
    assert extra["gear_ids"] == ["g-1"]  # one entry per activity, duplicates/empty uuids dropped
    assert extra["exercises"] == [
        {"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS"},
        {"category": "SQUAT", "name": None},
    ]
    assert extra["sets"] == 3  # REST excluded, duplicate exercise identity still counted per set

    # No gear, no exercises: both keys absent rather than empty.
    bare = payload | {"gear": [], "exercise_sets": {}}
    del bare["summary"]
    bare["summary"] = {**payload["summary"]}
    extra2 = next(iter(map_activity(raw(bare)))).extra
    assert "gear_ids" not in extra2 and "exercises" not in extra2


@pytest.mark.parametrize(
    "reply", [None, {}, {"calendarItems": None}, {"calendarItems": {}}, {"calendarItems": [None]}]
)
def test_incomplete_schedule_never_authorizes_mirror(garmin, monkeypatch, reply):
    from vibesynergy.query.calendar_mirror import garmin_coverage

    rt, fake = garmin
    monkeypatch.setattr(fake, "get_scheduled_workouts", lambda *_: reply, raising=False)
    result = rt.sync.sync("garmin", ["scheduled_workouts"], Window(DAY, DAY))[0]
    assert result.error
    assert garmin_coverage(rt.engine, DAY) is None


def test_schedule_coverage_is_latest_processed_window_not_historical_union(garmin, monkeypatch):
    from vibesynergy.query.calendar_mirror import garmin_coverage
    from vibesynergy.store.schema import raw_item

    rt, fake = garmin
    monkeypatch.setattr(
        fake, "get_scheduled_workouts", lambda *_: {"calendarItems": []}, raising=False
    )
    future = date(2027, 4, 1)
    result = rt.sync.sync("garmin", ["scheduled_workouts"], Window(DAY, future))[0]
    assert not result.error and not result.normalise_errors
    assert garmin_coverage(rt.engine, DAY) == future
    # Unchanged content must still produce a new complete receipt.
    rt.sync.sync("garmin", ["scheduled_workouts"], Window(DAY, future))
    assert garmin_coverage(rt.engine, DAY) == future
    # Pending processing (even with a successful fetch) blocks mutation.
    raw_id = rt.raw.current_id("garmin", "scheduled_workouts", "2026-09")
    with rt.engine.begin() as conn:
        conn.execute(raw_item.update().where(raw_item.c.id == raw_id).values(normalised_at=None))
    assert garmin_coverage(rt.engine, DAY) is None
    rt.raw.mark_normalised(raw_id)
    assert garmin_coverage(rt.engine, DAY) == future
    from datetime import timedelta

    from vibesynergy.core.timeutil import utc_now

    later = utc_now() + timedelta(days=2)
    with monkeypatch.context() as clock:
        clock.setattr("vibesynergy.query.calendar_mirror.utc_now", lambda: later)
        assert garmin_coverage(rt.engine, DAY) is None
    rt.sync.sync("garmin", ["scheduled_workouts"], Window(date(2024, 1, 1), date(2024, 1, 31)))
    assert rt.sync.state("garmin", "scheduled_workouts")["synced_until"] == future
    assert garmin_coverage(rt.engine, DAY) is None


RAW_DATASETS = (
    "rhr",
    "sleep_summary",
    "hrv_range",
    "max_metrics_range",
    "weekly_steps",
    "weekly_stress",
    "weekly_intensity_minutes",
    "device_solar",
)

# Present in garminconnect 0.3.16 and collected raw, with no mapper.
_PINNED_METHODS = {
    "rhr": "get_rhr_day",
    "sleep_summary": "get_sleep_daily",
    "hrv_range": "get_hrv_data_range",
    "max_metrics_range": "get_max_metrics_range",
    "weekly_steps": "get_weekly_steps",
    "weekly_stress": "get_weekly_stress",
    "weekly_intensity_minutes": "get_weekly_intensity_minutes",
    "device_solar": "get_device_solar_data",
}

# Checked against garminconnect 0.3.16. Names that are not methods on that pin.
_ABSENT_IN_PINNED_CLIENT = (
    "get_power_duration_curve",
    "get_acclimation",
    "get_energy_balance",
    "get_custom_foods",
    "get_custom_food",
    "get_activity_fit_messages",
    "get_activity_fit_data",
    "get_courses",
    "get_course_details",
    "download_course_gpx",
    "get_garmin_coach_workouts",
    "get_training_plan_workouts",
    "get_sleep_summary_range",
    "get_hrv_trend",
    "get_respiration_trend",
    "get_stats_range",
    "get_vo2max_trend",
    "get_training_load_trend",
    "get_running_tolerance_trend",
)


def test_pinned_client_methods_and_gated_sets() -> None:
    from garminconnect import Garmin

    from vibesynergy.sources.garmin.datasets import BY_NAME

    for name in _ABSENT_IN_PINNED_CLIENT:
        assert not hasattr(Garmin, name), name
    for dataset, method in _PINNED_METHODS.items():
        assert hasattr(Garmin, method)
        assert BY_NAME[dataset].spec.default_enabled
    assert hasattr(Garmin, "get_nutrition_daily_settings")
    assert hasattr(Garmin, "get_available_badge_challenges")
    assert hasattr(Garmin, "get_non_completed_badge_challenges")
    assert "nutrition_daily_settings" not in BY_NAME
    disabled = sorted(name for name, item in BY_NAME.items() if not item.spec.default_enabled)
    assert disabled == ["menstrual", "nutrition_food_log", "nutrition_meals", "pregnancy"]


def test_new_raw_datasets_archive_without_mapping(garmin) -> None:
    rt, _ = garmin
    mappers = rt.registry.get("garmin").mappers()
    assert not set(RAW_DATASETS) & set(mappers)
    results = rt.sync.sync("garmin", list(RAW_DATASETS), Window(DAY, DAY))
    assert {r.dataset: r.error for r in results if r.error} == {}
    assert all(r.normalise_errors == 0 and r.records == 0 and r.fetched for r in results)
    stored = {
        name: rt.raw.load(next(rt.raw.current_ids("garmin", name))).json() for name in RAW_DATASETS
    }
    assert stored["rhr"]["placeholder"] == "get_rhr_day"
    assert stored["sleep_summary"]["placeholder"] == "get_sleep_daily"
    assert stored["hrv_range"]["placeholder"] == "get_hrv_data_range"
    assert stored["max_metrics_range"]["placeholder"] == "get_max_metrics_range"
    assert stored["weekly_intensity_minutes"]["placeholder"] == "get_weekly_intensity_minutes"
    assert stored["device_solar"] == {"solar": {}}
    assert stored["weekly_steps"]["args"] == ["2026-09-20", "3"]
    assert stored["weekly_stress"]["args"] == ["2026-09-20", "3"]


def test_weekly_steps_cover_each_month_in_one_call(garmin) -> None:
    rt, _ = garmin
    rt.sync.sync("garmin", ["weekly_steps"], Window(date(2026, 8, 30), DAY))
    items = {
        rt.raw.load(item_id).item_key: rt.raw.load(item_id).json()
        for item_id in rt.raw.current_ids("garmin", "weekly_steps")
    }
    assert sorted(items) == ["2026-08", "2026-09"]
    assert items["2026-08"]["args"] == ["2026-08-31", "5"]
    assert items["2026-09"]["args"] == ["2026-09-20", "3"]


def test_device_solar_isolates_one_device(garmin) -> None:
    from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError

    rt, fake = garmin
    fake.get_devices = lambda: [{"deviceId": "111"}, {"deviceId": "222"}, {}]

    def solar(device_id, start, end):
        if device_id == "111":
            raise GarminConnectConnectionError("no solar input")
        return [{"device": device_id, "start": start, "end": end}]

    fake.get_device_solar_data = solar
    result = rt.sync.sync("garmin", ["device_solar"], Window(DAY, DAY))[0]
    assert result.error is None and result.records == 0
    payload = rt.raw.load(next(rt.raw.current_ids("garmin", "device_solar"))).json()
    assert set(payload["solar"]) == {"222"}
    assert payload["solar"]["222"][0]["start"] == "2026-09-01"
    assert payload["errors"]["111"].startswith("GarminConnectConnectionError")

    def limited(*_args):
        raise GarminConnectTooManyRequestsError("slow down")

    fake.get_device_solar_data = limited
    failed = rt.sync.sync("garmin", ["device_solar"], Window(DAY, DAY))[0]
    assert failed.error and failed.error.startswith("GarminConnectTooManyRequestsError")


def test_trend_ranges_wait_an_hour_on_a_plain_sync(garmin) -> None:
    rt, fake = garmin
    fake.get_devices = lambda: [{"deviceId": "1"}]
    names = [
        "sleep_summary",
        "hrv_range",
        "max_metrics_range",
        "weekly_steps",
        "weekly_stress",
        "weekly_intensity_minutes",
        "device_solar",
    ]
    rt.sync.sync("garmin", names, Window(DAY, DAY))
    fake.calls.clear()
    rt.sync.sync("garmin", window=Window(DAY, DAY))
    for method in (
        "get_sleep_daily",
        "get_hrv_data_range",
        "get_max_metrics_range",
        "get_weekly_steps",
        "get_weekly_stress",
        "get_weekly_intensity_minutes",
        "get_device_solar_data",
    ):
        assert method not in fake.calls
    assert "get_rhr_day" in fake.calls  # a daily dataset still refreshes with the others

    fake.calls.clear()
    rt.sync.sync("garmin", ["weekly_steps"], Window(DAY, DAY))
    assert "get_weekly_steps" in fake.calls


def test_partial_or_interrupted_schedule_revokes_prior_write_coverage(garmin, monkeypatch):
    from vibesynergy.query.calendar_mirror import garmin_coverage

    rt, fake = garmin
    monkeypatch.setattr(
        fake, "get_scheduled_workouts", lambda *_: {"calendarItems": []}, raising=False
    )
    rt.sync.sync("garmin", ["scheduled_workouts"], Window(DAY, DAY))
    assert garmin_coverage(rt.engine, DAY) == DAY

    def interrupted(*_):
        assert garmin_coverage(rt.engine, DAY) is None
        raise SystemExit("synthetic interruption")

    monkeypatch.setattr(fake, "get_scheduled_workouts", interrupted)
    with pytest.raises(SystemExit):
        rt.sync.sync("garmin", ["scheduled_workouts"], Window(DAY, DAY))
    assert garmin_coverage(rt.engine, DAY) is None


def test_a_stationary_session_counts_its_whole_time() -> None:
    """a 59-minute pilates class showed as 11 minutes (Garmin's active sets)."""
    from vibesynergy.sources.garmin.mappers import moving_seconds

    assert moving_seconds("strength_training", 655.0, 3580.0) == 3580.0
    assert moving_seconds("yoga", 600.0, 3600.0) == 3600.0
    assert moving_seconds("running", 2600.0, 2700.0) == 2600.0  # stops do not count
    assert moving_seconds("strength_training", 655.0, None) == 655.0


def test_a_rate_limit_stops_the_whole_garmin_sync_for_an_hour(garmin) -> None:
    from datetime import timedelta

    from garminconnect import GarminConnectTooManyRequestsError

    from vibesynergy.core.timeutil import utc_now

    rt, fake = garmin
    calls = []

    def limited(*_args):
        calls.append(1)
        raise GarminConnectTooManyRequestsError("slow down")

    fake.get_devices = lambda: [{"deviceId": "111"}]
    fake.get_device_solar_data = limited
    results = rt.sync.sync("garmin", ["device_solar", "device_solar"], Window(DAY, DAY))
    assert len(results) == 1 and len(calls) == 1  # the second dataset was not asked for
    assert results[0].stop_source
    wait = results[0].retry_at - utc_now()
    assert timedelta(minutes=50) < wait <= timedelta(hours=1)
