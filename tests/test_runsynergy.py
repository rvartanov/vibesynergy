"""RunSynergy calendar link: group trainings become the workout for "Training geven"."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from vibesynergy.core.config import load_settings
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import group_training, plan, plan_model, workout_model
from vibesynergy.runtime import Runtime
from vibesynergy.sources.runsynergy import (
    AHEAD_DAYS,
    PAST_DAYS,
    RunSynergySource,
    feed,
    map_trainings,
)
from vibesynergy.store.schema import plan_session, planned_item

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
LINK = "https://runsynergy.example/agenda/s3cr3t-token-abcdef.ics"

TUESDAY = {
    "v": 1,
    "club": "Loopgroep Stad",
    "club_id": 3,
    "groep": "Dinsdaggroep",
    "rol": "coach",
    "soort": "groep",
    "datum": "2026-11-17",
    "start": "19:00",
    "afgelast": False,
    "titel": "Wisselduurloop DL2/DL3",
    "type": "interval",
    "onderdelen": [
        {"deel": "warming_up", "min": 12, "tekst": "Inlopen DL1"},
        {"deel": "techniek", "min": 16, "tekst": "Loopscholing: knieheffen, A-loop (2x20 m)"},
        {"deel": "kern", "min": 42, "tekst": "7x 4' DL2 / 2' DL3, zonder stilstaan"},
        {"deel": "cooling_down", "min": 20, "tekst": "16 min uitlopen DL1, 4 min rekken"},
    ],
    "url": "https://runsynergy.example/training/123",
}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    raw = line.encode()
    parts = [raw[:75]]
    raw = raw[75:]
    while raw:
        parts.append(b" " + raw[:74])
        raw = raw[74:]
    return "\r\n".join(p.decode(errors="ignore") for p in parts)


def _summary(data: dict | None) -> str:
    return f"{(data or {}).get('club', 'Iets')}: {(data or {}).get('titel', '')}"


def _event(uid: str, start: str, data: dict | None, *, status: str = "CONFIRMED", seq: int = 0):
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "DTSTAMP:20261116T120000Z",
        f"DTSTART;TZID=Europe/Amsterdam:{start}",
        f"SUMMARY:{_escape(_summary(data))}",
        f"STATUS:{status}",
        f"SEQUENCE:{seq}",
    ]
    if data is not None:
        lines.append(_fold("X-RUNSYNERGY-DATA:" + _escape(json.dumps(data, ensure_ascii=False))))
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def _calendar(*events: str) -> str:
    return "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//RunSynergy//NL", *events, "END:VCALENDAR", ""]
    )


def _put(runtime, ics: str, today: date = date(2026, 11, 16)) -> None:
    first = date.fromordinal(today.toordinal() - PAST_DAYS)
    last = date.fromordinal(today.toordinal() + AHEAD_DAYS)
    window = f"{first}..{last}"
    stored = runtime.raw.put(
        source="runsynergy",
        dataset="trainings",
        item_key="calendar-link",
        payload=json.dumps({"ics": ics, "timezone": "Europe/Amsterdam", "window": window}).encode(),
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            list(map_trainings(runtime.raw.load(stored.id))),
            source="runsynergy",
            raw_item_id=stored.id,
        )


def _plan_with_coached_session(runtime, name="Training geven Loopgroep Stad 1u15"):
    plan_model.create_plan(
        runtime.engine,
        name="Tri 27",
        start=date(2026, 11, 2),
        end=date(2027, 7, 3),
        reason="r",
        author="athlete",
    )
    out = plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=date(2026, 11, 17),
        name=name,
        sport="Run",
        description="Training geven, zelf Z1.\n\nWarmup\n- 15m Z1 HR\n\nMain set\n- 55m Z1 HR",
        reason="r",
        author="Claude",
        slot="evening",
    )
    return out["session_id"]


# -- the feed -------------------------------------------------------------------------------


def test_feed_reads_escaped_folded_data_and_skips_other_events() -> None:
    other = TUESDAY | {"v": 2}
    ics = _calendar(
        _event("rs-3-2026-11-17-1@x", "20261117T190000", TUESDAY, seq=2),
        _event("plain@x", "20261118T190000", None),
        _event("future@x", "20261119T190000", other),
    )
    found = feed.parse(ics, "Europe/Amsterdam")
    assert [t.uid for t in found] == ["rs-3-2026-11-17-1@x"]
    one = found[0]
    assert (one.day, one.start, one.sequence, one.cancelled) == (
        date(2026, 11, 17),
        "19:00",
        2,
        False,
    )
    assert one.data["onderdelen"][2]["tekst"] == "7x 4' DL2 / 2' DL3, zonder stilstaan"
    assert (one.club, one.role, one.kind) == ("Loopgroep Stad", "coach", "groep")


def test_feed_reads_unescaped_data_and_cancellation() -> None:
    line = "X-RUNSYNERGY-DATA:" + json.dumps(TUESDAY | {"afgelast": True})
    ics = _calendar(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                "UID:u@x",
                "DTSTART;VALUE=DATE:20261117",
                "SUMMARY:Groepstraining",
                line,
                "END:VEVENT",
            ]
        )
    )
    (one,) = feed.parse(ics, "Europe/Amsterdam")
    assert one.cancelled and one.start == "19:00"  # date-only: the time comes from the data


def test_feed_refuses_a_page_that_is_not_a_calendar() -> None:
    with pytest.raises(feed.FeedError):
        feed.parse("<html>login</html>", "Europe/Amsterdam")


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (LINK, True),
        ("webcal://runsynergy.example/agenda/abc.ics", True),
        ("http://runsynergy.example/agenda/abc.ics", False),
        ("https://user:pw@runsynergy.example/agenda/abc.ics", False),
        ("https://runsynergy.example/", False),
    ],
)
def test_only_a_full_https_link_is_kept(url: str, ok: bool) -> None:
    if ok:
        assert feed.clean_url(url).startswith("https://")
    else:
        with pytest.raises(feed.FeedError):
            feed.clean_url(url)


def test_source_fetch_sends_the_etag_and_keeps_nothing_on_304(settings, monkeypatch) -> None:
    feed.save(settings.secrets_dir, LINK)
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, dict(headers)))
        if headers.get("If-None-Match") == '"v1"':
            return SimpleNamespace(status_code=304, headers={}, content=b"")
        body = _calendar(_event("u@x", "20261117T190000", TUESDAY)).encode()
        return SimpleNamespace(
            status_code=200, headers={"ETag": '"v1"'}, content=body, raise_for_status=lambda: None
        )

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    ctx = SimpleNamespace(settings=settings, config={}, secrets=None, log=None, raw=None)
    source = RunSynergySource(ctx)  # pyright: ignore[reportArgumentType]
    assert source.configured()
    (item,) = list(source.fetch(source.dataset("trainings"), None))
    assert item.payload["etag"] == '"v1"' and "BEGIN:VCALENDAR" in item.payload["ics"]
    source._previous = lambda: {"etag": '"v1"'}  # type: ignore[method-assign]
    assert list(source.fetch(source.dataset("trainings"), None)) == []
    assert calls[1][1]["If-None-Match"] == '"v1"'


# -- the workout ----------------------------------------------------------------------------


def test_the_group_schedule_becomes_a_whistle_workout_in_z1() -> None:
    text, how = group_training.build(TUESDAY)
    assert how == "text"
    parsed = workout_model.parse_intervals(text)
    assert parsed.unparsed == []
    warm, core, cool = parsed.steps
    assert warm["until_lap"] and warm["duration_s"] == 28 * 60 and warm["kind"] == "warmup"
    assert core["kind"] == "repeat" and core["times"] == 7
    assert [(s["duration_s"], s["cue"]) for s in core["steps"]] == [
        (240, "DL2"),
        (120, "DL3, zonder stilstaan"),
    ]
    assert cool["until_lap"] and cool["duration_s"] == 20 * 60
    every = [warm, *core["steps"], cool]
    assert all(s["target"] == {"type": "hr_zone", "low": 1, "high": 1} for s in every)
    assert workout_model.total_seconds(parsed.steps) == 90 * 60
    assert "Loopgroep Stad: Wisselduurloop DL2/DL3" in parsed.intro


def test_structured_core_wins_and_an_unreadable_core_is_one_block() -> None:
    structured = TUESDAY | {
        "kern_stappen": [
            {"herhaal": 5, "stappen": [{"min": 4, "zone": "DL2"}, {"min": 2, "zone": "DL3"}]}
        ]
    }
    steps, how = group_training.core_steps(structured)
    assert how == "structured" and steps[0]["times"] == 5
    vague = TUESDAY | {"onderdelen": [{"deel": "kern", "min": 30, "tekst": "Heuvels: 20m omhoog"}]}
    text, how = group_training.build(vague)
    assert how == "fallback"
    parsed = workout_model.parse_intervals(text)
    assert parsed.unparsed == []
    assert parsed.steps[1]["duration_s"] == 1800 and "20 m" in parsed.steps[1]["cue"]


# -- matching and adopting ------------------------------------------------------------------


def test_group_trainings_never_count_as_plan(runtime) -> None:
    _put(runtime, _calendar(_event("u@x", "20261117T190000", TUESDAY)))
    with runtime.engine.connect() as conn:
        assert conn.execute(select(planned_item.c.kind)).scalars().all() == ["group_training"]
    view = plan.plan(
        runtime.engine,
        plan.Window(date(2026, 11, 16), date(2026, 11, 22)),
        date(2026, 11, 16),
    )
    assert view["items"] == []


def test_a_coached_session_gets_the_proposal_and_adopting_writes_it(runtime) -> None:
    session_id = _plan_with_coached_session(runtime)
    _put(runtime, _calendar(_event("u@x", "20261117T190000", TUESDAY)))
    found = group_training.proposals(runtime.engine, date(2026, 11, 16), date(2026, 11, 22))
    proposal = found[session_id]
    assert proposal["state"] == "proposed" and proposal["minutes"] == 90
    out = group_training.adopt(runtime.engine, session_id)
    assert out["state"] == "adopted"
    with runtime.engine.connect() as conn:
        row = (
            conn.execute(select(plan_session).where(plan_session.c.id == session_id))
            .mappings()
            .one()
        )
    assert row["name"] == "Training geven Loopgroep Stad 1u15" and row["duration_s"] == 5400
    again = group_training.proposals(runtime.engine, date(2026, 11, 16), date(2026, 11, 22))
    assert again[session_id]["state"] == "adopted"
    # A change in RunSynergy makes a new proposal.
    changed = TUESDAY | {
        "onderdelen": [
            *TUESDAY["onderdelen"][:2],
            {"deel": "kern", "min": 30, "tekst": "5x 4' DL2 / 2' DL3"},
        ]
    }
    _put(runtime, _calendar(_event("u@x", "20261117T190000", changed, seq=1)))
    newer = group_training.proposals(runtime.engine, date(2026, 11, 16), date(2026, 11, 22))
    assert newer[session_id]["state"] == "proposed"


def test_runner_rows_other_days_and_cancelled_trainings(runtime) -> None:
    session_id = _plan_with_coached_session(runtime)
    runner = TUESDAY | {"rol": "loper"}
    _put(runtime, _calendar(_event("u@x", "20261117T190000", runner)))
    assert group_training.proposals(runtime.engine, date(2026, 11, 16), date(2026, 11, 22)) == {}
    _put(runtime, _calendar(_event("u@x", "20261117T190000", TUESDAY, status="CANCELLED")))
    found = group_training.proposals(runtime.engine, date(2026, 11, 16), date(2026, 11, 22))
    assert found[session_id]["state"] == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        group_training.adopt(runtime.engine, session_id)


def test_two_clubs_on_one_day_match_by_name(runtime) -> None:
    session_id = _plan_with_coached_session(runtime, "Training geven Loopgroep Dorp - 17-11")
    dorp = TUESDAY | {"club": "Loopgroep Dorp", "titel": "Heuvels"}
    _put(
        runtime,
        _calendar(
            _event("a@x", "20261117T190000", TUESDAY),
            _event("b@x", "20261117T200000", dorp),
        ),
    )
    found = group_training.proposals(runtime.engine, date(2026, 11, 16), date(2026, 11, 22))
    assert found[session_id]["club"] == "Loopgroep Dorp"


# -- the portal -----------------------------------------------------------------------------


def test_portal_saves_the_link_without_showing_it_and_adopts(env, monkeypatch) -> None:
    settings = replace(load_settings(), server={"public_url": f"https://{TAILNET}"})
    runtime = Runtime.open(settings)
    runtime.migrate()
    try:
        client = TestClient(create_portal_app(runtime), base_url=f"https://{TAILNET}:8447")
        bad = client.post(
            "/api/sources/runsynergy/link", json={"url": "http://x.example/a.ics"}, headers=WRITE
        )
        assert bad.status_code == 400
        saved = client.post("/api/sources/runsynergy/link", json={"url": LINK}, headers=WRITE)
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"] == {"set": True, "host": "runsynergy.example"}
        page = client.get("/api/settings")
        row = next(s for s in page.json()["data"]["sources"] if s["id"] == "runsynergy")
        assert row["enabled"] and row["login"]["state"] == "logged in"
        for body in (saved.text, page.text):
            assert "s3cr3t" not in body
        session_id = _plan_with_coached_session(runtime)
        _put(runtime, _calendar(_event("u@x", "20261117T190000", TUESDAY)))
        listed = client.get("/api/plan/group-trainings?start=2026-11-16&end=2026-11-22")
        assert [p["state"] for p in listed.json()["data"]["proposals"]] == ["proposed"]
        adopted = client.post(
            "/api/plan/session/group-training", json={"session_id": session_id}, headers=WRITE
        )
        assert adopted.status_code == 200, adopted.text
        off = client.post("/api/sources/runsynergy/disconnect", headers=WRITE)
        assert off.status_code == 200 and feed.link(settings.secrets_dir) is None
    finally:
        runtime.close()


# -- the day of a proposal (#113) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("lang", "start", "expected"),
    [
        ("nl", "19:00", "di 17 nov, 19:00"),
        ("en", "19:00", "Tue 17 Nov, 19:00"),
        ("nl", None, "di 17 nov"),
        ("en", None, "Tue 17 Nov"),
    ],
)
def test_a_proposal_says_its_day_and_time(lang, start, expected) -> None:
    assert group_training.when({"date": "2026-11-17", "start": start}, lang) == expected


def test_past_is_the_day_before_today_or_today_after_the_start() -> None:
    day = date(2026, 11, 17)
    assert group_training.is_past(day, "19:00", datetime(2026, 11, 18, 8, 0))
    assert not group_training.is_past(day, "19:00", datetime(2026, 11, 17, 18, 59))
    assert group_training.is_past(day, "19:00", datetime(2026, 11, 17, 19, 0))
    assert not group_training.is_past(day, None, datetime(2026, 11, 17, 23, 0))
    assert not group_training.is_past(day, "19:00", datetime(2026, 11, 16, 20, 0))
    assert not group_training.is_past(day, "19:00", None)


def _strength_same_day(runtime) -> str:
    out = plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=date(2026, 11, 17),
        name="Kracht 30'",
        sport="WeightTraining",
        description="Kracht\n\nMain set\n- 30m Z1 HR",
        reason="r",
        author="athlete",
        slot="morning",
    )
    return out["session_id"]


def test_a_proposal_is_marked_past_and_names_the_days_other_sessions(runtime) -> None:
    session_id = _plan_with_coached_session(runtime)
    other = _strength_same_day(runtime)
    _put(runtime, _calendar(_event("u@x", "20261117T190000", TUESDAY)))
    span = (date(2026, 11, 16), date(2026, 11, 22))
    ahead = group_training.proposals(runtime.engine, *span, now=datetime(2026, 11, 16, 9, 0))
    proposal = ahead[session_id]
    assert (proposal["date"], proposal["start"], proposal["past"]) == ("2026-11-17", "19:00", False)
    assert proposal["same_day"] == [
        {"session_id": other, "name": "Kracht 30'", "sport": "WeightTraining"}
    ]
    late = group_training.proposals(runtime.engine, *span, now=datetime(2026, 11, 18, 9, 0))
    assert late[session_id]["past"] is True
    # Waiting for you keeps it two days after its day, marked; then it leaves the list.
    waiting = group_training.waiting(runtime.engine, date(2026, 11, 18), datetime(2026, 11, 18, 9))
    assert [(p["session_id"], p["past"]) for p in waiting] == [(session_id, True)]
    assert "current" not in waiting[0]
    assert group_training.waiting(runtime.engine, date(2026, 11, 20)) == []


def test_waiting_for_you_lists_the_proposal_with_its_day(runtime) -> None:
    from vibesynergy.query import inbox

    session_id = _plan_with_coached_session(runtime)
    _strength_same_day(runtime)
    _put(runtime, _calendar(_event("u@x", "20261117T190000", TUESDAY)))
    rows = inbox._group_trainings(runtime.engine, date(2026, 11, 16), datetime(2026, 11, 16, 9))
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "group_training" and row["session_id"] == session_id
    assert (row["date"], row["start"], row["past"]) == ("2026-11-17", "19:00", False)
    assert [s["name"] for s in row["same_day"]] == ["Kracht 30'"]
    assert inbox.count(runtime.engine, date(2026, 11, 16)) == 1
    group_training.adopt(runtime.engine, session_id)
    assert inbox._group_trainings(runtime.engine, date(2026, 11, 16)) == []


def test_the_notice_says_the_day_in_dutch_and_english(runtime) -> None:
    from vibesynergy import alerts
    from vibesynergy.i18n import Translator

    proposal = {
        "date": "2026-11-17",
        "start": "19:00",
        "same_day": [{"session_id": "s1", "name": "Kracht 30'", "sport": "WeightTraining"}],
    }
    nl = alerts.group_training_text(proposal, Translator("nl"))
    assert nl.startswith("Groepstraining uit RunSynergy op di 17 nov, 19:00:")
    assert "Kracht 30'" in nl
    en = alerts.group_training_text({**proposal, "start": None, "same_day": []}, Translator("en"))
    assert en == (
        "Group training from RunSynergy on Tue 17 Nov: a workout for your Training geven "
        "session is ready to adopt."
    )


def test_the_notice_is_open_while_the_proposal_waits(runtime, monkeypatch) -> None:
    from vibesynergy import alerts

    session_id = _plan_with_coached_session(runtime)
    _put(runtime, _calendar(_event("u@x", "20261117T190000", TUESDAY)))
    monkeypatch.setattr(
        "vibesynergy.core.timeutil.local_now", lambda tz: datetime(2026, 11, 16, 9, 0)
    )
    found = alerts._group_trainings(runtime, date(2026, 11, 16))
    assert [(a.kind, a.link) for a in found] == [("group_training", "#/inbox")]
    assert "17 Nov, 19:00" in found[0].text
    # Its identity ignores digits; the session id keeps its own.
    assert not any(ch.isdigit() for ch in found[0].key)
    assert alerts._identity(found[0].key) == found[0].key
    group_training.adopt(runtime.engine, session_id)
    assert alerts._group_trainings(runtime, date(2026, 11, 16)) == []
    assert "group_training" in alerts.QUIET_END


def _mcp(rt, tool: str, **args) -> dict:
    import anyio
    from mcp import Client

    from vibesynergy.mcp.server import build_server

    async def go():
        async with Client(build_server(rt)) as client:
            return await client.call_tool(tool, args)

    result = anyio.run(go)
    text = result.content[0].text if result.content else ""
    assert not result.is_error, text
    return result.structured_content or json.loads(text)


def test_the_mcp_plan_view_has_the_proposal_with_its_date(runtime) -> None:
    session_id = _plan_with_coached_session(runtime)
    _put(runtime, _calendar(_event("u@x", "20261117T190000", TUESDAY)))
    out = _mcp(runtime, "vibesynergy_plan", start="2026-11-16", end="2026-11-22")
    data = out.get("data", out)
    [found] = data["group_trainings"]
    assert found["session_id"] == session_id
    assert (found["date"], found["start"], found["when"]) == (
        "2026-11-17",
        "19:00",
        "Tue 17 Nov, 19:00",
    )
    assert found["state"] == "proposed" and "past" in found and found["same_day"] == []
