"""The gear a planned workout needs, as one line at the top of its notes (#112)."""

from __future__ import annotations

from datetime import date

import pytest

from vibesynergy.query import (
    garmin_endurance,
    gear_line,
    plan_builder,
    workout_library,
    workout_model,
)


def _steps(text: str) -> list[dict]:
    return workout_model.parse_intervals(text).steps


def test_gear_is_recognised_from_the_steps() -> None:
    steps = _steps("Main set\n- Pull · boei+paddles 100mtr\n- Zijkick · zoomers 50mtr\n")
    assert gear_line.found(steps, "swim") == ["pull buoy", "paddles", "fins"]
    assert gear_line.line(gear_line.found(steps, "swim"), "nl") == (
        "Materiaal: pullbuoy, paddles, vinnen"
    )
    assert gear_line.line(["pull buoy", "paddles"], "en") == "Gear: pull buoy, paddles"
    kick = _steps("- Kick plank · ademen 50mtr\n- Snorkel 50mtr\n")
    assert gear_line.found(kick, "Swim") == ["snorkel", "kickboard"]
    assert gear_line.found(_steps("- Trail shoes 30m Z2 HR\n"), "run") == ["trail shoes"]


def test_no_false_positives() -> None:
    # Plank is the strength exercise, not a kickboard; "pull" inside a word is not a buoy.
    strength = _steps("- Plank 1m\n- Pullups 2m\n- Pulldown 2m\n")
    assert gear_line.found(strength, "strength") == []
    assert gear_line.found(_steps("- Pull through 10m Z2 HR\n"), "run") == []
    assert gear_line.found(_steps("- Pullover 50mtr\n"), "swim") == []
    assert gear_line.add("Rustig.", _steps("- Plank 1m\n"), "strength") == "Rustig."


def test_no_duplicate_line() -> None:
    steps = _steps("- Pull · boei 100mtr\n")
    once = gear_line.add("Rustig.", steps, "swim")
    assert once == "Materiaal: pullbuoy\nRustig."
    assert gear_line.add(once, steps, "swim") == once
    # A line the AI coach wrote in English counts too, as does one written by hand.
    assert gear_line.add("Gear: pull buoy\nEasy.", steps, "swim", "nl").count(":") == 1
    assert gear_line.add("materiaal: boei", steps, "swim") == "materiaal: boei"


@pytest.mark.parametrize("template", [t for t in workout_library.TEMPLATES])
def test_every_template_that_uses_gear_has_the_line(template) -> None:
    """Every template whose steps name gear, in every swim-drill week, has the gear line
    at the top of its text, in both languages; one without gear has none."""
    weeks = range(1, 6) if template.id == "swim_technique" else [None]
    for week in weeks:
        params = {"week": week} if week else {}
        for lang in ("nl", "en"):
            done = workout_library.render(template.id, lang, None, **params)
            parsed = workout_model.parse_intervals(done["text"])
            gear = gear_line.found(parsed.steps, template.sport)
            if gear:
                assert parsed.intro.splitlines()[0] == gear_line.line(gear, lang)
            else:
                assert not gear_line.has_line(parsed.intro)


def test_the_builder_writes_the_line_in_generated_sessions() -> None:
    monday = date(2026, 11, 16)  # ISO week 47: a drill week with gear
    found = plan_builder._sessions(
        monday, "base", "load", {"swim": 120.0, "run": 180.0}, {"tests": False}
    )
    swims = [s for s in found if s["template"] == "swim_technique"]
    assert swims
    for session in found:
        parsed = workout_model.parse_intervals(session["description"])
        gear = gear_line.found(parsed.steps, session["sport"])
        assert gear_line.has_line(parsed.intro) == bool(gear)
    assert swims[0]["description"].startswith("Materiaal: ")
    # The same week again gives the same text: no second line.
    again = plan_builder._sessions(
        monday, "base", "load", {"swim": 120.0, "run": 180.0}, {"tests": False}
    )
    assert [s["description"] for s in again] == [s["description"] for s in found]


def test_the_line_reaches_the_garmin_workout_description() -> None:
    week = date(2026, 11, 16).isocalendar()[1]
    text = workout_library.render("swim_technique", "nl", None, week=week)["text"]
    workout = garmin_endurance.build_swim("Techniek", workout_model.parse_intervals(text), 1.8)
    assert workout["description"].startswith("Materiaal: ")


def test_a_hand_written_session_gets_the_line_once(runtime) -> None:
    from sqlalchemy import select

    from vibesynergy.query import plan_model
    from vibesynergy.store.schema import plan_session

    plan_model.create_plan(
        runtime.engine,
        name="Test",
        start=date(2026, 11, 23),
        end=date(2026, 11, 29),
        reason="test",
        author="athlete",
    )
    text = "Rustig.\n\n- Pull · boei 400mtr\n- Kick plank 100mtr\n"
    ident = None
    for _ in range(2):  # an edit with the same text: still one line
        ident = plan_model.set_session(
            runtime.engine,
            session_id=ident,
            day=date(2026, 11, 24),
            name="Zwemmen",
            sport="Swim",
            description=text,
            reason="test",
            author="Claude",
        )["session_id"]
        with runtime.engine.connect() as conn:
            row = conn.execute(select(plan_session).where(plan_session.c.id == ident)).one()
        assert row.description == "Materiaal: pullbuoy, plank\nRustig."
        assert plan_model.session_text(dict(row._mapping)).startswith("Materiaal: pullbuoy")
