"""The coach feed: notes over MCP and the safety line on morning and evening advice."""

from __future__ import annotations

from vibesynergy.query import coach_workouts


def test_mcp_coach_log_only_notes(runtime):
    import anyio
    from mcp import Client

    from vibesynergy.mcp.server import build_server

    async def call(kind: str):
        async with Client(build_server(runtime)) as m:
            return await m.call_tool("vibesynergy_coach_log", {"text": "hi", "kind": kind})

    ok = anyio.run(lambda: call("note"))
    assert not ok.is_error
    for kind in ("approval", "push_started", "cancelled", "bogus"):
        result = anyio.run(lambda k=kind: call(k))
        assert result.is_error


def test_flag_day_keeps_coach_text(runtime):
    from vibesynergy.core.timeutil import today_in
    from vibesynergy.portal import prefs
    from vibesynergy.query import daily_coach, journal

    day = today_in(runtime.settings.timezone)
    summary = "Skip the tempo: 40 min easy on flat ground."
    coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary=summary,
        caveat="Keep Saturday's long run.",
        for_date=day,
        author="client:coach",
        idempotency_key="flag-morning",
    )
    coach_workouts.coach_write(
        runtime.engine,
        entry_type="note",
        summary="Moved long run to Sunday.",
        for_date=day,
        author="client:coach",
        idempotency_key="flag-note",
    )
    journal.set_checkin(runtime.engine, day, soreness=4)

    def morning_of(feed):
        return next(entry for entry in feed if entry["entry_type"] == "morning")

    feed = coach_workouts.coach_feed(runtime)
    morning = morning_of(feed)
    assert morning["advice_text"] == summary
    assert morning["caveat"] == "Keep Saturday's long run."
    assert morning["advice_mode"] == "advice_with_safety"
    assert morning["safety_line"]
    assert "Keep it easy" in morning["safety_line"]
    noted = next(entry for entry in feed if entry["entry_type"] == "note")
    assert noted["advice_text"] == "Moved long run to Sunday."
    assert noted["advice_mode"] == "advice"
    assert noted["safety_line"] is None
    view = daily_coach.read(runtime, day)["morning"]
    assert view["advice"]["interpretation"]["summary"] == summary
    assert view["display"]["mode"] == "safety"
    assert view["display"]["safety_text"]

    prefs.save(runtime.settings.data_dir, {"language": "nl"})
    dutch = morning_of(coach_workouts.coach_feed(runtime))
    assert dutch["advice_text"] == summary
    assert dutch["advice_mode"] == "advice_with_safety"
    assert "Houd het rustig" in dutch["safety_line"]

    journal.log_injury(runtime.engine, today=day, body_part="knee", side="left", pain=7, when=day)
    refused = morning_of(coach_workouts.coach_feed(runtime))
    assert refused["advice_text"] == summary
    assert refused["advice_mode"] == "safety_override"
    assert refused["safety_line"]
    assert refused["advice_text"] not in refused["safety_line"]


def test_coach_feed_safety_once_per_date(runtime, monkeypatch):
    from datetime import timedelta

    from vibesynergy.core.timeutil import today_in
    from vibesynergy.query import daily_coach

    day = today_in(runtime.settings.timezone)
    other = day - timedelta(days=1)
    for key, when, kind in (
        ("a", day, "morning"),
        ("b", day, "evening"),
        ("c", day, "note"),
        ("d", other, "morning"),
    ):
        coach_workouts.coach_write(
            runtime.engine,
            entry_type=kind,
            summary=f"Advice {key}",
            for_date=when,
            author="client:coach",
            idempotency_key=f"safety-once-{key}",
        )
    calls: list = []
    real = daily_coach.current_safety

    def counted(rt, when, **kwargs):
        calls.append(when)
        return real(rt, when, **kwargs)

    monkeypatch.setattr(daily_coach, "current_safety", counted)
    coach_workouts.coach_feed(runtime)
    assert calls.count(day) == 1
    assert calls.count(other) == 1
    assert len(calls) == 2
