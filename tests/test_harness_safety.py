"""#88: one output contract for every coach, the safety outcome in every task's context,
and the sharper fallback rules (deviation from the plan, measurement gaps, advice branches).

The regression cases are real days from the 90-day review (19-8 to 17-11-2026), rebuilt
with synthetic numbers of the same shape.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import anyio
import pytest
from mcp import Client
from sqlalchemy import select

from vibesynergy.coach_runner import context, contract, prompts, registry
from vibesynergy.core.timeutil import utc_now
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.query import dashboard, journal, measurement, plan, safety_floor
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log, daily_metric, daily_unified, raw_item

DAY = date(2026, 11, 17)
OK = {"level": "ok", "colour": "green", "reasons": ["HRV normaal, check-in goed"]}
FLAG = {"level": "flag", "rules": ["low_hrv", "low_readiness"]}


@pytest.fixture
def rt(runtime):
    rt = Runtime.open(runtime.settings)
    yield rt
    rt.close()


def _b(value, mean, std):
    return {
        "value": value,
        "baseline_mean": mean,
        "baseline_std": std,
        "z": (value - mean) / std,
        "n": 30,
    }


def _recovery(readiness=None, hrv=None, sleep=None):
    return {
        "hrv.night_avg": hrv or _b(50, 50, 5),
        "hr.resting": _b(48, 48, 2),
        "sleep.duration": sleep or _b(8 * 3600, 7.5 * 3600, 1800),
        "recovery.readiness": readiness or _b(70, 60, 10),
        "sleep.score": _b(85, 85, 5),
        "hrv_low_days": 0,
    }


LOAD = {"tsb": -5, "form": "neutral", "acwr": 1.1}


def _plan(*sessions):
    return {
        "today": list(sessions),
        "next_race": {"name": "Ultra 75 km", "days_to_go": 40},
        "plan_phase": {"phase": "build", "days_left": 26},
    }


def _session(title, status="today", **extra):
    return {"title": title, "status": status, **extra}


# --- the output contract: a weaker model's typical mistakes ---------------------------------


def _check(**changes):
    args = {
        "entry_type": "morning",
        "summary": "Houd de duurloop rustig.",
        "for_date": DAY,
        "today": DAY,
        "safety": OK,
        "floor": {"level": "ok", "rules": []},
    }
    return contract.check_write(**{**args, **changes})


def test_a_valid_daily_advice_passes_and_is_cleaned():
    found = _check(safety={**OK, "reasons": ["  HRV normaal  ", ""]})
    assert found == {
        "task": "morning",
        "safety": {"level": "ok", "colour": "green", "reasons": ["HRV normaal"]},
    }


@pytest.mark.parametrize(
    ("safety", "message"),
    [
        (None, "safety is required"),
        ({"colour": "green", "reasons": ["x"]}, "safety.level must be one of"),
        ({"level": "fine", "colour": "green", "reasons": ["x"]}, "safety.level must be one of"),
        ({"level": "ok", "colour": "amber", "reasons": ["x"]}, "colour must be 'green'"),
        ({"level": "refuse", "colour": "amber", "reasons": ["x"]}, "colour must be 'red'"),
        ({"level": "flag", "colour": "amber", "reasons": []}, "at least one short reason"),
        ({"level": "flag", "colour": "amber"}, "at least one short reason"),
        ({**OK, "verdict": "go"}, "unknown safety field(s): verdict"),
    ],
)
def test_a_missing_or_contradicting_safety_outcome_is_refused(safety, message):
    with pytest.raises(contract.ContractError, match="Refused by the output contract") as exc:
        _check(safety=safety)
    assert message in str(exc.value)


def test_milder_than_the_rules_needs_the_judgement_in_words():
    ease = {"level": "ease", "colour": "amber", "reasons": ["HRV laag, check-in goed"]}
    with pytest.raises(contract.ContractError, match="milder than the rules' 'flag'"):
        _check(safety=ease, floor=FLAG)
    weighed = {**ease, "override_reason": "Readiness 11 is van na de training; vanochtend 27."}
    assert _check(safety=weighed, floor=FLAG)["safety"]["override_reason"].startswith("Readiness")
    # Stricter than the rules is always allowed, and so is the rules' own level.
    stop = {"level": "refuse", "colour": "red", "reasons": ["voelt zich ziek"]}
    assert _check(safety=stop, floor=FLAG)["safety"]["level"] == "refuse"
    same = {"level": "flag", "colour": "amber", "reasons": ["HRV laag"]}
    assert _check(safety=same, floor=FLAG)["safety"]["level"] == "flag"


@pytest.mark.parametrize("rule", sorted(safety_floor.HARD_RULES))
def test_the_runner_can_never_soften_illness_and_pain(rule):
    soft = {**OK, "override_reason": "voelt zich prima"}
    floor = {"level": "refuse", "rules": [rule, "low_hrv"]}
    with pytest.raises(contract.ContractError, match="can never be softened"):
        _check(safety=soft, floor=floor, autonomous=True)
    # Over MCP the request is the athlete's own: validity only, so the reason is enough.
    assert _check(safety=soft, floor=floor)["safety"]["override_reason"] == "voelt zich prima"
    with pytest.raises(contract.ContractError, match=r"give safety\.override_reason"):
        _check(safety=OK, floor=floor)


def test_the_wrong_target_day_is_refused():
    with pytest.raises(contract.ContractError, match="is not the day this morning entry"):
        _check(for_date=DAY - timedelta(days=1))
    with pytest.raises(contract.ContractError, match="is not the day this morning entry"):
        _check(for_date=DAY + timedelta(days=1))
    # An evening entry may be about today or tomorrow, nothing else.
    assert _check(entry_type="evening", for_date=DAY + timedelta(days=1))["task"] == "evening"
    with pytest.raises(contract.ContractError, match="2026-11-17 or 2026-11-18"):
        _check(entry_type="evening", for_date=DAY + timedelta(days=2))


def test_a_report_that_is_too_long_is_refused_with_what_to_do():
    with pytest.raises(contract.ContractError, match="has 200 words; the contract is 70"):
        _check(summary="woord " * 200)
    assert _check(summary="woord " * 100)["task"] == "morning"  # within the slack
    with pytest.raises(contract.ContractError, match="the contract is 150"):
        _check(entry_type="note", summary="woord " * 300, safety=None)


def test_every_problem_is_named_at_once():
    with pytest.raises(contract.ContractError) as exc:
        _check(summary=" ", for_date=DAY - timedelta(days=3), safety={"level": "ok"})
    text = str(exc.value)
    assert "summary is required" in text and "is not the day" in text and "colour" in text


def test_the_task_decides_the_contract():
    # A note needs no safety; the same note as the injury check does.
    assert _check(entry_type="note", safety=None) == {"task": None, "safety": None}
    with pytest.raises(contract.ContractError, match="safety is required"):
        _check(entry_type="note", task="injury", safety=None)
    assert _check(entry_type="note", task="checkin")["task"] == "checkin"
    with pytest.raises(contract.ContractError, match="unknown task 'daily'"):
        _check(task="daily")
    with pytest.raises(contract.ContractError, match="writes entry_type 'note', not 'morning'"):
        _check(task="weekly")
    assert registry.daily() == ("morning", "evening", "injury", "checkin")


def test_the_rules_are_versioned():
    """The harness rules cannot change without a new RULES_VERSION: update both together."""
    text = "\n".join(
        (
            prompts.HARNESS_RULES,
            prompts.SAFETY_STEPS,
            prompts.SAFETY_FIELD,
            prompts.RUNNER_RULES,
            prompts.MCP_RULES,
            prompts.INSIGHT_RULES,
        )
    )
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    assert (prompts.RULES_VERSION, digest) == (6, RULES_DIGEST), digest
    assert contract.parse_stamp(contract.stamp(14, 2, 2)) == {
        "harness": contract.HARNESS_VERSION,
        "rules": 2,
        "prompt": "14",
        "context": 2,
    }
    assert contract.parse_stamp("latest") is None


RULES_DIGEST = "3fa2727c175f"


@pytest.mark.parametrize("job", [t for t in registry.live() if t != "insight"])
def test_the_rules_are_numbered_steps_any_model_can_follow(job):
    rules = prompts.rules_for(job, "Alex")
    daily = job in registry.daily()
    steps = [line[:2] for line in rules.splitlines() if line[:2] in ("1.", "2.", "3.", "4.")]
    assert steps[:2] == ["1.", "2."]
    assert ("BEPAAL het veiligheidsniveau" in rules) is daily
    assert ("safety (verplicht)" in rules) is daily
    assert ("WEEG wat de regels niet kunnen wegen" in rules) is daily
    assert f"hooguit {registry.BY_ID[job].contract.max_words} woorden" in rules
    assert "GEWEIGERD?" in rules and "{" not in rules.replace('{"', "").replace("[{", "")
    if daily:  # the example in the rules passes the contract it describes
        example = {
            "level": "ease",
            "colour": "amber",
            "reasons": ["HRV onder normaal, dus niet hard", "Check-in goed en lang geslapen"],
            "override_reason": "Readiness 11 is gemeten na de training; vanochtend was hij 27.",
        }
        assert json.dumps(example["reasons"][0], ensure_ascii=False) in rules
        assert contract.check_safety(example, FLAG)["level"] == "ease"


# --- the same harness over MCP ------------------------------------------------------------


def _mcp(rt, tool, **args):
    async def go():
        tokens = set_connection("client-1", scopes=frozenset({"read", "coach.write"}), role="own")
        try:
            async with Client(build_server(rt)) as client:
                return await client.call_tool(tool, args)
        finally:
            reset_connection(tokens)

    return anyio.run(go)


def _entries(rt):
    with rt.engine.connect() as conn:
        return conn.execute(select(coach_decision_log)).mappings().all()


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr("vibesynergy.mcp.server.today_in", lambda tz: DAY)
    return DAY


def _write(**changes):
    return {
        "entry_type": "morning",
        "summary": "Houd de duurloop rustig.",
        "idempotency_key": "morning-2026-11-17",
        "for_date": DAY.isoformat(),
        "safety": OK,
        **changes,
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"safety": None}, "safety is required"),
        ({"safety": {"level": "ok", "colour": "red", "reasons": ["x"]}}, "colour must be"),
        ({"for_date": "2026-11-10"}, "is not the day this morning entry is about"),
        ({"summary": "woord " * 150}, "the contract is 70"),
        ({"entry_type": "note", "task": "injury", "safety": None}, "safety is required"),
    ],
)
def test_an_mcp_write_that_misses_the_contract_is_refused_and_not_stored(
    rt, today, changes, message
):
    result = _mcp(rt, "vibesynergy_coach_write", **_write(**changes))
    assert result.is_error
    text = result.content[0].text
    assert "Refused by the output contract" in text and message in text
    assert _entries(rt) == []


def test_an_mcp_coach_gets_the_runners_rules_context_and_contract(rt, today):
    found = _mcp(rt, "vibesynergy_coach_prompt", job="morning", include_context=True)
    data = found.structured_content["data"]
    # One source: the rules are the runner's, with the MCP transport note.
    assert data["rules_md"] == prompts.rules_for("morning", via="mcp")
    assert prompts.harness_rules("morning") in prompts.rules_for("morning")  # the runner's
    assert data["contract"]["safety_required"] is True
    assert data["contract"]["target_day"] == "today" and data["contract"]["max_words"] == 70
    assert data["harness"]["stamp"] == prompts.harness_stamp(rt.settings, "morning", rt.engine)
    assert data["harness"]["chain"][:4] == list(registry.BASE_CONTEXT)
    # The same preloaded chain as the runner's start message.
    assert data["context"]["text"] == context.preload(rt, "morning", DAY)["text"]
    assert '<context name="safety" v="2">' in data["context"]["text"]
    plain = _mcp(rt, "vibesynergy_coach_prompt", job="weekly").structured_content["data"]
    assert "context" not in plain and plain["contract"]["safety_required"] is False
    book = _mcp(rt, "vibesynergy_coach_handbook", include_protocol=False)
    harness = book.structured_content["data"]["harness"]
    assert harness["version"] == contract.HARNESS_VERSION
    assert harness["tasks"]["evening"]["target_day"] == "today_or_tomorrow"
    assert harness["daily_advice_tasks"] == list(registry.daily())


def test_an_mcp_entry_records_the_harness_it_was_written_under(rt, today):
    stamp = prompts.harness_stamp(rt.settings, "morning", rt.engine)
    result = _mcp(rt, "vibesynergy_coach_write", **_write(harness=stamp, task="morning"))
    assert not result.is_error
    data = result.structured_content["data"]
    assert data["meta"]["safety"] == OK
    assert data["meta"]["harness"] == {
        "role": "own",  # the athlete's own agent (#118)
        "target": today.isoformat(),  # the day this advice is for (#90)
        "version": contract.HARNESS_VERSION,
        "via": "mcp",
        "task": "morning",
        "stamp": stamp,
        "current": stamp,
        "follows_current": True,
        "rules_floor": data["meta"]["harness"]["rules_floor"],
    }
    assert result.structured_content["meta"]["warnings"] == []
    # Without the stamp, or with an old one, the entry is stored and says so.
    old = _mcp(
        rt,
        "vibesynergy_coach_write",
        **_write(idempotency_key="again", harness="h1.r1.p3.c1"),
    )
    meta = old.structured_content["data"]["meta"]["harness"]
    assert meta["follows_current"] is False and meta["stamp"] == "h1.r1.p3.c1"
    assert "not written under the current harness" in old.structured_content["meta"]["warnings"][0]
    none = _mcp(rt, "vibesynergy_coach_write", **_write(idempotency_key="third"))
    assert none.structured_content["data"]["meta"]["harness"]["stamp"] is None
    # A plain note has no task contract; it still records how it came in.
    note = _mcp(
        rt,
        "vibesynergy_coach_write",
        entry_type="note",
        summary="Los bericht.",
        idempotency_key="note-1",
        for_date="2026-11-01",
    )
    assert note.structured_content["data"]["meta"]["harness"]["task"] is None
    assert note.structured_content["meta"]["warnings"] == []


def test_an_mcp_write_is_checked_for_validity_not_approval(rt, today, monkeypatch):
    """A request through the athlete's own agent is his own: the same form checks as the
    runner, and no approval step. An AI coach acting on its own (the runner, an app with
    another role, #118) is barred from softening illness."""
    from vibesynergy.query import coach_brief

    sick = {"safety": {"level": "refuse", "rules": ["sick"]}}
    monkeypatch.setattr(coach_brief, "today_brief", lambda rt_, day: sick)
    eased = {**OK, "override_reason": "Alex vroeg het zelf: koorts is weg, alleen wandelen."}
    result = _mcp(rt, "vibesynergy_coach_write", **_write(safety=eased))
    assert not result.is_error
    meta = result.structured_content["data"]["meta"]
    assert meta["harness"]["via"] == "mcp" and meta["safety"]["level"] == "ok"
    assert meta["harness"]["rules_floor"] == {"level": "refuse", "rules": ["sick"]}
    # The same write from a runner run is refused.
    token = contract.set_run({"task": "morning", "for_date": DAY, "stamp": "h2.r2.pseed.c2"})
    try:
        refused = _mcp(rt, "vibesynergy_coach_write", **_write(safety=eased, idempotency_key="r"))
    finally:
        contract.reset_run(token)
    assert refused.is_error and "can never be softened" in refused.content[0].text


# --- safety in the context of every task --------------------------------------------------


def _seed_short_night(rt, day=DAY, sleep_s=4.5 * 3600):
    """Twelve ordinary days, then a night of 4 h 30: the floor fires on short sleep. No
    injury is logged."""
    now = utc_now()
    rows = []
    for back in range(12):
        when = day - timedelta(days=back)
        for metric, value, unit in (
            ("sleep.duration", sleep_s if back == 0 else 8 * 3600, "s"),
            ("recovery.readiness", 60, "score"),
            ("hrv.night_avg", 55, "ms"),
            ("hr.resting", 44, "bpm"),
        ):
            rows.append(
                {
                    "subject_id": 1,
                    "date": when,
                    "metric": metric,
                    "value": float(value),
                    "unit": unit,
                    "source": "garmin",
                }
            )
    with rt.engine.begin() as conn:
        conn.execute(
            raw_item.insert(),
            {
                "id": "raw-seed",
                "source": "garmin",
                "dataset": "seed",
                "item_key": "seed",
                "sha256": "ab" * 32,
                "content_type": "application/json",
                "size": 2,
                "request": {},
                "fetched_at": now,
                "last_seen_at": now,
                "is_current": True,
            },
        )
        conn.execute(daily_unified.insert(), rows)
        conn.execute(
            daily_metric.insert(),
            [{**row, "raw_item_id": "raw-seed", "updated_at": now} for row in rows],
        )
    journal.set_checkin(rt.engine, day, sleep_quality=2, soreness=4, mood=3)


def _block_data(found, name):
    text = found["text"].split(f'<context name="{name}"', 1)[1].split("</context>", 1)[0]
    return json.loads(text.split(context.BLOCKS[name].title + ": ", 1)[1].strip())


def test_a_fired_floor_reaches_the_coach_when_the_injury_log_is_empty(rt):
    _seed_short_night(rt)
    assert journal.injuries(rt.engine, DAY, days=14) == []
    found = context.preload(rt, "morning", DAY)
    block = next(b for b in found["blocks"] if b["name"] == "safety")
    assert block == {"name": "safety", "version": 2, "chars": block["chars"], "ok": True}
    safety = _block_data(found, "safety")
    assert safety["injuries"] == "geen" or safety["injuries"] == []
    assert safety["floor"]["level"] == "flag" and safety["floor"]["colour"] == "amber"
    assert {"short_sleep", "sore"} <= set(safety["floor"]["rules"])
    rules = {r["rule"]: r["text"] for r in safety["floor"]["reasons"]}
    assert rules["short_sleep"] == "only 4 h 30 min of sleep"
    assert safety["checkin"]["soreness"] == 4 and safety["sleep_incomplete"] is False
    assert safety["sleep_check"] is None  # the check-in agrees with the short night


def test_the_sleep_check_and_the_incomplete_mark_are_in_the_context(rt):
    _seed_short_night(rt)
    journal.set_checkin(rt.engine, DAY, sleep_quality=5, mood=4)
    asked = _block_data(context.preload(rt, "evening", DAY), "safety")
    assert asked["sleep_check"]["state"] == "suspect" and asked["sleep_incomplete"] is False
    journal.set_sleep_incomplete(rt.engine, DAY, True)
    marked = _block_data(context.preload(rt, "evening", DAY), "safety")
    assert marked["sleep_check"]["state"] == "incomplete" and marked["sleep_incomplete"] is True
    assert "short_sleep" not in marked["floor"]["rules"]


@pytest.mark.parametrize("task_id", registry.live())
def test_every_task_starts_with_the_safety_outcome(rt, monkeypatch, task_id):
    _seed_short_night(rt)
    monkeypatch.setitem(
        context.BLOCKS, "weather", context.Block("weather", "Weer", 100, lambda *a: {"days": []})
    )
    found = context.preload(rt, task_id, DAY)
    assert [b["name"] for b in found["blocks"]][:4] == list(registry.BASE_CONTEXT)
    safety = _block_data(found, "safety")
    if task_id == "insight":
        # The insight writes a public line (INS-2): the injury log, no health numbers.
        assert set(safety) == {"injuries"}
    else:
        assert safety["floor"]["level"] == "flag" and "short_sleep" in safety["floor"]["rules"]


def test_one_part_failing_does_not_hide_the_others(rt, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr("vibesynergy.query.coach_brief.today_brief", boom)
    safety = _block_data(context.preload(rt, "morning", DAY), "safety")
    assert safety["floor"].startswith("niet beschikbaar (RuntimeError)")
    assert "injuries" in safety


# --- measurement gaps (17-11-2026) --------------------------------------------------------

# Garmin's readings on 17-11: the first on the partial night, the morning one on the whole
# night, two after training.
READINGS = [
    {
        "timestamp": "2026-11-16T23:05:05.0",
        "score": 3,
        "sleepScore": 36,
        "inputContext": "AFTER_WAKEUP_RESET",
    },
    {
        "timestamp": "2026-11-17T08:06:47.0",
        "score": 27,
        "sleepScore": 56,
        "inputContext": "AFTER_WAKEUP_RESET",
    },
    {
        "timestamp": "2026-11-17T12:08:02.0",
        "score": 27,
        "sleepScore": 56,
        "inputContext": "AFTER_POST_EXERCISE_RESET",
    },
    {
        "timestamp": "2026-11-17T19:46:34.0",
        "score": 11,
        "sleepScore": 56,
        "inputContext": "AFTER_POST_EXERCISE_RESET",
    },
]
NIGHT = [
    {"at": "2026-11-16T23:03:17+00:00", "seconds": None},
    {"at": "2026-11-16T23:13:15+00:00", "seconds": 12540.0},
    {"at": "2026-11-17T02:34:13+00:00", "seconds": 12540.0},
    {"at": "2026-11-17T08:05:52+00:00", "seconds": 39300.0},
    {"at": "2026-11-17T19:52:05+00:00", "seconds": 39300.0},
]


def test_a_night_that_is_updated_later_is_detected():
    found = measurement.sleep_update(NIGHT)
    assert found == {
        "state": "updated",
        "first_s": 12540.0,
        "now_s": 39300.0,
        "first_seen_at": "2026-11-16T23:13:15+00:00",
        "updated_at": "2026-11-17T08:05:52+00:00",
    }
    assert measurement.sleep_update(NIGHT[:3]) is None  # not updated yet
    steady = [{"at": "a", "seconds": 27000.0}, {"at": "b", "seconds": 28200.0}]
    assert measurement.sleep_update(steady) is None  # twenty minutes more is no update


def test_which_readiness_reading_counts():
    sleep = measurement.sleep_update(NIGHT)
    evening = measurement.readiness_reading(READINGS, sleep=sleep, sleep_score=56)
    assert evening == {
        "value": 11.0,
        "context": "after_exercise",
        "morning": 27.0,
        "partial_night": False,
    }
    # 09:10: the morning reading on the whole night is the day's value: nothing to say.
    assert measurement.readiness_reading(READINGS[:2], sleep=sleep, sleep_score=56) is None
    # 02:30 with the night updated since: the only reading is from the partial night.
    early = measurement.readiness_reading(READINGS[:1], sleep=sleep, sleep_score=56)
    assert early == {"value": 3.0, "context": "morning", "morning": None, "partial_night": True}
    assert measurement.readiness_reading([], sleep=None) is None
    assert measurement.night(None, None, DAY) is None  # no raw archive: nothing claimed


def test_night_reads_the_raw_history(rt):
    def put(dataset, payload):
        rt.raw.put(
            source="garmin",
            dataset=dataset,
            item_key=DAY.isoformat(),
            payload=json.dumps(payload).encode(),
            content_type="application/json",
        )

    def sleep(seconds, score):
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": seconds,
                "sleepScores": {"overall": {"value": score}},
            }
        }

    put("sleep", sleep(12540, 36))
    put("training_readiness", READINGS[:1])
    put("sleep", sleep(39300, 56))
    put("training_readiness", READINGS)
    found = measurement.night(rt.engine, rt.raw, DAY)
    assert found["sleep"]["first_s"] == 12540.0 and found["sleep"]["now_s"] == 39300.0
    assert found["readiness"] == {
        "value": 11.0,
        "context": "after_exercise",
        "morning": 27.0,
        "partial_night": False,
    }
    assert measurement.night(rt.engine, rt.raw, DAY - timedelta(days=1)) is None


def test_17_11_the_floor_judges_the_morning_reading_and_says_so():
    """Readiness 11 in the evening was the reading after training; the morning's was 27."""
    gap = {
        "sleep": measurement.sleep_update(NIGHT),
        "readiness": measurement.readiness_reading(
            READINGS, sleep=measurement.sleep_update(NIGHT), sleep_score=56
        ),
    }
    recovery = _recovery(readiness=_b(11, 55, 15), sleep=_b(39300, 7.5 * 3600, 3600))
    before = dashboard._verdict(recovery, LOAD, _plan(), [], sick=False)
    assert before["rules"] == ["low_readiness"] and before["safety_level"] == "flag"
    after = dashboard._verdict(recovery, LOAD, _plan(), [], sick=False, measurement=gap)
    # 27 is above the low score and well under his usual: ease, not flag.
    assert after["rules"] == ["low_readiness"] and after["safety_level"] == "ease"
    assert after["measurement"] == gap
    assert [n["rule"] for n in after["notes"]] == ["sleep_updated", "readiness_not_morning"]
    assert after["reasons"] == [
        "readiness below normal",
        "The night was updated after its first reading: 3 h 29 min became 10 h 55 min.",
        "Readiness is 11 now, after training; this morning it was 27, and the morning counts.",
    ]
    assert after["sleep_check"] is None  # nothing to ask the athlete


def test_a_readiness_from_a_partial_night_does_not_count():
    gap = {
        "readiness": {"value": 3.0, "context": "morning", "morning": None, "partial_night": True}
    }
    recovery = _recovery(readiness=_b(3, 55, 15))
    assert dashboard._verdict(recovery, LOAD, _plan(), [], sick=False)["level"] == "amber"
    verdict = dashboard._verdict(recovery, LOAD, _plan(), [], sick=False, measurement=gap)
    assert verdict["level"] == "green" and verdict["rules"] == []
    assert "Readiness was computed on a partial night, so it does not count." in verdict["reasons"]


def test_29_8_a_reading_after_training_is_not_a_low_morning():
    """29-8: 14 after training, 78 that morning: the day was amber on the wrong reading."""
    gap = {
        "readiness": {
            "value": 14.0,
            "context": "after_exercise",
            "morning": 78.0,
            "partial_night": False,
        }
    }
    recovery = _recovery(readiness=_b(14, 55, 15))
    verdict = dashboard._verdict(recovery, LOAD, _plan(), [], sick=False, measurement=gap)
    assert verdict["level"] == "green" and verdict["rules"] == []
    assert verdict["reasons"][-1].startswith("Readiness is 14 now, after training")


def test_21_8_a_low_morning_counts_when_the_day_recovers():
    """21-8: 24 on waking, 50 later in the day: the morning was the warning."""
    gap = {
        "readiness": {"value": 50.0, "context": "later", "morning": 24.0, "partial_night": False}
    }
    recovery = _recovery(readiness=_b(50, 55, 15))
    verdict = dashboard._verdict(recovery, LOAD, _plan(), [], sick=False, measurement=gap)
    assert verdict["safety_level"] == "flag" and verdict["rules"] == ["low_readiness"]
    assert (
        "Readiness is 50 now; this morning it was 24, and the morning counts." in verdict["reasons"]
    )


def test_two_readings_on_the_same_side_of_the_rule_are_not_mentioned():
    gap = {
        "readiness": {
            "value": 57.0,
            "context": "after_exercise",
            "morning": 77.0,
            "partial_night": False,
        }
    }
    verdict = dashboard._verdict(_recovery(), LOAD, _plan(), [], sick=False, measurement=gap)
    assert verdict["notes"] == [] and verdict["level"] == "green"


# --- deviation from the plan: intensity and RPE, not duration alone ------------------------


def _item(title, minutes=None, **fields):
    extra = {"duration_s": minutes * 60} if minutes else {}
    return {"title": title, "sport": "running", "extra": extra, **fields}


def test_17_11_longer_strength_at_rpe_4_is_not_a_deviation():
    """Kracht 50' ran 1 h 15 at RPE 4: the session that was planned."""
    item = _item("Kracht MS-instap 50' (squat)", 50, sport="strength_training")
    assert plan.planned_duration_s(item) == 3000
    assert plan.deviation(item, {"moving_s": 4535, "rpe": 4}) is None
    # The same length at RPE 7 is a deviation, and so is one with nothing known.
    assert plan.deviation(item, {"moving_s": 4535, "rpe": 7})["kind"] == "longer"
    assert plan.deviation(item, {"moving_s": 4535})["kind"] == "longer"
    # Without an RPE: heart rate nearly all in zones 1-2 is a low effort (12-11).
    easy = {"moving_s": 3859, "hr_zones": {"1": 1551, "2": 54, "3": 22}}
    assert plan.deviation(_item("Kracht MT-B 50' (RDL, licht)", 50), easy) is None


def test_17_11_a_group_run_above_its_zone_is_harder_than_planned():
    """Training geven 1u15, done in 1 h 30 with 28 minutes above zone 1: not "done"."""
    item = _item("Training geven Loopgroep Leeuwarden 1u15", 75, coached=True)
    assert plan.zone_ceiling(item) == 1
    run = {"moving_s": 5388, "rpe": 3, "hr_zones": {"1": 3708, "2": 1100, "3": 580}}
    assert plan.deviation(item, run) == {"kind": "harder", "above_s": 1680, "ceiling": 1}
    # 12-11: four minutes above zone 1 in 47 is the session as planned.
    calm = {"moving_s": 2803, "hr_zones": {"1": 2570, "2": 264}}
    assert plan.deviation(_item("Training geven Rûnom Mantgum", 45, coached=True), calm) is None


def test_harder_than_planned_counts_whatever_the_length():
    """1-11 and 4-11: Duurloop Z2 with 20 and 11 minutes above zone 2."""
    long = {"moving_s": 6005, "hr_zones": {"1": 2071, "2": 2751, "3": 1170, "4": 17}}
    found = plan.deviation(_item("Duurloop Z2 60'", 60), long)
    assert found == {"kind": "harder", "above_s": 1187, "ceiling": 2}
    on_time = {"moving_s": 2400, "hr_zones": {"1": 600, "2": 1137, "3": 663}}
    assert plan.deviation(_item("Duurloop Z2 40'", 40), on_time)["kind"] == "harder"
    # Five minutes above the zone is not: it needs ten minutes and 15% of the session.
    brief = {"moving_s": 2400, "hr_zones": {"1": 600, "2": 1500, "3": 300}}
    assert plan.deviation(_item("Duurloop Z2 40'", 40), brief) is None
    # No ceiling in the plan (a tempo run): zones are not judged.
    assert plan.zone_ceiling(_item("Tempoloop 13.5 km")) is None
    assert plan.zone_ceiling(_item("Herstelrit")) == 2
    assert plan.zone_ceiling(_item("Duurloop Z2 met 4x Z4")) == 4


def test_15_11_a_shorter_session_is_named_as_shorter():
    item = _item("Lange duurloop zondaggroep 1u50", 110)
    found = plan.deviation(item, {"moving_s": 3881, "rpe": 3})
    assert found == {"kind": "shorter", "planned_s": 6600.0, "actual_s": 3881}
    assert plan.departs_from_plan(item, {"moving_s": 3881}) is True
    assert plan.departs_from_plan(item, {"moving_s": 6500}) is False


def test_distance_and_named_intensity_still_count():
    tempo = {"title": "Tempoloop 13.5 km", "sport": "running", "extra": {}}
    found = plan.deviation(tempo, {"distance_m": 31000, "moving_s": 10800})
    # The figures go along, so the start page can say why the session is "changed".
    assert found == {"kind": "distance", "planned_m": 13500, "actual_m": 31000}
    easy = {"title": "Easy", "sport": "running", "extra": {"intensity": "easy"}}
    found = plan.deviation(easy, {"name": "Tempo run", "moving_s": 3000})
    assert found == {"kind": "intensity", "planned": "easy", "done": "hard"}


# --- the advice, branch by branch ----------------------------------------------------------


def test_17_11_the_advice_no_longer_calls_a_longer_easy_session_a_deviation():
    done = _plan(
        _session("Training geven Loopgroep Leeuwarden 1u15", "done", coached=True),
        _session("Kracht MS-instap 50' (squat)", "done"),
    )
    advice = dashboard._advice("amber", done, completed_count=2)
    assert advice == "Today's Training geven Loopgroep Leeuwarden 1u15 is done; recover well."
    harder = _plan(
        _session("Kracht MS-instap 50' (squat)", "done"),
        _session(
            "Training geven Loopgroep Leeuwarden 1u15",
            "changed",
            coached=True,
            deviation={"kind": "harder", "above_s": 1680, "ceiling": 1},
        ),
    )
    assert dashboard._advice("amber", harder, completed_count=2) == (
        "Today's Training geven Loopgroep Leeuwarden 1u15 ran harder than planned: 28 minutes "
        "above the planned zone. Recover before you add more."
    )


@pytest.mark.parametrize(
    ("deviation", "text"),
    [
        ({"kind": "longer"}, "Today's X ran longer than planned. Recover before you add more."),
        ({"kind": "shorter"}, "Today's X was shorter than planned."),
        ({"kind": "distance"}, "Today's X differed from the plan. Recover before you add more."),
        (None, "Today's X differed from the plan. Recover before you add more."),
    ],
)
def test_a_finished_session_says_how_it_departed(deviation, text):
    session = _session("X", "changed")
    if deviation:
        session["deviation"] = deviation
    assert dashboard._advice("green", _plan(session), completed_count=1) == text


def test_the_deviation_that_matters_most_leads():
    sessions = _plan(
        _session("Zwemmen", "changed", deviation={"kind": "shorter"}),
        _session("Duurloop", "changed", deviation={"kind": "harder", "above_s": 900}),
    )
    assert dashboard._advice("green", sessions).startswith("Today's Duurloop ran harder")


def test_amber_with_a_session_still_to_come_says_what_amber_means():
    """20-8, 10-10 and others: "One activity recorded today." said nothing about amber."""
    hard = _plan(_session("VO2 max 5x3'"))
    assert dashboard._advice("amber", hard, completed_count=1) == (
        "One activity recorded today. Still on the plan: VO2 max 5x3'. "
        "Do it controlled or swap it for an easy session."
    )
    easy = _plan(_session("Mobiliteit 20 min"))
    assert dashboard._advice("amber", easy, completed_count=2) == (
        "2 activities recorded today. Still on the plan: Mobiliteit 20 min. "
        "Keep it at an easy effort."
    )
    assert dashboard._advice("amber", _plan(), completed_count=1) == (
        "One activity recorded today. Keep anything more easy."
    )
    # Green is unchanged.
    assert dashboard._advice("green", _plan(), completed_count=1) == "One activity recorded today."
    assert dashboard._advice("green", hard, completed_count=1) == (
        "One activity recorded today. Still on the plan: VO2 max 5x3'."
    )


def test_14_10_a_red_day_swaps_the_session_that_is_certain():
    """Sick, with a session that could not be matched and one still to do: the advice named
    the unmatched one."""
    sessions = _plan(_session("Basis", "uncertain"), _session("Herstel - binnenfiets rustig"))
    assert dashboard._advice("red", sessions) == (
        "Swap today's Herstel - binnenfiets rustig for rest or a very easy session."
    )
    only = _plan(_session("Basis", "uncertain"))
    assert dashboard._advice("red", only) == "Swap today's Basis for rest or a very easy session."
    # Not red: an unmatched session still asks for a review first (9-9, 28-9, 20-10).
    assert "cannot be matched reliably" in dashboard._advice("amber", sessions)
    assert "cannot be matched reliably" in dashboard._advice("green", only, completed_count=1)


@pytest.mark.parametrize(
    ("level", "sessions", "count", "text"),
    [
        ("green", [_session("Duurloop Z2 40'")], 0, "Today's plan: Duurloop Z2 40', go ahead."),
        (
            "amber",
            [_session("Basis rit")],
            0,
            "Today's plan: Basis rit, fine at an easy effort.",
        ),
        (
            "amber",
            [_session("Drempel 3x10'")],
            0,
            "Today's plan: Drempel 3x10'; do it controlled or swap for an easy session.",
        ),
        (
            "red",
            [_session("MTB (flexibel) 1u15")],
            0,
            "Swap today's MTB (flexibel) 1u15 for rest or a very easy session.",
        ),
        (
            "red",
            [_session("Lange duurloop", "done")],
            1,
            "Today's Lange duurloop is done; recover well.",
        ),
        ("green", [], 0, "Nothing planned: a good day for quality if you feel like it."),
        ("amber", [], 0, "Nothing planned: keep any training easy."),
        ("red", [], 0, "Nothing planned: rest."),
        ("red", [], 1, "Nothing planned: rest."),
        ("green", [], 3, "3 activities recorded today."),
    ],
)
def test_every_advice_branch_of_the_last_90_days(level, sessions, count, text):
    assert dashboard._advice(level, _plan(*sessions), completed_count=count) == text


def test_the_plan_marks_a_longer_easy_session_done_and_a_harder_one_changed(runtime):
    """End to end through the plan: RPE from the watch and stored heart-rate zones."""
    from vibesynergy.derive import unify
    from vibesynergy.model import Activity, PlannedItem
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.store.schema import activity_unified, activity_zone_own

    raw = runtime.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="case-17-11",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(
                    "kracht",
                    DAY,
                    "plan_workout",
                    "Kracht MS-instap 50' (squat)",
                    sport="strength_training",
                ),
                PlannedItem("loop", DAY, "plan_workout", "Duurloop Z2 60'", sport="running"),
                Activity(
                    "gym",
                    datetime(2026, 11, 17, 13, tzinfo=UTC),
                    DAY,
                    "strength_training",
                    name="Middagsessie krachttraining",
                    moving_s=4535,
                    extra={"rpe": 40},
                ),
                Activity(
                    "run",
                    datetime(2026, 11, 17, 18, tzinfo=UTC),
                    DAY,
                    "running",
                    name="Avondloop",
                    moving_s=3700,
                    distance_m=10000,
                ),
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    unify.rebuild(runtime.engine)
    with runtime.engine.begin() as conn:
        run_id = conn.execute(
            select(activity_unified.c.group_id).where(activity_unified.c.sport == "running")
        ).scalar_one()
        conn.execute(
            activity_zone_own.insert().values(
                group_id=run_id,
                kind="hr",
                inputs="test",
                zones={"1": 900, "2": 1600, "3": 1200},
                computed_at=utc_now(),
            )
        )
    out = plan.plan(runtime.engine, plan.Window(DAY, DAY), DAY)
    by_title = {i["title"]: i for i in out["items"]}
    gym = by_title["Kracht MS-instap 50' (squat)"]
    assert gym["status"] == "done" and "deviation" not in gym
    run = by_title["Duurloop Z2 60'"]
    assert run["status"] == "changed"
    assert run["deviation"] == {"kind": "harder", "above_s": 1200, "ceiling": 2}
    assert set(run["done_by"][0]) == {
        "id",
        "sport",
        "name",
        "moving_s",
        "elapsed_s",
        "distance_m",
        "ascent_m",
    }
