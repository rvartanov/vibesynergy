"""Every planned workout is complete on its own (#192): no "zelfde drills als vorige week"."""

from __future__ import annotations

from datetime import date

import pytest

from vibesynergy.query import plan_builder, plan_lint, self_contained, workout_library
from vibesynergy.query.workout_model import parse_intervals

# A technique swim as the AI coach could write it: a reference and nameless steps.
NAMELESS = """Techniek, rustig. Zelfde drills als vorige week.

- Licht 200mtr Z1 Pace

6x
- Licht 50mtr Z1 Pace
- 20s rest
"""
NAMED = NAMELESS.replace("6x\n- Licht 50mtr", "6x\n- Zijligging · vinnen 50mtr")

# Made-up session texts in the style of a real plan, with phrases that look like a
# reference but are none: "de dag na de test", "t.o.v. vorige week", "zoals in de wedstrijd".
NORMAL_TEXTS = [
    "Rustige duur de dag na de looptest, dus niets forceren. Gelijkmatig, lange slag. "
    "Zwem op gevoel: Licht = RPE 3-4, Middel = RPE 5-6.",
    "Test, ochtend. Na het inzwemmen 300 m voluit en gelijkmatig, 4' rust, dan 100 m "
    "voluit. Voel je je niet fris, dan 30' rustig en de test schuift.",
    "Hoge cadans, zittend. Opbouw t.o.v. vorige week (3x8' → 3x10'). Gaat het niet, dan "
    "de hele rit 50' op 55-65%.",
    "Rit na het zwemmen, op een glooiend parcours. Twee blokken op racetempo; eet en drink "
    "zoals in de wedstrijd (elk half uur).",
    "Groepsles, geen doelen. Week 6: korte versnellingen (20-40 s) op eigen tempo.",
    "Kracht 2x (ma 3x8-10 RPE 7; do zelfde gewicht, RPE 6). Gewicht per oefening noteren.",
    "Techniek, focus deze week: ademhaling. Per drill 2x50; daarna de 100's rustig.",
    # "vorige week" alone is about the week, not about this session's content.
    "Rustig aan na de zware vorige week.",
    "Easy after last week's race; same effort all the way.",
    "Easy, as last week was hard.",
]

REFERENCES = [
    "Zelfde drills als vorige week.",
    "Dezelfde opbouw als dinsdag.",
    "Hetzelfde als vorige keer.",
    "Techniek zoals in week 4.",
    "Net zoals vorige week.",
    "Zelfde als 3-2, nog geen opbouw.",
    "Same drills as last week.",
    "The same set as last time.",
    "As in week 4, easy.",
    "Like last week.",
]


@pytest.mark.parametrize("text", REFERENCES)
def test_references_are_found(text) -> None:
    assert self_contained.references(text)


@pytest.mark.parametrize("text", NORMAL_TEXTS)
def test_no_false_positives(text) -> None:
    assert self_contained.references(text) == []


def test_nameless_steps_are_only_intensity_words() -> None:
    assert self_contained.nameless(parse_intervals(NAMELESS).steps)
    assert not self_contained.nameless(parse_intervals(NAMED).steps)
    # An exact target (power, cadence) is the step's content on the watch.
    ride = parse_intervals("Zelfde als vorige week.\n\n4x\n- 6m 75-85% 50-60rpm\n- 3m 55%\n")
    assert not self_contained.nameless(ride.steps)


def _plan(runtime):
    from vibesynergy.query import plan_model

    plan_model.create_plan(
        runtime.engine,
        name="Test",
        start=date(2026, 11, 23),
        end=date(2026, 12, 6),
        reason="test",
        author="athlete",
    )
    return plan_model


def _set(plan_model, runtime, text):
    return plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=date(2026, 11, 30),
        name="Zwemmen techniek",
        sport="Swim",
        description=text,
        reason="Horizon aanvullen",
        author="client:coach-runner",
    )


def test_a_reference_with_nameless_steps_is_refused(runtime) -> None:
    plan_model = _plan(runtime)
    with pytest.raises(ValueError, match="incomplete_reference"):
        _set(plan_model, runtime, NAMELESS)


def test_a_reference_with_named_steps_is_a_warning(runtime) -> None:
    plan_model = _plan(runtime)
    out = _set(plan_model, runtime, NAMED)
    found = [c for c in out["checks"] if c["code"] == "incomplete_reference"]
    assert len(found) == 1 and found[0]["level"] == "warning"
    assert found[0]["params"]["phrase"] == "Zelfde drills als vorige week"
    assert "vorige week" in found[0]["message"]


def test_a_complete_session_has_no_check(runtime) -> None:
    plan_model = _plan(runtime)
    out = _set(plan_model, runtime, NAMED.replace(" Zelfde drills als vorige week.", ""))
    assert not [c for c in out["checks"] if c["code"] == "incomplete_reference"]


def test_the_ai_coach_reads_the_refusal(runtime) -> None:
    """The refusal reaches the AI coach's tool call as its text, so it can write again; it
    comes before the approval gate, so it is never stored as a proposal either."""
    import anyio
    from mcp import Client

    from vibesynergy.mcp.server import build_server

    _plan(runtime)

    async def go():
        async with Client(build_server(runtime)) as client:
            return await client.call_tool(
                "vibesynergy_plan_session_set",
                {
                    "reason": "test",
                    "date": "2026-11-30",
                    "name": "Zwemmen",
                    "sport": "Swim",
                    "description": NAMELESS,
                },
            )

    result = anyio.run(go)
    assert result.is_error and "incomplete_reference" in result.content[0].text
    assert "write each drill and step out by name" in result.content[0].text


def _clean(text: str) -> None:
    parsed = parse_intervals(text)
    assert self_contained.finding(parsed.intro, parsed.steps, parsed.garmin_block) is None, text


@pytest.mark.parametrize("template", [t.id for t in workout_library.TEMPLATES])
def test_every_builder_template_is_complete(template) -> None:
    weeks = range(1, 54) if template == "swim_technique" else [None]
    for lang in ("nl", "en"):
        for week in weeks:
            params = {"week": week} if week else {}
            _clean(workout_library.render(template, lang, None, **params)["text"])


@pytest.mark.parametrize("kind", ["load", "recovery"])
def test_the_builder_writes_every_session_out(kind) -> None:
    monday = date(2026, 11, 30)  # a recovery week's technique swim: its own drills, in full
    found = plan_builder._sessions(
        monday, "base", kind, {"swim": 120.0, "ride": 180.0, "run": 180.0}, {"tests": True}
    )
    assert found
    for session in found:
        _clean(session["description"])
    swims = [s for s in found if s["template"] == "swim_technique"]
    assert swims and "Drills:" in swims[0]["description"]


def test_a_plan_finding_is_listed(runtime) -> None:
    plan_model = _plan(runtime)
    ident = _set(plan_model, runtime, NAMED)["session_id"]
    found = plan_lint.lint(runtime.engine, date(2026, 11, 23), date(2026, 12, 6))
    assert [f["code"] for f in found if f["session_id"] == ident] == ["incomplete_reference"]
