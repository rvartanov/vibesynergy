"""API coach runner: settings, schedule, tool loop, budget, mail, replies, insight trigger.

The model is a scripted fake and mail is captured; nothing leaves the test.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_evidence import START, activity, put

from vibesynergy import mailer
from vibesynergy.coach_runner import MODEL, prompts, runner, schedule
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.coach_runner.model import ModelError
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import agent_events, coach_replies
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import (
    audit_log,
    coach_decision_log,
    coach_reply,
    coach_run,
    injury_log,
    job,
)

KEY = "sk-ant-api03-SYNTHETIC-coach-runner-key-never-shown"
TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
DAY = date(2026, 11, 1)  # a Sunday
DASH = "\N{EN DASH}"
# 09:05 in Amsterdam on 1 Nov 2026 (CET, UTC+1)
MORNING = datetime(2026, 11, 1, 8, 5, tzinfo=UTC)


def usage(inp=1000, out=200, write=0, read=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
    }


def tool_use(name, arguments, n=1, **u):
    return {
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": f"tu_{n}", "name": name, "input": arguments},
        ],
        "stop_reason": "tool_use",
        "usage": usage(**u),
    }


def done(text="Klaar.", **u):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": usage(**u),
    }


def morning_write(summary="Alex, houd de duurloop rustig: Z2.", day=DAY, **extra):
    return {
        "entry_type": "morning",
        "summary": summary,
        "idempotency_key": f"morning-{day.isoformat()}",
        "for_date": day.isoformat(),
        "details": "HRV binnen band.",
        "kb_rules": ["HRV-4"],
        **extra,
    }


class FakeModel:
    """Answers from a script; each entry is a response dict, a callable(request) or an
    exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def create(self, request):
        self.requests.append(json.loads(json.dumps(request)))  # a copy, as sent
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step


@pytest.fixture
def rt(runtime):
    settings = replace(
        runtime.settings,
        server={"public_url": f"https://{TAILNET}"},
        alerts={"email_to": "athlete@example.com"},
    )
    rt = Runtime.open(settings)
    yield rt
    rt.close()


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda settings: True)
    monkeypatch.setattr(
        mailer,
        "send",
        lambda settings, to, subject, body, html=None: sent.append((to, subject, body, html)),
    )
    return sent


def enable(rt, **changes):
    runner_settings.set_api_key(rt, KEY)
    runner_settings.save(rt, {"enabled": True, **changes})


def run_row(rt, job="morning", key=None):
    return runner.get_run(rt, job, key or DAY.isoformat())


def audits(rt, action):
    with rt.engine.connect() as conn:
        return conn.execute(select(audit_log).where(audit_log.c.action == action)).mappings().all()


# --- settings --------------------------------------------------------------------------------


def test_off_by_default(rt):
    cfg = runner_settings.load(rt)
    assert cfg.enabled is False and cfg.api_key_set is False
    assert cfg.monthly_cap_usd == 95.0
    assert schedule.due(rt, MORNING) is False
    model = FakeModel()
    assert runner.run(rt, "morning", DAY.isoformat(), DAY, client=model).status == "off"
    assert model.requests == [] and run_row(rt) is None


def test_cap_from_config_and_portal(runtime):
    rt = Runtime.open(replace(runtime.settings, coach_runner={"monthly_cap_usd": 50}))
    try:
        assert runner_settings.load(rt).monthly_cap_usd == 50
        runner_settings.save(rt, {"monthly_cap_usd": 70})
        assert runner_settings.load(rt).monthly_cap_usd == 70
    finally:
        rt.close()


def test_settings_api_never_returns_the_key(rt, caplog):
    caplog.set_level(logging.DEBUG)
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    first = client.get("/api/coach-runner").json()["data"]
    assert first["enabled"] is False and first["api_key_set"] is False
    assert first["model"] == MODEL and first["monthly_cap_usd"] == 95.0
    refused = client.post("/api/coach-runner/settings", json={"enabled": True}, headers=WRITE)
    assert refused.status_code == 400 and refused.json()["error"] == "Add an API key first."
    bad = client.post("/api/coach-runner/key", json={"value": "hello"}, headers=WRITE)
    assert bad.status_code == 400
    saved = client.post("/api/coach-runner/key", json={"value": KEY}, headers=WRITE)
    assert saved.status_code == 200 and saved.json()["data"]["api_key_set"] is True
    on = client.post(
        "/api/coach-runner/settings",
        json={"enabled": True, "monthly_cap_usd": 60},
        headers=WRITE,
    ).json()["data"]
    assert on["enabled"] is True and on["monthly_cap_usd"] == 60
    too_high = client.post(
        "/api/coach-runner/settings", json={"monthly_cap_usd": 5000}, headers=WRITE
    )
    assert too_high.status_code == 400
    # Writes need the portal's write header.
    assert client.post("/api/coach-runner/key", json={"value": KEY}).status_code == 403
    responses = [client.get("/api/coach-runner").text, saved.text, json.dumps(on)]
    assert all(KEY not in text for text in responses)
    with rt.engine.connect() as conn:
        rows = conn.execute(select(audit_log)).mappings().all()
    assert {"coach_runner.api_key", "coach_runner.settings"} <= {r["action"] for r in rows}
    assert all(KEY not in json.dumps(dict(r), default=str) for r in rows)
    assert KEY not in caplog.text
    # The key file is encrypted on disk.
    for path in (rt.settings.secrets_dir / runner_settings.STORE).iterdir():
        assert KEY.encode() not in path.read_bytes()
    cleared = client.post("/api/coach-runner/key/clear", headers=WRITE).json()["data"]
    assert cleared["api_key_set"] is False and cleared["enabled"] is False


# --- schedule --------------------------------------------------------------------------------


def local(hour, minute, day=DAY):
    """A local Amsterdam time on ``day`` as UTC (CET in November)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC) - timedelta(hours=1)


def test_schedule_times_and_queue(rt):
    enable(rt)
    # DAY is the 1st; the month report has its own test below.
    runner_settings.save(rt, {"schedule": {"monthly": {"on": False}}})
    assert schedule.due_runs(rt, local(9, 3)) == []
    assert [r["job"] for r in schedule.due_runs(rt, local(9, 4))] == ["morning"]
    assert schedule.due_runs(rt, local(12, 5)) == []  # past the grace window
    assert [r["job"] for r in schedule.due_runs(rt, local(19, 44))] == ["weekly"]  # Sunday
    assert schedule.due_runs(rt, local(19, 44, DAY + timedelta(days=1))) == []  # Monday
    # The weekly review is still inside its window when the evening run starts.
    assert [r["job"] for r in schedule.due_runs(rt, local(21, 45))] == ["evening", "weekly"]
    assert [r["job"] for r in schedule.due_runs(rt, local(22, 45))] == ["evening"]
    assert schedule.due(rt, local(9, 30))
    queued = schedule.enqueue_due(rt, local(9, 30))
    assert len(queued) == 1 and schedule.enqueue_due(rt, local(9, 35)) == []  # deduped
    with rt.engine.connect() as conn:
        (row,) = conn.execute(select(job).where(job.c.kind == schedule.JOB)).mappings().all()
    assert row["payload"]["job"] == "morning" and row["max_attempts"] == 2
    # The scheduler service and the handlers are wired; coach runs have their own lane.
    worker = rt.worker()
    assert {schedule.JOB, schedule.SCHEDULE_JOB} <= set(worker._handlers)
    assert rt.worker(lane="coach")._kinds == (schedule.JOB,)
    assert schedule.JOB in rt.worker(lane="sync")._exclude_kinds


def test_schedule_is_set_in_the_settings(rt):
    enable(rt)
    week = {"weekly": {"time": "08:30", "days": [5]}}
    runner_settings.save(rt, {"schedule": {"morning": {"time": "07:15", "days": [0, 2]}, **week}})
    runner_settings.save(rt, {"schedule": {"evening": {"on": False}}})
    cfg = runner_settings.load(rt).schedule
    assert cfg["morning"] == {"on": True, "time": "07:15", "days": [0, 2]}
    assert cfg["evening"]["on"] is False and cfg["evening"]["time"] == "21:45"
    monday, saturday = DAY + timedelta(days=1), DAY + timedelta(days=6)
    assert schedule.due_runs(rt, local(9, 4)) == []  # Sunday: no morning, no week review now
    assert [r["job"] for r in schedule.due_runs(rt, local(7, 15, monday))] == ["morning"]
    assert schedule.due_runs(rt, local(21, 45, monday)) == []  # the evening is off
    assert [r["job"] for r in schedule.due_runs(rt, local(8, 31, saturday))] == ["weekly"]
    for bad in (
        {"morning": {"time": "25:00"}},
        {"morning": {"days": [7]}},
        {"morning": {"days": [1, 1]}},
        {"morning": {"days": []}},
        {"weekly": {"days": [1, 2]}},
        {"lunch": {"on": True}},
        {"morning": {"colour": "red"}},
    ):
        with pytest.raises(runner_settings.RunnerSettingsError):
            runner_settings.save(rt, {"schedule": bad})
    runner_settings.save(rt, {"schedule": {"morning": {"on": False, "days": []}}})  # off: fine


def test_test_button_queues_one_run_now(rt):
    with pytest.raises(schedule.TestRunError):
        schedule.enqueue_test(rt, "morning", MORNING)  # off
    enable(rt)
    with pytest.raises(schedule.TestRunError):
        schedule.enqueue_test(rt, "insight", MORNING)
    schedule.enqueue_test(rt, "evening", MORNING)
    with pytest.raises(schedule.TestRunError):
        schedule.enqueue_test(rt, "evening", MORNING)  # the same second: already queued
    with rt.engine.connect() as conn:
        (row,) = conn.execute(select(job).where(job.c.kind == schedule.JOB)).mappings().all()
    payload = row["payload"]
    assert payload["job"] == "evening" and payload["trigger"] == "test"
    assert payload["run_key"].startswith("test-") and payload["for_date"] == DAY.isoformat()
    # The scheduled evening run of that day still follows: its key is the date.
    assert [r["job"] for r in schedule.due_runs(rt, local(21, 45))] == ["evening", "weekly"]


def test_once_per_day(rt, mail):
    enable(rt)
    model = FakeModel(tool_use("vibesynergy_coach_write", morning_write()), done())
    assert runner.run(rt, "morning", DAY.isoformat(), DAY, client=model).status == "done"
    again = FakeModel()
    assert runner.run(rt, "morning", DAY.isoformat(), DAY, client=again).status == "skipped"
    assert again.requests == []
    assert schedule.due_runs(rt, local(9, 30)) == []
    assert len(mail) == 1


# --- one run ---------------------------------------------------------------------------------


def test_morning_run_tools_mail_and_audit(rt, mail):
    enable(rt)
    model = FakeModel(
        tool_use("vibesynergy_thresholds", {}, n=1, write=20_000),
        tool_use("vibesynergy_add_note", {"text": "nope"}, n=2, read=20_000),  # not offered
        tool_use("vibesynergy_coach_write", morning_write(), n=3, read=21_000),
        done(read=22_000),
    )
    result = runner.run(rt, "morning", DAY.isoformat(), DAY, client=model)
    assert result.status == "done" and result.turns == 4 and result.mailed
    first = model.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert first["cache_control"] == {"type": "ephemeral"}
    names = [t["name"] for t in first["tools"]]
    assert names == sorted(names)
    assert {"vibesynergy_today", "vibesynergy_kb_upsert", "vibesynergy_icu_create_event"} <= set(
        names
    )
    assert {"vibesynergy_add_note", "vibesynergy_sql", "vibesynergy_activity_publish"}.isdisjoint(
        names
    )
    assert "vibesynergy_refresh" not in names
    start = first["messages"][0]["content"]
    assert 'idempotency_key "morning-2026-11-01"' in start and "geen." in start
    # The tool result reached the model; the refused tool came back as an error.
    second = model.requests[1]["messages"]
    assert second[1]["content"][0]["type"] == "thinking"  # assistant content passed back as is
    result_block = second[2]["content"][0]
    assert result_block["tool_use_id"] == "tu_1" and '"sports"' in result_block["content"]
    refused = model.requests[2]["messages"][4]["content"][0]
    assert refused["is_error"] is True and "Unknown tool" in refused["content"]
    # The entry was written through the query layer, as the runner's app.
    with rt.engine.connect() as conn:
        entry = (
            conn.execute(
                select(coach_decision_log).where(
                    coach_decision_log.c.idempotency_key == "morning-2026-11-01"
                )
            )
            .mappings()
            .one()
        )
    assert entry["author"] == "client:coach-runner" and entry["text"].startswith("Alex")
    assert entry["meta"]["author_name"] == "AI coach (API)"
    row = run_row(rt)
    assert row["status"] == "done" and row["entry_id"] == entry["id"] and row["mailed_at"]
    assert row["input_tokens"] == 4000 and row["output_tokens"] == 800
    assert row["cache_write_tokens"] == 20_000 and row["cache_read_tokens"] == 63_000
    expected = (4000 * 4 + 800 * 20 + 20_000 * 5 + 63_000 * 0.2) / 1_000_000
    assert row["cost_usd"] == pytest.approx(expected)
    # The run log keeps what was preloaded and the output-contract result.
    assert [b["name"] for b in row["harness"]["blocks"]][:2] == ["athlete", "communication_rule"]
    assert row["harness"]["contract"] == []
    # Mail: one report, Dutch subject, the entry text and a link to the report page.
    ((to, subject, body, page),) = mail
    assert to == "athlete@example.com"
    # The same HTML layout as the morning alert mail, with a button to the report.
    assert page and "Openen in VibeSynergy" in page and f"#/coach/report/{entry['id']}" in page
    assert "HRV binnen band." not in page  # the details stay on the report page (7-11)
    assert subject == f"Coach ochtend {DASH} zo 1 nov"
    assert body.startswith("Alex, houd de duurloop rustig")
    assert f"https://{TAILNET}:8447/#/coach/report/{entry['id']}" in body
    # Audit: every tool call as the runner's client, and the run with its cost.
    mcp_rows = [
        r for r in audits(rt, "vibesynergy_coach_write") if r["client_id"] == "coach-runner"
    ]
    assert mcp_rows and mcp_rows[0]["scope"] == "coach.write"
    (run_audit,) = audits(rt, "coach_runner.run")
    assert run_audit["actor"] == "coach_runner"
    assert run_audit["detail"]["cost_usd"] == pytest.approx(expected, abs=1e-4)
    assert run_audit["detail"]["status"] == "done"


def test_weekly_and_evening_prompts(rt, mail):
    enable(rt)
    write = {
        "entry_type": "note",
        "summary": "Weekreview 2026-W44\nAlex, volume binnen doel.",
        "idempotency_key": "weekly-2026-11-01",
        "for_date": "2026-11-01",
    }
    model = FakeModel(tool_use("vibesynergy_coach_write", write), done())
    assert runner.run(rt, "weekly", DAY.isoformat(), DAY, client=model).status == "done"
    system = model.requests[0]["system"][-1]["text"]
    assert "wekelijkse review" in system and "met SendUserMessage" not in system
    assert mail[0][1] == f"Coach weekreview {DASH} zo 1 nov"


def test_make_again_writes_a_new_report_and_send_again_mails_it(rt, mail):
    """a report must be possible to make and send again."""
    from vibesynergy.query import coach_runner as runner_q

    enable(rt)
    first = FakeModel(tool_use("vibesynergy_coach_write", morning_write()), done())
    done_first = runner.run(rt, "morning", DAY.isoformat(), DAY, client=first)
    # A redo has its own key, so the coach writes a new entry instead of getting the old one.
    key = runner.entry_key("morning", DAY, "test-20261101T100000")
    assert key == "morning-2026-11-01-test-20261101T100000"
    assert "test-20261101T100000" in runner.start_message(
        rt, "morning", DAY, run_key="test-20261101T100000"
    )
    redo_write = morning_write("Alex, toch rustig aan vandaag.", idempotency_key=key)
    redo = FakeModel(tool_use("vibesynergy_coach_write", redo_write), done())
    again = runner.run(rt, "morning", "test-20261101T100000", DAY, trigger="test", client=redo)
    assert again.status == "done" and again.entry_id != done_first.entry_id
    mail.clear()
    runner_q.resend(rt, "morning")
    ((_, _, body, _),) = mail
    assert body.startswith("Alex, toch rustig aan vandaag.")
    with pytest.raises(runner_q.TestRunError):
        runner_q.resend(rt, "weekly")  # nothing to send yet


def test_no_entry_means_failed_and_a_warning(rt, mail):
    """when something goes wrong I must get a message."""
    enable(rt)
    result = runner.run(rt, "evening", DAY.isoformat(), DAY, client=FakeModel(done()))
    assert result.status == "failed" and "no coach entry" in result.error
    ((_, subject, body, html),) = mail  # one warning, no report
    assert subject.startswith("Coachrapport mislukt: Avondreview")
    assert "Nu testen" in body and "no coach entry" in body and html is None


def test_a_crashing_tool_is_named_in_the_warning(rt, mail):
    enable(rt)
    box_call = runner.ToolBox.call

    def crash(self, name, arguments):
        if name == "vibesynergy_coach_write":
            self.crashed.append(name)
            return "The tool vibesynergy_coach_write failed (TypeError).", True
        return box_call(self, name, arguments)

    from unittest import mock

    with mock.patch.object(runner.ToolBox, "call", crash):
        model = FakeModel(tool_use("vibesynergy_coach_write", morning_write()), done())
        result = runner.run(rt, "morning", DAY.isoformat(), DAY, client=model)
    assert result.status == "failed" and "vibesynergy_coach_write" in result.error
    assert len(mail) == 1 and "vibesynergy_coach_write" in mail[0][2]


def test_crash_leaves_the_job_retryable_once(rt, mail):
    enable(rt)
    flaky = FakeModel(ModelError("API error 529", retry=True))
    with pytest.raises(runner.RunnerError):
        runner.run(rt, "morning", DAY.isoformat(), DAY, client=flaky)
    assert run_row(rt)["status"] == "failed" and run_row(rt)["attempts"] == 1
    good = FakeModel(tool_use("vibesynergy_coach_write", morning_write()), done())
    assert runner.run(rt, "morning", DAY.isoformat(), DAY, client=good).status == "done"
    assert run_row(rt)["attempts"] == 2
    # A process that died mid-run on its last attempt is not run a third time.
    other = DAY + timedelta(days=1)
    with rt.engine.begin() as conn:
        now = datetime.now(UTC)
        conn.execute(
            coach_run.insert().values(
                job="morning",
                run_key=other.isoformat(),
                for_date=other,
                trigger="schedule",
                status="running",
                attempts=2,
                model=MODEL,
                started_at=now,
                updated_at=now,
                turns=1,
                input_tokens=10,
                output_tokens=10,
                cache_write_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.01,
            )
        )
    never = FakeModel()
    assert runner.run(rt, "morning", other.isoformat(), other, client=never).status == "skipped"
    assert never.requests == [] and run_row(rt, key=other.isoformat())["status"] == "failed"


def test_permanent_error_is_not_retried(rt):
    enable(rt)
    model = FakeModel(ModelError("the API key was refused (401)", retry=False))
    result = runner.run(rt, "morning", DAY.isoformat(), DAY, client=model)
    assert result.status == "failed" and "401" in result.error


def test_job_queue_runs_it_in_the_coach_lane(rt, mail, monkeypatch):
    enable(rt)
    fake = FakeModel(tool_use("vibesynergy_coach_write", morning_write()), done())
    monkeypatch.setattr(runner, "AnthropicClient", lambda key: fake)
    schedule.enqueue_due(rt, MORNING)
    assert rt.worker(lane="sync").run_once() is False  # the sync lane leaves it alone
    monkeypatch.setattr(schedule, "utc_now", lambda: MORNING)
    assert rt.worker(lane="coach").run_once() is True
    assert run_row(rt)["status"] == "done" and len(mail) == 1


# --- budget ----------------------------------------------------------------------------------


def test_budget_limits_stop_and_warn(rt, mail):
    enable(rt, monthly_cap_usd=1)
    # 45k output tokens = $0.90 per turn: past 80 % after one turn, past 100 % after two.
    model = FakeModel(
        tool_use("vibesynergy_thresholds", {}, n=1, out=45_000),
        tool_use("vibesynergy_thresholds", {}, n=2, out=45_000),
        tool_use("vibesynergy_thresholds", {}, n=3, out=45_000),
    )
    big = Runtime.open(replace(rt.settings, coach_runner={"max_output_tokens": 200_000}))
    try:
        result = runner.run(big, "morning", DAY.isoformat(), DAY, client=model)
    finally:
        big.close()
    assert len(model.requests) == 2  # the third turn never started
    assert result.status == "budget" and result.stop == "monthly_cap"
    subjects = [subject for _, subject, *_ in mail]
    assert subjects == ["Coach via API gestopt: maandlimiet bereikt"]
    # Next run: no model call, recorded as stopped by the budget; nothing is due.
    later = FakeModel()
    stopped = runner.run(rt, "evening", DAY.isoformat(), DAY, client=later)
    assert stopped.status == "budget" and later.requests == []
    assert schedule.due_runs(rt, local(21, 50)) == []
    assert len(mail) == 1  # warned once per month
    # A higher cap lets the stopped run go again the same day.
    runner_settings.save(rt, {"monthly_cap_usd": 50})
    assert [r["job"] for r in schedule.due_runs(rt, local(22, 50))] == ["evening"]


def test_eighty_percent_warning(rt, mail):
    enable(rt, monthly_cap_usd=1)
    model = FakeModel(tool_use("vibesynergy_coach_write", morning_write(), out=42_000), done(out=0))
    assert runner.run(rt, "morning", DAY.isoformat(), DAY, client=model).status == "done"
    subjects = [subject for _, subject, *_ in mail]
    assert "Coach via API: 80% van de maandlimiet gebruikt" in subjects


def test_per_run_limits(runtime, mail):
    rt = Runtime.open(
        replace(runtime.settings, coach_runner={"max_turns": 2, "max_output_tokens": 50_000})
    )
    try:
        enable(rt)
        loop = FakeModel(*(tool_use("vibesynergy_thresholds", {}, n=i) for i in range(5)))
        result = runner.run(rt, "morning", DAY.isoformat(), DAY, client=loop)
        assert result.stop == "max_turns" and len(loop.requests) == 2
        assert loop.requests[0]["max_tokens"] == 16_000
        wordy = FakeModel(
            *(tool_use("vibesynergy_thresholds", {}, n=i, out=40_000) for i in range(5))
        )
        result = runner.run(rt, "evening", DAY.isoformat(), DAY, client=wordy)
        assert result.stop == "max_output_tokens" and len(wordy.requests) == 2
        assert wordy.requests[1]["max_tokens"] == 10_000  # what is left of the run's budget
    finally:
        rt.close()


# --- replies ---------------------------------------------------------------------------------


def test_reply_roundtrip_and_approval(rt, mail, caplog):
    caplog.set_level(logging.INFO)
    enable(rt)
    model = FakeModel(
        tool_use(
            "vibesynergy_coach_write", morning_write("Voorstel: dinsdag interval naar woensdag?")
        ),
        done(),
    )
    runner.run(rt, "morning", DAY.isoformat(), DAY, client=model)
    entry_id = run_row(rt)["entry_id"]
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    blank = client.post(
        "/api/coach/replies", json={"entry_id": entry_id, "text": " "}, headers=WRITE
    )
    assert blank.status_code == 400
    missing = client.post("/api/coach/replies", json={"entry_id": 999, "text": "x"}, headers=WRITE)
    assert missing.status_code == 404
    reply = client.post(
        "/api/coach/replies",
        json={"entry_id": entry_id, "text": "Ja, akkoord met het voorstel. Privé-zin 4711."},
        headers=WRITE,
    ).json()["data"]
    edited = client.post(
        f"/api/coach/replies/{reply['id']}",
        json={"text": "Akkoord, verplaats hem. Privé-zin 4711."},
        headers=WRITE,
    ).json()["data"]
    assert edited["text"].startswith("Akkoord") and edited["read"] is False
    page = client.get(f"/api/coach/report/{entry_id}").json()["data"]
    assert page["entry"]["id"] == entry_id and page["replies"][0]["id"] == reply["id"]
    feed = client.get("/api/coach/feed").json()["data"]["entries"]
    assert next(e for e in feed if e["id"] == entry_id)["replies"][0]["text"].startswith("Akkoord")
    # The evening run gets the unread reply and the approval rule, then marks it read.
    evening = {
        **morning_write("Alex, interval staat nu op woensdag.", entry_type="evening"),
        "idempotency_key": "evening-2026-11-01",
        "details": 'Akkoord van Alex: "Akkoord, verplaats hem."',
    }
    later = FakeModel(tool_use("vibesynergy_coach_write", evening), done())
    assert runner.run(rt, "evening", DAY.isoformat(), DAY, client=later).status == "done"
    start = later.requests[0]["messages"][0]["content"]
    assert "Akkoord, verplaats hem." in start and f"entry #{entry_id}" in start
    rules = later.requests[0]["system"][-1]["text"]
    assert '"akkoord" of "ja"' in rules and "citeer het" in rules
    with rt.engine.connect() as conn:
        row = conn.execute(select(coach_reply)).mappings().one()
    assert row["read_at"] is not None and row["read_run_id"] == run_row(rt, "evening")["id"]
    assert coach_replies.unread(rt.engine) == []
    late_edit = client.post(
        f"/api/coach/replies/{reply['id']}", json={"text": "toch niet"}, headers=WRITE
    )
    assert late_edit.status_code == 400
    # A third run sees no reply any more.
    third = FakeModel(done())
    runner.run(rt, "weekly", DAY.isoformat(), DAY, client=third)
    assert "geen." in third.requests[0]["messages"][0]["content"]
    # The reply text, the prompt and the key never reach the log.
    assert "4711" not in caplog.text and KEY not in caplog.text
    assert "Voorstel" not in caplog.text
    assert any(r["action"] == "coach.reply.add" for r in audits(rt, "coach.reply.add"))


# --- insight on activity.published -------------------------------------------------------------


T0 = START + timedelta(hours=1)


def strava_public(private=False):
    return replace(
        activity(), source_id="2", name="Strava title", fit_id=None, extra={"private": private}
    )


def publish(rt):
    agent_events.since(rt, now=START - timedelta(hours=1))
    agent_events.since(rt, now=START - timedelta(hours=1), key="published_since")
    put(rt, "garmin", activity())
    put(rt, "strava", strava_public(private=False))
    agent_events.observe(rt, now=T0)
    return agent_events.run(rt, now=T0 + timedelta(minutes=6), http=lambda *a: None)


def coach_jobs(rt):
    with rt.engine.connect() as conn:
        return conn.execute(select(job).where(job.c.kind == schedule.JOB)).mappings().all()


def test_insight_is_not_queued_while_off(rt):
    result = publish(rt)
    assert result["published"] and coach_jobs(rt) == []


def test_insight_run_on_activity_published(rt, mail, monkeypatch):
    enable(rt)
    result = publish(rt)
    (queued,) = coach_jobs(rt)
    payload = queued["payload"]
    assert payload["job"] == "insight" and payload["run_key"] == result["published"]
    assert payload["payload"]["activity_ids"] == ["garmin:1"]
    assert payload["for_date"] == START.date().isoformat()
    private = {
        "entry_type": "activity_assessment",
        "summary": "x",
        "idempotency_key": "insight-garmin:1",
        "for_date": START.date().isoformat(),
        "activity_id": "strava:2",
        "public_insight": "Rustige duurloop ondanks lage HRV.",
    }
    clean = {**private, "public_insight": "Rustige duurloop, netjes in Z2.", "summary": "Netjes."}
    wrong_type = {**clean, "entry_type": "morning", "idempotency_key": "x-1"}
    fake = FakeModel(
        tool_use("vibesynergy_coach_write", wrong_type, n=1),
        tool_use("vibesynergy_coach_write", private, n=2),
        tool_use("vibesynergy_coach_write", clean, n=3),
        done(),
    )
    monkeypatch.setattr(runner, "AnthropicClient", lambda key: fake)
    assert rt.worker(lane="coach").run_once() is True
    names = {t["name"] for t in fake.requests[0]["tools"]}
    assert "vibesynergy_activity" in names and "vibesynergy_coach_write" in names
    assert {
        "vibesynergy_notes",
        "vibesynergy_recovery",
        "vibesynergy_today",
        "vibesynergy_injuries",
    }.isdisjoint(names)
    assert {"vibesynergy_icu_update_event", "vibesynergy_coach_entries"}.isdisjoint(names)
    start = fake.requests[0]["messages"][0]["content"]
    assert f"event_id={result['published']}" in start and "activity_ids=garmin:1" in start
    errors = [fake.requests[i]["messages"][-1]["content"][0] for i in (1, 2)]
    assert errors[0]["is_error"] and "activity_assessment" in errors[0]["content"]
    assert errors[1]["is_error"] and "INS-2" in errors[1]["content"]
    with rt.engine.connect() as conn:
        (entry,) = (
            conn.execute(
                select(coach_decision_log).where(
                    coach_decision_log.c.entry_type == "activity_assessment"
                )
            )
            .mappings()
            .all()
        )
    assert entry["meta"]["public_insight"] == "Rustige duurloop, netjes in Z2."
    row = runner.get_run(rt, "insight", result["published"])
    assert row["status"] == "done" and row["trigger"] == "event"
    assert mail == []  # an insight has no report
    # The same event never queues a second run.
    with rt.engine.begin() as conn:
        assert (
            schedule.on_agent_event(
                rt, conn, {"type": "activity.published", "id": result["published"], "data": {}}
            )
            is None
        )


def test_internal_subscriber_failure_never_blocks_the_event(rt, monkeypatch):
    def broken(*_):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_events, "internal_subscribers", lambda: [broken])
    assert publish(rt)["published"]


# --- prompts ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job",
    [
        "morning",
        "evening",
        "weekly",
        "monthly",
        "block",
        "race",
        "test",
        "thresholds",
        "kb",
        "injury",
        "checkin",
        "insight",
    ],
)
def test_prompts_load_from_docs_without_the_routine_comment(rt, job):
    text = prompts.job_prompt(rt.settings, job)
    assert not text.startswith("<!--") and "trig_" not in text
    body, rules = text.split("## VibeSynergy API-runner")
    assert "SendUserMessage" not in body
    assert job == "insight" or "geen SendUserMessage" in rules


def test_prompt_rewrites(tmp_path, rt):
    (tmp_path / "morning.md").write_text(
        "<!-- copy -->\nGebruik ToolSearch om tools te laden. Upload via de Garmin connector.\n"
        "Stuur Alex met SendUserMessage: kort."
    )
    custom = replace(rt.settings, coach_runner={"prompts_dir": str(tmp_path)})
    text = prompts.job_prompt(custom, "morning")
    assert "ToolSearch om" not in text and "Garmin connector" not in text
    assert "Stuur Alex in de summary van je vibesynergy_coach_write" in text
    with pytest.raises(prompts.PromptError):
        prompts.job_prompt(custom, "evening")


def test_the_runner_reads_the_prompt_edited_in_the_portal(rt):
    """8-11: an edit under Coach → Methods & rules must reach the API coach."""
    from vibesynergy.query import coach_prompts, knowledge

    coach_prompts.seed(rt.engine)
    seeded = prompts.job_prompt(rt.settings, "evening", rt.engine)
    assert seeded == prompts.job_prompt(rt.settings, "evening")  # same as the seed file
    knowledge.upsert_article(
        rt.engine,
        slug=coach_prompts.slug_for("evening"),
        author="portal",
        title=coach_prompts.TITLES["evening"],
        body_md="Schrijf alleen: rust goed.",
        topic=coach_prompts.TOPIC,
        evidence_grade="C",
        status="active",
    )
    text = prompts.job_prompt(rt.settings, "evening", rt.engine)
    assert text.startswith("Schrijf alleen: rust goed.") and prompts.rules_for("morning") in text


def test_model_choice_per_kind_of_run(rt, mail):
    """a model choice for the coach and for the Garmin/Strava line."""
    from vibesynergy.coach_runner import pricing

    enable(rt)
    assert runner_settings.load(rt).models == {"coach": MODEL, "insight": MODEL}
    runner_settings.save(rt, {"models": {"coach": "claude-sonnet-5"}})
    runner_settings.save(rt, {"models": {"insight": "claude-haiku-4-5-20251001"}})
    cfg = runner_settings.load(rt)
    assert cfg.models == {"coach": "claude-sonnet-5", "insight": "claude-haiku-4-5-20251001"}
    with pytest.raises(runner_settings.RunnerSettingsError):
        runner_settings.save(rt, {"models": {"coach": "gpt-4"}})
    with pytest.raises(runner_settings.RunnerSettingsError):
        runner_settings.save(rt, {"models": {"weekly": "claude-sonnet-5"}})
    model = FakeModel(tool_use("vibesynergy_coach_write", morning_write()), done())
    assert runner.run(rt, "morning", DAY.isoformat(), DAY, client=model).status == "done"
    first = model.requests[0]
    assert first["model"] == "claude-sonnet-5" and first["thinking"] == {"type": "adaptive"}
    assert run_row(rt)["model"] == "claude-sonnet-5"
    # Haiku 4.5 gets a thinking budget instead of adaptive thinking with an effort level.
    assert runner._reasoning("claude-haiku-4-5-20251001", "high") == {
        "thinking": {"type": "enabled", "budget_tokens": 4000}
    }
    counts = {"input": 1_000_000, "output": 0, "cache_write": 0, "cache_read": 0}
    assert pricing.cost_usd(counts, "claude-sonnet-5") == 3.0
    assert pricing.cost_usd(counts, "unknown") == 4.0  # the dearest, to stop in time


def test_openai_format_translates_both_ways() -> None:
    """OpenAI, xAI and local servers speak Chat Completions; the runner speaks Messages."""
    from vibesynergy.coach_runner.model import OpenAICompatClient

    request = {
        "model": "gpt-5",
        "max_tokens": 1000,
        "system": [{"type": "text", "text": "Be brief."}],
        "tools": [
            {
                "name": "vibesynergy_today",
                "description": "Today",
                "input_schema": {"type": "object"},
            }
        ],
        "messages": [
            {"role": "user", "content": "Go"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "x"},
                    {
                        "type": "tool_use",
                        "id": "c1",
                        "name": "vibesynergy_today",
                        "input": {"date": None},
                    },
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}],
            },
        ],
        "thinking": {"type": "adaptive"},
    }
    body = OpenAICompatClient.to_openai(request)
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "vibesynergy_today"
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert body["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert "thinking" not in body
    answer = OpenAICompatClient.from_openai(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c2",
                                "function": {"name": "vibesynergy_week", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    assert answer["stop_reason"] == "tool_use"
    assert answer["content"][0] == {
        "type": "tool_use",
        "id": "c2",
        "name": "vibesynergy_week",
        "input": {},
    }
    assert answer["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_provider_and_model_per_task(rt, mail) -> None:
    enable(rt)
    runner_settings.set_api_key(rt, "xai-1234567890abcdef", provider="xai")
    runner_settings.save(
        rt,
        {
            "tasks": {"evening": {"provider": "xai", "model": "grok-4"}},
            "local_url": "http://localhost:11434/v1",
        },
    )
    cfg = runner_settings.load(rt)
    assert cfg.tasks["evening"] == {"provider": "xai", "model": "grok-4"}
    assert cfg.tasks["morning"]["provider"] == "anthropic" and cfg.usable("local")
    assert (
        runner.provider_for(cfg, "evening") == "xai"
        and runner.model_for(cfg, "evening") == "grok-4"
    )
    for bad in (
        {"evening": {"provider": "acme"}},
        {"lunch": {"provider": "xai"}},
        {"evening": {"model": "two words"}},
    ):
        with pytest.raises(runner_settings.RunnerSettingsError):
            runner_settings.save(rt, {"tasks": bad})
    with pytest.raises(runner_settings.RunnerSettingsError):
        runner_settings.save(rt, {"local_url": "not a url"})
    # The evening run goes to xAI's model, without Anthropic's thinking settings.
    model = FakeModel(
        tool_use(
            "vibesynergy_coach_write",
            morning_write(
                entry_type="evening",
                idempotency_key=f"evening-{DAY.isoformat()}",
            ),
        ),
        done(),
    )
    runner.run(rt, "evening", DAY.isoformat(), DAY, client=model)
    first = model.requests[0]
    assert first["model"] == "grok-4" and "thinking" not in first and "output_config" not in first


def test_insight_task_can_be_switched_off(rt):
    """The insight task has its own switch; the reports keep theirs in the schedule."""
    enable(rt)
    assert runner_settings.load(rt).tasks["insight"]["on"] is True
    assert "on" not in runner_settings.load(rt).tasks["morning"]
    runner_settings.save(rt, {"tasks": {"insight": {"on": False}}})
    assert runner_settings.load(rt).tasks["insight"]["on"] is False
    with pytest.raises(runner_settings.RunnerSettingsError):
        runner_settings.save(rt, {"tasks": {"morning": {"on": False}}})
    event = {"id": 1, "type": "activity.published", "data": {"activities": [{"id": "a1"}]}}
    with rt.engine.begin() as conn:
        assert schedule.on_agent_event(rt, conn, event) is None


def test_monthly_report_runs_on_its_day_of_the_month(rt):
    """Task 14: the month report runs on a day of the month, not a weekday. Off by
    default (the block review covers it)."""
    enable(rt)
    assert runner_settings.load(rt).schedule["monthly"]["on"] is False
    runner_settings.save(rt, {"schedule": {"monthly": {"on": True}}})
    first = date(2026, 12, 1)

    def monthly(day):
        return [r["job"] for r in schedule.due_runs(rt, local(10, 4, day)) if r["job"] == "monthly"]

    assert monthly(first) == ["monthly"]
    assert monthly(first + timedelta(days=1)) == []
    runner_settings.save(rt, {"schedule": {"monthly": {"monthday": 2}}})
    assert monthly(first + timedelta(days=1)) == ["monthly"]
    for bad in (0, 29, True, "1"):
        with pytest.raises(runner_settings.RunnerSettingsError):
            runner_settings.save(rt, {"schedule": {"monthly": {"monthday": bad}}})
    with pytest.raises(runner_settings.RunnerSettingsError):
        runner_settings.save(rt, {"schedule": {"weekly": {"monthday": 1}}})
    assert "Maandrapport" in prompts.job_prompt(rt.settings, "monthly", rt.engine)


def test_catch_up_queues_a_report_that_never_ran(rt):
    """Task 16: a report with no run today is queued once, after its window."""
    enable(rt)
    cfg = runner_settings.load(rt)
    missed = {
        "id": "ev1",
        "type": "coach.run_missed",
        "data": {"task": "morning", "date": "2026-11-01"},
    }

    def catch(now):
        with rt.engine.begin() as conn:
            return schedule._catch_up(rt, conn, cfg, missed, now=now)

    assert catch(local(11, 0)) is None  # still inside the window: the schedule runs it
    queued = catch(local(13, 0))
    assert queued is not None and catch(local(13, 5)) is None  # once
    with rt.engine.connect() as conn:
        row = conn.execute(select(job).where(job.c.id == queued)).mappings().one()
    assert row["payload"] == {
        "job": "morning",
        "run_key": "2026-11-01",
        "for_date": "2026-11-01",
        "trigger": "catch_up",
    }
    old = {**missed, "data": {"task": "morning", "date": "2026-10-31"}}
    with rt.engine.begin() as conn:
        assert schedule._catch_up(rt, conn, cfg, old, now=local(13, 0)) is None  # never late days
    runner_settings.save(rt, {"tasks": {"catch_up": {"on": False}}})
    off = runner_settings.load(rt)
    weekly = {**missed, "data": {"task": "weekly", "date": "2026-11-01"}}
    with rt.engine.begin() as conn:
        assert schedule._catch_up(rt, conn, off, weekly, now=local(23, 0)) is None  # switched off
        assert schedule._catch_up(rt, conn, cfg, weekly, now=local(23, 0)) is not None


def test_injury_check_starts_on_real_pain_once_a_day(rt):
    """Task 7: pain of 4+ or pain that affects movement, logged for today."""
    from vibesynergy.query import journal

    enable(rt)
    runner_settings.save(rt, {"schedule": {"monthly": {"on": False}}})

    def injury_due(now):
        return [r for r in schedule.due_runs(rt, now) if r["job"] == "injury"]

    journal.log_injury(
        rt.engine, today=DAY, body_part="calf", side="left", kind="pain", pain=2, timing="after"
    )
    assert injury_due(local(16, 0) + timedelta(hours=1)) == []  # mild: no check
    journal.log_injury(
        rt.engine, today=DAY, body_part="calf", side="left", pain=5, timing="after", when=DAY
    )
    with rt.engine.begin() as conn:
        conn.execute(injury_log.update().values(updated_at=local(15, 0)))
    assert injury_due(local(15, 5)) == []  # still settling
    (due,) = injury_due(local(15, 20))
    assert due == {"job": "injury", "run_key": "2026-11-01", "for_date": "2026-11-01"}
    runner_settings.save(rt, {"tasks": {"injury": {"on": False}}})
    assert injury_due(local(15, 20)) == []


def test_checkin_reaction_after_the_morning_report(rt):
    """Task 8: a hard check-in after the morning report, before 20:00, once a day."""
    from vibesynergy.query import journal
    from vibesynergy.store.schema import checkin

    enable(rt)
    runner_settings.save(rt, {"schedule": {"monthly": {"on": False}}})

    def checkin_due(now):
        return [r for r in schedule.due_runs(rt, now) if r["job"] == "checkin"]

    def saved_at(when):
        with rt.engine.begin() as conn:
            conn.execute(checkin.update().values(updated_at=when))

    journal.set_checkin(rt.engine, DAY, sleep_quality=2, mood=4)  # one low score
    saved_at(local(11, 0))
    assert checkin_due(local(11, 30)) == []
    journal.set_checkin(rt.engine, DAY, stress=5)  # two now
    saved_at(local(11, 0))
    assert checkin_due(local(11, 30)) == []  # no morning report yet: it will read it
    with rt.engine.begin() as conn:
        conn.execute(
            coach_run.insert().values(
                job="morning",
                run_key=DAY.isoformat(),
                for_date=DAY,
                trigger="schedule",
                status="done",
                attempts=1,
                model=MODEL,
                started_at=local(9, 4),
                updated_at=local(9, 10),
                finished_at=local(9, 10),
                turns=1,
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.0,
            )
        )
    assert checkin_due(local(11, 10)) == []  # still settling
    (due,) = checkin_due(local(11, 30))
    assert due["run_key"] == DAY.isoformat()
    assert checkin_due(local(20, 30)) == []  # the evening review takes it from here
    runner_settings.save(rt, {"tasks": {"checkin": {"on": False}}})
    assert checkin_due(local(11, 30)) == []


def test_race_brief_runs_seven_days_and_one_day_before(rt, monkeypatch):
    """Task 13: a brief at 18:04, 7 days and 1 day before a race, not otherwise."""
    from vibesynergy.query import plan

    enable(rt)
    race_day = DAY + timedelta(days=7)
    monkeypatch.setattr(
        plan,
        "calendar_races",
        lambda engine, today: [{"name": "Trail", "days_away": (race_day - today).days}],
    )

    def race_due(day):
        return [r["job"] for r in schedule.due_runs(rt, local(18, 4, day)) if r["job"] == "race"]

    assert race_due(DAY) == ["race"]  # 7 days before
    assert race_due(DAY + timedelta(days=3)) == []
    assert race_due(race_day - timedelta(days=1)) == ["race"]
    assert race_due(race_day) == []
    runner_settings.save(rt, {"schedule": {"race": {"on": False}}})
    assert race_due(DAY) == []


def test_block_review_on_the_sunday_a_phase_ends(rt, monkeypatch):
    """Task 12: on the Sunday whose week has another phase than the next week."""
    from vibesynergy.query import plan_model

    enable(rt)
    # DAY (1-11) is a Sunday; its week starts 26-10.
    weeks = [
        {"monday": "2026-10-19", "phase": "base"},
        {"monday": "2026-10-26", "phase": "base"},
        {"monday": "2026-11-02", "phase": "build"},
    ]
    monkeypatch.setattr(plan_model, "view", lambda engine, start, end: {"weeks": weeks})

    def block_due(day):
        return [r["job"] for r in schedule.due_runs(rt, local(20, 14, day)) if r["job"] == "block"]

    assert schedule.block_ending(rt, DAY) == {"phase": "base", "next_phase": "build"}
    assert block_due(DAY) == ["block"]
    weeks[2]["phase"] = "base"  # the same phase goes on: no review
    assert block_due(DAY) == []
    assert schedule.block_ending(rt, DAY - timedelta(days=1)) is None  # not the week's end
    weeks[1]["phase"] = "race"  # a one-week phase: no review of its own
    assert block_due(DAY) == []


def test_test_evaluation_after_a_planned_test_is_done(rt, monkeypatch):
    """Task 10: a planned test with an activity of its sport that day, once."""
    from vibesynergy.query import plan_model
    from vibesynergy.store.schema import activity_unified

    enable(rt)
    sessions = [
        {"session_id": "lake27:2026-11-01:run-test", "name": "Looptest 30'", "sport": "Run"},
        {"session_id": "lake27:2026-11-01:swim", "name": "Techniek zwemmen", "sport": "Swim"},
    ]
    monkeypatch.setattr(plan_model, "view", lambda engine, start, end: {"sessions": sessions})
    cfg = runner_settings.load(rt)
    assert schedule.tests_done(rt, cfg, DAY) == []  # not done yet
    with rt.engine.begin() as conn:
        conn.execute(
            activity_unified.insert().values(
                group_id="garmin:1",
                subject_id=1,
                start_utc=local(10, 0),
                local_date=DAY,
                tz_offset_s=3600,
                sport="running",
                name="Looptest",
                elapsed_s=3400,
                primary_source="garmin",
                sources=["garmin:1"],
                updated_at=local(11, 0),
                kind="training",
            )
        )
    assert schedule.tests_done(rt, cfg, DAY) == [("lake27:2026-11-01:run-test", ["garmin:1"])]
    (item,) = [r for r in schedule.due_runs(rt, local(12, 0)) if r["job"] == "test"]
    assert item["payload"]["activity_ids"] == ["garmin:1"]
    assert "Geplande sessie: lake27:2026-11-01:run-test" in runner.start_message(
        rt, "test", DAY, payload=item["payload"]
    )
    runner_settings.save(rt, {"tasks": {"test": {"on": False}}})
    assert schedule.tests_done(rt, runner_settings.load(rt), DAY) == []


def test_only_the_test_task_may_propose_thresholds():
    from vibesynergy.coach_runner import registry

    proposers = {t.id for t in registry.TASKS if t.scopes}
    assert proposers == {"test", "thresholds", "kb"}
    assert all(registry.BY_ID[i].scopes == ("plan.propose",) for i in proposers)


def test_threshold_check_runs_once_per_signal(rt, monkeypatch):
    """Task 11: Monday, only with a signal, and the same signal only once."""
    from vibesynergy.query import thresholds

    enable(rt)
    found = []
    monkeypatch.setattr(thresholds, "signals", lambda engine, today: list(found))
    monday = DAY + timedelta(days=1)

    def check_due():
        return [r for r in schedule.due_runs(rt, local(9, 34, monday)) if r["job"] == "thresholds"]

    assert check_due() == []  # nothing measured differs
    found.append({"sport": "run", "metric": "max_hr", "current": 179.0, "measured": 185.0})
    (due,) = check_due()
    assert due["run_key"].startswith("signals-")
    with rt.engine.begin() as conn:
        conn.execute(
            coach_run.insert().values(
                job="thresholds",
                run_key=due["run_key"],
                for_date=monday,
                trigger="schedule",
                status="done",
                attempts=1,
                model=MODEL,
                started_at=local(9, 34, monday),
                updated_at=local(9, 40, monday),
                turns=1,
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.0,
            )
        )
    assert check_due() == []  # the same signal: not again
    found[0] = {**found[0], "measured": 188.0}
    assert len(check_due()) == 1  # a new value is a new finding


def test_threshold_signals_from_measured_values(rt):
    """A run peak above the confirmed max HR is a signal; at or below it is not."""
    from vibesynergy.query import athlete_threshold, thresholds
    from vibesynergy.store.schema import activity_unified

    row = athlete_threshold.propose_threshold(
        rt.engine, sport="run", metric="max_hr", value=179, unit="bpm", method="manual"
    )
    athlete_threshold.confirm_threshold(rt.engine, row["id"])

    def run(peak):
        with rt.engine.begin() as conn:
            conn.execute(activity_unified.delete())
            conn.execute(
                activity_unified.insert().values(
                    group_id="garmin:9",
                    subject_id=1,
                    start_utc=local(10, 0),
                    local_date=DAY,
                    tz_offset_s=3600,
                    sport="running",
                    name="Groep",
                    elapsed_s=3600,
                    max_hr=peak,
                    primary_source="garmin",
                    sources=["garmin:9"],
                    updated_at=local(11, 0),
                    kind="training",
                )
            )
        return [s for s in thresholds.signals(rt.engine, DAY) if s["metric"] == "max_hr"]

    assert run(181) == []  # within the margin
    (signal,) = run(186)
    assert signal["current"] == 179.0 and signal["measured"] == 186.0
    assert signal["activity_id"] == "garmin:9"


def test_kb_care_on_its_day_and_only_by_proposal(rt):
    """Task 18: the 15th at 10:34; a direct KB write is refused in that run."""
    from vibesynergy.coach_runner import context
    from vibesynergy.coach_runner.tools import kb_guard

    enable(rt)
    fifteenth = date(2026, 11, 15)

    def kb_due(day):
        return [r["job"] for r in schedule.due_runs(rt, local(10, 34, day)) if r["job"] == "kb"]

    assert kb_due(fifteenth) == ["kb"]
    assert kb_due(fifteenth + timedelta(days=1)) == []
    guard = kb_guard()
    assert "propose" in guard("vibesynergy_kb_upsert", {"slug": "x"})
    assert guard("vibesynergy_kb_propose_change", {}) is None
    block = context.BLOCKS["kb_due"].build(rt, DAY, {})
    assert len(block["check"]) <= context.KB_PER_MONTH and "pending" in block


def test_rules_name_the_athlete_and_point_at_the_plan_model(rt):
    """no person's name in the code; the coach writes VibeSynergy's plan, not intervals."""
    from vibesynergy.portal import prefs

    text = prompts.job_prompt(rt.settings, "morning")
    assert "Alex" not in text.split("## VibeSynergy API-runner")[1]
    assert "de atleet" in text and "vibesynergy_plan_session_set" in text
    prefs.save(rt.settings.data_dir, {"athlete_name": "Sanne"})
    text = prompts.job_prompt(rt.settings, "morning")
    assert "VibeSynergy mailt die tekst aan\n  Sanne" in text
    assert "Antwoorden van Sanne sinds de vorige run: geen." in runner.start_message(
        rt, "morning", DAY
    )
    with pytest.raises(ValueError):
        prefs.save(rt.settings.data_dir, {"athlete_name": " "})


def test_the_harness_over_mcp(rt, mail):
    """another AI sees the tasks and the run log and can start one run."""
    import anyio
    from mcp import Client

    from vibesynergy.mcp.server import build_server

    def call(tool, **args):
        async def go():
            async with Client(build_server(rt)) as client:
                return await client.call_tool(tool, args)

        result = anyio.run(go)
        text = result.content[0].text if result.content else ""
        if result.is_error:
            raise ValueError(text)
        return result.structured_content or json.loads(text)

    tasks = {t["id"]: t for t in call("vibesynergy_harness_tasks")["data"]["tasks"]}
    assert tasks["morning"]["schedule"]["time"] == "09:04" and tasks["morning"]["on"]
    assert tasks["catch_up"]["runs"] == "code" and tasks["monthly"]["on"] is False
    with pytest.raises(ValueError, match="Switch Coach via API on first"):
        call("vibesynergy_harness_run", task="morning")
    enable(rt)
    assert call("vibesynergy_harness_run", task="morning")["data"] == {"queued": "morning"}
    with pytest.raises(ValueError, match="Unknown report"):
        call("vibesynergy_harness_run", task="insight")
    model = FakeModel(tool_use("vibesynergy_coach_write", morning_write(), n=1), done())
    runner.run(rt, "morning", DAY.isoformat(), DAY, client=model)
    (run,) = call("vibesynergy_harness_runs", task="morning")["data"]["runs"]
    assert run["status"] == "done" and run["harness"]["contract"] == []


def test_an_mcp_coach_sees_the_runners_reports(rt):
    """11-11: vibesynergy_today showed only the caller's own entries, so a coach over MCP missed
    this morning's report from the runner. One coach, two doors: both count."""
    from vibesynergy.query import coach_brief, coach_workouts

    assert coach_brief.RUNNER_AUTHOR == runner.RUNNER_AUTHOR
    coach_workouts.coach_write(
        rt.engine,
        entry_type="morning",
        summary="Alex, rustig aan vandaag.",
        idempotency_key="morning-2026-11-11",
        for_date=date(2026, 11, 11),
        author=runner.RUNNER_AUTHOR,
    )
    entries = coach_brief._coach_entries(rt)
    assert entries and entries[0]["summary"] == "Alex, rustig aan vandaag."
