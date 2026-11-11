"""Swim technique drills by weekly focus and the athlete's kit."""

from __future__ import annotations

from vibesynergy.query import swim_drills, workout_library, workout_model


def test_four_week_rotation_and_kit_fallback() -> None:
    assert [swim_drills.focus_for_week(w) for w in (46, 47, 48, 49, 50, 51)] == [
        "balance",
        "catch",
        "open_water",
        "breathing",
        "length",
        "balance",
    ]
    full = swim_drills.drill_cues("length")
    assert "Pull · boei+paddles" in full and "Catch-up · zoomers" in full
    bare = swim_drills.drill_cues("length", equipment=())
    assert "Pull" in bare and "Catch-up" in bare and not any("·" in c for c in bare)
    assert swim_drills.hundred_cue("length", equipment=()) == "Rustig"


def test_technique_workout_names_each_drill_and_round_trips() -> None:
    done = workout_library.render("swim_technique", "nl", week=47)
    text = done["text"]
    assert "focus deze week: catch en trek" in text
    assert text.count("Sculling · boei 50mtr") == 2  # each drill twice
    assert "Middel drill" not in text
    parsed = workout_model.parse_intervals(text)
    assert not parsed.unparsed
    assert (
        workout_model.to_intervals(workout_model.Workout(steps=parsed.steps, intro=parsed.intro))
        == text
    )


def test_every_drill_has_instructions_in_both_languages() -> None:
    """the drills come with their instructions."""
    for drills in swim_drills.DRILLS.values():
        for _nl, _en, _kit, plain_nl, _plain_en in drills:
            assert {"nl", "en"} <= set(swim_drills.HOW[plain_nl]), plain_nl
    for week in range(46, 51):
        text = workout_library.render("swim_technique", "nl", week=week)["text"]
        assert "Drills:\n• " in text
        assert len(workout_model.parse_intervals(text).intro) <= 1024  # Garmin's description
