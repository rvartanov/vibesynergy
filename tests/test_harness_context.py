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
    assert '<context name="safety" v="4">' in text


def test_contract_issues():
    """The second net after the write check: what only the run knows (#88)."""
    good = {
        "entry_type": "morning",
        "text": "kort",
        "for_date": DAY,
        "meta": {
            "harness": {"via": "runner", "task": "morning"},
            "safety": {"level": "ok", "colour": "green", "reasons": ["x"]},
        },
    }
    assert runner.contract_issues("morning", good, DAY) == []
    long = {**good, "text": "woord " * 200}
    assert runner.contract_issues("morning", long, DAY) == ["200 words, contract is 70"]
    other_day = {**good, "for_date": date(2026, 10, 31)}
    assert runner.contract_issues("morning", other_day, DAY) == [
        "for_date 2026-10-31 instead of 2026-11-01"
    ]
    assert runner.contract_issues("morning", {**good, "meta": {}}, DAY) == [
        "not written under this run's harness",
        "no safety outcome (level, colour, reasons)",
    ]
    assert runner.contract_issues("weekly", {**good, "text": ""}, DAY) == [
        "entry_type morning instead of note",
        "no summary",
        "not written under this run's harness",
    ]


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


# --- the activity block (#158) -------------------------------------------------------------

OWN = "Toch nog een trail gelopen.\nPrachtig weer en lekker gelopen."
BLOCK = (
    "[/*]\n-Persoonlijke AI inzicht: oude regel.\n-Stats:\n  📊 Training load: 177\n"
    "[Generated with VibeSynergy */]"
)


def _brick_with_long_garmin_extra(rt):
    from dataclasses import replace

    from test_evidence import START, activity, put

    from vibesynergy.model import ActivityLap, ActivityZoneTime
    from vibesynergy.query import journal

    noise = {
        "userRoles": ["SCOPE_GOLF_API_READ", "SCOPE_CONNECT_WRITE"] * 40,
        "ownerProfileImageUrlLarge": "https://example.invalid/profile-large.png",
        "ownerFullName": "Some Owner",
        "ownerDisplayName": "owner-display",
        "splitSummaries": [{"splitType": f"S{i}", "duration": i * 1.5} for i in range(120)],
        "description": BLOCK,
        "rpe": 30,
        "feel": 50,
    }
    garmin = replace(activity(), name="Ochtendsessie trailrunning", extra=noise)
    laps = [
        ActivityLap("1", i, elapsed_s=300, distance_m=1000, avg_hr=130 + i, avg_power=250,
                    avg_speed=3.3, extra={"intensityType": "ACTIVE", "lengthDTOs": [1] * 50})
        for i in range(1, 31)
    ]  # fmt: skip
    zones = [ActivityZoneTime("1", "hr", z, 120.0 * z, low=100 + 10 * z) for z in range(1, 6)]
    put(rt, "garmin", garmin, *laps, *zones)
    strava = replace(
        activity(), source_id="2", name="Eigen titel", fit_id=None,
        extra={"description": f"{OWN}\n\n{BLOCK}", "private": False},
    )  # fmt: skip
    put(rt, "strava", strava)
    journal.add_note(rt.engine, text="Linkerkuit iets stijf na afloop.", start=START.date(),
                     activity_id="garmin:1")  # fmt: skip
    return "garmin:1"


def test_activity_block_carries_the_own_words_not_the_provider_extra(rt):
    group = _brick_with_long_garmin_extra(rt)
    found = context.preload(rt, "insight", DAY, activity_ids=[group])
    text = found["text"]
    block = text[text.index('<context name="activity" v="2">') :]
    block = block[: block.index("</context>")]
    assert "Prachtig weer en lekker gelopen." in block
    assert "Linkerkuit iets stijf na afloop." in block
    assert "Eigen titel" in block
    for gone in ("userRoles", "ownerProfileImageUrl", "ownerFullName", "splitSummaries",
                 "lengthDTOs", "oude regel", "[/*]", "ingekort"):  # fmt: skip
        assert gone not in block, gone
    # The athlete's words come before the numbers, and the effort has its label.
    assert block.index("Prachtig weer") < block.index('"summary"')
    assert '"rpe": {"value": 3' in block and '"label"' in block
    assert '"intensity": "ACTIVE"' in block and '"zones"' in block
    report = next(b for b in found["blocks"] if b["name"] == "activity")
    assert report["ok"] and report["chars"] <= context.ACTIVITY_BUDGET


def test_the_own_text_drops_only_vibesynergys_block():
    assert context.own_text(f"{OWN}\n\n{BLOCK}") == OWN
    assert context.own_text(BLOCK) is None
    assert context.own_text(f"{BLOCK}\n\nOnder het blok geschreven.") == (
        "Onder het blok geschreven."
    )


def test_a_brick_of_five_fits_and_the_own_words_are_never_cut():
    own = {"sources": [{"source": "strava", "title": "t", "description": "w" * 1500}],
           "notes": ["n" * 300]}  # fmt: skip
    laps = [{"i": i, "duration": "5:00", "distance": "1.00 km", "avg_hr": 140} for i in range(60)]
    views = [
        {"id": f"garmin:{n}", "own_words": own, "summary": {"sport": "running"},
         "laps": laps, "zones": {"hr": [{"zone": 1, "s": 10}]}}
        for n in range(5)
    ]  # fmt: skip
    fitted = context.fit_activities(views, context.ACTIVITY_BUDGET)
    assert all(v["own_words"] == own for v in fitted)
    assert all(v.get("laps_total", 0) == 60 for v in fitted)
    rendered = context._render(context.BLOCKS["activity"], fitted)
    assert "w" * 1500 in rendered and "ingekort" not in rendered
