"""The sleep check (#53, #100): every night checked, marked, and the athlete decides last.

Synthetic nights only. 17-11-2026 is the regression case: Garmin first reported 3 h 29 min
(the watch stopped at 00:04), later joined the rest of the night to it (10 h 55 min), but
with no REM, eight hours of light sleep without a change and HRV only until midnight.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import anyio
import pytest
from mcp import Client
from sqlalchemy import select

from vibesynergy.coach_runner import triggers
from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.query import analysis, journal, safety_floor, views
from vibesynergy.query import series as qs
from vibesynergy.query import sleep_quality as sq
from vibesynergy.store.schema import daily_metric, daily_unified, raw_item, sleep_check

DAY = date(2026, 11, 17)
TZ = "Europe/Amsterdam"
SHORT = 3 * 3600 + 29 * 60
USUAL = {"bed": (22.75 - 12) * 60, "wake": (7.5 + 12) * 60, "nights": 28}  # 22:45 to 07:30


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def _every(start: str, end: str, minutes: int, value: float) -> list[tuple[int, float]]:
    out, at, stop = [], _ms(start), _ms(end)
    while at <= stop:
        out.append((at, value))
        at += minutes * 60_000
    return out


def night(
    start="2026-11-16T22:00:00",
    end="2026-11-17T06:00:00",
    *,
    deep=5400,
    light=14400,
    rem=6300,
    awake=900,
    hrv_until=None,
    levels=None,
    avg_hr=50,
):
    """A Garmin sleep payload (GMT times), ordinary unless told otherwise."""
    asleep = deep + light + rem
    hrv_end = hrv_until or end
    if levels is None:
        levels, at, cycle = [], _ms(start), [(1.0, 40), (0.0, 30), (1.0, 20), (2.0, 20)]
        while at < _ms(end):
            for level, minutes in cycle:
                stop = min(at + minutes * 60_000, _ms(end))
                levels.append({"startGMT": at, "endGMT": stop, "activityLevel": level})
                at = stop
    return {
        "dailySleepDTO": {
            "calendarDate": DAY.isoformat(),
            "sleepStartTimestampGMT": _ms(start),
            "sleepEndTimestampGMT": _ms(end),
            "sleepTimeSeconds": asleep,
            "deepSleepSeconds": deep,
            "lightSleepSeconds": light,
            "remSleepSeconds": rem,
            "awakeSleepSeconds": awake,
            "deviceRemCapable": True,
            "avgHeartRate": avg_hr,
            "sleepScores": {"overall": {"value": 80}},
        },
        "sleepHeartRate": [],
        "hrvData": [{"startGMT": t, "value": 55.0} for t, _ in _every(start, hrv_end, 5, 55)],
        "sleepLevels": [
            {
                "startGMT": datetime.fromtimestamp(r["startGMT"] / 1000, UTC)
                .replace(tzinfo=None)
                .isoformat(),
                "endGMT": datetime.fromtimestamp(r["endGMT"] / 1000, UTC)
                .replace(tzinfo=None)
                .isoformat(),
                "activityLevel": r["activityLevel"],
            }
            for r in levels
        ],
    }


def heart(*spans: tuple[str, str, float]) -> list[tuple[float, float]]:
    """Two-minute heart-rate samples over each (start, end, bpm) span."""
    rows = [(t, v) for s, e, v in spans for t, v in _every(s, e, 2, v)]
    return sq.heart_samples([{"heartRateValues": [list(r) for r in rows]}])


ALL_NIGHT = heart(("2026-11-16T18:00:00", "2026-11-17T09:00:00", 50))


def check(payload, samples=ALL_NIGHT, **kwargs):
    return sq.assess(payload, samples, timezone=TZ, **kwargs)


def rules(found):
    return {r["rule"]: r["state"] for r in found["reasons"]}


def texts(found):
    return [sq.reason_text(r) for r in found["reasons"]]


# --- the rules, one by one ---------------------------------------------------------------------


def test_an_ordinary_night_is_ok():
    found = check(night(), usual=USUAL, sleep_quality=4)
    assert found["state"] == "ok" and found["reasons"] == [] and found["metrics"] == {}
    assert found["window"]["start"] == "2026-11-16T23:00:00+01:00"
    assert check({"dailySleepDTO": {"sleepTimeSeconds": None}}) is None  # no window: no night


def test_watch_worn_a_gap_in_the_heart_rate_is_incomplete():
    """The issue's example: no heart rate between 00:04 and 06:30 (local)."""
    samples = heart(
        ("2026-11-16T18:00:00", "2026-11-16T23:04:00", 50),
        ("2026-11-17T05:30:00", "2026-11-17T09:00:00", 50),
    )
    found = check(night(end="2026-11-17T06:30:00"), samples)
    assert rules(found) == {"hr_gap": "incomplete"}
    assert texts(found) == ["no heart rate between 00:04 and 06:30"]
    assert set(found["metrics"]) == {"sleep", "hrv", "readiness", "resting_hr"}
    # A gap of 25 minutes is under the threshold of 30.
    short_gap = heart(
        ("2026-11-16T18:00:00", "2026-11-17T01:00:00", 50),
        ("2026-11-17T01:25:00", "2026-11-17T09:00:00", 50),
    )
    assert "hr_gap" not in rules(check(night(), short_gap))
    # Many short gaps: under 80% of the night measured.
    patchy = heart(
        *[(f"2026-11-17T0{h}:00:00", f"2026-11-17T0{h}:30:00", 50) for h in range(0, 7)],
        ("2026-11-16T21:00:00", "2026-11-16T23:59:00", 50),
    )
    found = check(night(), patchy)
    assert rules(found)["hr_gap"] == "incomplete"
    assert texts(found)[0].startswith("heart rate measured for only ")
    # No heart rate at all in the window, while the day was measured.
    none = check(night(), heart(("2026-11-17T09:00:00", "2026-11-17T12:00:00", 60)))
    assert texts(none) == ["no heart rate during the night"]
    # Never fetched: nothing claimed.
    assert "hr_gap" not in rules(check(night(), None))


def test_window_a_night_far_from_the_usual_times_is_suspect():
    late = check(night("2026-11-17T03:30:00", "2026-11-17T09:00:00", light=11000), usual=USUAL)
    assert rules(late) == {"window_off": "suspect"}
    assert texts(late) == ["the night ran from 04:30 to 10:00, far from your usual 22:45 to 07:30"]
    assert late["metrics"] == {"sleep": "suspect", "readiness": "suspect"}
    # Three hours off is still an ordinary night; too few nights is no "usual".
    assert check(night("2026-11-17T01:00:00", "2026-11-17T08:00:00"), usual=USUAL)["state"] == "ok"
    few = {**USUAL, "nights": 3}
    assert check(night("2026-11-17T03:30:00", "2026-11-17T09:00:00"), usual=few)["state"] == "ok"


def test_window_a_short_night_while_the_heart_rate_runs_on_is_suspect():
    """17-11, 03:24 local: the night ended at 00:04, the heart rate stayed at a sleeping
    level for hours after it."""
    cut = night(
        "2026-11-16T19:22:00", "2026-11-16T23:04:00", deep=3240, light=9300, rem=0, awake=780
    )
    samples = heart(
        ("2026-11-16T18:00:00", "2026-11-16T23:04:00", 55),
        ("2026-11-16T23:06:00", "2026-11-17T02:20:00", 48),
    )
    found = check(cut, samples)
    assert rules(found)["hr_runs_on"] == "suspect"
    assert "your heart rate stayed at a sleeping level for 3 h 12 min after it" in texts(found)[0]
    # Awake after it (heart rate well above the night's): an ordinary short night.
    awake = heart(
        ("2026-11-16T18:00:00", "2026-11-16T23:04:00", 55),
        ("2026-11-16T23:06:00", "2026-11-17T02:20:00", 75),
    )
    assert "hr_runs_on" not in rules(check(cut, awake))


def test_stages_no_rem_or_an_implausible_spread_is_bad():
    no_rem = check(night(rem=0, light=20700))
    assert rules(no_rem)["no_rem"] == "bad"
    assert "no REM sleep recorded in a night of 7 h 15 min" in texts(no_rem)
    assert no_rem["metrics"] == {"sleep": "bad", "readiness": "bad"}
    # Three hours or less: too short to judge.
    assert "no_rem" not in rules(check(night("2026-11-16T22:00:00", "2026-11-17T01:00:00",
                                              deep=3000, light=7000, rem=0)))  # fmt: skip
    # A watch that cannot tell REM is not judged on it.
    blind = night(rem=0, light=20700)
    blind["dailySleepDTO"]["deviceRemCapable"] = False
    assert "no_rem" not in rules(check(blind))
    deep = check(night(deep=16000, light=6000, rem=4000))
    assert rules(deep) == {"stage_spread": "bad"}
    assert texts(deep) == ["62% deep sleep, more than is plausible"]


def test_stages_one_stage_without_a_change_for_hours_is_bad():
    levels = [
        {"startGMT": _ms("2026-11-16T22:00:00"), "endGMT": _ms("2026-11-16T23:00:00"),
         "activityLevel": 0.0},
        {"startGMT": _ms("2026-11-16T23:00:00"), "endGMT": _ms("2026-11-17T04:30:00"),
         "activityLevel": 1.0},
        {"startGMT": _ms("2026-11-17T04:30:00"), "endGMT": _ms("2026-11-17T06:00:00"),
         "activityLevel": 2.0},
    ]  # fmt: skip
    found = check(night(levels=levels))
    assert rules(found) == {"one_stage": "bad"}
    assert texts(found) == ["5 h 30 min of light sleep without a single change"]


def test_hrv_over_part_of_the_night_is_incomplete_for_hrv():
    found = check(night(hrv_until="2026-11-17T00:30:00"))
    assert rules(found) == {"hrv_partial": "incomplete"}
    assert found["metrics"] == {"hrv": "incomplete"}
    assert texts(found) == ["HRV measured only from 23:00 to 01:35"]
    assert check(night(hrv_until="2026-11-17T05:00:00"))["state"] == "ok"  # 7 of 8 hours


def test_updated_later_is_said_and_a_partial_readiness_does_not_count():
    gap = {
        "sleep": {"first_s": 12540.0, "now_s": 39300.0},
        "readiness": {"value": 3.0, "context": "morning", "morning": None, "partial_night": True},
    }
    found = check(night(), gap=gap)
    assert rules(found) == {"updated": "ok", "readiness_partial": "incomplete"}
    assert found["metrics"] == {"readiness": "incomplete"}
    assert "the night was updated after its first reading: 3 h 29 min became 10 h 55 min" in (
        texts(found)
    )
    assert check(night(), gap={"sleep": gap["sleep"]})["state"] == "ok"  # said, not a mark


def test_check_in_against_the_measurement_is_suspect():
    short = night("2026-11-16T22:00:00", "2026-11-17T02:00:00", deep=3000, light=8000, rem=2000)
    found = check(short, None, sleep_quality=5)
    assert rules(found) == {"checkin": "suspect"}
    assert check(short, None, sleep_quality=3)["state"] == "ok"  # the check-in agrees
    fallback = sq.from_checkin({"sleep_quality": 5}, SHORT)
    assert fallback["state"] == "suspect" and fallback["not_counted"] == []
    assert sq.from_checkin({"sleep_quality": 2}, SHORT) is None


# --- 17-11-2026 -------------------------------------------------------------------------------


def _17_11():
    levels = [
        (0.0, "2026-11-16T19:22:00", "2026-11-16T20:40:00"),
        (1.0, "2026-11-16T20:40:00", "2026-11-16T22:26:55"),
        (1.0, "2026-11-16T22:26:55", "2026-11-17T06:29:55"),
    ]
    return night(
        "2026-11-16T19:22:00",
        "2026-11-17T06:30:00",
        deep=3240,
        light=36060,
        rem=0,
        awake=780,
        hrv_until="2026-11-16T23:02:00",
        avg_hr=55,
        levels=[{"startGMT": _ms(s), "endGMT": _ms(e), "activityLevel": a} for a, s, e in levels],
    )


def test_17_11_the_whole_night_is_badly_recorded_and_hrv_incomplete():
    gap = {"sleep": {"first_s": 12540.0, "now_s": 39300.0}}
    found = check(_17_11(), usual=USUAL, sleep_quality=4, gap=gap)
    assert found["state"] == "bad"
    assert found["metrics"] == {"sleep": "bad", "hrv": "incomplete", "readiness": "bad"}
    assert rules(found) == {
        "no_rem": "bad",
        "stage_spread": "bad",
        "one_stage": "bad",
        "hrv_partial": "incomplete",
        "updated": "ok",
    }
    assert "HRV measured only from 20:22 to 00:07" in texts(found)
    assert "92% light sleep, more than is plausible" in texts(found)
    mark = sq.decide(found, None)
    assert mark["not_counted"] == ["sleep", "hrv", "readiness"]
    words = sq.describe(mark)
    assert words["headline"] == "Sleep may be badly recorded"
    assert words["effect"] == "Its sleep, HRV and readiness don't count for the advice."


def test_17_11_the_floor_does_not_count_the_broken_night():
    signals = {
        "checkin": {"sleep_quality": 4},
        "sleep_s": SHORT,
        "readiness": 3,
        "hrv_z": -1.6,
        "rhr_up_bpm": 1,
    }
    plain = safety_floor.evaluate(signals)
    assert {"short_sleep", "low_readiness", "low_hrv"} <= set(plain["rules"])
    mark = sq.decide(check(_17_11()), None)
    marked = safety_floor.evaluate({**signals, "sleep_check": mark})
    assert not {"short_sleep", "low_readiness", "low_hrv"} & set(marked["rules"])
    assert marked["level"] == "ok"
    note = next(n for n in marked["notes"] if n["rule"] == "sleep_check")
    assert note["metrics"] == ["sleep", "HRV", "readiness"]
    # "No, it's right": everything counts again.
    right = safety_floor.evaluate({**signals, "sleep_check": sq.decide(check(_17_11()), "ok")})
    assert {"short_sleep", "low_readiness", "low_hrv"} <= set(right["rules"])


def test_a_suspect_night_still_counts_and_is_named():
    mark = sq.from_checkin({"sleep_quality": 5}, SHORT)
    found = safety_floor.evaluate({"sleep_s": SHORT, "sleep_check": mark})
    assert "short_sleep" in found["rules"]
    assert [n["rule"] for n in found["notes"]] == ["sleep_check_suspect"]


def test_the_athletes_choice_beats_the_check():
    auto = check(_17_11())
    assert sq.decide(auto, "ok")["state"] == "ok" and sq.decide(auto, "ok")["not_counted"] == []
    assert sq.decide(auto, "ok")["auto_state"] == "bad"
    yes = sq.decide(None, "incomplete")  # nothing found, but he says so (#53)
    assert yes["state"] == "incomplete" and yes["not_counted"] == ["sleep", "readiness"]
    hrv = sq.decide(check(night(hrv_until="2026-11-17T00:30:00")), "incomplete")
    assert hrv["not_counted"] == ["sleep", "hrv", "readiness"]
    assert sq.decide(None, None) is None


def test_the_old_mark_still_drops_short_sleep_and_its_readiness_not_hrv():
    signals = {"sleep_s": SHORT, "readiness": 3, "hrv_z": -1.2}
    marked = safety_floor.evaluate(
        {**signals, "checkin": {"sleep_quality": 5, "sleep_incomplete": True}}
    )
    assert "short_sleep" not in marked["rules"] and "low_readiness" not in marked["rules"]
    assert "low_hrv" in marked["rules"]  # HRV is its own measurement


# --- stored, recomputed, overridden -------------------------------------------------------------


def _put(rt, dataset, key, payload):
    rt.raw.put(
        source="garmin",
        dataset=dataset,
        item_key=key,
        payload=json.dumps(payload).encode(),
        content_type="application/json",
    )


def _heart_payload(*spans):
    rows = [list(r) for s, e, v in spans for r in _every(s, e, 2, v)]
    return {"heartRateValues": rows}


def _seed_17_11(rt):
    _put(rt, "heart_rate", "2026-11-16", _heart_payload(("2026-11-16T08:00:00",
                                                         "2026-11-16T22:58:00", 55)))  # fmt: skip
    _put(rt, "heart_rate", "2026-11-17", _heart_payload(("2026-11-16T23:00:00",
                                                         "2026-11-17T09:00:00", 50)))  # fmt: skip
    _put(rt, "sleep", DAY.isoformat(), _17_11())


def test_the_check_is_stored_and_recomputed_when_garmin_updates_the_night(runtime):
    rt = runtime
    _put(rt, "sleep", DAY.isoformat(), night())
    first = sq.night(rt.engine, rt.raw, DAY, TZ)
    assert first["state"] == "ok" and first["checked_at"]
    with rt.engine.connect() as conn:
        row = conn.execute(select(sleep_check)).one()
    assert row.state == "ok" and row.date == DAY
    # A read with nothing new does not check again.
    assert sq.night(rt.engine, rt.raw, DAY, TZ)["checked_at"] == first["checked_at"]
    _seed_17_11(rt)  # Garmin's new version of the night
    again = sq.night(rt.engine, rt.raw, DAY, TZ)
    assert again["state"] == "bad" and again["checked_at"] != first["checked_at"]
    # Without the raw archive the stored check is what is known.
    assert sq.night(rt.engine, None, DAY, TZ)["state"] == "bad"


def test_the_athlete_decides_last_and_can_undo(runtime):
    rt = runtime
    _seed_17_11(rt)
    sq.set_override(rt.engine, DAY, "ok")
    right = sq.night(rt.engine, rt.raw, DAY, TZ)
    assert right["state"] == "ok" and right["override"] == "ok" and right["auto_state"] == "bad"
    assert journal.checkins(rt.engine, Window(DAY, DAY))[0]["sleep_incomplete"] is False
    sq.set_override(rt.engine, DAY, None)
    assert sq.night(rt.engine, rt.raw, DAY, TZ)["state"] == "bad"
    with pytest.raises(ValueError):
        sq.set_override(rt.engine, DAY, "maybe")


def test_marking_a_night_keeps_the_check_in(runtime):
    engine = runtime.engine
    journal.set_checkin(engine, DAY, sleep_quality=5, soreness=1)
    row = journal.set_sleep_incomplete(engine, DAY, True)
    assert row["sleep_incomplete"] is True and row["sleep_quality"] == 5
    assert journal.set_checkin(engine, DAY, mood=4)["sleep_incomplete"] is True
    assert journal.set_sleep_incomplete(engine, DAY, False)["sleep_incomplete"] is False
    assert journal.context(engine, DAY)["checkin"]["sleep_incomplete"] is False  # "it's right"
    row = journal.set_sleep_incomplete(engine, DAY, None)
    assert row["sleep_incomplete"] is None and row["soreness"] == 1
    assert "sleep_incomplete" not in journal.context(engine, DAY)["checkin"]


def _daily(rt, metric, values: dict[date, float], unit="ms"):
    now = utc_now()
    rows = [
        {"subject_id": 1, "date": d, "metric": metric, "value": v, "unit": unit, "source": "garmin"}
        for d, v in values.items()
    ]
    with rt.engine.begin() as conn:
        if conn.scalar(select(raw_item.c.id).where(raw_item.c.id == "raw-seed")) is None:
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
            [{**r, "raw_item_id": "raw-seed", "updated_at": now} for r in rows],
        )


def test_a_broken_night_stays_out_of_the_baseline_and_the_trend(runtime):
    rt = runtime
    days = {DAY - timedelta(days=back): 55.0 + (back % 3) for back in range(1, 21)}
    days[DAY] = 54.0
    _daily(rt, "hrv.night_avg", {**days, DAY - timedelta(days=5): 20.0})
    before = qs.baseline(rt.engine, "hrv.night_avg", DAY)
    sq.set_override(rt.engine, DAY - timedelta(days=5), "incomplete")  # no HRV in the mark …
    assert qs.baseline(rt.engine, "hrv.night_avg", DAY) == before  # … so HRV still counts
    with rt.engine.begin() as conn:
        conn.execute(
            sleep_check.insert(),
            {
                "subject_id": 1,
                "date": DAY - timedelta(days=5),
                "inputs": "x",
                "state": "incomplete",
                "result": sq.assess(night(hrv_until="2026-11-17T00:30:00"), None, timezone=TZ),
                "checked_at": utc_now(),
            },
        )
    after = qs.baseline(rt.engine, "hrv.night_avg", DAY)
    assert after["left_out_nights"] == 1 and after["n"] == before["n"] - 1
    assert after["baseline_mean"] > before["baseline_mean"]
    assert after["baseline_std"] < before["baseline_std"]
    trend = analysis.trend(rt.engine, "hrv.night_avg", Window(DAY - timedelta(days=20), DAY))
    assert trend["left_out_nights"] == [str(DAY - timedelta(days=5))]
    kept = qs.baseline(rt.engine, "hrv.night_avg", DAY, exclude_marked=False)
    assert kept["n"] == before["n"]


def test_explain_and_the_sleep_view_show_the_mark(runtime):
    rt = runtime
    _seed_17_11(rt)
    found = views.explain(rt.engine, "hrv.night_avg", DAY, raw=rt.raw, timezone=TZ)
    assert found["sleep_check"]["state"] == "bad"
    assert found["sleep_check"]["this_metric"] == "incomplete"
    assert found["sleep_check"]["counts"] is False
    assert "sleep_check" not in views.explain(rt.engine, "activity.load", DAY)
    sq.set_override(rt.engine, DAY, "incomplete")
    assert "marked_incomplete" in views.explain(rt.engine, "sleep.duration", DAY)


def test_a_changed_mark_is_a_new_signal_for_the_advice(runtime):
    rt = runtime
    _put(rt, "sleep", DAY.isoformat(), night())
    before = triggers.fingerprint(rt, DAY)
    assert "sleep_check" in triggers.KINDS
    _seed_17_11(rt)
    after = triggers.fingerprint(rt, DAY)
    assert before["sleep_check"] != after["sleep_check"]
    sq.set_override(rt.engine, DAY, "ok")
    assert triggers.fingerprint(rt, DAY)["sleep_check"] != after["sleep_check"]


def test_the_choice_over_mcp(runtime, monkeypatch):
    rt = runtime
    monkeypatch.setattr("vibesynergy.mcp.server.today_in", lambda tz: DAY)
    _seed_17_11(rt)

    async def go(**args):
        tokens = set_connection("client-1", scopes=frozenset({"read", "diary.write"}), role="own")
        try:
            async with Client(build_server(rt)) as client:
                return await client.call_tool("vibesynergy_sleep_check_set", args)
        finally:
            reset_connection(tokens)

    found = anyio.run(lambda: go(choice="ok"))
    data = found.structured_content["data"]
    assert data["sleep_check"]["state"] == "ok" and data["sleep_check"]["auto_state"] == "bad"
    data = anyio.run(lambda: go(choice="clear")).structured_content["data"]
    assert data["sleep_check"]["state"] == "bad"
    refused = anyio.run(lambda: go(choice="maybe"))
    assert refused.is_error
