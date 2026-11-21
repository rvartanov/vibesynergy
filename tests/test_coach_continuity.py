"""#115: one ongoing conversation per day, and every notice traceable to what the athlete sees.

The day thread is preloaded into every daily advice run (block ``day_thread``) and named in
the start message; a later version must say what changed against the current one
(``changed_because``) or the output contract refuses it. Every notice, push and mail is
read back from a stored entry and links to exactly that entry; a missing or replaced
version is not sent. MCP coaches write into the same thread and get it through
``vibesynergy_coach_prompt(include_context=true)``. The continuity check reports gaps.

The model is a scripted fake, mail and push are captured and all data is synthetic.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import insert, select
from test_coach_runner import KEY, SAFE, FakeModel, done, morning_write, tool_use

from vibesynergy import alerts, mailer, webpush
from vibesynergy.coach_runner import context, contract, report, runner
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_consistency, coach_replies, daily_advice
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import audit_log, coach_decision_log

TAILNET = "server.example.ts.net"
DAY = date(2026, 11, 2)  # a Monday
AMBER = {"level": "ease", "colour": "amber", "reasons": ["Rustpols derde dag hoger"]}
BECAUSE = "Rustpols is weer normaal en de check-in is goed, dus de duurloop mag door."


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
    """Every mail and push, captured; the kind advice also goes by mail here."""
    out: dict[str, list] = {"mail": [], "push": []}
    monkeypatch.setattr(mailer, "configured", lambda settings: True)
    monkeypatch.setattr(
        mailer,
        "send",
        lambda settings, to, subject, body, html=None: out["mail"].append((subject, body)),
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


def _run(rt, key, summary, safety=SAFE, *, because=None, sessions=None, script=None):
    extra: dict = {"safety": safety}
    if because is not None:
        extra["changed_because"] = because
    if sessions is not None:
        extra["sessions"] = sessions
    write = morning_write(summary, day=DAY, **extra)
    write["idempotency_key"] = runner.entry_key("morning", DAY, key)
    model = FakeModel(*(script or (tool_use("vibesynergy_coach_write", write), done())))
    trigger = "schedule" if key == DAY.isoformat() else "event"
    result = runner.run(
        rt, "morning", key, DAY, trigger=trigger, payload={"changes": ["hrv"]}, client=model
    )
    return result, model


def _notices(rt):
    return [e for e in alerts.for_portal(rt.settings)["recent"] if e.get("kind") == "advice"]


def _text(rt, entry_id):
    return report.stored(rt, entry_id)["text"]


# --- the contract --------------------------------------------------------------------------


PREVIOUS = {"id": 1018, "version": 1, "advice": "Rustpols derde dag op rij hoger: rustig aan."}


def test_a_first_version_needs_no_reason():
    assert contract.check_changed_because(None, None) is None
    assert contract.check_changed_because({"previous": 1, "reason": "x"}, None) is None


def test_a_later_version_without_changed_because_is_refused_with_the_id():
    with pytest.raises(contract.ContractError) as exc:
        contract.check_changed_because(None, PREVIOUS)
    assert "changed_because is required" in str(exc.value) and "#1018" in str(exc.value)


def test_citing_an_older_version_is_refused():
    """A run that read an old thread (or none) names the wrong version."""
    with pytest.raises(contract.ContractError, match="previous must be 1018"):
        contract.check_changed_because({"previous": 1000, "reason": BECAUSE}, PREVIOUS)


def test_a_reason_of_a_word_is_refused():
    with pytest.raises(contract.ContractError, match="reason needs a sentence"):
        contract.check_changed_because({"previous": 1018, "reason": "beter"}, PREVIOUS)


def test_a_valid_reason_is_cleaned():
    found = contract.check_changed_because(
        {"previous": "1018", "reason": f"  {BECAUSE} "}, PREVIOUS
    )
    assert found == {"previous": 1018, "reason": BECAUSE}


def test_check_write_holds_a_daily_advice_to_the_thread():
    args = {
        "entry_type": "morning",
        "summary": "Duurloop mag door.",
        "for_date": DAY,
        "today": DAY,
        "safety": SAFE,
        "floor": {"level": "ok", "rules": []},
        "previous": PREVIOUS,
    }
    with pytest.raises(contract.ContractError, match="changed_because is required"):
        contract.check_write(**args)
    ok = contract.check_write(**args, changed_because={"previous": 1018, "reason": BECAUSE})
    assert ok["changed_because"]["previous"] == 1018
    # A plain note is no version of the day's advice.
    note = {**args, "entry_type": "note", "safety": None}
    assert contract.check_write(**note)["changed_because"] is None


# --- the runner: preloaded thread, refusal and repair ----------------------------------------


def test_every_run_gets_the_day_thread_and_must_build_on_it(rt, sent):
    first, _ = _run(rt, DAY.isoformat(), "Rustpols hoger: rustig aan.", AMBER)
    assert first.status == "done", first.error
    coach_replies.add(rt.engine, first.entry_id, "Ik voel me prima, mag ik toch lopen?")

    # The second run writes without changed_because: refused, repaired from the message.
    bare = morning_write("Rustpols weer normaal: duurloop mag door.", day=DAY)
    bare["idempotency_key"] = runner.entry_key("morning", DAY, "event-1")
    fixed = {**bare, "changed_because": {"previous": first.entry_id, "reason": BECAUSE}}
    script = (
        tool_use("vibesynergy_coach_write", bare),
        tool_use("vibesynergy_coach_write", fixed, n=2),
        done(),
    )
    second, model = _run(rt, "event-1", "", script=script)
    assert second.status == "done", second.error
    start = model.requests[0]["messages"][0]["content"]
    # The start message names the version this one replaces; the context has the thread.
    assert f"advies #{first.entry_id}" in start and '"previous":' in start
    assert '<context name="day_thread"' in start
    assert "Rustpols hoger: rustig aan." in start and "mag ik toch lopen" in start
    refused = model.requests[1]["messages"][-1]["content"][0]
    assert refused.get("is_error") and "changed_because is required" in refused["content"]
    assert f"#{first.entry_id}" in refused["content"]

    harness = runner.get_run(rt, "morning", "event-1")["harness"]
    assert harness["previous"] == first.entry_id and harness["contract"] == []
    found = daily_advice.current(rt, DAY, trigger=False)
    assert found["entry"]["id"] == second.entry_id
    assert found["entry"]["changed_because"]["reason"] == BECAUSE
    old, new = found["versions"][1], found["versions"][0]
    assert (new["version"], new["of"], new["status"]) == (2, 2, "current")
    assert (old["status"], old["replaced_by"]) == ("replaced", second.entry_id)


def test_the_second_net_fails_a_version_that_ignores_the_thread():
    entry = {
        "entry_type": "morning",
        "text": "Duurloop mag door.",
        "for_date": DAY,
        "meta": {"safety": SAFE, "harness": {"via": "runner", "task": "morning"}},
    }
    issues = runner.contract_issues("morning", entry, DAY, previous=1018)
    assert issues == ["ignores the day thread: no changed_because for advice #1018"]
    entry["meta"]["changed_because"] = {"previous": 1018, "reason": BECAUSE}
    assert runner.contract_issues("morning", entry, DAY, previous=1018) == []


def test_the_rules_say_to_build_on_the_thread():
    from vibesynergy.coach_runner import prompts, registry

    text = prompts.harness_rules("morning")
    assert "BOUW VOORT op de dagdraad" in text and "changed_because" in text
    for task in registry.daily():
        assert "day_thread" in registry.chain_for(task)
    assert context.VERSION >= 4 and contract.HARNESS_VERSION >= 3


# --- everything sent is stored and linked ------------------------------------------------------


def test_every_notice_push_and_mail_is_the_linked_stored_entry(rt, sent):
    """For every alert of kind advice, daily_coach or report: the text is in the entry it
    links to."""
    first, _ = _run(rt, DAY.isoformat(), "Rustpols hoger: rustig aan.", AMBER)
    because = {"previous": first.entry_id, "reason": BECAUSE}
    second, _ = _run(rt, "event-1", "Rustpols weer normaal: duurloop mag door.", because=because)
    weekly = runner.run(
        rt,
        "weekly",
        DAY.isoformat(),
        DAY,
        client=FakeModel(
            tool_use(
                "vibesynergy_coach_write",
                {
                    "entry_type": "note",
                    "summary": "Weekreview 2026-W45: goede week, fietsen loopt iets achter.",
                    "idempotency_key": runner.entry_key("weekly", DAY),
                    "for_date": DAY.isoformat(),
                },
            ),
            done(),
        ),
    )
    assert weekly.status == "done", weekly.error

    # A coach note from an app (no daily advice): the kind daily_coach.
    with rt.engine.begin() as conn:
        note_id = conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="note",
                author="client:app",
                entry_type="morning",
                for_date=DAY,
                text="Goedemorgen, vandaag een rustige dag.",
                meta={},
            )
        ).inserted_primary_key[0]
    store = SecretStore(rt.settings.secrets_dir, alerts.SECRETS)
    store.set(alerts.STATE, {alerts.CHECKED: (utc_now() - timedelta(hours=1)).isoformat()})
    alerts.check(rt)

    def linked(text):
        found = re.search(r"#/coach/report/(\d+)", text)
        assert found, text
        return int(found.group(1))

    # Portal notices: one per day for the advice, linked to exactly the current version.
    portal = [
        e
        for e in alerts.for_portal(rt.settings)["recent"]
        if e["kind"] in ("advice", "daily_coach")
    ]
    assert {e["kind"] for e in portal} == {"advice", "daily_coach"}
    for notice in portal:
        ident = linked(notice["link"])
        assert notice["entry_id"] == ident
        assert coach_consistency.text_in_entry(notice["excerpt"], _text(rt, ident))
        assert notice["excerpt"][:40] in notice["text"]
    advice = [e for e in portal if e["kind"] == "advice"]
    assert len(advice) == 1 and advice[0]["entry_id"] == second.entry_id

    # Pushes open exactly that entry and show its words; a push that bundles a coach note
    # with other alerts carries the note's own link in its text.
    pushed = set()
    for _title, body, url in sent["push"]:
        ident = linked(url if "/coach/report/" in url else body)
        assert report.excerpt({"text": _text(rt, ident)})[:60] in " ".join(body.split())
        pushed.add(ident)
    assert pushed == {first.entry_id, second.entry_id, note_id}

    # Mails with a coach text (not the other alerts): the report text and its link.
    coach_mails = [
        (subject, body)
        for subject, body in sent["mail"]
        if subject.startswith("Coach") or "/coach/report/" in body
    ]
    for _subject, body in coach_mails:
        ident = linked(body)
        assert " ".join(_text(rt, ident).split())[:60] in " ".join(body.split())
    mailed = {linked(b) for _s, b in coach_mails}
    assert {first.entry_id, second.entry_id, weekly.entry_id} <= mailed  # daily_coach: no mail
    assert coach_consistency.check(rt, DAY)["findings"] == []


def test_no_notice_for_a_missing_or_a_replaced_entry(rt, sent):
    assert report.send(rt, "morning", DAY, {"id": 999_999, "text": "Niet bewaard."}) == []
    assert report.send(rt, "weekly", DAY, {"id": 999_999, "text": "Niet bewaard."}) == []
    first, _ = _run(rt, DAY.isoformat(), "Rustpols hoger: rustig aan.", AMBER)
    because = {"previous": first.entry_id, "reason": BECAUSE}
    second, _ = _run(rt, "event-1", "Rustpols weer normaal: duurloop mag door.", because=because)
    before = (len(sent["push"]), len(sent["mail"]))
    # The older version is replaced: it is never sent again, not even by hand.
    assert report.send(rt, "morning", DAY, {"id": first.entry_id}) == []
    assert (len(sent["push"]), len(sent["mail"])) == before
    assert _notices(rt)[0]["entry_id"] == second.entry_id


def test_a_version_that_only_rewords_points_the_notice_at_itself(rt, sent):
    keep = [{"title": "Duurloop", "decision": "keep"}]
    first, _ = _run(rt, DAY.isoformat(), "Duurloop rustig.", sessions=keep)
    pushes = len(sent["push"])
    because = {"previous": first.entry_id, "reason": "Niets veranderd, alleen anders gezegd."}
    second, _ = _run(rt, "event-1", "Doe de duurloop rustig.", because=because, sessions=keep)
    assert not second.mailed and len(sent["push"]) == pushes  # no new push for wording
    (notice,) = _notices(rt)
    assert notice["entry_id"] == second.entry_id
    assert notice["link"] == f"#/coach/report/{second.entry_id}"
    assert "Doe de duurloop rustig." in notice["text"]


def test_the_report_page_says_where_a_version_stands(rt, sent):
    first, _ = _run(rt, DAY.isoformat(), "Rustpols hoger: rustig aan.", AMBER)
    because = {"previous": first.entry_id, "reason": BECAUSE}
    second, _ = _run(rt, "event-1", "Rustpols weer normaal: duurloop mag door.", because=because)
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    old = client.get(f"/api/coach/report/{first.entry_id}").json()["data"]["thread"]
    assert old["status"] == "replaced" and old["current_id"] == second.entry_id
    assert (old["version"], old["of"]) == (1, 2)
    new = client.get(f"/api/coach/report/{second.entry_id}").json()["data"]["thread"]
    assert new["status"] == "current" and new["changed_because"]["reason"] == BECAUSE
    assert client.get("/api/coach/continuity").json()["data"]["findings"] == []


# --- the continuity check ------------------------------------------------------------------------


def test_the_check_reports_gaps_and_runs_once_a_day(rt, sent):
    today = today_in(rt.settings.timezone)
    store = SecretStore(rt.settings.secrets_dir, alerts.SECRETS)
    # A notice from somewhere else (#107/#109): a text that is stored nowhere.
    store.set(
        alerts.ADVICE_STATE,
        [{"key": f"advice:{today.isoformat()}", "kind": "advice", "text": "Coach: rust."}],
    )
    # A later version, written under this harness, that changed colour without a reason.
    for text, safety in (("Rustig aan.", AMBER), ("Train volgens plan.", SAFE)):
        with rt.engine.begin() as conn:
            conn.execute(
                insert(coach_decision_log).values(
                    subject_id=1,
                    at=utc_now(),
                    kind="note",
                    author="client:coach-runner",
                    entry_type="morning",
                    for_date=today,
                    text=text,
                    meta={
                        "safety": safety,
                        "harness": {"via": "runner", "task": "morning", "version": 3},
                    },
                )
            )
    rules = {f["rule"] for f in coach_consistency.check(rt, today)["findings"]}
    assert {"notice_without_entry", "contradiction"} <= rules
    assert coach_consistency.daily(rt) is not None
    assert coach_consistency.daily(rt) is None  # once a day
    with rt.engine.connect() as conn:
        rows = conn.execute(
            select(audit_log).where(audit_log.c.action == coach_consistency.ACTION)
        ).all()
    assert len(rows) == 1 and rows[0].outcome == "gaps"
    page = coach_consistency.for_page(rt, __import__("vibesynergy.i18n").i18n.translator("nl"))
    assert any("niet bij je AI-coach bewaard" in f["message"] for f in page)


# --- MCP coaches join the same thread ------------------------------------------------------------


def _mcp(rt, tool, role="own", **args):
    async def go():
        tokens = set_connection("client-1", scopes=frozenset({"read", "coach.write"}), role=role)
        try:
            async with Client(build_server(rt)) as client:
                return await client.call_tool(tool, args)
        finally:
            reset_connection(tokens)

    return anyio.run(go)


def test_an_mcp_coach_writes_into_the_same_thread_with_the_same_notice(rt, sent):
    today = today_in(rt.settings.timezone)
    with rt.engine.begin() as conn:
        first = conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="note",
                author="client:coach-runner",
                entry_type="morning",
                for_date=today,
                text="Rustpols hoger: rustig aan.",
                meta={"safety": AMBER, "harness": {"via": "runner", "task": "morning"}},
            )
        ).inserted_primary_key[0]

    prompt = _mcp(rt, "vibesynergy_coach_prompt", job="morning", include_context=True)
    text = prompt.structured_content["data"]["context"]["text"]
    assert '<context name="day_thread"' in text and "Rustpols hoger" in text

    args = {
        "entry_type": "morning",
        "summary": "Rustpols weer normaal: duurloop mag door.",
        "idempotency_key": f"morning-{today.isoformat()}",
        "for_date": today.isoformat(),
        "safety": SAFE,
        "task": "morning",
    }
    refused = _mcp(rt, "vibesynergy_coach_write", **args)
    assert refused.is_error and "changed_because is required" in refused.content[0].text
    written = _mcp(
        rt,
        "vibesynergy_coach_write",
        **args,
        changed_because={"previous": first, "reason": BECAUSE},
    )
    assert not written.is_error, written.content[0].text
    ident = written.structured_content["data"]["id"]

    found = daily_advice.current(rt, today, trigger=False)
    assert [v["id"] for v in found["versions"]] == [ident, first]
    assert found["versions"][0]["changed_because"]["reason"] == BECAUSE
    (notice,) = _notices(rt)
    assert notice["entry_id"] == ident and notice["key"] == f"advice:{today.isoformat()}"
    assert sent["push"] and sent["push"][-1][2] == f"/#/coach/report/{ident}"

    # The same thread in vibesynergy_today and vibesynergy_coach_entries, in the same order.
    day = _mcp(rt, "vibesynergy_today").structured_content["data"]
    assert [v["id"] for v in day["advice"]["versions"]] == [ident, first]
    assert day["continuity"] == []
    entries = _mcp(rt, "vibesynergy_coach_entries", period="last_7d").structured_content["data"]
    states = {e["id"]: e["advice_version"] for e in entries["entries"]}
    assert states[ident]["status"] == "current" and states[first]["status"] == "replaced"
    assert [e["id"] for e in entries["entries"] if e["id"] in (ident, first)] == [ident, first]


def test_a_second_opinion_needs_no_reason_and_is_no_version(rt, sent):
    today = today_in(rt.settings.timezone)
    with rt.engine.begin() as conn:
        conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="note",
                author="client:coach-runner",
                entry_type="morning",
                for_date=today,
                text="Rustig aan.",
                meta={"safety": AMBER, "harness": {"via": "runner", "task": "morning"}},
            )
        )
    found = _mcp(
        rt,
        "vibesynergy_coach_write",
        role="advisory",
        entry_type="morning",
        summary="Ik zou toch lopen.",
        idempotency_key=f"morning-{today.isoformat()}",
        for_date=today.isoformat(),
        safety=SAFE,
        task="morning",
    )
    assert not found.is_error, found.content[0].text
    assert len(daily_advice.current(rt, today, trigger=False)["versions"]) == 1
    assert _notices(rt) == []


# --- one label for every advice: the day it is for (#138) ----------------------------------------

LONG_EVENING = (
    "Oranje: je herstel is nog niet terug op je eigen niveau, dus morgen de rustige rit van "
    "1u15 in zone 2 en niets daarboven. Je rustpols ligt twee dagen hoger en je slaap was "
    "korter dan normaal; je HRV ligt nog net onder normaal, dus houd het rustig en eet goed "
    "na afloop. Voel je je morgenochtend fris, dan mag de rit iets langer."
)


def _advice_entry(rt, day, entry_type, task, text, safety):
    with rt.engine.begin() as conn:
        return conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="note",
                author="client:coach-runner",
                entry_type=entry_type,
                for_date=day,
                text=text,
                meta={"safety": safety, "harness": {"via": "runner", "task": task}},
            )
        ).inserted_primary_key[0]


def test_an_evening_advice_is_named_after_tomorrow_everywhere(rt, sent):
    """Robert, 20-11 (#138): a check-in advice for today and the evening run's advice for
    tomorrow. Every place names each advice after the day it is for, with the same colour."""
    from vibesynergy.core.user_datetime import format_user_short_day

    today = today_in(rt.settings.timezone)
    tomorrow = today + timedelta(days=1)
    checkin = _advice_entry(
        rt, today, "note", "checkin", "Vandaag blijft rust; hooguit pilates.", SAFE
    )
    assert daily_advice.announce(rt, checkin)
    evening = _advice_entry(rt, today, "evening", "evening", LONG_EVENING, AMBER)
    assert daily_advice.announce(rt, evening)

    nl = __import__("vibesynergy.i18n").i18n.translator("nl")
    stored = report.stored(rt, evening)
    run = daily_advice.run_label(stored, rt.settings.timezone, nl)
    assert re.fullmatch(r"avondrun \d\d:\d\d", run)
    short = format_user_short_day(tomorrow, "nl")
    tomorrow_title = f"Advies voor morgen \N{EN DASH} {short} ({run})"
    today_title = f"Advies voor vandaag \N{EN DASH} {format_user_short_day(today, 'nl')}"

    # The notices: one per target day, each with the day it is for.
    notices = {n["key"]: n for n in _notices(rt)}
    late = notices[f"advice:{tomorrow.isoformat()}"]
    early = notices[f"advice:{today.isoformat()}"]
    assert late["entry_id"] == evening and late["advice_day"] == tomorrow.isoformat()
    assert late["text"].startswith(f"{tomorrow_title}: Oranje")
    assert early["entry_id"] == checkin and early["text"].startswith(today_title + " (check-in-run")
    # Cut cleanly: never inside a word, and marked with "…".
    assert late["text"].endswith("\N{HORIZONTAL ELLIPSIS}") and len(late["text"]) <= 300
    assert late["text"].removesuffix("\N{HORIZONTAL ELLIPSIS}").rstrip(" .").split()[-1] in (
        LONG_EVENING.replace(".", " ").replace(",", " ").replace(";", " ").split()
    )

    # The push and the mail carry the same title.
    titles = [title for title, _body, url in sent["push"] if url == f"/#/coach/report/{evening}"]
    assert titles == [tomorrow_title]
    assert any(subject == tomorrow_title for subject, _ in sent["mail"])
    push_body = next(b for _t, b, u in sent["push"] if u == f"/#/coach/report/{evening}")
    assert push_body.startswith("Oranje") and push_body.endswith("\N{HORIZONTAL ELLIPSIS}")
    assert len(push_body) <= report.SHORT_LIMIT

    # The report page and the Coach page: the entry says the day it is for and its run.
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    page = client.get(f"/api/coach/report/{evening}").json()["data"]
    assert page["entry"]["advice_day"] == tomorrow.isoformat()
    assert page["entry"]["advice_job"] == "evening"
    assert page["thread"]["date"] == tomorrow.isoformat()
    feed = {e["id"]: e for e in client.get("/api/coach/feed").json()["data"]["entries"]}
    assert feed[evening]["advice_day"] == tomorrow.isoformat()
    assert feed[checkin]["advice_day"] == today.isoformat()

    # The start page: today's advice stays the check-in's; tomorrow's is in its own card.
    card = daily_advice.upcoming(rt, today, tr=nl)
    assert card is not None and card["entry_id"] == evening and card["date"] == tomorrow.isoformat()
    assert card["level"] == "amber" and card["colour"] == "Oranje"
    assert card["label"] == tomorrow_title.split(" (")[0] and card["run"] == run
    assert card["link"] == late["link"] == f"#/coach/report/{evening}"
    assert daily_advice.current(rt, today, trigger=False)["entry"]["id"] == checkin

    # The MCP: the same days, the same entries.
    day = _mcp(rt, "vibesynergy_today").structured_content["data"]
    assert day["advice"]["entry"]["id"] == checkin
    assert day["advice"]["label"].startswith("Advice for today")
    assert day["advice_tomorrow"]["entry_id"] == evening
    assert day["advice_tomorrow"]["date"] == tomorrow.isoformat()
    assert day["advice_tomorrow"]["level"] == "amber"
    assert day["continuity"] == []
    entries = _mcp(rt, "vibesynergy_coach_entries", period="last_7d").structured_content["data"]
    labels = {e["id"]: e["advice_label"] for e in entries["entries"]}
    assert labels[evening]["date"] == tomorrow.isoformat()
    assert labels[evening]["day"].startswith("Advice for tomorrow")
    assert labels[evening]["run"].startswith("evening run ")
    assert labels[checkin]["date"] == today.isoformat()

    # One day and one colour for each advice, in every place.
    assert {late["advice_day"], card["date"], labels[evening]["date"]} == {tomorrow.isoformat()}
    assert coach_consistency.check(rt, today)["findings"] == []


def test_the_check_flags_a_notice_named_after_the_run_day(rt, sent):
    """The notice of 20-11 21:46: the evening advice for Saturday labelled "vr 20 nov"."""
    from vibesynergy.core.user_datetime import format_user_short_day

    today = today_in(rt.settings.timezone)
    tomorrow = today + timedelta(days=1)
    evening = _advice_entry(rt, today, "evening", "evening", "Oranje: morgen rustig.", AMBER)
    store = SecretStore(rt.settings.secrets_dir, alerts.SECRETS)
    old = f"Coach avond \N{EN DASH} {format_user_short_day(today, 'nl')}: Oranje: morgen rustig."
    store.set(
        alerts.ADVICE_STATE,
        [
            {
                "key": f"advice:{tomorrow.isoformat()}",
                "kind": "advice",
                "at": utc_now().isoformat(),
                "text": old,
                "entry_id": evening,
                "excerpt": "Oranje: morgen rustig.",
            }
        ],
    )
    found = coach_consistency.check(rt, today)["findings"]
    assert [f["rule"] for f in found] == ["notice_day_differs"]
    assert tomorrow.isoformat() in found[0]["evidence"]
    # A notice whose own day says another day than its entry is flagged too.
    store.set(
        alerts.ADVICE_STATE,
        [
            {
                "key": f"advice:{tomorrow.isoformat()}",
                "kind": "advice",
                "text": f"Advies voor {format_user_short_day(tomorrow, 'nl')}: Oranje",
                "entry_id": evening,
                "advice_day": today.isoformat(),
            }
        ],
    )
    (gap,) = coach_consistency.check(rt, today)["findings"]
    assert gap["rule"] == "notice_day_differs" and "advice_day" in gap["evidence"]
    page = coach_consistency.for_page(rt, __import__("vibesynergy.i18n").i18n.translator("nl"))
    assert any("andere dag" in f["message"] for f in page)
