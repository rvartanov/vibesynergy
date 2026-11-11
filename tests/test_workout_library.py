"""The workout library: templates that render to plan text the rest already reads."""

from __future__ import annotations

import pytest

from vibesynergy.query import garmin_endurance as ge
from vibesynergy.query import garmin_strength as gs
from vibesynergy.query import workout_library as wl
from vibesynergy.query.workout_model import parse_intervals

RELATIVE = {"pct_ftp", "pct_lthr", "hr_zone", "pace_zone", "none"}


def _targets(steps):
    for step in steps:
        if step["kind"] == "repeat":
            yield from _targets(step["steps"])
        elif "target" in step:
            yield step["target"]["type"]


@pytest.mark.parametrize("template", [t.id for t in wl.TEMPLATES if t.sport != "strength"])
def test_every_template_renders_to_text_that_parses_back_and_reaches_garmin(template) -> None:
    for lang in ("nl", "en"):
        out = wl.render(template, lang)
        parsed = parse_intervals(out["text"])
        assert not parsed.unparsed and parsed.steps, (template, lang)
        assert set(_targets(parsed.steps)) <= RELATIVE  # ICU-6: never absolute watts or bpm
        assert out["seconds"] > 0 and "{" not in out["name"]
    values = {"ftp": 265, "lthr": 162, "max_hr": 179, "css": 135}
    build = {"run": ge.build_run, "ride": ge.build_ride}.get(out["sport"])
    workout = build("x", parsed, values) if build else ge.build_swim("x", parsed, values["css"])
    assert workout["workoutSegments"][0]["workoutSteps"]


def test_parameters_scale_the_workout_and_are_checked() -> None:
    short = wl.render("ride_force", reps=4, work_min=5)
    longer = wl.render("ride_force", reps=4, work_min=6)
    assert short["name"] == "Kracht op de fiets 4x5'" and longer["name"].endswith("4x6'")
    assert "- 6m 75-85% 50-60rpm" in longer["text"]
    assert wl.render("run_easy", "en", minutes=30)["name"] == "Short easy run 30'"
    with pytest.raises(ValueError, match="takes minutes"):
        wl.render("run_easy", watts=200)
    with pytest.raises(ValueError, match="positive"):
        wl.render("run_easy", minutes=0)
    with pytest.raises(ValueError, match="unknown template"):
        wl.render("run_marathon")


def test_catalogue_names_an_easier_alternative_that_exists() -> None:
    catalogue = wl.catalogue("en")
    ids = {t["id"] for t in catalogue}
    for t in catalogue:
        assert t["easier"] is None or t["easier"] in ids
        assert t["load"] in ("easy", "moderate", "hard")
    hard = [t for t in catalogue if t["load"] == "hard" and "test" not in t["phases"]]
    assert all(t["easier"] for t in hard)  # a hard day always has a fallback (PLAN-1)


@pytest.mark.parametrize("template", [t.id for t in wl.TEMPLATES if t.sport == "strength"])
def test_strength_templates_are_valid_garmin_blocks_without_weights(template) -> None:
    out = wl.render(template, minutes=45)
    block = gs.parse_block(out["text"])  # raises on an exercise outside Garmin's library
    assert block is not None and len(block.steps) >= 5
    assert "weight=" not in out["text"]  # the coach adds weights from the athlete's history
    assert out["type"] == "WeightTraining" and out["text"].endswith("- 45m free")
    assert out["seconds"] == 45 * 60


def test_run_names_say_the_kind_of_run() -> None:
    """Herstelloop, Korte duurloop, Lange duurloop."""
    assert wl.render("run_recovery", "nl", minutes=25)["name"] == "Herstelloop 25'"
    assert wl.render("run_easy", "nl", minutes=30)["name"] == "Korte duurloop 30'"
    assert wl.render("run_long", "nl", minutes=110)["name"] == "Lange duurloop 110'"


def test_no_deadlift_from_the_floor() -> None:
    """The RDL, not the deadlift from the floor: easier on the lower back."""
    for template in wl.TEMPLATES:
        if template.sport != "strength":
            continue
        assert "BARBELL_DEADLIFT" not in wl.render(template.id, "nl")["text"], template.id
