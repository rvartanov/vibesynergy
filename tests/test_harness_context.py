"""Harness context blocks: every chain name has a block, each stays within budget
and a failing block never sinks the run."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from vibesynergy.coach_runner import context, model, registry, runner
from vibesynergy.runtime import Runtime

DAY = date(2026, 11, 1)


@pytest.fixture
def rt(runtime):
    rt = Runtime.open(runtime.settings)
    yield rt
    rt.close()


def test_every_chain_name_has_a_block():
    for task in registry.TASKS:
        for name in registry.chain_for(task.id):
            assert name in context.BLOCKS, (task.id, name)


@pytest.mark.parametrize("task_id", registry.live())
def test_preload_builds_each_block_within_budget(rt, monkeypatch, task_id):
    monkeypatch.setitem(
        context.BLOCKS, "weather", context.Block("weather", "Weer", 100, lambda *a: {"days": []})
    )
    found = context.preload(rt, task_id, DAY)
    names = [b["name"] for b in found["blocks"]]
    assert names == list(registry.chain_for(task_id))
    for block in found["blocks"]:
        assert block["ok"], block
        assert block["chars"] <= context.BLOCKS[block["name"]].budget + 60
    assert found["text"].startswith('<context name="athlete"')


def test_a_failing_block_is_reported_not_raised(rt, monkeypatch):
    def boom(rt, day, cache):
        raise RuntimeError("down")

    monkeypatch.setitem(context.BLOCKS, "weather", context.Block("weather", "Weer", 100, boom))
    found = context.preload(rt, "morning", DAY)
    weather = next(b for b in found["blocks"] if b["name"] == "weather")
    assert weather["ok"] is False
    assert "Niet beschikbaar (RuntimeError)" in found["text"]


def test_long_blocks_are_cut_to_budget():
    block = context.Block("x", "X", 10, lambda *a: None)
    assert context._render(block, "a" * 50).startswith("a" * 10 + " …")


def test_start_message_carries_the_context(rt, monkeypatch):
    monkeypatch.setitem(
        context.BLOCKS, "weather", context.Block("weather", "Weer", 100, lambda *a: "zonnig")
    )
    text = runner.start_message(rt, "morning", DAY)
    assert '<context name="weather" v="1">\nWeer: zonnig' in text


def test_contract_issues():
    assert runner.contract_issues("morning", {"entry_type": "morning", "text": "kort"}) == []
    long = {"entry_type": "morning", "text": "woord " * 200}
    assert runner.contract_issues("morning", long) == ["200 words, contract is 70"]
    assert runner.contract_issues("weekly", {"entry_type": "morning", "text": ""})


def test_no_tools_model_is_refused(monkeypatch):
    monkeypatch.setattr(runner, "model_capabilities", lambda provider, name: set())
    with pytest.raises(runner.RunnerError, match="lacks what morning needs"):
        cfg = SimpleNamespace(tasks={})
        runner._loop(None, cfg, {"model": "m"}, "morning", DAY, None, object(), None)
    assert model.capabilities("local", "any") == {"tool_use"}


def test_volume_leaves_the_multisport_holder_out():
    """the parts count, the holder not again (a brick was counted twice in context)."""
    found = {
        "buckets": [
            {"period": "2026-11-02", "sport": "running", "count": 1, "moving_s": 1200},
            {"period": "2026-11-02", "sport": "multi_sport", "count": 1, "moving_s": 7900},
        ]
    }
    assert context._volume(found) == [
        {"period": "2026-11-02", "sport": "running", "n": 1, "h": 0.33, "km": 0.0}
    ]


def test_long_step_text_and_daily_series_are_shortened():
    assert context._short("x" * 900).endswith(" …") and len(context._short("x" * 900)) < 710
    points = [{"date": f"2026-11-0{i}", "value": i} for i in range(1, 6)]
    assert context._series_ends(points) == {"from": points[0], "to": points[-1], "n": 5}
