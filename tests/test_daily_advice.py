"""#89: the daily advice is the AI's (colour, safety, reasons), the rules are the fallback
with a reason from a fixed list, and every view reads one source.

Synthetic data only: thirty ordinary days of HRV so the page is not the onboarding guide,
coach entries written through the MCP tool or straight into the log, runs in ``coach_run``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import insert, select

from vibesynergy import i18n
from vibesynergy.coach_runner import report, runner, schedule
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.portal import prefs
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_brief, daily_advice, daily_coach, dashboard, journal, workouts
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import audit_log, coach_decision_log, coach_run, daily_unified, job

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
EASE = {"level": "ease", "colour": "amber", "reasons": ["Check-in moe, HRV normaal"]}
GREEN = {"level": "ok", "colour": "green", "reasons": ["HRV binnen je band"]}


@pytest.fixture
def rt(runtime, monkeypatch):
    settings = replace(
        runtime.settings,
        server={"public_url": f"https://{TAILNET}"},
        alerts={"email_to": "athlete@example.com"},
    )
    rt = Runtime.open(settings)
    day = today_in(rt.settings.timezone)
    with rt.engine.begin() as conn:
        conn.execute(
            daily_unified.insert(),
            [
                {
                    "subject_id": 1,
                    "date": day - timedelta(days=back),
                    "metric": "hrv.night_avg",
                    "value": 52.0 if back == 0 else 50.0 + back % 5 - 2,
                    "unit": "ms",
                    "source": "garmin",
                }
                for back in range(30)
            ],
        )
    # The runner is on and within budget unless a test says otherwise.
    monkeypatch.setattr(daily_advice, "_runner", lambda rt_: (True, False))
    yield rt
    rt.close()


def _day(rt):
    return today_in(rt.settings.timezone)


def _mcp(rt, tool, **args):
    async def go():
        tokens = set_connection("client-1", scopes=frozenset({"read", "coach.write"}), role="own")
        try:
            async with Client(build_server(rt)) as client:
                return await client.call_tool(tool, args)
        finally:
            reset_connection(tokens)

    return anyio.run(go)


def _write(rt, safety=EASE, summary="Doe de duurloop rustig, 45 minuten in Z2.", key="m-1"):
    # A later version names the one it replaces (#115).
    before = daily_advice.previous_version(rt, _day(rt))
    because = (
        {"previous": before["id"], "reason": "Nieuwe versie na nieuwe gegevens van vandaag."}
        if before
        else None
    )
    result = _mcp(
        rt,
        "vibesynergy_coach_write",
        entry_type="morning",
        summary=summary,
        idempotency_key=key,
        for_date=_day(rt).isoformat(),
        safety=safety,
        **({"changed_because": because} if because else {}),
    )
    assert not result.is_error, result.content[0].text
    return result.structured_content["data"]


def _entry(rt, meta, *, text="Rustig aan vandaag.", at=None, entry_type="morning"):
    with rt.engine.begin() as conn:
        return conn.execute(
            insert(coach_decision_log)
            .values(
                subject_id=1,
                at=at or utc_now(),
                kind="coach",
                author="client:coach-runner",
                entry_type=entry_type,
                for_date=_day(rt),
                text=text,
                meta=meta,
            )
            .returning(coach_decision_log.c.id)
        ).scalar_one()


def _run(rt, status, *, error=None, job_name="morning", key=None, started=None):
    now = started or utc_now()
    with rt.engine.begin() as conn:
        conn.execute(
            insert(coach_run).values(
                job=job_name,
                run_key=key or f"k-{now.timestamp()}",
                for_date=_day(rt),
                trigger="schedule",
                status=status,
                attempts=1,
                model="claude-test",
                started_at=now,
                updated_at=now,
                finished_at=None if status == "running" else now,
                turns=1,
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.0,
                error=error,
            )
        )


def _board(rt, **kwargs):
    return dashboard.dashboard(
        rt.engine,
        _day(rt),
        timezone=rt.settings.timezone,
        athlete=rt.settings.athlete,
        sources=rt.registry.enabled_ids(),
        gear=workouts.gear(rt.engine, rt.raw, _day(rt))["gear"],
        rt=rt,
        **kwargs,
    )


def _shape(found):
    return (found["source"], found["level"], found["safety_level"], found["advice"])


# --- one source -------------------------------------------------------------------------------


def test_every_view_gives_the_same_text_colour_and_level(rt):
    _write(rt)
    board = _board(rt)
    advice = board["advice"]
    assert advice["source"] == "ai" and advice["fallback"] is None
    assert (advice["level"], advice["safety_level"]) == ("amber", "ease")
    assert advice["advice"] == "Doe de duurloop rustig, 45 minuten in Z2."
    assert advice["reasons"] == EASE["reasons"]
    # The rules keep their own outcome beside it (the shadow, the floor for the contract).
    assert board["verdict"]["level"] == "green" and board["safety"]["level"] == "ok"

    standalone = daily_advice.current(rt)
    brief = coach_brief.today_brief(rt, _day(rt))
    mcp = _mcp(rt, "vibesynergy_today").structured_content["data"]
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    api = client.get("/api/advice").json()["data"]
    page = client.get("/api/dashboard").json()["data"]["advice"]
    for found in (standalone, brief["advice"], mcp["advice"], api, page):
        assert _shape(found) == _shape(advice)
        assert found["text"] == advice["text"] and found["reasons"] == advice["reasons"]
    for verdict in (brief["verdict"], mcp["verdict"]):
        assert (verdict["level"], verdict["safety_level"], verdict["advice"]) == (
            "amber",
            "ease",
            advice["advice"],
        )
    # The mail leads with the same colour and headline, in the mail's language.
    tr = i18n.translator(report.language(rt.settings))
    in_mail = daily_advice.current(rt, _day(rt), tr=tr, trigger=False)
    assert _shape(in_mail) == _shape(advice)
    # The mail leads with the advice's one view (#147): the colour, its meaning and when
    # this version was written, in the mail's language.
    view = daily_advice.current(rt, tr=tr, trigger=False)["view"]
    assert view["verdict"] == f"{tr('Amber')} \N{EN DASH} {in_mail['headline']}"
    entry = {"id": 1, "text": advice["advice"]}
    body = report.body(rt, entry, view)
    assert body.startswith(f"{view['verdict']}\n{view['stamp']}\n\n{advice['advice']}")
    html = report.html(rt, "Coach", entry, view)
    assert tr("Amber") in html and in_mail["headline"] in html and view["stamp"] in html
    # The coach page shows the AI's words without the rules' safety line beside them.
    morning = daily_coach.read(rt, _day(rt))["morning"]
    assert morning["display"]["mode"] == "advice" and morning["ai_safety"]["level"] == "ease"


def test_rules_only_is_the_same_everywhere_too(rt):
    _write(rt)
    prefs.save(rt.settings.data_dir, {"daily_advice": "rules"})
    board = _board(rt)
    advice = board["advice"]
    assert advice["mode"] == "rules" and advice["source"] == "rules"
    assert advice["fallback"] is None  # a choice, not a fallback
    rules = board["verdict"]
    assert (advice["level"], advice["advice"]) == (rules["level"], rules["advice"])
    brief = coach_brief.today_brief(rt, _day(rt))
    assert _shape(brief["advice"]) == _shape(advice) == _shape(daily_advice.current(rt))
    # Without AI the coach page goes back to the rules' safety handling.
    assert daily_coach.read(rt, _day(rt))["morning"]["ai_safety"] is None


def test_off_gives_no_daily_advice(rt):
    prefs.save(rt.settings.data_dir, {"daily_advice": "off"})
    advice = _board(rt)["advice"]
    assert advice["mode"] == "off" and advice["level"] is None and advice["advice"] is None
    assert "Settings" in advice["off_text"]


def test_the_setting_is_validated(rt):
    with pytest.raises(ValueError, match="daily_advice"):
        prefs.save(rt.settings.data_dir, {"daily_advice": "sometimes"})
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    reply = client.post("/api/prefs", json={"changes": {"daily_advice": "rules"}}, headers=WRITE)
    assert reply.status_code == 200 and reply.json()["data"]["daily_advice"] == "rules"
    assert daily_advice.mode(rt) == "rules"


def test_a_share_link_without_the_coach_gets_the_rules(rt):
    _write(rt)
    found = _board(rt, allow_ai=False)["advice"]
    assert found["source"] == "rules" and found["fallback"] is None


# --- the fallback, with its reason ------------------------------------------------------------


def _fallback(rt):
    found = _board(rt)["advice"]
    assert found["source"] == "rules", found
    return found["fallback"]["reason"]


def test_no_run_yet(rt):
    assert _fallback(rt) == "no_run"


def test_a_failed_run(rt):
    _run(rt, "failed", error="no coach entry written (stopped: end_turn)")
    assert _fallback(rt) == "run_failed"


@pytest.mark.parametrize(
    "meta",
    [
        # The runner marked it invalid after the run.
        {
            "safety": EASE,
            "harness": {"via": "runner", "task": "morning", "valid": False, "issues": ["x"]},
        },
        # An entry from before the harness, without a safety outcome.
        {"harness": {"via": "mcp", "task": "morning"}},
        # A colour that contradicts the level never counts, wherever it came from.
        {"safety": {"level": "ok", "colour": "red", "reasons": ["x"]}},
        {"safety": {"level": "flag", "colour": "amber", "reasons": []}},
    ],
)
def test_output_outside_the_contract(rt, meta):
    _entry(rt, meta)
    assert _fallback(rt) == "contract"


def test_a_run_that_failed_the_contract(rt):
    _entry(rt, {"safety": EASE, "harness": {"valid": False}})
    _run(rt, "failed", error="output contract: no safety outcome (level, colour, reasons)")
    assert _fallback(rt) == "contract"


def test_ai_switched_off(rt, monkeypatch):
    monkeypatch.setattr(daily_advice, "_runner", lambda rt_: (False, False))
    assert _fallback(rt) == "ai_off"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("budget", None),
        ("failed", "could not reach the API"),
        ("failed", "API error 529"),
        ("failed", "no anthropic API key"),
    ],
)
def test_budget_used_up_or_provider_down(rt, status, error):
    _run(rt, status, error=error)
    assert _fallback(rt) == "unavailable"


def test_over_budget_before_any_run(rt, monkeypatch):
    monkeypatch.setattr(daily_advice, "_runner", lambda rt_: (True, True))
    assert _fallback(rt) == "unavailable"


def test_a_newer_run_that_failed_hands_back_to_the_rules(rt):
    _write(rt)
    _run(rt, "failed", error="no coach entry written", started=utc_now() + timedelta(seconds=5))
    assert _fallback(rt) == "run_failed"


def test_a_failed_evening_run_only_touches_tomorrow(rt):
    """The evening run prepares tomorrow: its failure never replaces today's valid
    morning advice, and tomorrow shows the rules with the reason."""
    _write(rt)
    _run(
        rt,
        "failed",
        job_name="evening",
        error="no coach entry written",
        started=utc_now() + timedelta(seconds=5),
    )
    today = _board(rt)["advice"]
    assert today["source"] == "ai" and today["fallback"] is None and today["updating"] is None
    tomorrow = daily_advice.current(rt, _day(rt) + timedelta(days=1), trigger=False)
    assert tomorrow["source"] == "rules" and tomorrow["fallback"]["reason"] == "run_failed"


def test_a_run_for_another_day_never_counts(rt):
    _write(rt)
    # Yesterday's morning run failed: it was about yesterday, not today.
    with rt.engine.begin() as conn:
        conn.execute(
            insert(coach_run).values(
                job="morning",
                run_key="old",
                for_date=_day(rt) - timedelta(days=1),
                trigger="schedule",
                status="failed",
                attempts=1,
                model="m",
                started_at=utc_now() + timedelta(seconds=5),
                updated_at=utc_now(),
                turns=0,
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.0,
                error="boom",
            )
        )
    assert _board(rt)["advice"]["source"] == "ai"


def test_a_run_in_progress_keeps_the_ai_advice_updating(rt):
    _write(rt)
    _run(rt, "running", started=utc_now() + timedelta(seconds=5))
    found = _board(rt)["advice"]
    assert found["source"] == "ai" and found["updating"]["kind"] == "running"


def test_the_newest_valid_advice_wins(rt):
    _write(rt, safety=GREEN, summary="Ga ervoor.", key="first")
    _write(rt, summary="Toch rustiger.", key="second")
    _entry(rt, {"safety": EASE, "harness": {"valid": False}}, text="Ongeldig.")
    found = _board(rt)["advice"]
    assert found["advice"] == "Toch rustiger." and found["level"] == "amber"


# --- a hard new signal after the run ------------------------------------------------------------


def _sick(rt):
    journal.add_note(rt.engine, text="Koorts", start=_day(rt), tags=["sick"])


def test_a_hard_signal_after_the_advice_starts_a_new_run_at_once(rt):
    _write(rt, safety=GREEN, summary="Ga ervoor.")
    _sick(rt)
    found = _board(rt)["advice"]
    # The AI advice stays, marked as updating; the run is queued once.
    assert found["source"] == "ai" and found["updating"]["kind"] == "new_signal"
    key = daily_advice.rerun_key(_day(rt), ["sick"])
    with rt.engine.connect() as conn:
        queued = conn.execute(select(job.c.payload).where(job.c.kind == schedule.JOB)).all()
    assert [p for (p,) in queued] == [
        {"job": "morning", "run_key": key, "for_date": _day(rt).isoformat(), "trigger": "signal"}
    ]
    _board(rt)
    with rt.engine.connect() as conn:
        assert len(conn.execute(select(job.c.id).where(job.c.kind == schedule.JOB)).all()) == 1


def test_a_hard_signal_falls_back_only_when_the_new_run_fails(rt):
    _write(rt, safety=GREEN, summary="Ga ervoor.")
    _sick(rt)
    _run(rt, "failed", key=daily_advice.rerun_key(_day(rt), ["sick"]), error="boom")
    assert _fallback(rt) == "new_signal"


def test_a_hard_signal_without_a_runner_falls_back_at_once(rt, monkeypatch):
    _write(rt, safety=GREEN, summary="Ga ervoor.")
    _sick(rt)
    monkeypatch.setattr(daily_advice, "_runner", lambda rt_: (False, False))
    assert _fallback(rt) == "new_signal"


def test_a_signal_the_ai_already_saw_is_not_new(rt):
    _sick(rt)
    sick = {"level": "refuse", "colour": "red", "reasons": ["Koorts: rust vandaag"]}
    _write(rt, safety=sick, summary="Rust vandaag.")
    found = _board(rt)["advice"]
    assert found["source"] == "ai" and found["level"] == "red" and found["updating"] is None


# --- the shadow ----------------------------------------------------------------------------------


def test_differences_with_the_rules_are_shown_and_logged_once(rt):
    _write(rt)  # amber/ease, the rules say green/ok
    found = _board(rt)["advice"]
    assert found["differences"] == [
        {"field": "colour", "ai": "amber", "rules": "green"},
        {"field": "safety_level", "ai": "ease", "rules": "ok"},
    ]
    assert found["rules"]["level"] == "green"
    _board(rt)
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(select(audit_log).where(audit_log.c.action == "advice.shadow"))
            .mappings()
            .all()
        )
    assert len(rows) == 1 and rows[0]["outcome"] == "stricter"
    assert rows[0]["detail"]["ai"] == {"colour": "amber", "level": "ease"}


def test_no_difference_no_log(rt):
    _write(rt, safety=GREEN)
    found = _board(rt)["advice"]
    assert found["differences"] == []
    with rt.engine.connect() as conn:
        assert (
            conn.execute(
                select(audit_log.c.id).where(audit_log.c.action == "advice.shadow")
            ).first()
            is None
        )


# --- the manual refresh -------------------------------------------------------------------------


def test_refresh_syncs_then_runs_one_advice_after_the_syncs(rt, monkeypatch):
    monkeypatch.setattr(rt.registry, "enabled_ids", lambda: ["garmin", "strava"])
    monkeypatch.setattr(schedule, "_active", lambda rt_: object())
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    start = client.post("/api/advice/refresh", json={}, headers=WRITE).json()["data"]
    assert start["ai"] is True and set(start["sync"]) == {"garmin", "strava"}
    ids = ",".join(str(i) for i in start["sync"].values())
    status = client.get("/api/advice/refresh", params={"key": start["key"], "jobs": ids})
    state = status.json()["data"]
    assert state["sync"] == {"total": 2, "done": 0, "failed": []}
    assert state["run"] == "waiting" and state["finished"] is False
    with rt.engine.connect() as conn:
        row = conn.execute(select(job).where(job.c.kind == schedule.JOB)).mappings().one()
    assert row["payload"]["wait_for"] == list(start["sync"].values())
    assert row["payload"]["trigger"] == "manual" and row["payload"]["job"] == "morning"
    # While the syncs run, the advice run waits without using an attempt.
    from vibesynergy.sync.worker import JobError

    queued = type("Job", (), {"payload": row["payload"]})()
    with pytest.raises(JobError) as waited:
        schedule.handle(rt, queued)
    assert waited.value.retry_at is not None
    # The syncs are done, the run failed: the page falls back with the reason.
    with rt.engine.begin() as conn:
        conn.execute(job.update().where(job.c.kind == "sync").values(status="done"))
        conn.execute(job.update().where(job.c.kind == schedule.JOB).values(status="done"))
    _run(rt, "failed", key=start["key"], error="no coach entry written")
    state = client.get("/api/advice/refresh", params={"key": start["key"], "jobs": ids}).json()
    assert state["data"]["run"] == "failed" and state["data"]["finished"] is True
    assert _fallback(rt) == "run_failed"


def test_refresh_without_ai_only_syncs(rt, monkeypatch):
    prefs.save(rt.settings.data_dir, {"daily_advice": "rules"})
    monkeypatch.setattr(rt.registry, "enabled_ids", lambda: ["garmin"])
    found = daily_advice.refresh(rt, now=datetime(2026, 11, 17, 8, 0, tzinfo=UTC))
    assert found["ai"] is False and found["reason"] == "rules"
    with rt.engine.connect() as conn:
        assert conn.execute(select(job.c.id).where(job.c.kind == schedule.JOB)).first() is None
    assert runner.get_run(rt, "morning", found["key"]) is None


def test_refresh_status_refuses_an_unknown_key(rt):
    with pytest.raises(ValueError):
        daily_advice.refresh_status(rt, "../etc", [])
