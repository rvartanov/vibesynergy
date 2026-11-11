"""The coach and alert mail as HTML."""

from __future__ import annotations

from vibesynergy import alerts, mail_html
from vibesynergy.i18n import translator


def test_coach_mail_has_sections_chips_and_escapes() -> None:
    record = {
        "kind": "daily_coach",
        "rich": {
            "text": "Rust houden <vandaag>.",
            "sessions": [{"title": "Rustdag", "decision": "keep", "note": "Geen training."}],
            "details": "Besluit: rustdag houden.\nMorgen (zo 8-11): lange rit.\nlosse regel",
        },
    }
    other = {"kind": "silent", "level": "warn", "text": "Withings synct niet."}
    page = mail_html.render(
        [record, other], translator("nl"), title="Je coach", link="https://x/#/coach"
    )
    assert "Rust houden &lt;vandaag&gt;." in page and "<vandaag>" not in page
    assert "✓ Houden" in page and ">Vandaag<" in page and ">Details<" in page
    assert ">Besluit<" in page and ">Morgen (zo 8-11)<" in page and "losse regel" in page
    assert "▲ Let op" in page and "Withings synct niet." in page
    assert 'href="https://x/#/coach"' in page and "Openen in VibeSynergy" in page


def test_alert_mail_goes_out_with_an_html_part(runtime, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(alerts, "channels", lambda settings, engine=None: {"email": True})
    monkeypatch.setattr(
        alerts,
        "collect",
        lambda rt, today, since=None: [
            alerts.Alert("system:Garmin", "warn", "Garmin: login needed.", kind="login")
        ],
    )

    def send(subject, body, channel, html=None):
        sent.append((body, html))

    alerts.check(runtime, send=send)
    body, page = sent[0]
    assert "Garmin: login needed." in body and page and "Garmin: login needed." in page


def test_a_runner_note_is_not_mailed_twice(runtime) -> None:
    # the API runner mails its own report; the morning alert leaves that note out.
    from datetime import UTC, datetime, timedelta

    from vibesynergy.store.schema import coach_decision_log, coach_run

    now = datetime.now(UTC)
    with runtime.engine.begin() as conn:
        entry = conn.execute(
            coach_decision_log.insert().values(
                subject_id=1,
                at=now,
                kind="coach",
                entry_type="morning",
                text="Rust houden.",
                meta={},
                author="coach",
            )
        ).inserted_primary_key[0]
    assert len(alerts._daily(runtime, now - timedelta(hours=1))) == 1
    with runtime.engine.begin() as conn:
        conn.execute(
            coach_run.insert().values(
                job="morning",
                run_key="k",
                for_date=now.date(),
                status="done",
                entry_id=entry,
                trigger="schedule",
                attempts=1,
                model="claude-opus-5-5",
                started_at=now,
                updated_at=now,
                turns=1,
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.0,
            )
        )
    assert alerts._daily(runtime, now - timedelta(hours=1)) == []


def test_report_lines_stay_lines_with_bold_headings() -> None:
    """8-11: the report's lines ran together in one paragraph."""
    from vibesynergy import i18n

    text = "Alex, morgen zwemtechniek 40 minuten.\nHerstel: goed.\nLet op: niets bijzonders."
    record = {"kind": "daily_coach", "rich": {"text": text, "sessions": []}}
    page = mail_html.render([record], i18n.translator("nl"), title="Coach avond", link=None)
    assert "Alex, morgen zwemtechniek 40 minuten.</p>" in page
    assert "<b>Herstel:</b> goed.</p>" in page and "<b>Let op:</b> niets bijzonders.</p>" in page
