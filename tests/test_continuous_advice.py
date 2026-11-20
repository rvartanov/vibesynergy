"""#90: continuous advice. The fixed advice run at 07:00, new data that makes a new version
(with the wait and the daily limit), the evening advice for tomorrow, one current advice per
target day with its versions, and mail only on a real change.

The model is a scripted fake, mail is captured and all data is synthetic.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select
from test_coach_runner import (
    KEY,
    SAFE,
    FakeModel,
    done,
    local,
    morning_write,
    tool_use,
)

from vibesynergy import alerts, mailer
from vibesynergy.coach_runner import runner, schedule, triggers
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_runner as runner_q
from vibesynergy.query import daily_advice, journal
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log, coach_run, daily_unified, job

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
DAY = date(2026, 11, 2)  # a Monday: no week review, block or month report in the way
AMBER = {"level": "ease", "colour": "amber", "reasons": ["Check-in moe"]}


@pytest.fixture
def rt(runtime):
    settings = replace(
        runtime.settings,
        server={"public_url": f"https://{TAILNET}"},
        alerts={"email_to": "athlete@example.com"},
    )
    rt = Runtime.open(settings)
    runner_settings.set_api_key(rt, KEY)
    runner_settings.save(rt, {"enabled": True})
    yield rt
    rt.close()


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda settings: True)
    # These tests read the advice report as a mail: route the kind "advice" to email too
    # (by default it is a push and a portal notice, no mail).
    routes = alerts.routes
    monkeypatch.setattr(
        alerts, "routes", lambda settings: {**routes(settings), "advice": ["portal", "email"]}
    )
    monkeypatch.setattr(
        mailer,
        "send",
        lambda settings, to, subject, body, html=None: sent.append((to, subject, body, html)),
    )
    return sent


def at(hour, minute, day=DAY):
    return local(hour, minute, day)


def base_run(rt, day=DAY, *, job_name="morning", key=None, started=None, trigger="schedule"):
    """A finished daily-advice run that stored the inputs of now, as the runner does."""
    target = daily_advice.target_day(job_name, day)
    started = started or at(7, 0, day)
    with rt.engine.begin() as conn:
        conn.execute(
            insert(coach_run).values(
                job=job_name,
                run_key=key or day.isoformat(),
                for_date=day,
                trigger=trigger,
                status="done",
                attempts=1,
                model="claude-test",
                started_at=started,
                updated_at=started,
                finished_at=started,
                turns=1,
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.5,
                harness={
                    "inputs": triggers.fingerprint(rt, target),
                    "target": target.isoformat(),
                },
            )
        )


def updates(rt, now):
    """The runs new data asks for now (the scheduler's tick)."""
    return [r for r in schedule.due_runs(rt, now) if r.get("trigger") == "event"]


def hrv(rt, value, day=DAY):
    with rt.engine.begin() as conn:
        conn.execute(
            delete(daily_unified).where(
                daily_unified.c.date == day, daily_unified.c.metric == "hrv.night_avg"
            )
        )
        conn.execute(
            insert(daily_unified).values(
                subject_id=1,
                date=day,
                metric="hrv.night_avg",
                value=value,
                unit="ms",
                source="garmin",
            )
        )


# --- the fixed run and the settings -------------------------------------------------------------


def test_the_advice_runs_at_seven_and_the_time_is_a_setting(rt):
    assert runner_settings.load(rt).schedule["morning"]["time"] == "07:00"
    assert [r["job"] for r in schedule.due_runs(rt, at(7, 0))] == ["morning"]
    assert schedule.due_runs(rt, at(6, 59)) == []
    runner_settings.save(rt, {"schedule": {"morning": {"time": "06:30"}}})
    assert [r["job"] for r in schedule.due_runs(rt, at(6, 30))] == ["morning"]


def test_a_schedule_saved_before_moves_the_morning_to_seven_once(rt):
    store = runner_settings.store(rt)
    doc = store.get(runner_settings.SETTINGS_DOC)
    doc["schedule"] = {
        "morning": {"on": True, "time": "09:00", "days": [0, 1, 2]},
        "evening": {"on": True, "time": "21:30", "days": [0, 1]},
    }
    doc.pop("schedule_version", None)
    store.set(runner_settings.SETTINGS_DOC, doc)
    cfg = runner_settings.load(rt).schedule
    assert cfg["morning"] == {"on": True, "time": "07:00", "days": [0, 1, 2]}
    assert cfg["evening"]["time"] == "21:30"  # every other choice stays
    runner_settings.save(rt, {"monthly_cap_usd": 50})  # any save keeps it
    runner_settings.save(rt, {"schedule": {"morning": {"time": "08:15"}}})
    assert runner_settings.load(rt).schedule["morning"]["time"] == "08:15"  # a new choice holds


def test_the_daily_maximum_is_a_setting_with_its_cost(rt):
    assert runner_settings.load(rt).advice == {"max_runs": triggers.DEFAULT_MAX_RUNS}
    runner_settings.save(rt, {"advice": {"max_runs": 6}})
    assert runner_settings.load(rt).advice["max_runs"] == 6
    for bad in ({"max_runs": -1}, {"max_runs": 25}, {"max_runs": True}, {"often": 2}, 3):
        with pytest.raises(runner_settings.RunnerSettingsError):
            runner_settings.save(rt, {"advice": bad})
    base_run(rt, today_in(rt.settings.timezone), started=utc_now())
    advice = runner_q.overview(rt)["advice"]
    assert advice["max_runs"] == 6 and advice["cost_per_run_usd"] == 0.5
    assert advice["max_cost_per_day_usd"] == 3.0 and advice["max_cost_per_month_usd"] == 90.0
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    saved = client.post(
        "/api/coach-runner/settings", json={"advice": {"max_runs": 2}}, headers=WRITE
    )
    assert saved.status_code == 200 and saved.json()["data"]["advice"]["max_runs"] == 2


# --- the triggers -------------------------------------------------------------------------------


def test_a_checkin_after_the_fixed_run_starts_the_checkin_task_after_the_wait(rt, mail):
    """The check-in task never ran before (#90): it waited for a check-in after a 09:04
    run and two low scores. Now a check-in after the fixed run starts it."""
    base_run(rt)
    journal.set_checkin(rt.engine, DAY, sleep_quality=4, mood=4)
    assert updates(rt, at(8, 11)) == []  # waits a few minutes for more data
    (item,) = updates(rt, at(8, 17))
    assert item["job"] == "checkin" and item["for_date"] == DAY.isoformat()
    assert item["payload"] == {"changes": ["checkin"], "target": DAY.isoformat()}
    assert item["run_key"].startswith(f"event-{DAY.isoformat()}-")
    (queued,) = schedule.enqueue_due(rt, at(8, 17))
    with rt.engine.connect() as conn:
        row = conn.execute(select(job).where(job.c.id == queued)).mappings().one()
    assert row["payload"]["trigger"] == "event"
    write = {
        "entry_type": "note",
        "task": "checkin",
        "summary": "Je check-in is goed; de duurloop blijft staan.",
        "idempotency_key": runner.entry_key("checkin", DAY, item["run_key"]),
        "for_date": DAY.isoformat(),
        "safety": SAFE,
    }
    model = FakeModel(tool_use("vibesynergy_coach_write", write), done())
    result = schedule.handle(rt, replace_payload(row), client=model)
    assert result.status == "done", result.error
    first = model.requests[0]["messages"][0]["content"]
    assert "nieuwe versie van het huidige advies" in first and "de check-in" in first
    assert runner.last_runs(rt)["checkin"]["status"] == "done"  # it really runs
    with rt.engine.connect() as conn:
        entry = (
            conn.execute(
                select(coach_decision_log).where(coach_decision_log.c.id == result.entry_id)
            )
            .mappings()
            .one()
        )
    harness = entry["meta"]["harness"]
    assert harness["target"] == DAY.isoformat() and harness["trigger"] == "event"
    assert harness["changes"] == ["checkin"]
    assert updates(rt, at(8, 40)) == []  # that run saw the check-in


def replace_payload(row):
    class Job:
        payload = row["payload"]

    return Job()


def test_pain_starts_the_injury_task_at_once(rt):
    """The injury check never ran: only pain of 4 or more counted. Now any pain or injury
    for today starts it, without waiting."""
    base_run(rt)
    journal.log_injury(
        rt.engine, today=DAY, body_part="calf", side="left", kind="pain", pain=2, timing="after"
    )
    (item,) = updates(rt, at(10, 0))
    assert item["job"] == "injury" and item["payload"]["changes"] == ["injury"]
    runner_settings.save(rt, {"tasks": {"injury": {"on": False}}})
    (item,) = updates(rt, at(10, 0))
    assert item["job"] == "morning"  # the task is off: the advice is still updated


def test_three_syncs_give_one_run(rt):
    base_run(rt)
    hrv(rt, 48.0)
    assert updates(rt, at(9, 0)) == []
    hrv(rt, 49.0)
    assert updates(rt, at(9, 3)) == []
    hrv(rt, 51.0)
    assert updates(rt, at(9, 6)) == []
    assert updates(rt, at(9, 10)) == []  # four minutes quiet
    (item,) = updates(rt, at(9, 11))
    assert item["job"] == "morning" and item["payload"]["changes"] == ["hrv"]


def test_data_that_keeps_changing_runs_after_the_longest_wait(rt):
    base_run(rt)
    for minute in range(0, 30, 3):
        hrv(rt, 40.0 + minute)
        assert updates(rt, at(9, minute)) == []
    hrv(rt, 80.0)
    assert len(updates(rt, at(9, 30))) == 1


def test_the_same_data_written_again_starts_nothing(rt):
    hrv(rt, 50.0)
    base_run(rt)
    hrv(rt, 50.0)  # a sync that rewrites the same night
    assert triggers.check(rt, runner_settings.load(rt), at(9, 0))["changes"] == []
    assert updates(rt, at(9, 30)) == []


def test_data_before_the_fixed_run_waits_for_it_except_a_note(rt):
    yesterday = DAY - timedelta(days=1)
    base_run(rt, yesterday, job_name="evening", started=at(21, 45, yesterday))
    hrv(rt, 44.0)
    assert updates(rt, at(5, 0)) == [] and updates(rt, at(6, 50)) == []
    check = triggers.check(rt, runner_settings.load(rt), at(6, 50))
    assert check["changes"] == ["hrv"] and check["held"] is True
    assert [r["job"] for r in schedule.due_runs(rt, at(7, 0))] == ["morning"]  # it reads it
    journal.add_note(rt.engine, text="Keelpijn en koorts.", start=DAY, tags=["sick"])
    (item,) = updates(rt, at(5, 1))
    assert item["job"] == "morning" and item["payload"]["changes"] == ["hrv", "note"]


def test_after_the_evening_run_new_data_updates_tomorrows_advice(rt):
    base_run(rt)
    base_run(rt, job_name="evening", started=at(21, 45))
    tomorrow = DAY + timedelta(days=1)
    assert triggers.target_for(rt, DAY) == tomorrow
    journal.add_note(rt.engine, text="Kuit voelt stijf.", start=DAY)
    (item,) = updates(rt, at(22, 30))
    assert item["job"] == "evening" and item["for_date"] == DAY.isoformat()
    assert item["payload"]["target"] == tomorrow.isoformat()


def test_the_daily_maximum_stops_updates(rt):
    runner_settings.save(rt, {"advice": {"max_runs": 1}})
    base_run(rt)
    base_run(rt, key="event-earlier", trigger="event", started=at(8, 0))
    hrv(rt, 30.0)
    assert updates(rt, at(9, 0)) == [] and updates(rt, at(9, 40)) == []
    check = triggers.check(rt, runner_settings.load(rt), at(9, 40))
    assert check["limit"] is True and check["changes"] == ["hrv"]
    runner_settings.save(rt, {"advice": {"max_runs": 0}})  # 0: updates off
    assert triggers.check(rt, runner_settings.load(rt), at(9, 40))["off"] is True
    assert updates(rt, at(9, 40)) == []


def test_a_run_already_queued_for_the_day_is_not_queued_twice(rt):
    base_run(rt)
    journal.log_injury(
        rt.engine, today=DAY, body_part="knee", side="left", kind="pain", pain=3, timing="rest"
    )
    assert len(schedule.enqueue_due(rt, at(10, 0))) == 1
    hrv(rt, 30.0)
    assert updates(rt, at(10, 40)) == []  # the queued run reads it


# --- versions, the target day, the mail -----------------------------------------------------------


def _advice_run(rt, key, safety, summary, *, sessions=None, trigger="event", day=DAY):
    extra = {"safety": safety, "summary": summary}
    if sessions is not None:
        extra["sessions"] = sessions
    before = daily_advice.previous_version(rt, day)
    if before is not None:  # a later version names the one it replaces (#115)
        extra["changed_because"] = {
            "previous": before["id"],
            "reason": "Nieuwe HRV binnen, het advies opnieuw bekeken.",
        }
    write = morning_write(day=day, **extra)
    write["idempotency_key"] = runner.entry_key("morning", day, key)
    model = FakeModel(tool_use("vibesynergy_coach_write", write), done())
    found = runner.run(
        rt, "morning", key, day, trigger=trigger, payload={"changes": ["hrv"]}, client=model
    )
    assert found.status == "done", found.error
    return found


def test_mail_only_on_a_real_change_and_the_versions_on_the_coach_page(rt, mail):
    keep = [{"title": "Duurloop", "decision": "keep", "note": ""}]
    first = _advice_run(
        rt, DAY.isoformat(), AMBER, "Rustig aan.", sessions=keep, trigger="schedule"
    )
    assert first.mailed and len(mail) == 1
    words = _advice_run(rt, "event-a", AMBER, "Doe het rustig aan vandaag.", sessions=keep)
    assert not words.mailed and len(mail) == 1  # wording alone: no mail
    assert runner.get_run(rt, "morning", "event-a")["harness"]["mail"] == "no_change"
    skip = [{"title": "Duurloop", "decision": "skip", "note": "rust"}]
    session = _advice_run(rt, "event-b", AMBER, "Sla de duurloop over.", sessions=skip)
    assert session.mailed and len(mail) == 2  # a session decision changed
    colour = _advice_run(rt, "event-c", SAFE, "Je bent hersteld.", sessions=skip)
    assert colour.mailed and len(mail) == 3

    found = daily_advice.current(rt, DAY)
    assert found["source"] == "ai" and found["level"] == "green"
    assert found["entry"]["changes"] == ["hrv"] and found["entry"]["trigger"] == "event"
    assert found["entry"]["differences"] == [
        {"field": "colour", "from": "amber", "to": "green"},
        {"field": "safety_level", "from": "ease", "to": "ok"},
    ]
    history = found["versions"]
    assert [v["advice"] for v in history] == [
        "Je bent hersteld.",
        "Sla de duurloop over.",
        "Doe het rustig aan vandaag.",
        "Rustig aan.",
    ]
    assert history[1]["differences"] == [
        {
            "field": "sessions",
            "sessions": ["Duurloop"],
            "changes": [{"title": "Duurloop", "from": "keep", "to": "skip"}],
        }
    ]
    assert history[2]["differences"] == [] and history[3]["first"] is True
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    api = client.get("/api/advice", params={"date": DAY.isoformat()}).json()["data"]
    assert len(api["versions"]) == 4


def test_the_evening_advice_is_for_tomorrow(rt, mail):
    """Last night's evening advice is today's AI advice until the fixed run (#90)."""
    tomorrow = DAY + timedelta(days=1)
    write = {
        "entry_type": "evening",
        "summary": "Morgen rustig: herstel na vandaag.",
        "idempotency_key": runner.entry_key("evening", DAY),
        "for_date": tomorrow.isoformat(),  # wrong: the run writes on its own day
        "safety": AMBER,
    }
    fixed = {**write, "for_date": DAY.isoformat()}
    model = FakeModel(
        tool_use("vibesynergy_coach_write", write),
        tool_use("vibesynergy_coach_write", fixed, n=2),
        done(),
    )
    found = runner.run(rt, "evening", DAY.isoformat(), DAY, client=model)
    assert found.status == "done", found.error
    refused = model.requests[1]["messages"][-1]["content"][0]
    assert refused.get("is_error") and "for_date must be" in refused["content"]
    start = model.requests[0]["messages"][0]["content"]
    assert "geldt voor" in start and tomorrow.isoformat() in start
    assert daily_advice.current(rt, tomorrow)["source"] == "ai"
    assert daily_advice.current(rt, tomorrow)["entry"]["for_date"] == DAY.isoformat()
    assert daily_advice.current(rt, DAY)["source"] == "rules"
    assert len(mail) == 1  # the first advice for tomorrow


def _today_rt(rt):
    """A valid AI advice for today, made by today's fixed run, to read with the real clock."""
    day = today_in(rt.settings.timezone)
    with rt.engine.begin() as conn:
        conn.execute(
            insert(daily_unified),
            [
                {
                    "subject_id": 1,
                    "date": day - timedelta(days=back),
                    "metric": "hrv.night_avg",
                    "value": 50.0 + back % 5 - 2,
                    "unit": "ms",
                    "source": "garmin",
                }
                for back in range(1, 30)
            ],
        )
    runner_settings.save(rt, {"schedule": {"evening": {"on": False}}})
    base_run(rt, day, started=utc_now() - timedelta(hours=1))
    with rt.engine.begin() as conn:
        conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now() - timedelta(minutes=55),
                kind="coach",
                author="client:coach-runner",
                entry_type="morning",
                for_date=day,
                text="Rustig aan vandaag.",
                meta={"safety": AMBER, "harness": {"via": "runner", "task": "morning"}},
            )
        )
    return day


def test_new_data_shows_the_advice_is_being_updated(rt):
    day = _today_rt(rt)
    assert daily_advice.current(rt, day)["updating"] is None
    journal.set_checkin(rt.engine, day, mood=2)
    found = daily_advice.current(rt, day)
    assert found["source"] == "ai" and found["updating"]["kind"] == "waiting"
    assert found["updating"]["changes"] == ["checkin"]


def test_at_the_daily_maximum_the_rules_give_the_advice_with_the_reason(rt):
    day = _today_rt(rt)
    runner_settings.save(rt, {"advice": {"max_runs": 1}})
    base_run(rt, day, key="event-x", trigger="event", started=utc_now() - timedelta(minutes=30))
    assert daily_advice.current(rt, day)["source"] == "ai"  # nothing new: the AI's stands
    journal.set_checkin(rt.engine, day, mood=2)
    found = daily_advice.current(rt, day)
    assert found["source"] == "rules" and found["fallback"]["reason"] == "limit"
    assert found["fallback"]["text"] == daily_advice.REASONS["limit"]


def test_a_changed_advice_is_a_push_and_a_portal_notice_not_a_mail(rt, monkeypatch) -> None:
    """Robert, 18-11-2026: no 07:00 mail; a notice in the app and on the phone, and a
    choice under Settings → Alerts (the kind "advice")."""
    from vibesynergy import webpush
    from vibesynergy.coach_runner import report

    mails, pushes = [], []
    monkeypatch.setattr(mailer, "configured", lambda settings: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **k: mails.append(a))
    monkeypatch.setattr(
        webpush, "send_all", lambda rt, t, b, url="/", tag=None: pushes.append((t, b)) or 1
    )
    monkeypatch.setattr(
        alerts, "channels", lambda settings, engine=None: {"email": True, "push": True}
    )
    text = "Robert, vandaag rustig: 30 minuten op praattempo.\n\nDetails."
    with rt.engine.begin() as conn:
        ident = conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="coach",
                author="client:coach-runner",
                entry_type="morning",
                for_date=DAY,
                text=text,
                meta={"safety": AMBER, "harness": {"via": "runner", "task": "morning"}},
            )
        ).inserted_primary_key[0]
    entry = {"id": ident, "text": text}

    used = report.send(rt, "morning", DAY, entry)
    assert sorted(used) == ["portal", "push"] and mails == []
    assert "30 minuten op praattempo" in pushes[0][1] and "Details" not in pushes[0][1]
    notices = [e for e in alerts.for_portal(rt.settings)["recent"] if e["kind"] == "advice"]
    # The notice links to exactly the stored version (#115).
    assert len(notices) == 1 and notices[0]["link"] == f"#/coach/report/{ident}"
    assert notices[0]["entry_id"] == ident

    # Sending the same advice again replaces its notice; a report sent again by hand mails.
    report.send(rt, "morning", DAY, entry)
    assert len([e for e in alerts.for_portal(rt.settings)["recent"] if e["kind"] == "advice"]) == 1
    assert report.send(rt, "morning", DAY, entry, by_mail=True) == ["email"] and len(mails) == 1

    # Email can be chosen for the kind in Settings → Alerts.
    routes = alerts.routes
    monkeypatch.setattr(alerts, "routes", lambda s: {**routes(s), "advice": ["email"]})
    assert report.send(rt, "morning", DAY, entry) == ["email"] and len(mails) == 2

    # Other reports (the weekly review) still go by mail.
    assert report.send(rt, "weekly", DAY, entry) == ["email"]


def test_two_versions_on_one_day_are_one_notice_and_one_push_tag(rt, monkeypatch) -> None:
    """#108: one notice per target day, the newest; a later version says so and carries a
    version marker; the push has a per-day tag so the phone replaces the earlier one."""
    from vibesynergy import webpush

    pushes = []
    monkeypatch.setattr(
        webpush, "send_all", lambda rt, t, b, url="/", tag=None: pushes.append((t, tag)) or 1
    )
    monkeypatch.setattr(
        alerts, "channels", lambda settings, engine=None: {"email": False, "push": True}
    )
    first = _advice_run(rt, DAY.isoformat(), AMBER, "Rustpols hoger.", trigger="schedule")
    second = _advice_run(rt, "event-1", SAFE, "Rustpols weer normaal.")
    assert first.mailed and second.mailed  # each version went out: push and notice

    notices = [e for e in alerts.for_portal(rt.settings)["recent"] if e["kind"] == "advice"]
    assert len(notices) == 1
    assert notices[0]["key"] == f"advice:{DAY.isoformat()}"
    assert notices[0]["version"] == str(second.entry_id)
    assert notices[0]["text"].startswith(("Bijgewerkt:", "Updated:"))
    assert [tag for _, tag in pushes] == [f"advice-{DAY.isoformat()}"] * 2
    assert not pushes[0][0].startswith(("Bijgewerkt:", "Updated:"))
    assert pushes[1][0].startswith(("Bijgewerkt:", "Updated:"))


def test_the_advice_kind_is_in_the_alert_settings() -> None:
    label, default = alerts.KINDS["advice"]
    assert default == ["portal", "push"] and "email" not in default
    assert "AI coach" in label


def test_an_activity_insight_does_not_use_up_the_daily_updates(rt):
    """19-11-2026: an insight run (started by the activity event) counted as an advice
    update, so the limit was reached one update too early."""
    runner_settings.save(rt, {"advice": {"max_runs": 1}})
    base_run(rt)
    base_run(rt, job_name="insight", key="evt_activity", trigger="event", started=at(8, 0))
    assert triggers.runs_today(rt, at(9, 0)) == 0
    assert triggers.limit_reached(rt, runner_settings.load(rt), at(9, 0)) is False
    base_run(rt, key="event-earlier", trigger="event", started=at(8, 30))
    assert triggers.runs_today(rt, at(9, 0)) == 1
