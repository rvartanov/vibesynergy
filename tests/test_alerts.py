"""The generic mailer and alerts (a mail when something new needs action)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar

import pytest

from vibesynergy import alerts, mailer
from vibesynergy.core.timeutil import utc_now
from vibesynergy.portal import prefs
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import history_fill
from vibesynergy.sync.scheduler import Scheduler


class FakeSMTP:
    sent: ClassVar[list] = []
    logins: ClassVar[list] = []

    def __init__(self, host, port, **kw):
        self.host, self.port, self.tls = host, port, False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kw):
        self.tls = True

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))

    def send_message(self, message):
        FakeSMTP.sent.append((self.host, self.port, self.tls, message))


class FakeIMAP(FakeSMTP):
    appended: ClassVar[list] = []

    def append(self, folder, flags, when, data):
        FakeIMAP.appended.append((folder, data))


@pytest.fixture
def mail_rt(runtime, monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins, FakeIMAP.appended = [], [], []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(mailer.imaplib, "IMAP4_SSL", FakeIMAP)
    settings = replace(
        runtime.settings,
        mail={
            "smtp_host": "smtp.example.com",
            "username": "me@example.com",
            "imap_host": "imap.example.com",
        },
        alerts={"email_to": "me@example.com"},
    )
    rt = Runtime.open(settings)
    mailer.save_password(settings, "app-pass")
    yield rt
    rt.close()


def test_mailer_sends_and_files_a_copy(mail_rt) -> None:
    mailer.send(mail_rt.settings, "me@example.com", "Hello", "Body")
    host, port, tls, message = FakeSMTP.sent[0]
    assert (host, port, tls) == ("smtp.example.com", 587, True)
    assert message["To"] == "me@example.com" and message["Subject"] == "Hello"
    assert FakeSMTP.logins[0] == ("me@example.com", "app-pass")
    assert FakeIMAP.appended[0][0] == "Sent"
    ssl_settings = replace(
        mail_rt.settings, mail={**mail_rt.settings.mail, "smtp_security": "ssl", "imap_host": None}
    )
    mailer.send(ssl_settings, "me@example.com", "Hi", "B")
    assert FakeSMTP.sent[-1][1:3] == (465, False)


def test_mailer_needs_setup(runtime) -> None:
    with pytest.raises(mailer.MailError, match="not set up"):
        mailer.send(runtime.settings, "x@example.com", "s", "b")


def test_alerts_send_once_and_resolve(mail_rt, monkeypatch) -> None:
    sent = []
    current = [alerts.Alert("system:Garmin", "warn", "Garmin: login needed.", kind="login")]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    record = lambda subject, body, channel: sent.append((subject, body))  # noqa: E731
    assert alerts.check(mail_rt, send=record)["new"] == ["system:Garmin"]
    assert alerts.check(mail_rt, send=record)["new"] == []  # not repeated
    assert len(sent) == 1 and "login needed" in sent[0][1]
    current.clear()
    assert alerts.check(mail_rt, send=record)["resolved"] == ["system:Garmin"]
    assert sent[-1][0] == "VibeSynergy: resolved"
    assert "Resolved: Garmin: login needed." in sent[-1][1]
    assert alerts.channels(mail_rt.settings) == {"email": True}


def test_interruption_after_external_send_is_ambiguous_and_not_replayed(mail_rt, monkeypatch):
    from vibesynergy.core.secrets import SecretStore

    current = [alerts.Alert("system:Garmin", "warn", "Sign in again.")]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    sent = []

    def interrupted(subject, body, channel):
        sent.append((subject, body, channel))
        raise RuntimeError("process interrupted after provider accepted the send")

    with pytest.raises(RuntimeError, match="interrupted"):
        alerts.check(mail_rt, send=interrupted)
    state = SecretStore(mail_rt.settings.secrets_dir, alerts.SECRETS).get(alerts.STATE)
    ident = next(iter(state[alerts.DELIVERIES]))
    assert ident.startswith("email:new:system:Garmin:")
    assert state[alerts.DELIVERIES][ident]["status"] == "attempting"
    result = alerts.check(mail_rt, send=interrupted)
    assert len(sent) == 1
    assert result["ambiguous"] == [ident]
    state = SecretStore(mail_rt.settings.secrets_dir, alerts.SECRETS).get(alerts.STATE)
    assert state[alerts.DELIVERIES][ident]["status"] == "ambiguous"
    assert alerts.for_portal(mail_rt.settings)["ambiguous"] == [
        {
            "id": ident,
            "at": state[alerts.DELIVERIES][ident]["at"],
            "status": "ambiguous",
        }
    ]


def test_interrupted_email_preserves_event_and_is_not_replayed(mail_rt, monkeypatch):
    from vibesynergy.core.secrets import SecretStore

    fixed = datetime(2026, 10, 3, 12, tzinfo=UTC)
    monkeypatch.setattr(alerts, "utc_now", lambda: fixed)
    monkeypatch.setattr(alerts, "channels", lambda settings, engine=None: {"email": True})
    monkeypatch.setattr(
        alerts,
        "collect",
        lambda rt, today, since=None: (
            [alerts.Alert("job:7", "warn", "Check the source.", event=True, kind="job")]
            if since is None
            else []
        ),
    )
    calls = []

    def interrupted(subject, body, channel):
        calls.append(channel)
        if channel == "email":
            raise RuntimeError("provider accepted email before interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        alerts.check(mail_rt, send=interrupted)
    store = SecretStore(mail_rt.settings.secrets_dir, alerts.SECRETS)
    before = store.get(alerts.STATE)
    assert [event["key"] for event in alerts.for_portal(mail_rt.settings)["recent"]] == ["job:7"]
    assert [v["status"] for v in before[alerts.DELIVERIES].values()] == ["attempting"]
    assert alerts.check(mail_rt, send=interrupted)["new"] == []
    after = store.get(alerts.STATE)
    assert calls == ["email"]
    assert [v["status"] for v in after[alerts.DELIVERIES].values()] == ["ambiguous"]
    assert [event["key"] for event in alerts.for_portal(mail_rt.settings)["recent"]] == ["job:7"]


def test_ambiguous_verdict_does_not_suppress_later_episode(mail_rt, monkeypatch):
    from vibesynergy.core.secrets import SecretStore

    fixed = datetime(2026, 10, 3, 12, tzinfo=UTC)
    monkeypatch.setattr(alerts, "utc_now", lambda: fixed)
    current = [alerts.Alert("verdict:red", "warn", "Take it easy today.")]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    calls = []

    def interrupted(subject, body, channel):
        calls.append(subject)
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        alerts.check(mail_rt, send=interrupted)
    current.clear()
    quiet = alerts.check(mail_rt, send=interrupted)  # a red day ending is not a mail
    assert quiet["resolved"] == ["verdict:red"]
    assert calls == ["VibeSynergy: Take it easy today."]
    current.append(alerts.Alert("verdict:red", "warn", "Take it easy today."))
    sent = []
    assert alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(subject))[
        "new"
    ] == ["verdict:red"]
    assert len(sent) == 1 and sent[0].startswith("VibeSynergy: Take it easy")
    records = SecretStore(mail_rt.settings.secrets_dir, alerts.SECRETS).get(alerts.STATE)[
        alerts.DELIVERIES
    ]
    new_ids = [key for key in records if key.startswith("email:new:verdict:red:")]
    assert len(new_ids) == 2 and new_ids[0] != new_ids[1]
    assert [records[key]["status"] for key in new_ids] == ["ambiguous", "delivered"]


def test_scheduler_queues_alert_checks(mail_rt) -> None:
    clock = [0.0]
    scheduler = Scheduler(mail_rt.settings, mail_rt.registry, mail_rt.jobs, clock=lambda: clock[0])
    assert "alerts" in scheduler.run_once()
    assert "alerts" not in scheduler.run_once()  # not again within 15 minutes


def test_old_whatsapp_routes_and_config_are_ignored(mail_rt, caplog) -> None:
    """WhatsApp was removed: a stored route or an old config key changes nothing."""
    from vibesynergy.core import config

    (mail_rt.settings.data_dir / prefs.FILE).write_text(
        '{"alert_routes": {"injury": ["portal", "whatsapp"]}}'
    )
    assert alerts.routes(mail_rt.settings)["injury"] == ["portal"]
    saved = prefs.save(mail_rt.settings.data_dir, {"alert_routes": {"job": ["email", "whatsapp"]}})
    assert saved["alert_routes"] == {"job": ["email"]}
    path = mail_rt.settings.config_path.with_name("old.toml")
    path.write_text(
        '[alerts]\nenabled = true\nemail_to = "a@example.com"\nwhatsapp_phone = "+31600000000"\n'
    )
    config._RETIRED_WARNED.discard("[alerts] whatsapp_phone")
    with caplog.at_level("WARNING", logger="vibesynergy.core.config"):
        first = config.load_settings(path)
        config.load_settings(path)
    assert first.alerts == {"enabled": True, "email_to": "a@example.com"}
    warnings = [r.getMessage() for r in caplog.records if "whatsapp_phone" in r.getMessage()]
    assert warnings == ["[alerts] whatsapp_phone is ignored; nothing uses it any more"]
    assert set(alerts.channels(first)) == {"email"}
    # Saving the Alerts section clears the old key, so the warning stops.
    from vibesynergy.core import config_edit

    config_edit.apply_changes(path, {"alerts": {"email_to": "b@example.com"}})
    assert "whatsapp" not in path.read_text() and 'email_to = "b@example.com"' in path.read_text()


def test_failed_jobs_alert_once_and_only_new_ones(mail_rt) -> None:
    sent = []
    record = lambda subject, body, channel: sent.append(body)  # noqa: E731
    old = mail_rt.jobs.enqueue("sync", {"source": "garmin"}, max_attempts=1)
    mail_rt.jobs.fail(mail_rt.jobs.claim("w").id, "JobError: boom")
    alerts.check(mail_rt, send=record)  # first check: old failures don't alert
    assert not any("boom" in body for body in sent) and old
    limited = mail_rt.jobs.enqueue("sync", {"source": "strava"}, max_attempts=1)
    mail_rt.jobs.fail(mail_rt.jobs.claim("w").id, "JobError: StravaRateLimited: 429")
    mail_rt.jobs.enqueue("sync", {"source": "demo"}, max_attempts=1)
    mail_rt.jobs.fail(mail_rt.jobs.claim("w").id, "JobError: kaput")
    out = alerts.check(mail_rt, send=record)
    assert out["new"] and out["new"][0].startswith("job-source:demo:") and limited
    assert "kaput" not in sent[-1]
    assert "Your data couldn't be updated" in sent[-1]
    out = alerts.check(mail_rt, send=record)
    assert out["new"] == [] and out["resolved"] == []  # a one-off: no repeat, no "resolved"


def test_legacy_icloud_job_alert_is_hidden_and_unconfigured_job_stays_quiet(mail_rt) -> None:
    from vibesynergy.core.secrets import SecretStore

    store = SecretStore(mail_rt.settings.secrets_dir, alerts.SECRETS)
    store.set(
        alerts.STATE,
        {
            alerts.RECENT: [
                {
                    "key": "job:old",
                    "kind": "job",
                    "text": "A sync job for icloud failed: ValueError: old",
                    "at": utc_now().isoformat(),
                },
                {
                    "key": "job:other",
                    "kind": "job",
                    "text": "JobError: old",
                    "at": utc_now().isoformat(),
                },
            ]
        },
    )
    shown = alerts.for_portal(mail_rt.settings)["recent"]
    assert [a["key"] for a in shown] == ["job:other"]
    assert "JobError" not in shown[0]["text"] and "JobError" in shown[0]["detail"]
    alerts.check(mail_rt, send=lambda *a: None)
    mail_rt.jobs.enqueue("sync", {"source": "icloud"}, max_attempts=1)
    mail_rt.jobs.fail(mail_rt.jobs.claim("w").id, "ValueError: old")
    assert alerts.check(mail_rt, send=lambda *a: None)["new"] == []


def test_each_kind_goes_where_it_was_routed(mail_rt, monkeypatch) -> None:
    sent = []
    current = [
        alerts.Alert("injury:Knee", "stop", "Knee: pain changes how you move.", kind="injury"),
        alerts.Alert("system:Garmin", "warn", "Garmin: login needed.", kind="login"),
        alerts.Alert("system:Strava", "warn", "Strava hasn't synced for 30 hours.", kind="silent"),
    ]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    record = lambda subject, body, channel: sent.append((channel, body))  # noqa: E731
    prefs.save(mail_rt.settings.data_dir, {"alert_routes": {"silent": ["portal"]}})
    assert alerts.routes(mail_rt.settings)["silent"] == ["portal"]
    assert alerts.routes(mail_rt.settings)["injury"] == ["portal", "email", "push"]
    out = alerts.check(mail_rt, send=record)
    assert set(out["new"]) == {"injury:Knee", "system:Garmin", "system:Strava"}
    assert len(sent) == 1 and "login needed" in sent[0][1]
    assert "Knee" not in sent[0][1] and "Strava" not in sent[0][1]
    shown = alerts.for_portal(mail_rt.settings)
    assert {a["key"] for a in shown["open"]} == {"injury:Knee", "system:Garmin", "system:Strava"}
    # Off the portal too: gone from its list, still open underneath.
    prefs.save(mail_rt.settings.data_dir, {"alert_routes": {"silent": []}})
    assert {a["key"] for a in alerts.for_portal(mail_rt.settings)["open"]} == {
        "injury:Knee",
        "system:Garmin",
    }
    current.pop(0)  # the knee is fine again: it was logged, so there was no mail to follow
    alerts.check(mail_rt, send=record)
    assert len(sent) == 1 and "Resolved" not in sent[-1][1]


def test_history_runs_and_imports_are_announced_once(mail_rt) -> None:
    sent = []
    record = lambda subject, body, channel: sent.append(body)  # noqa: E731
    alerts.check(mail_rt, send=record)  # the first check sets the starting point
    now = utc_now()
    row = {"partial": [], "retry": [], "retried": False, "failures": 0, "done": 3,
           "busy_s": 0.0, "created_at": now, "updated_at": now, "message": None,
           "finished_at": None}  # fmt: skip
    with mail_rt.engine.begin() as conn:
        stuck = {"status": "stuck", "message": "stuck at 2019-03"}
        done = {"status": "done", "finished_at": now}
        conn.execute(
            history_fill.insert(),
            [
                {**row, "source": "garmin", "method": "api", **stuck},
                {**row, "source": "strava", "method": "archive", **done},
            ],
        )
    out = alerts.check(mail_rt, send=record)
    assert set(out["new"]) == {"history:1:stuck", "history:2:done"}
    assert "Strava export import finished" in sent[-1] and "stuck at 2019-03" in sent[-1]
    recent = alerts.for_portal(mail_rt.settings)["recent"]
    assert [e["key"] for e in recent] == ["history:2:done"]
    assert alerts.check(mail_rt, send=record)["new"] == []  # once


CALF = (
    "Right calf: De pijn verandert hoe je beweegt: stop met training die het pijn doet "
    "en ga naar een fysiotherapeut of arts."
)
RED = (
    "Rust of houd het heel rustig: een blessure verandert hoe je beweegt, "
    "Een open klacht betekent dat zwaar werk vandaag rustig blijft.. "
    "Vervang Herstel van vandaag door rust of een heel rustige sessie. "
    "Grutte Pier Ultra - Gaasterland trail 50k over 6 dagen: bewaar je frisheid, niets nieuws."
)


def test_a_logged_injury_is_never_mailed(mail_rt, monkeypatch) -> None:
    clock = {"at": datetime(2026, 10, 18, 8, tzinfo=UTC)}
    monkeypatch.setattr(alerts, "utc_now", lambda: clock["at"])
    current = [
        alerts.Alert("injury:Right calf", "warn", "Right calf: Pain 4/10. Ease off.", kind="injury")
    ]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    sent = []
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    current[:] = [alerts.Alert("injury:Right calf", "stop", CALF, kind="injury")]
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    current.append(
        alerts.Alert("injury:Left knee", "stop", "Left knee: stop what hurts.", kind="injury")
    )
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    clock["at"] += timedelta(hours=25)
    current[:] = [alerts.Alert("injury:Right calf", "stop", CALF, kind="injury")]
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert sent == []
    assert [item["key"] for item in alerts.for_portal(mail_rt.settings)["open"]] == [
        "injury:Right calf"
    ]


def test_ending_a_self_reported_red_day_does_not_mail(mail_rt, monkeypatch) -> None:
    sent = []
    current = [
        alerts.Alert("injury:Right calf", "stop", CALF, kind="injury"),
        alerts.Alert("verdict:red", "warn", RED, kind="health", external=False),
    ]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    first = alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert set(first["new"]) == {"injury:Right calf", "verdict:red"}
    current.clear()
    done = alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert set(done["resolved"]) == {"injury:Right calf", "verdict:red"}
    assert sent == []


def test_same_reading_is_not_mailed_again_the_same_day(mail_rt, monkeypatch) -> None:
    clock = {"at": datetime(2026, 10, 18, 8, tzinfo=UTC)}
    monkeypatch.setattr(alerts, "utc_now", lambda: clock["at"])
    text = "HRV below your usual range 3 days running."
    key = "recovery:HRV below your usual range # days running."

    def reading() -> alerts.Alert:
        return alerts.Alert(key, "warn", text)

    current = [reading()]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    sent = []
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    current.clear()
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    current.append(reading())
    again = alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert again["new"] == [key]
    assert len(sent) == 1
    clock["at"] += timedelta(hours=25)
    current.clear()
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    current.append(reading())
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert len(sent) == 2


def test_a_stricter_reading_is_mailed(mail_rt, monkeypatch) -> None:
    sent = []
    current = [alerts.Alert("recovery:hrv", "warn", "HRV is low. Take it easy.", kind="health")]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    current[:] = [alerts.Alert("recovery:hrv", "stop", "HRV is low. Rest today.", kind="health")]
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert len(sent) == 2 and "STOP:" in sent[-1] and "Rest today" in sent[-1]


def test_a_changing_hour_count_is_the_same_alert(mail_rt, monkeypatch) -> None:
    from vibesynergy.core.secrets import SecretStore

    store = SecretStore(mail_rt.settings.secrets_dir, alerts.SECRETS)
    store.set(
        alerts.STATE,
        {
            "system:Garmin hasn't synced for 30 hours.": {
                "since": utc_now().isoformat(),
                "text": "Garmin hasn't synced for 30 hours.",
                "episode": 1,
                "kind": "silent",
                "level": "warn",
                "link": "#/settings/data/sync",
            }
        },
    )
    current = [
        alerts.Alert(
            "system:Garmin hasn't synced for # hours.",
            "warn",
            "Garmin hasn't synced for 31 hours.",
            kind="silent",
        )
    ]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    sent = []
    out = alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert out["new"] == [] and out["resolved"] == [] and sent == []
    assert alerts._alert_key("recovery", "HRV below your usual range 4 days running.") == (
        "recovery:HRV below your usual range # days running."
    )


def test_a_failed_reading_does_not_resolve_open_alerts(mail_rt, monkeypatch) -> None:
    current = [alerts.Alert("injury:Right calf", "stop", CALF, kind="injury")]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    sent = []
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    held = alerts.Collected([], hold=frozenset({"attention", "verdict"}))
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: held)
    out = alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert out["resolved"] == [] and out["open"] == ["injury:Right calf"]
    assert sent == []
    state = alerts.for_portal(mail_rt.settings)["open"]
    assert [item["key"] for item in state] == ["injury:Right calf"]


def test_login_recovery_is_still_mailed_in_dutch(mail_rt, monkeypatch) -> None:
    prefs.save(mail_rt.settings.data_dir, {"language": "nl"})
    current = [
        alerts.Alert("system:Garmin", "warn", "Garmin: opnieuw inloggen nodig.", kind="login")
    ]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    sent = []
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append((subject, body)))
    current.clear()
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append((subject, body)))
    assert sent[-1][0] == "VibeSynergy: opgelost"
    assert "Opgelost: Garmin: opnieuw inloggen nodig." in sent[-1][1]


def test_a_red_day_from_a_logged_complaint_is_not_mailed(mail_rt, monkeypatch) -> None:
    def board(*args, **kwargs):
        return {
            "attention": [
                {"level": "stop", "area": "injury", "message": CALF, "link": "#/today"},
                {
                    "level": "warn",
                    "area": "injury",
                    "message": "Right calf: Pain 5/10 on 18-10-2026 (above 3): ease off.",
                    "link": "#/today",
                },
            ],
            "verdict": {
                "level": "red",
                "rules": ["affects_movement", "injury_warning"],
                "headline": "Rest or keep it very easy",
                "reasons": [
                    "an injury changes how you move",
                    "An open complaint means keeping hard work easy.",
                ],
                "advice": (
                    "Swap today's Herstel for rest or a very easy session. "
                    "Grutte Pier Ultra in 6 days: protect freshness, nothing new."
                ),
                "text": RED,
            },
        }

    monkeypatch.setattr(alerts.dashboard, "dashboard", board)
    found = alerts.collect(mail_rt, date(2026, 10, 18))
    by_key = {item.key: item for item in found}
    assert by_key["injury:Right calf"].external is False
    assert by_key["verdict:red"].external is False
    assert ".." not in by_key["verdict:red"].text
    assert "open complaint" not in by_key["verdict:red"].text.lower()
    sent = []
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: found)
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert sent == []


def test_a_red_day_from_a_reading_is_mailed(mail_rt, monkeypatch) -> None:
    def board(*args, **kwargs):
        return {
            "attention": [{"level": "stop", "area": "injury", "message": CALF, "link": "#/today"}],
            "verdict": {
                "level": "red",
                "rules": ["affects_movement", "hrv_rhr"],
                "signals": [
                    {"rule": "affects_movement", "text": "an injury changes how you move"},
                    {"rule": "hrv_rhr", "text": "HRV low 3 days and resting HR up"},
                ],
                "headline": "Rest or keep it very easy",
                "advice": "Swap today's Herstel for rest or a very easy session.",
                "text": RED,
            },
        }

    monkeypatch.setattr(alerts.dashboard, "dashboard", board)
    found = alerts.collect(mail_rt, date(2026, 10, 18))
    by_key = {item.key: item for item in found}
    assert by_key["verdict:red"].external is True
    assert "HRV low 3 days" in by_key["verdict:red"].text
    assert "injury changes" not in by_key["verdict:red"].text.lower()
    assert ".." not in by_key["verdict:red"].text
    sent = []
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: found)
    alerts.check(mail_rt, send=lambda subject, body, channel: sent.append(body))
    assert len(sent) == 1 and "HRV low 3 days" in sent[0] and CALF not in sent[0]


def test_a_red_day_from_being_sick_is_not_mailed(mail_rt, monkeypatch) -> None:
    def board(*args, **kwargs):
        return {
            "attention": [],
            "verdict": {
                "level": "red",
                "rules": ["sick", "red_flag_note"],
                "headline": "Rest or keep it very easy",
                "reasons": ["you logged being sick", "What you reported needs rest today."],
                "advice": "Swap today's VO2 max for rest or a very easy session.",
                "text": "Rest or keep it very easy: you logged being sick.. Swap.",
            },
        }

    monkeypatch.setattr(alerts.dashboard, "dashboard", board)
    found = {item.key: item for item in alerts.collect(mail_rt, date(2026, 10, 18))}
    assert found["verdict:red"].external is False
    assert found["verdict:red"].text == "Swap today's VO2 max for rest or a very easy session."
    assert "sick" not in found["verdict:red"].text.lower()


def test_settings_lists_only_open_alerts(mail_rt, monkeypatch) -> None:
    """Settings -> Alerts "Open now": the bookkeeping in the state is not an alert."""
    current = [alerts.Alert("system:Garmin", "warn", "Garmin: login needed.", kind="login")]
    monkeypatch.setattr(alerts, "collect", lambda rt, today, since=None: list(current))
    alerts.check(mail_rt, send=lambda *a: None)
    opened = alerts.open_alerts(mail_rt.settings)
    assert [a["key"] for a in opened] == ["system:Garmin"]
    assert opened[0]["text"] == "Garmin: login needed."


def test_coach_mail_lists_the_verdict_per_session() -> None:
    from vibesynergy import alerts

    lines = alerts._session_lines(
        [
            {"title": "Duurloop Z2 40'", "decision": "keep"},
            {"title": "Kracht MT-A", "decision": "adjust", "note": "RDL 50 kg"},
            {"title": "x", "decision": "maybe"},
        ],
        lambda text: {"Keep": "Houden", "Adjust": "Aanpassen"}.get(text, text),
    )
    assert lines == ["✓ Houden: Duurloop Z2 40'", "~ Aanpassen: Kracht MT-A (RDL 50 kg)"]
