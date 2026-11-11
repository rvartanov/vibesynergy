"""Diary notes to intervals.icu wellness comments: opt-in, private only, never overwrite."""

from __future__ import annotations

from datetime import date

import pytest

from vibesynergy.portal import prefs
from vibesynergy.query import icu_notes, journal

DAY = date(2026, 10, 30)


class FakeIcu:
    def __init__(self, private: bool, comments: str | None = None) -> None:
        self.athlete = {
            "private_wellness_keys": ["menstrualPhase"] + (["comments"] if private else [])
        }
        self.wellness = {} if comments is None else {"comments": comments}
        self.puts: list[dict] = []

    def get_athlete(self) -> dict:
        return self.athlete

    def get_wellness(self, day: date) -> dict:
        return dict(self.wellness)

    def put_wellness(self, day: date, fields: dict) -> dict:
        assert set(fields) == {"comments"}
        self.puts.append(dict(fields))
        self.wellness.update(fields)
        return dict(self.wellness)


def _notes(runtime) -> None:
    journal.add_note(runtime.engine, text="Calf tight", start=DAY, tags=["pain"], source="portal")
    journal.add_note(runtime.engine, text="Slept well", start=DAY, source="claude")
    # Imported copies of what Garmin already has are not sent again.
    journal.add_note(
        runtime.engine,
        text="Run: felt strong (Garmin).",
        start=DAY,
        source="garmin",
        source_ref="1",
    )


def test_off_by_default_and_a_preference(runtime) -> None:
    assert icu_notes.enabled(runtime.settings.data_dir) is False
    prefs.save(runtime.settings.data_dir, {icu_notes.PREF: True})
    assert icu_notes.enabled(runtime.settings.data_dir) is True
    with pytest.raises(ValueError):
        prefs.save(runtime.settings.data_dir, {icu_notes.PREF: "yes"})


def test_day_text_is_the_athletes_own_notes(runtime) -> None:
    _notes(runtime)
    assert icu_notes.day_text(runtime.engine, DAY) == "Calf tight [pain]\nSlept well"


def test_nothing_is_sent_unless_comments_are_private(runtime) -> None:
    _notes(runtime)
    icu = FakeIcu(private=False)
    with pytest.raises(icu_notes.NotPrivate):
        icu_notes.push_day(runtime.engine, DAY, icu)
    assert icu.puts == []
    assert icu_notes._push_quietly(runtime.engine, [DAY], icu) == 0


def test_private_comments_are_written_and_never_overwritten(runtime) -> None:
    _notes(runtime)
    icu = FakeIcu(private=True)
    assert icu_notes.push_day(runtime.engine, DAY, icu) == "sent"
    assert icu.puts == [{"comments": "Calf tight [pain]\nSlept well"}]
    assert icu_notes.push_day(runtime.engine, DAY, icu) == "same"
    # A later note replaces only what VibeSynergy sent itself.
    journal.add_note(runtime.engine, text="Legs heavy", start=DAY, source="portal")
    assert icu_notes.push_day(runtime.engine, DAY, icu) == "sent"
    assert icu.wellness["comments"].endswith("Legs heavy")
    # Edited on intervals.icu: kept.
    icu.wellness["comments"] = "Written on intervals.icu"
    journal.add_note(runtime.engine, text="More", start=DAY, source="portal")
    assert icu_notes.push_day(runtime.engine, DAY, icu) == "kept"
    assert icu.wellness["comments"] == "Written on intervals.icu"


def test_existing_comment_is_kept(runtime) -> None:
    _notes(runtime)
    icu = FakeIcu(private=True, comments="My own words")
    assert icu_notes.push_day(runtime.engine, DAY, icu) == "kept"
    assert icu.puts == []


def test_retry_only_when_switched_on(runtime) -> None:
    _notes(runtime)
    icu = FakeIcu(private=True)
    assert icu_notes.retry_recent(runtime.engine, runtime.settings.data_dir, DAY, icu) == 0
    prefs.save(runtime.settings.data_dir, {icu_notes.PREF: True})
    assert icu_notes.retry_recent(runtime.engine, runtime.settings.data_dir, DAY, icu) == 1
    assert icu_notes.retry_recent(runtime.engine, runtime.settings.data_dir, DAY, icu) == 0
