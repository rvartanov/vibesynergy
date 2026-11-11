"""One safety floor: the dashboard, daily coaching and a planned session share a level."""

from __future__ import annotations

from datetime import date, timedelta

from vibesynergy.query import daily_coach, dashboard, safety_floor

SLEEP_6H02 = 6 * 3600 + 120


def _b(value, mean, sd):
    return {"value": value, "baseline_mean": mean, "baseline_std": sd, "z": (value - mean) / sd}


def _recovery(**changes):
    base = {
        "hrv.night_avg": _b(55, 50, 5),
        "hr.resting": _b(44, 44, 1),
        "sleep.duration": _b(SLEEP_6H02, 8 * 3600, 1800),
        "recovery.readiness": _b(53, 60, 8),
        "sleep.score": _b(80, 80, 5),
        "hrv_low_days": 0,
    }
    base.update(changes)
    return base


def _plan(title="Tempoloop 13.5 km"):
    return {
        "today": [{"title": title, "status": "today"}],
        "next_event": None,
        "next_race": None,
        "plan_phase": {"phase": "build", "days_left": 40},
    }


LOAD = {"tsb": -5, "form": "neutral", "acwr": 1.1}


def test_rough_patch_is_at_least_flag_on_the_dashboard_and_the_coach():
    signals = {
        "sleep_s": SLEEP_6H02,
        "readiness": 53,
        "checkin": {"soreness": 5},
        "injuries": [],
        "tags": [],
    }
    floor = safety_floor.evaluate(signals, date(2026, 10, 16))
    assert floor["level"] in ("flag", "refuse")
    assert "sore" in floor["rules"]

    verdict = dashboard._verdict(
        _recovery(),
        LOAD,
        _plan(),
        [],
        sick=False,
        checkin={"soreness": 5},
    )
    assert verdict["safety_level"] in ("flag", "refuse")
    assert "go ahead" not in verdict["advice"]
    coach = daily_coach.safety(
        {
            "injuries": [],
            "tags": [],
            "recovery": {"sleep.duration": SLEEP_6H02, "recovery.readiness": 53},
            "hrv_baseline": None,
            "checkin": {"soreness": 5},
            "week": {"volume_ramp_pct": None},
        }
    )
    assert coach["level"] == verdict["safety_level"]
    assert coach["level"] in ("flag", "refuse")

    clear = dashboard._verdict(_recovery(), LOAD, _plan(), [], sick=False)
    assert clear["safety_level"] == "ok"
    assert "go ahead" in clear["advice"]


def test_sick_is_refuse_and_the_dashboard_is_red():
    floor = safety_floor.evaluate({"sick": True, "tags": ["sick"], "injuries": [], "checkin": None})
    assert floor["level"] == "refuse"
    assert "sick" in floor["rules"]
    verdict = dashboard._verdict(_recovery(), LOAD, _plan(), [], sick=True)
    assert verdict["level"] == "red"
    assert verdict["safety_level"] == "refuse"
    coach = daily_coach.safety(
        {
            "injuries": [],
            "tags": ["sick"],
            "recovery": {},
            "hrv_baseline": None,
            "checkin": None,
            "week": {"volume_ramp_pct": None},
        }
    )
    assert coach["level"] == "refuse"


def test_stored_rule_names_stay_on_the_coach():
    """Review rows keep the old rule names. Sick is now rest, with the dashboard."""
    names = daily_coach.SAFETY_RULES
    assert names["sick"] == "refuse"
    assert names["sore"] == "flag"
    assert names["short_sleep"] == "flag"
    assert names["pain_7"] == "refuse"


def test_null_soreness_does_not_fire():
    for checkin in ({"mood": 3}, {"note": "x"}, {}, {"soreness": None, "mood": 3}, None):
        floor = safety_floor.evaluate({"checkin": checkin, "injuries": [], "tags": []})
        assert floor["level"] == "ok"
        assert "sore" not in floor["rules"]


def test_hard_session_with_pain_is_flag_and_names_the_injury():
    session = {
        "date": "2026-10-16",
        "title": "Easy spin",
        "intensity": "hard",
        "steps": [{"type": "work", "duration_s": 2400}],
    }
    signals = {
        "today": date(2026, 10, 16),
        "acwr": 1.0,
        "daily_safety": {"level": "ok", "rules": []},
        "recent_races": [],
        "injuries": [{"latest_pain": 4, "flags": []}],
    }
    one = safety_floor.evaluate_session(session, signals)
    assert one["level"] == "flag"
    assert "injury_warning" in one["rules"]
    # The intensity field wins over an easy title.
    assert one["intensity"] == "hard"


def test_long_duurloop_and_hill_words():
    short = {"title": "Lange duurloop", "duration_s": 45 * 60}
    long = {"title": "Lange duurloop", "duration_s": 2 * 3600}
    assert safety_floor.session_is_hard(short) is False
    assert safety_floor.session_is_hard(long) is True
    assert safety_floor.session_is_hard({"title": "Chill jog"}) is False
    assert safety_floor.session_is_hard({"title": "Heuveltraining"}) is True
    easy = safety_floor.evaluate_session(
        {"title": "Hill repeats", "intensity": "easy"},
        {"injuries": [{"latest_pain": 5, "flags": []}]},
    )
    assert easy["intensity"] == "easy"
    assert easy["level"] == "ok"


def test_dashboard_and_daily_coaching_return_the_same_level(runtime):
    """Seeded rough patch: soreness 5, sleep just over 6 h, readiness 53, a tempo on the plan."""
    import anyio
    from fastapi.testclient import TestClient
    from mcp import Client

    from vibesynergy.core.timeutil import today_in, utc_now
    from vibesynergy.mcp.identity import reset_connection, set_connection
    from vibesynergy.mcp.server import build_server
    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.query import journal
    from vibesynergy.store.schema import daily_unified, planned_item, raw_item

    today = today_in(runtime.settings.timezone)
    now = utc_now()
    metrics = []
    for back in range(0, 12):
        day = today - timedelta(days=back)
        sleep = SLEEP_6H02 if back == 0 else 8 * 3600
        for metric, value, unit in (
            ("sleep.duration", sleep, "s"),
            ("recovery.readiness", 53, "score"),
            ("hrv.night_avg", 55, "ms"),
            ("hr.resting", 44, "bpm"),
        ):
            metrics.append(
                {
                    "subject_id": 1,
                    "date": day,
                    "metric": metric,
                    "value": float(value),
                    "unit": unit,
                    "source": "garmin",
                }
            )
    with runtime.engine.begin() as conn:
        conn.execute(
            raw_item.insert(),
            {
                "id": "raw-tempo",
                "source": "garmin",
                "dataset": "calendar",
                "item_key": "tempo",
                "sha256": "cd" * 32,
                "content_type": "application/json",
                "size": 2,
                "request": {},
                "fetched_at": now,
                "last_seen_at": now,
                "is_current": True,
            },
        )
        conn.execute(
            planned_item.insert(),
            {
                "source": "garmin",
                "source_id": "tempo-13",
                "subject_id": 1,
                "date": today,
                "kind": "plan_workout",
                "title": "Tempoloop 13.5 km",
                "sport": "running",
                "is_race": False,
                "primary": False,
                "extra": {},
                "raw_item_id": "raw-tempo",
                "updated_at": now,
            },
        )
        conn.execute(daily_unified.insert(), metrics)
    journal.set_checkin(runtime.engine, today, soreness=5)

    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    dash = client.get("/api/dashboard").json()["data"]
    coaching = client.get("/api/daily-coaching", params={"date": today.isoformat()}).json()["data"]

    async def call():
        tokens = set_connection("user", "user")
        try:
            async with Client(build_server(runtime)) as mcp:
                return await mcp.call_tool(
                    "vibesynergy_daily_coaching", {"date": today.isoformat()}
                )
        finally:
            reset_connection(tokens)

    mcp_payload = anyio.run(call).structured_content["data"]
    dashboard_level = dash["safety"]["level"]
    coach_level = coaching["morning"]["safety"]["level"]
    mcp_level = mcp_payload["morning"]["safety"]["level"]
    assert dashboard_level == coach_level == mcp_level
    assert dashboard_level in ("flag", "refuse")
    assert "go ahead" not in dash["verdict"]["advice"].lower()
    assert "sore" in dash["safety"]["rules"]
    assert "sore" in coaching["morning"]["safety"]["rules"]
