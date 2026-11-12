"""One safety floor for the dashboard, daily coaching and plan checks.

The dashboard and the daily coach used to keep separate thresholds. The dashboard
ignored the morning check-in, so a sore day could still say "go ahead". Both now
call ``evaluate``. A missing check-in score is not zero.

Levels, from mild to strict: ok, ease, flag, refuse. ``ease`` means take it easier.
``flag`` means no hard training. ``refuse`` means rest or very easy. Callers that
still speak in green / amber / red map ok → green, ease and flag → amber, refuse → red.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

THRESHOLDS = {
    "short_sleep_s": 6 * 3600,  # flag; under very_short_sleep_s is the same flag, stronger reason
    "very_short_sleep_s": 5 * 3600,
    "hrv_z": -1.0,  # flag
    "hrv_z_strong": -1.5,  # still flag; the old daily-coach line
    "hrv_low_streak": 3,
    "rhr_up_bpm": 5,
    "readiness_low": 25,
    "readiness_z": -1.0,  # ease when the score itself is not already low
    "soreness": 4,
    "pain_flag": 3,
    "pain_refuse": 7,
    "tsb_deep": -30,
    "acwr_high": 1.5,
    "ramp_pct": 50,
}

# Shared with the dashboard's session wording and with plan checks.
HARD_WORDS = (
    "vo2",
    "drempel",
    "threshold",
    "sprint",
    "interval",
    "tempo",
    "race",
    "wedstrijd",
    "long hard",
    "lange zware",
    "heuvel",
    "fartlek",
    "progression",
)
EASY_WORDS = ("basis", "base", "herstel", "recovery", "easy", "rustig", "mobiliteit", "mobility")
REFUSE_TAGS = {"fever", "chest_pain", "dizzy", "dizziness"}
LEVELS = ("ok", "ease", "flag", "refuse")
_RANK = {level: index for index, level in enumerate(LEVELS)}
_HILL = re.compile(r"\bhills?\b")


def stricter(current: str, new: str) -> str:
    if _RANK[new] > _RANK[current]:
        return new
    return current


def signals_from_coach_inputs(inputs: dict) -> dict[str, Any]:
    """Map a daily-coach capture onto the floor. Missing scores stay missing."""
    recovery = inputs.get("recovery") or {}
    hrv = inputs.get("hrv_baseline") or {}
    week = inputs.get("week") or {}
    load = inputs.get("load") or {}
    rhr = inputs.get("rhr_baseline")
    rhr_up = None
    if (
        isinstance(rhr, dict)
        and rhr.get("value") is not None
        and rhr.get("baseline_mean") is not None
    ):
        rhr_up = rhr["value"] - rhr["baseline_mean"]
    ready = inputs.get("readiness_baseline")
    tags = list(inputs.get("tags") or [])
    return {
        "checkin": inputs.get("checkin"),
        "injuries": inputs.get("injuries") or [],
        "tags": tags,
        "sick": bool(inputs.get("sick")) or "sick" in tags,
        "sleep_s": recovery.get("sleep.duration"),
        "readiness": recovery.get("recovery.readiness"),
        "readiness_z": ready.get("z") if isinstance(ready, dict) else None,
        "hrv_z": hrv.get("z") if isinstance(hrv, dict) else None,
        "hrv_low_days": inputs.get("hrv_low_days") or 0,
        "rhr_up_bpm": rhr_up,
        "tsb": load.get("tsb"),
        "acwr": load.get("acwr"),
        "form": load.get("form"),
        "ramp_pct": week.get("volume_ramp_pct"),
        "recent_s": week.get("last_7d_moving_s"),
        "planned_s": week.get("planned_7d_moving_s"),
    }


def signals_from_dashboard(
    recovery: dict,
    load: dict,
    injuries: list,
    sick: bool,
    *,
    checkin: dict | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Map the dashboard's already loaded blocks onto the floor."""
    hrv = recovery.get("hrv.night_avg") or {}
    rhr = recovery.get("hr.resting") or {}
    sleep = recovery.get("sleep.duration") or {}
    readiness = recovery.get("recovery.readiness") or {}
    rhr_up = None
    if rhr.get("value") is not None and rhr.get("baseline_mean") is not None:
        rhr_up = rhr["value"] - rhr["baseline_mean"]
    tag_list = list(tags or [])
    return {
        "checkin": checkin,
        "injuries": injuries or [],
        "tags": tag_list,
        "sick": bool(sick) or "sick" in tag_list,
        "sleep_s": sleep.get("value"),
        "readiness": readiness.get("value"),
        "readiness_z": readiness.get("z"),
        "hrv_z": hrv.get("z"),
        "hrv_low_days": recovery.get("hrv_low_days") or 0,
        "rhr_up_bpm": rhr_up,
        "tsb": (load or {}).get("tsb"),
        "acwr": (load or {}).get("acwr"),
        "form": (load or {}).get("form"),
        "ramp_pct": (load or {}).get("ramp_pct"),
    }


def evaluate(signals: dict, day: date | None = None) -> dict[str, Any]:
    """Strictest level for one day. ``reasons`` are English sentence keys for the portal."""
    del day  # the caller owns the calendar day; the signals are already for that day
    rules: list[str] = []
    reasons: list[dict[str, Any]] = []
    level = "ok"

    def hit(rule: str, new: str, key: str, **extra: Any) -> None:
        nonlocal level
        if rule not in rules:
            rules.append(rule)
            item: dict[str, Any] = {"key": key, "rule": rule}
            item.update(extra)
            reasons.append(item)
        level = stricter(level, new)

    tags = set(signals.get("tags") or [])
    if signals.get("sick") or "sick" in tags:
        hit("sick", "refuse", "you logged being sick")
    if tags & REFUSE_TAGS:
        hit("red_flag_note", "refuse", "What you reported needs rest today.")

    for item in signals.get("injuries") or []:
        flags = item.get("flags") or []
        flag_names = {f.get("rule") for f in flags}
        pain = item.get("latest_pain")
        if "affects_movement" in flag_names or any(f.get("level") == "stop" for f in flags):
            hit("affects_movement", "refuse", "an injury changes how you move")
        if pain is not None and pain >= THRESHOLDS["pain_refuse"]:
            hit("pain_7", "refuse", "Pain is high, so rest today.")
        elif (pain is not None and pain >= THRESHOLDS["pain_flag"]) or flag_names & {
            "pain_above_3",
            "worsening",
        }:
            hit(
                "injury_warning",
                "flag",
                "An open complaint means keeping hard work easy.",
            )

    hrv_z = signals.get("hrv_z")
    low_days = signals.get("hrv_low_days") or 0
    rhr_up = signals.get("rhr_up_bpm")
    rhr_high = rhr_up is not None and rhr_up >= THRESHOLDS["rhr_up_bpm"]
    if low_days >= THRESHOLDS["hrv_low_streak"] and rhr_high:
        hit(
            "hrv_rhr",
            "refuse",
            "HRV low {n} days and resting HR up",
            values={"n": low_days},
        )
    if (
        signals.get("form") == "overreaching risk"
        and hrv_z is not None
        and hrv_z < THRESHOLDS["hrv_z"]
    ):
        hit("deep_fatigue", "refuse", "deep fatigue with low HRV")

    sleep = signals.get("sleep_s")
    if sleep is not None and sleep < THRESHOLDS["short_sleep_s"]:
        hit("short_sleep", "flag", "only {time} of sleep", seconds=sleep)
    ready = signals.get("readiness")
    ready_z = signals.get("readiness_z")
    if ready is not None and ready <= THRESHOLDS["readiness_low"]:
        hit("low_readiness", "flag", "readiness below normal")
    elif ready_z is not None and ready_z < THRESHOLDS["readiness_z"]:
        hit("low_readiness", "ease", "readiness below normal")
    if hrv_z is not None and hrv_z < THRESHOLDS["hrv_z"]:
        hit("low_hrv", "flag", "HRV below your usual range")
    checkin = signals.get("checkin") or {}
    if not isinstance(checkin, dict):
        checkin = {}
    soreness = checkin.get("soreness")
    if soreness is not None and soreness >= THRESHOLDS["soreness"]:
        hit("sore", "flag", "Your legs are very sore, so keep today easy.")
    ramp = signals.get("ramp_pct")
    planned, recent = signals.get("planned_s"), signals.get("recent_s")
    # Within the plan's own hours (+15%) a steep rise is the build the plan asks for.
    as_planned = bool(planned) and recent is not None and recent <= planned * 1.15
    if ramp is not None and ramp >= THRESHOLDS["ramp_pct"] and not as_planned:
        hit("load_spike", "flag", "training load rising fast")
    acwr = signals.get("acwr")
    if acwr is not None and acwr > THRESHOLDS["acwr_high"]:
        hit("acwr_high", "flag", "training load rising fast")
    tsb = signals.get("tsb")
    if tsb is not None and tsb < THRESHOLDS["tsb_deep"]:
        hit("tsb_deep", "ease", "fatigue well above fitness")
    if rhr_high and "hrv_rhr" not in rules:
        hit(
            "rhr_up",
            "ease",
            "resting HR {n} bpm up",
            values={"n": round(rhr_up)},
        )

    return {"level": level, "rules": sorted(rules), "reasons": reasons}


def title_band(title: str | None) -> str:
    """hard / easy / normal from a session title. Used by the dashboard's plan sentence."""
    text = (title or "").lower()
    if _hard_text(text):
        return "hard"
    if any(word in text for word in EASY_WORDS):
        return "easy"
    return "normal"


def _hard_text(text: str) -> bool:
    if any(word in text for word in HARD_WORDS):
        return True
    return _HILL.search(text) is not None
