"""The coach feed checks an advice against today's data only for yesterday and today, once
per (day, kind): rebuilding every past day's state made the Coach page take 20+ seconds."""

from __future__ import annotations

from datetime import date, timedelta

from vibesynergy.query import coach_workouts, daily_coach

SNAP = {"version": None, "documents": {}}


def _entry(day: date, kind: str = "morning", evidence: str = "old") -> dict:
    return {"for_date": day.isoformat(), "entry_type": kind, "meta": {"evidence_version": evidence}}


def test_recent_advice_is_checked_once_per_day_and_kind(monkeypatch) -> None:
    calls: list[tuple[date, str]] = []

    def fake_capture(rt, day, kind, **_):
        calls.append((day, kind))
        return {"input_version": "new"}

    monkeypatch.setattr(daily_coach, "capture", fake_capture)
    today = date(2026, 3, 10)
    captures: dict = {}
    since = today - timedelta(days=1)
    found = [
        coach_workouts.coach_entry_freshness(
            entry, SNAP, rt=None, captures=captures, evidence_from=since
        )
        for entry in (_entry(today), _entry(today), _entry(today, evidence="new"))
    ]
    assert found == ["needs_revision", "needs_revision", "current"]
    assert calls == [(today, "morning")]


def test_advice_of_an_earlier_day_is_not_rebuilt(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("an earlier day's state was rebuilt")

    monkeypatch.setattr(daily_coach, "capture", fail)
    today = date(2026, 3, 10)
    old = _entry(today - timedelta(days=5), "evening")
    assert (
        coach_workouts.coach_entry_freshness(
            old, SNAP, rt=None, captures={}, evidence_from=today - timedelta(days=1)
        )
        == "current"
    )


def test_without_a_limit_every_day_is_still_checked(monkeypatch) -> None:
    monkeypatch.setattr(daily_coach, "capture", lambda *a, **k: {"input_version": "new"})
    old = _entry(date(2026, 3, 1))
    assert coach_workouts.coach_entry_freshness(old, SNAP, rt=None) == "needs_revision"
