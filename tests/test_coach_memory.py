"""The coach keeps "what to remember" itself."""

from __future__ import annotations

import pytest

from vibesynergy.query import coach


def _items(runtime) -> list[dict]:
    return coach.state(runtime.engine, "context")["active"]["payload"]["constraints"]


def test_coach_adds_updates_and_retires_its_own_item(runtime) -> None:
    out = coach.memory_write(
        runtime.engine,
        "add",
        reason="Alex said so in a reply of 5-11",
        actor="client:coach",
        kind="commitment",
        text="Geeft donderdag de groepstraining in Dorp",
        days=[4],
    )
    item = _items(runtime)[0]
    assert item["id"] == out["item_id"] and item["source"] == "coach" and item["days"] == [4]

    coach.memory_write(
        runtime.engine,
        "update",
        reason="moved",
        actor="client:coach",
        item_id=item["id"],
        text="Geeft dinsdag en donderdag groepstraining",
        days=[2, 4],
    )
    assert _items(runtime)[0]["days"] == [2, 4]

    coach.memory_write(
        runtime.engine, "retire", reason="stopped", actor="client:coach", item_id=item["id"]
    )
    assert _items(runtime)[0]["active"] is False
    history = coach.state(runtime.engine, "context")["history"]
    assert [h["actor"] for h in history][:3] == ["client:coach"] * 3
    assert history[0]["reason"] == "stopped"


def test_athlete_items_are_theirs(runtime) -> None:
    current = coach.state(runtime.engine, "context")
    coach.apply_active(
        runtime.engine,
        "context",
        {"constraints": [{"id": "mine", "kind": "preference", "text": "Geen zwemmen op zondag"}]},
        expected_version=current["version"],
        actor="portal",
    )
    with pytest.raises(ValueError, match="athlete's own"):
        coach.memory_write(
            runtime.engine, "retire", reason="x", actor="client:coach", item_id="mine"
        )
    with pytest.raises(ValueError, match="why"):
        coach.memory_write(
            runtime.engine, "add", reason=" ", actor="c", kind="preference", text="x"
        )
    coach.memory_write(
        runtime.engine, "add", reason="r", actor="c", kind="equipment", text="Wahoo Kickr"
    )
    assert [i["source"] for i in _items(runtime)] == ["user", "coach"]
