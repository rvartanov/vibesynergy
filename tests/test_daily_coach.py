"""Morning and evening coaching reads the coach feed and the fixed safety rules."""

from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from test_evidence import START, put

from vibesynergy import alerts
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.model import DailyMetric
from vibesynergy.portal import prefs
from vibesynergy.query import coach_workouts, journal
from vibesynergy.query import daily_coach as q
from vibesynergy.store.schema import coach_decision_log

DAY = START.date()
_TZ = ZoneInfo("Europe/Amsterdam")


def inputs(**changes):
    base = {
        "injuries": [],
        "tags": [],
        "recovery": {},
        "hrv_baseline": None,
        "checkin": None,
        "week": {"volume_ramp_pct": None},
    }
    return {**base, **changes}


def test_safety_rules_are_deterministic():
    assert q.safety(inputs()) == {"level": "ok", "rules": []}
    flag = {
        "short_sleep": inputs(recovery={"sleep.duration": 4 * 3600}),
        "low_readiness": inputs(recovery={"recovery.readiness": 20}),
        "low_hrv": inputs(hrv_baseline={"z": -2.0}),
        "sore": inputs(checkin={"soreness": 4}),
        "load_spike": inputs(week={"volume_ramp_pct": 60}),
        "injury_warning": inputs(
            injuries=[{"latest_pain": 4, "flags": [{"level": "warn", "rule": "pain_above_3"}]}]
        ),
    }
    for rule, data in flag.items():
        out = q.safety(data)
        assert out["level"] == "flag" and rule in out["rules"]
    refuse = {
        "affects_movement": inputs(
            injuries=[{"latest_pain": 2, "flags": [{"level": "stop", "rule": "affects_movement"}]}]
        ),
        "pain_7": inputs(injuries=[{"latest_pain": 7, "flags": []}]),
        "red_flag_note": inputs(tags=["fever"]),
        "sick": inputs(tags=["sick"]),
    }
    for rule, data in refuse.items():
        out = q.safety(data)
        assert out["level"] == "refuse" and rule in out["rules"]
    assert q.safety(inputs(injuries=[{"latest_pain": 2, "flags": []}]))["level"] == "ok"
    assert q.safety(inputs(recovery={}))["level"] == "ok"


def test_safety_partial_checkin(runtime):
    """A check-in with a null soreness score must not crash coaching for that day."""
    import anyio
    from fastapi.testclient import TestClient
    from mcp import Client
    from sqlalchemy.dialects.sqlite import insert

    from vibesynergy.mcp.identity import reset_connection, set_connection
    from vibesynergy.mcp.server import build_server
    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.store.schema import checkin

    assert q.safety(inputs(checkin={"mood": 3})) == {"level": "ok", "rules": []}
    assert q.safety(inputs(checkin={"note": "x"})) == {"level": "ok", "rules": []}
    assert q.safety(inputs(checkin={})) == {"level": "ok", "rules": []}
    assert q.safety(inputs(checkin={"soreness": None, "mood": 3}))["level"] == "ok"
    assert "sore" in q.safety(inputs(checkin={"soreness": 4}))["rules"]

    day = today_in(runtime.settings.timezone)
    journal.set_checkin(runtime.engine, day, mood=3)
    assert q.capture(runtime, day, "morning")["inputs"]["safety"]["level"] == "ok"
    note_day = day - timedelta(days=1)
    journal.set_checkin(runtime.engine, note_day, note="x")
    assert q.capture(runtime, note_day, "morning")["inputs"]["safety"]["level"] == "ok"
    empty_day = day - timedelta(days=2)
    with runtime.engine.begin() as conn:
        conn.execute(insert(checkin).values(subject_id=1, date=empty_day, updated_at=utc_now()))
    assert q.capture(runtime, empty_day, "morning")["inputs"]["safety"]["level"] == "ok"

    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    headers = {"x-vibesynergy": "portal"}
    saved = client.post(
        "/api/diary/checkin",
        json={"date": day.isoformat(), "mood": 3},
        headers=headers,
    )
    assert saved.status_code == 200
    coaching = client.get("/api/daily-coaching", params={"date": day.isoformat()})
    assert coaching.status_code == 200
    assert coaching.json()["data"]["automatic"] is False
    assert coaching.json()["data"]["narrative_enabled"] is False
    feed = client.get("/api/coach/feed")
    assert feed.status_code == 200

    async def call():
        tokens = set_connection("user", "user")
        try:
            async with Client(build_server(runtime)) as mcp:
                return await mcp.call_tool(
                    "vibesynergy_coach_write",
                    {
                        "entry_type": "morning",
                        "summary": "Easy 40 minutes.",
                        "idempotency_key": "partial-checkin-morning",
                        "for_date": day.isoformat(),
                    },
                )
        finally:
            reset_connection(tokens)

    result = anyio.run(call)
    assert result.structured_content["data"]["entry_type"] == "morning"
    morning = client.get("/api/daily-coaching", params={"date": day.isoformat()}).json()["data"]
    assert morning["morning"]["advice"]["interpretation"]["summary"] == "Easy 40 minutes."


def test_read_uses_safety_until_a_coach_writes(runtime):
    put(
        runtime,
        "garmin",
        DailyMetric(DAY, "sleep.duration", 4 * 3600, "s"),
    )
    for rid in runtime.raw.current_ids("garmin"):
        runtime.raw.mark_normalised(rid)
    snapshot = q.read(runtime, DAY)
    assert snapshot["automatic"] is False
    assert snapshot["narrative_enabled"] is False
    assert snapshot["morning"]["state"] == "no_coach"
    assert snapshot["morning"]["advice"] is None
    assert snapshot["morning"]["safety"]["level"] in {"ok", "flag", "refuse"}
    with pytest.raises(ValueError, match="retired"):
        q.prepare(runtime, DAY, "morning")
    with pytest.raises(ValueError, match="retired"):
        q.complete(runtime, "gone", "morning", {}, None)


def test_new_morning_coach_write_raises_an_alert(runtime):
    assert alerts._daily(runtime, None) == []
    since = utc_now() - timedelta(seconds=2)
    coach_workouts.coach_write(
        runtime.engine,
        entry_type="evening",
        summary="Evening only.",
        idempotency_key="alert-evening",
        for_date=DAY,
        author="client:coach",
    )
    assert alerts._daily(runtime, since) == []
    coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary="Keep today easy.",
        idempotency_key="alert-morning",
        for_date=DAY,
        author="client:coach",
    )
    found = alerts._daily(runtime, since)
    assert len(found) == 1
    assert found[0].kind == "daily_coach"
    assert found[0].text == "Messages from your coach: Keep today easy."
    assert found[0].detail == "Keep today easy."
    assert found[0].link == "#/coach"
    assert found[0].event is True
    prefs.save(runtime.settings.data_dir, {"language": "nl"})
    dutch = alerts._daily(runtime, since)
    assert dutch[0].text == "Berichten van je coach: Keep today easy."
    assert dutch[0].detail == "Keep today easy."
    view = q.read(runtime, DAY)
    assert view["morning"]["source"] == "coach_decision_log"
    assert view["morning"]["advice"]["interpretation"]["summary"] == "Keep today easy."
    assert "daily_coach" in alerts.KINDS
    assert "ai_report" not in alerts.KINDS


def test_daily_coach_alert_quotes_each_note_and_skips_blanks(runtime):
    since = utc_now() - timedelta(seconds=2)
    prefix = "Take it easy today. " * 20
    alpha = f"{prefix}End alpha."
    beta = f"{prefix}End beta."
    with runtime.engine.begin() as conn:
        conn.execute(
            coach_decision_log.insert().values(
                subject_id=1,
                at=utc_now(),
                kind="note",
                author="client:coach",
                entry_type="morning",
                text=" \n\t ",
                meta={},
            )
        )
    first = coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary=alpha,
        idempotency_key="alert-alpha",
        for_date=DAY,
        author="client:coach",
    )
    second = coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary=beta,
        idempotency_key="alert-beta",
        for_date=DAY,
        author="client:coach",
    )
    found = alerts._daily(runtime, since)
    assert [item.detail for item in found] == [first["text"], second["text"]]
    assert found[0].detail.endswith("End alpha.")
    assert found[1].detail.endswith("End beta.")
    for item in found:
        heading, _, excerpt = item.text.partition(": ")
        assert heading == "Messages from your coach"
        assert 280 <= len(excerpt) <= 300
        assert excerpt.startswith("Take it easy today.")
        assert "End alpha." not in excerpt and "End beta." not in excerpt
        assert item.link == "#/coach"
        assert item.kind == "daily_coach"


def test_daily_coach_email_keeps_each_note_and_links_to_coach(runtime, monkeypatch):
    runtime.settings = replace(
        runtime.settings, server={"public_url": "https://server.example.ts.net"}
    )
    prefs.save(
        runtime.settings.data_dir,
        {"language": "nl", "alert_routes": {"daily_coach": ["email"]}},
    )
    monkeypatch.setattr(alerts, "channels", lambda settings, engine=None: {"email": True})
    monkeypatch.setattr(
        alerts, "collect", lambda rt, today, since=None: alerts._daily(rt, since, today)
    )
    sent: list[tuple[str, str]] = []
    assert (
        alerts.check(runtime, send=lambda subject, body, channel: sent.append((subject, body)))[
            "new"
        ]
        == []
    )
    same = "Same words, two messages."
    coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary=same,
        idempotency_key="alert-same-1",
        for_date=DAY,
        author="client:coach",
    )
    coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary=same,
        idempotency_key="alert-same-2",
        for_date=DAY,
        author="client:coach",
    )
    alerts.check(runtime, send=lambda subject, body, channel: sent.append((subject, body)))
    assert len(sent) == 1
    subject, body = sent[0]
    # a short subject, and each note once, without label and excerpt in front.
    assert subject.startswith("VibeSynergy coach: ") and len(subject) <= 90
    assert "Note:" not in body and "Berichten van je coach:" not in body
    assert body.count(same) == 2
    assert "https://server.example.ts.net:8447/#/coach" in body
    assert "#/dashboard" not in body


def test_a_load_only_flag_says_load_and_keeps_the_coach_advice(runtime):
    """a load spike is no reported complaint: no physio text, and a coach's advice stays."""
    text = q.fixed_safety_summary(runtime, "morning", "flag", ["load_spike"])
    assert "load" in text and "physiotherapist" not in text
    pain = q.fixed_safety_summary(runtime, "morning", "flag", ["sore"])
    assert "physiotherapist" in pain
    today = today_in(runtime.settings.timezone)
    ready = {
        "safety": {"level": "flag", "rules": ["load_spike"]},
        "interpretation": {"summary": "x"},
    }
    assert q.display_for(runtime, today, "morning", ready)["mode"] == "advice"
    sore = {"safety": {"level": "flag", "rules": ["load_spike", "sore"]}, "interpretation": {}}
    assert q.display_for(runtime, today, "morning", sore)["mode"] == "safety"


def test_a_rise_within_the_planned_hours_is_no_load_spike():
    """a build week that follows the plan does not flag, even after quiet weeks."""
    planned = inputs(
        week={
            "volume_ramp_pct": 80,
            "last_7d_moving_s": 7 * 3600,
            "planned_7d_moving_s": 6.5 * 3600,
        }
    )
    assert "load_spike" not in q.safety(planned)["rules"]
    over = inputs(
        week={
            "volume_ramp_pct": 80,
            "last_7d_moving_s": 9 * 3600,
            "planned_7d_moving_s": 6.5 * 3600,
        }
    )
    assert "load_spike" in q.safety(over)["rules"]
    no_plan = inputs(week={"volume_ramp_pct": 80, "last_7d_moving_s": 7 * 3600})
    assert "load_spike" in q.safety(no_plan)["rules"]


def test_a_training_you_give_is_no_hard_session_on_an_amber_day():
    """'Training geven …, Ext. interval kort' with you running along easy."""
    from vibesynergy.query import dashboard

    plan = {
        "today": [
            {
                "title": "Training geven Dorp, Ext. interval kort",
                "status": "today",
                "coached": True,
            }
        ],
        "next_race": None,
        "plan_phase": None,
    }
    text = dashboard._advice("amber", plan)
    assert "fine at an easy effort" in text and "swap" not in text


def test_an_archived_review_shows_no_safety_text(runtime):
    """an old block review with mixed rules is history, not advice for today."""
    today = today_in(runtime.settings.timezone)
    old = today - timedelta(days=30)
    ready = {"safety": {"level": "flag", "rules": ["load_spike", "low_hrv"]}}
    shown = q.display_for(runtime, old, "block", ready)
    assert shown["archive"] is True and shown["mode"] == "advice" and shown["safety_text"] is None


def test_mail_headline_cuts_at_a_clause_not_mid_word():
    from vibesynergy.alerts import _headline

    note = "Wo 4-11 ochtend: herstel goed (HRV 47 in band 42-56, RHR 45 vs 44,7). Zwemmen houden."
    assert _headline(note) == "Wo 4-11 ochtend: herstel goed"
    long = "Ochtend: " + "heel lange zin zonder haakje of punt " * 4
    out = _headline(long)
    assert out.endswith("…") and len(out) <= 70 and not out[:-1].endswith(" ")
