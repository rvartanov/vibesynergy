"""#147: one day of advice through the whole chain, every surface in the same words.

Robert, 21-11-2026: "De AI-meldingen en adviezen zijn nog altijd onduidelijk en niet
eenduidig." The day of 21-11 in short: the evening preview for Saturday, the morning update,
the check-in, the ride done ("verder niets meer vandaag") and the evening preview for
Sunday. Every surface (banner, push, mail, Meldingen, start page, Coach feed, report page,
MCP) reads the advice from one view (``advice_view``) and agrees on the day, the headline,
the colour state and the why-line; the after-ride version is quiet.

All data is synthetic; mail and push are captured; the plan is the view's one input that is
replaced (``advice_view.planned_today``), so the test is about the chain, not the matching.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import insert
from test_coach_runner import KEY

from vibesynergy import alerts, i18n, mailer, webpush
from vibesynergy.coach_runner import report
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.core.user_datetime import format_user_short_day
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import advice_view, coach_consistency, daily_advice
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log

TAILNET = "server.example.ts.net"
RIDE = "Lange rit rustig Z2 1u15"
SUNDAY = "Lange duurloop zondaggroep 1u45"
AMBER = {
    "level": "flag",
    "colour": "amber",
    "reasons": ["Je hartslagvariabiliteit lag de hele week onder je band."],
}
EASE = {**AMBER, "level": "ease"}
GREEN = {"level": "ok", "colour": "green", "reasons": ["Nacht en check-in goed."]}
BECAUSE = "Nieuw is de gedane rit; kleur, veiligheid en de sessies blijven gelijk."
NL = i18n.translator("nl")


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
def sent(monkeypatch):
    out: dict[str, list] = {"mail": [], "push": []}
    monkeypatch.setattr(mailer, "configured", lambda settings: True)
    monkeypatch.setattr(
        mailer,
        "send",
        lambda settings, to, subject, body, html=None: out["mail"].append((subject, body, html)),
    )
    monkeypatch.setattr(
        webpush,
        "send_all",
        lambda rt, title, body, url="/", tag=None: out["push"].append((title, body, url)) or 1,
    )
    monkeypatch.setattr(
        alerts, "channels", lambda settings, engine=None: {"email": True, "push": True}
    )
    routes = alerts.routes
    monkeypatch.setattr(
        alerts,
        "routes",
        lambda settings: {**routes(settings), "advice": ["portal", "push", "email"]},
    )
    return out


@pytest.fixture
def plan(monkeypatch):
    """Today's plan: the ride, not done until ``plan["ride"] = "done"``."""
    state = {"ride": "today"}

    def planned(rt, day, today):
        if day != today:
            return None
        return [{"title": f"\N{BICYCLE} {RIDE}", "sport": "cycling", "status": state["ride"]}]

    monkeypatch.setattr(advice_view, "planned_today", planned)
    return state


def _write(rt, day, entry_type, task, text, safety, *, trigger, changes=(), sessions=(), prev=None):
    meta = {
        "safety": safety,
        "sessions": [{"title": t, "decision": d} for t, d in sessions],
        "harness": {
            "via": "runner",
            "task": task,
            "trigger": trigger,
            "changes": list(changes),
            "role": "primary",
            "version": 3,
        },
    }
    if prev is not None:
        meta["changed_because"] = {"previous": prev, "reason": BECAUSE}
    with rt.engine.begin() as conn:
        ident = conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="note",
                author="client:coach-runner",
                entry_type=entry_type,
                for_date=day,
                text=text,
                meta=meta,
            )
        ).inserted_primary_key[0]
    return ident, daily_advice.announce(rt, ident)


def _mcp(rt, tool, **args):
    async def go():
        tokens = set_connection("client-1", scopes=frozenset({"read"}), role="own")
        try:
            async with Client(build_server(rt)) as client:
                return await client.call_tool(tool, args)
        finally:
            reset_connection(tokens)

    return anyio.run(go).structured_content["data"]


def _pushed(sent, ident):
    return [p for p in sent["push"] if p[2] == f"/#/coach/report/{ident}"]


def test_one_day_of_advice_reads_the_same_on_every_surface(rt, sent, plan):
    today = today_in(rt.settings.timezone)
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    label = f"Advies voor vandaag \N{EN DASH} {format_user_short_day(today, 'nl')}"

    # 1. Friday evening: the preview for Saturday, the day's first advice. Pushed.
    preview, used = _write(
        rt,
        yesterday,
        "evening",
        "evening",
        "Robert, morgen de rustige rit van 1u15, strikt rustig.",
        AMBER,
        trigger="schedule",
        sessions=[(RIDE, "keep"), (SUNDAY, "adjust")],
    )
    assert "push" in used and "email" in used
    # 2. The morning update: the same colour and sessions. Quiet.
    morning, used = _write(
        rt,
        today,
        "morning",
        "morning",
        "Robert, vandaag de rustige rit van 1u15, strikt rustig.",
        AMBER,
        trigger="schedule",
        sessions=[(RIDE, "keep")],
        prev=preview,
    )
    assert used == [] and not _pushed(sent, morning)
    # 3. The check-in: green now. A real change: pushed and mailed.
    checkin, used = _write(
        rt,
        today,
        "note",
        "checkin",
        "Check-in goed: vandaag de rit zoals gepland, gelijkmatig.",
        GREEN,
        trigger="event",
        changes=["checkin"],
        sessions=[(RIDE, "keep"), (SUNDAY, "adjust")],
        prev=morning,
    )
    assert "push" in used and _pushed(sent, checkin)
    # 4. After the ride: it only reports the day. Quiet: no push, no mail.
    plan["ride"] = "done"
    pushes, mails = len(sent["push"]), len(sent["mail"])
    after, used = _write(
        rt,
        today,
        "morning",
        "morning",
        "Robert, de rit van vandaag is gedaan zoals bedoeld; verder niets meer vandaag.\n"
        "Vandaag: 1u18 rustig en gelijkmatig.",
        GREEN,
        trigger="event",
        changes=["activity"],
        sessions=[(RIDE, "done"), (SUNDAY, "adjust")],
        prev=checkin,
    )
    assert used == [] and len(sent["push"]) == pushes and len(sent["mail"]) == mails
    # 5. Saturday evening: the preview for Sunday. A new day's advice: pushed.
    sunday, used = _write(
        rt,
        today,
        "evening",
        "evening",
        "Robert, morgen de korte zondagloop, 1u15 rustig met de groep.",
        EASE,
        trigger="schedule",
        sessions=[(SUNDAY, "adjust")],
    )
    assert "push" in used

    # The one view of today's current advice: done for today, after the workout.
    view = advice_view.for_entry_id(rt, after, NL)
    assert view is not None
    assert view["label"] == label and view["state"] == "done"
    assert view["headline"] == view["verdict"] == "Gedaan voor vandaag"
    assert view["recovery"] == "Herstel: groen \N{EN DASH} Nacht en check-in goed."
    assert view["why"] == "na je training"
    assert view["stamp"] == f"Bijgewerkt {view['time']}, na je training"
    assert view["notice"].startswith(
        f"Advies voor vandaag bijgewerkt om {view['time']}, na je training: Robert, de rit"
    )
    assert (view["version"], view["of"], view["status"]) == (4, 4, "current")
    words = " ".join(str(v) for v in view.values()) + str(view["push"])
    for runner_word in ("ochtendrun", "run ", "Ochtendbeslissing", "Coach ochtend", "Versie"):
        assert runner_word not in words

    def same(found):
        assert found["label"] == view["label"]
        assert found["state"] == view["state"] and found["headline"] == view["headline"]
        assert found["stamp"] == view["stamp"] and found["why"] == view["why"]

    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    # Banner and Meldingen (both read /api/alerts): made from the entry when read.
    notices = {
        n["key"]: n
        for n in client.get("/api/alerts?lang=nl").json()["data"]["recent"]
        if n.get("kind") == "advice"
    }
    banner = notices[f"advice:{today.isoformat()}"]
    assert banner["entry_id"] == after and banner["link"] == f"#/coach/report/{after}"
    assert banner["text"] == view["notice"] and banner["state"] == "done"
    assert banner["version"] == str(checkin)  # quiet: a closed notice stays closed
    assert notices[f"advice:{tomorrow.isoformat()}"]["text"].startswith(
        f"Advies voor morgen \N{EN DASH} {format_user_short_day(tomorrow, 'nl')}: Robert"
    )
    # Push and mail: the check-in's, in the same words as its own view.
    checkin_view = advice_view.for_entry_id(rt, checkin, NL)
    assert checkin_view is not None
    ((title, body, _url),) = _pushed(sent, checkin)
    assert (
        title
        == checkin_view["heading"]
        == (f"Advies voor vandaag bijgewerkt \N{EN DASH} {format_user_short_day(today, 'nl')}")
    )
    assert body == checkin_view["push"]["body"]
    assert body.startswith(f"{checkin_view['stamp']}. Groen \N{EN DASH} Goed hersteld. Check-in")
    _subject, text, html = next(m for m in sent["mail"] if m[0] == title)
    assert text.startswith(f"{checkin_view['verdict']}\n{checkin_view['stamp']}\n\n")
    assert checkin_view["stamp"] in html
    # Start page: today's card and tomorrow's.
    board = client.get("/api/dashboard?lang=nl").json()["data"]
    same(board["advice"]["view"])
    assert board["advice"]["entry"]["id"] == after
    assert board["advice_tomorrow"]["view"]["label"].startswith("Advies voor morgen")
    assert board["advice_tomorrow"]["entry_id"] == sunday
    # Coach feed and report page.
    feed = {e["id"]: e for e in client.get("/api/coach/feed?lang=nl").json()["data"]["entries"]}
    same(feed[after]["advice_view"])
    assert feed[preview]["advice_view"]["stamp"].endswith(", avondvooruitblik")
    page = client.get(f"/api/coach/report/{after}?lang=nl").json()["data"]
    same(page["view"])
    assert [v["id"] for v in page["versions"]] == [after, checkin, morning, preview]
    assert page["versions"][1]["why"] == "na je check-in"
    # MCP, in English: the same entry, state and why.
    day = _mcp(rt, "vibesynergy_today")
    assert day["advice"]["view"]["entry_id"] == after
    assert day["advice"]["view"]["state"] == "done"
    assert day["advice"]["view"]["why"] == "after your workout"
    assert day["advice"]["view"]["headline"] == "Done for today"
    entries = _mcp(rt, "vibesynergy_coach_entries", period="last_7d")
    shown = {e["id"]: e["advice_view"] for e in entries["entries"] if e.get("advice_view")}
    assert shown[after]["state"] == "done" and shown[sunday]["label"].startswith(
        "Advice for tomorrow"
    )
    # And the thread is consistent.
    assert coach_consistency.check(rt, today)["findings"] == []


def test_the_check_flags_a_colour_that_does_not_fit_the_day(rt, sent, plan):
    """The ride is done by the plan, but the newest version still asks for a colour and no
    new version is on its way: the label and the day contradict each other."""
    today = today_in(rt.settings.timezone)
    ident, _ = _write(
        rt,
        today,
        "morning",
        "morning",
        "Robert, vandaag de rustige rit van 1u15.",
        AMBER,
        trigger="schedule",
        sessions=[(RIDE, "keep")],
    )
    assert coach_consistency.check(rt, today)["findings"] == []
    plan["ride"] = "done"
    view = advice_view.for_entry_id(rt, ident, NL)
    assert view is not None and view["state"] == "colour"  # it never says done by itself
    (found,) = coach_consistency.check(rt, today)["findings"]
    assert found["rule"] == "label_day_state" and f"#{ident}" in found["evidence"]
    page = coach_consistency.for_page(rt, NL)
    assert "past niet bij de dag" in page[0]["message"]


def test_a_version_that_only_reports_is_quiet():
    def entry(ident, colour, level, *sessions):
        return {
            "id": ident,
            "meta": {
                "safety": {"level": level, "colour": colour, "reasons": ["x"]},
                "sessions": [{"title": t, "decision": d} for t, d in sessions],
            },
        }

    first = entry(1, "amber", "flag", (RIDE, "keep"), (SUNDAY, "adjust"))
    assert advice_view.notable([], first) == ["new"]
    # Done, missed or no longer named: no news. Kept as before: no news.
    assert advice_view.notable([first], entry(2, "amber", "flag", (RIDE, "done"))) == []
    assert advice_view.notable([first], entry(2, "amber", "flag", (RIDE, "missed"))) == []
    assert advice_view.notable([first], entry(2, "amber", "flag", (SUNDAY, "adjust"))) == []
    # A milder level in the same colour: no news; a stricter one is.
    assert advice_view.notable([first], entry(2, "amber", "ease", (RIDE, "keep"))) == []
    milder = entry(1, "amber", "ease", (RIDE, "keep"))
    assert advice_view.notable([milder], entry(2, "amber", "flag", (RIDE, "keep"))) == [
        "safety_level"
    ]
    # Another colour, or a session dropped, adjusted or proposed: news.
    assert advice_view.notable([first], entry(2, "green", "ok")) == ["colour"]
    assert advice_view.notable([first], entry(2, "amber", "flag", (RIDE, "skip"))) == ["sessions"]
    assert advice_view.notable([first], entry(2, "amber", "flag", ("Zwemmen", "proposal"))) == [
        "sessions"
    ]
    # A session is held against the newest version that names it.
    later = entry(2, "amber", "flag", (RIDE, "keep"))
    assert advice_view.notable([later, first], entry(3, "amber", "flag", (SUNDAY, "adjust"))) == []


def test_the_day_is_done_only_when_the_version_says_so():
    planned = [{"title": "\N{BICYCLE} Lange rit", "sport": "cycling", "status": "done"}]

    def entry(*sessions):
        return {"meta": {"sessions": [{"title": t, "decision": d} for t, d in sessions]}}

    assert advice_view.day_done(entry(("Lange rit", "done")), planned)
    # Written before the ride: the plan has it done, the text still asks for it.
    assert not advice_view.day_done(entry(("Lange rit", "keep")), planned)
    assert not advice_view.day_done(entry(), planned)
    # A rest day or no plan is never "done for today".
    assert not advice_view.day_done(entry(("Lange rit", "done")), [])
    assert not advice_view.day_done(entry(("Lange rit", "done")), None)
    # A second session still to do keeps the colour.
    swim = {"title": "Zwemmen", "sport": "swimming", "status": "today"}
    assert not advice_view.day_done(entry(("Lange rit", "done")), [*planned, swim])
    assert advice_view.day_done(entry(("Lange rit", "done"), ("Zwemmen", "skip")), [*planned, swim])


def test_the_why_line_names_what_came_in(rt):
    def why(first=False, **harness):
        return advice_view.why_text(
            advice_view.why_keys({"meta": {"harness": harness}}, first=first), NL
        )

    assert why(changes=["activity"]) == "na je training"
    assert why(changes=["checkin"]) == "na je check-in"
    assert why(changes=["sleep", "hrv", "sleep_check"]) == "nacht binnen"
    assert why(changes=["sleep", "plan"]) == "nacht binnen en plan gewijzigd"
    assert why(trigger="manual") == "op je verzoek"
    assert why(trigger="schedule", task="morning") == "ochtendupdate"
    assert why(first=True, task="evening") == "avondvooruitblik"
    assert why(first=True, task="morning") == ""


def test_every_view_text_has_a_dutch_translation():
    import json
    from pathlib import Path

    nl = json.loads(
        (Path(__file__).parents[1] / "src/vibesynergy/portal/static/i18n/nl.json").read_text()
    )
    texts = [*advice_view.WHY.values(), *advice_view.SHORT_LABELS.values(), advice_view.DONE]
    texts += list(daily_advice.DAY_LABELS.values())
    assert not [t for t in texts if t not in nl]


def test_the_report_keeps_its_title_for_other_reports(rt):
    assert report.subject(rt.settings, "weekly", today_in(rt.settings.timezone)).startswith("Coach")
