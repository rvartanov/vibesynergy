"""The coach's verdict per session."""

from __future__ import annotations

from datetime import date

import pytest

from vibesynergy.query import coach_workouts


def test_sessions_are_stored_with_known_decisions(runtime) -> None:
    row = coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary="Rustige dag: houd de duurloop, kracht lichter.",
        idempotency_key="morning-2026-11-06",
        for_date=date(2026, 11, 6),
        sessions=[
            {"title": "Duurloop Z2 40'", "decision": "keep"},
            {"title": "Kracht MT-A", "decision": "adjust", "note": " RDL 50 kg "},
        ],
    )
    assert row["meta"]["sessions"] == [
        {"title": "Duurloop Z2 40'", "decision": "keep"},
        {"title": "Kracht MT-A", "decision": "adjust", "note": "RDL 50 kg"},
    ]
    with pytest.raises(ValueError, match="keep, adjust"):
        coach_workouts.coach_write(
            runtime.engine,
            entry_type="morning",
            summary="x",
            idempotency_key="morning-x",
            for_date=date(2026, 11, 6),
            sessions=[{"title": "Rit", "decision": "maybe"}],
        )
