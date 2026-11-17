"""A night the watch cut short (#53): asked, not told; marked, it leaves the advice."""

from __future__ import annotations

from datetime import date

from vibesynergy.query import journal, safety_floor

DAY = date(2026, 11, 17)
SHORT = 3 * 3600 + 29 * 60


def _signals(checkin: dict | None) -> dict:
    return {"checkin": checkin, "sleep_s": SHORT, "readiness": 3, "hrv_z": -1.2}


def test_a_short_night_with_a_good_check_in_is_suspect_not_fact() -> None:
    assert journal.sleep_check({"sleep_quality": 5}, SHORT) == {
        "state": "suspect",
        "measured_s": SHORT,
        "sleep_quality": 5,
    }
    assert journal.sleep_check({"sleep_quality": 3}, SHORT) is None  # check-in agrees
    assert journal.sleep_check({"sleep_quality": 5}, 6 * 3600) is None  # not that short
    assert journal.sleep_check(None, SHORT) is None
    marked = journal.sleep_check({"sleep_quality": 5, "sleep_incomplete": True}, SHORT)
    assert marked == {"state": "incomplete", "measured_s": SHORT}


def test_a_marked_night_drops_short_sleep_and_its_readiness_not_hrv() -> None:
    plain = safety_floor.evaluate(_signals({"sleep_quality": 5}))
    assert {"short_sleep", "low_readiness", "low_hrv"} <= set(plain["rules"])
    marked = safety_floor.evaluate(_signals({"sleep_quality": 5, "sleep_incomplete": True}))
    assert "short_sleep" not in marked["rules"] and "low_readiness" not in marked["rules"]
    assert "low_hrv" in marked["rules"]  # HRV is its own measurement


def test_marking_a_night_is_stored_undoable_and_keeps_the_check_in(runtime) -> None:
    engine = runtime.engine
    journal.set_checkin(engine, DAY, sleep_quality=5, soreness=1)
    row = journal.set_sleep_incomplete(engine, DAY, True)
    assert row["sleep_incomplete"] is True and row["sleep_quality"] == 5
    assert journal.context(engine, DAY)["checkin"]["sleep_incomplete"] is True
    # Changing a score later keeps the mark.
    assert journal.set_checkin(engine, DAY, mood=4)["sleep_incomplete"] is True
    row = journal.set_sleep_incomplete(engine, DAY, False)
    assert row["sleep_incomplete"] is False and row["soreness"] == 1
    assert "sleep_incomplete" not in journal.context(engine, DAY)["checkin"]
    # A day without a check-in can be marked too.
    other = journal.set_sleep_incomplete(engine, date(2026, 11, 16), True)
    assert other["sleep_incomplete"] is True and other["sleep_quality"] is None


def test_explain_shows_the_mark(runtime) -> None:
    from vibesynergy.query import views

    journal.set_sleep_incomplete(runtime.engine, DAY, True)
    assert "marked_incomplete" in views.explain(runtime.engine, "sleep.duration", DAY)
    assert "marked_incomplete" in views.explain(runtime.engine, "recovery.readiness", DAY)
    assert "marked_incomplete" not in views.explain(runtime.engine, "hrv.night_avg", DAY)
