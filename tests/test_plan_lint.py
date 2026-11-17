# ruff: noqa: RUF001  (rule texts use the typographic marks of the knowledge base)
"""Consistency layer 1 (#60): the plan checks, on the real cases of 17-11-2026."""

from __future__ import annotations

from datetime import date

import pytest

from vibesynergy.query import knowledge, plan_lint, plan_model

# The strength session of 17-11 as it was: written to come after the run test.
OLD_STRENGTH = """Maximale kracht, instap. Na de looptest.

Main set
- 45m free

[garmin]
warmup time=5m "5' rustig fietsen of roeien"
CORE/INCHWORM sets=1 reps=6 rest=30s weight=bw "Inchworm"
SQUAT/BARBELL_BACK_SQUAT sets=1 reps=8 rest=60s weight=20kg "Opwarmset lege stang"
SQUAT/BARBELL_BACK_SQUAT sets=3 reps=6 rest=180s weight=65kg "Back squat 65 kg"
"""
NEW_STRENGTH = """Maximale kracht, instap.

Main set
- 50m free

[garmin]
warmup time=6m "6' rustig fietsen of roeien"
CORE/INCHWORM sets=1 reps=6 weight=bw "Inchworm"
HIP_RAISE/HIP_RAISE sets=1 reps=10 weight=bw "Bilbrug"
WARM_UP/STRETCH_LUNGING_HIP_FLEXOR sets=1 time=30s side=both "Heupopener"
SQUAT/BARBELL_BACK_SQUAT sets=1 reps=8 rest=60s weight=20kg "Opwarmset lege stang"
SQUAT/BARBELL_BACK_SQUAT sets=3 reps=6 rest=180s weight=65kg "Back squat 65 kg"
"""
RUN_TEST = "Looptest.\n\nWarmup\n- 12m Z1 HR\n\nMain set\n- 30m Z4 HR"


@pytest.mark.parametrize(
    ("name", "minutes"),
    [
        ("Kracht MS-instap 45' (squat)", 45),
        ("Training geven Loopgroep Leeuwarden 1u15", 75),
        ("Rustige rit Z2 1u", 60),
        ("Zwemmen duur 35'", 35),
        ("Looptest 30'", None),  # the test part, not the session
        ("FTP-test 20'", None),
        ("Intervallen 6x3'", None),
        ("Training geven Rûnom Mantgum - 19-11", None),
    ],
)
def test_name_minutes(name: str, minutes: int | None) -> None:
    assert plan_lint.name_minutes(name) == minutes


def _plan(runtime) -> None:
    plan_model.create_plan(
        runtime.engine,
        name="Fry 27",
        start=date(2026, 11, 2),
        end=date(2027, 7, 3),
        reason="r",
        author="athlete",
    )


def _set(runtime, day: date, name: str, sport: str, text: str, slot: str) -> dict:
    return plan_model.set_session(
        runtime.engine,
        session_id=None,
        day=day,
        name=name,
        sport=sport,
        description=text,
        reason="r",
        author="Claude",
        slot=slot,
    )


def _codes(found: list[dict]) -> list[tuple[str, str]]:
    return sorted((f["code"], f["name"]) for f in found)


def test_the_17_11_gaps_are_found_and_the_fix_is_clean(runtime) -> None:
    _plan(runtime)
    day = date(2026, 11, 17)
    out = _set(runtime, day, "Kracht MS-instap 45' (squat)", "WeightTraining", OLD_STRENGTH, "s")
    # Without the run test that day: the warm-up is too short and the text points nowhere.
    assert _codes(out["checks"]) == [
        ("stale_reference", "Kracht MS-instap 45' (squat)"),
        ("strength_warmup", "Kracht MS-instap 45' (squat)"),
    ]
    warmup = next(f for f in out["checks"] if f["code"] == "strength_warmup")
    assert warmup["rule"] == "STR-4" and "activation" in warmup["message"]
    test = _set(runtime, day, "Looptest 30'", "Run", RUN_TEST, "run-test")
    # Adding the run test names the session that mentions it, for a look again.
    assert _codes(test["checks"]) == [
        ("review_context", "Kracht MS-instap 45' (squat)"),
        ("strength_warmup", "Kracht MS-instap 45' (squat)"),
    ]
    # The run test moves a week on (16-11): the strength session is named for a look again.
    moved = plan_model.move_session(
        runtime.engine,
        session_id=test["session_id"],
        day=date(2026, 11, 24),
        reason="na de verkoudheid",
        author="Claude",
    )
    codes = _codes(moved["checks"])
    assert ("review_context", "Kracht MS-instap 45' (squat)") in codes
    assert ("stale_reference", "Kracht MS-instap 45' (squat)") in codes
    # The fix of 17-11: a full warm-up and a name that matches the length.
    fixed = _set(runtime, day, "Kracht MS-instap 45' (squat)", "WeightTraining", NEW_STRENGTH, "s")
    assert _codes(fixed["checks"]) == [("name_duration", "Kracht MS-instap 45' (squat)")]
    named = _set(runtime, day, "Kracht MS-instap 50' (squat)", "WeightTraining", NEW_STRENGTH, "s")
    assert named["checks"] == []
    assert plan_lint.lint(runtime.engine, date(2026, 11, 16), date(2026, 11, 30)) == []


def test_the_day_after_points_at_the_day_before(runtime) -> None:
    _plan(runtime)
    _set(runtime, date(2026, 11, 24), "Looptest 30'", "Run", RUN_TEST, "run-test")
    ride = "Rustige rit, de dag na de looptest.\n\nMain set\n- 60m Z2 HR"
    out = _set(runtime, date(2026, 11, 25), "Rustige rit Z2 1u", "Ride", ride, "ride")
    assert out["checks"] == []
    later = _set(runtime, date(2026, 11, 27), "Rustige rit Z2 1u", "Ride", ride, "ride")
    assert _codes(later["checks"]) == [("stale_reference", "Rustige rit Z2 1u")]


def test_a_skipped_session_flags_the_text_that_needs_it(runtime) -> None:
    _plan(runtime)
    day = date(2026, 11, 24)
    test = _set(runtime, day, "Looptest 30'", "Run", RUN_TEST, "run-test")
    _set(
        runtime,
        day,
        "Kracht MS 55' (squat)",
        "WeightTraining",
        NEW_STRENGTH.replace(
            "Maximale kracht, instap.", "Maximale kracht. Na de looptest."
        ).replace("- 50m free", "- 55m free"),
        "s",
    )
    skipped = plan_model.skip_session(
        runtime.engine, session_id=test["session_id"], reason="ziek", author="Claude"
    )
    assert ("review_context", "Kracht MS 55' (squat)") in _codes(skipped["checks"])


def test_the_dashboard_lists_plan_checks(runtime) -> None:
    from vibesynergy.i18n import translator
    from vibesynergy.query import dashboard

    _plan(runtime)
    _set(
        runtime,
        date(2026, 11, 17),
        "Kracht MS-instap 45' (squat)",
        "WeightTraining",
        OLD_STRENGTH,
        "s",
    )
    found = dashboard._plan_checks(runtime.engine, date(2026, 11, 16), translator("nl"))
    texts = [f["message"] for f in found]
    assert any("zonder volledige warming-up (STR-4)" in t for t in texts)
    assert all(f["area"] == "plan" and f["level"] == "warning" for f in found)


# -- knowledge base: a new rule under a taken id ------------------------------------------


def test_an_unrelated_text_does_not_replace_an_existing_rule(runtime) -> None:
    knowledge.upsert_article(
        runtime.engine,
        slug="evidence-concurrent-strength",
        title="Concurrent strength",
        body_md="Kracht naast duur.",
        topic="concurrent-strength",
        author="client:coach",
        rules=[
            {
                "id": "STR-3",
                "text": "Altijd kuit- en voetwerk, 2×/week: zware kuitheffen met gestrekte en "
                "gebogen knie (3 × 8–15, langzaam), balans op één been en voetspieren.",
                "grade": "C",
                "active": True,
            }
        ],
    )
    warmup = (
        "Elke krachtsessie begint met een volledige warming-up: 5–8 min rustig cardio, 2–3 "
        "oefeningen activatie en mobiliteit, dan oplopende opwarmsets."
    )
    with pytest.raises(ValueError, match=r"STR-3 already exists.*STR-4"):
        knowledge.propose_change(
            runtime.engine,
            kind="rule",
            target="STR-3",
            rationale="nieuwe regel",
            author="client:coach",
            changes={"text": warmup},
        )
    # A revision of the same rule goes through, and so does a deliberate replacement.
    revised = knowledge.propose_change(
        runtime.engine,
        kind="rule",
        target="STR-3",
        rationale="minder herhalingen",
        author="client:coach",
        changes={
            "text": "Altijd kuit- en voetwerk, 2×/week: zware kuitheffen met gestrekte en "
            "gebogen knie (3 × 8–12, langzaam), balans op één been."
        },
    )
    assert revised["status"] == "pending"
    replaced = knowledge.propose_change(
        runtime.engine,
        kind="rule",
        target="STR-3",
        rationale="bewust vervangen",
        author="client:coach",
        changes={"text": warmup},
        replace=True,
    )
    assert replaced["status"] == "pending"
    new = knowledge.propose_change(
        runtime.engine,
        kind="rule",
        target="STR-4",
        rationale="nieuwe regel",
        author="client:coach",
        changes={"text": warmup, "article_slug": "evidence-concurrent-strength"},
    )
    assert new["base_version"] == 0
