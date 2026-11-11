"""VibeSynergy's training load per activity: the only one fitness, fatigue and form use.

Formulas (an hour at threshold is 100):
- power: TSS = s · NP · IF / (FTP · 3600) · 100, IF = NP / FTP (rides with power);
- swim pace: s · IF³ / 3600 · 100, IF = speed / CSS speed (sTSS);
- run pace: s · IF² / 3600 · 100, IF = speed / threshold speed;
- heart rate: Banister TRIMP over the stream, normalised so an hour at LTHR is 100 (HRSS).
Which one per activity: ``method_for``. Checked on the athlete's data on 11-11-2026 against
intervals.icu, which then counted the same sessions: rides with power within 1 %, runs and
strength on the heart-rate stream within 4-7 % (median), pool swims on pace within 1 %.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def power_load(seconds: float, np_watts: float, ftp: float) -> float:
    intensity = np_watts / ftp
    return seconds * np_watts * intensity / (ftp * 3600) * 100


def pace_load(seconds: float, speed: float, threshold_speed: float, exponent: int) -> float:
    return seconds * (speed / threshold_speed) ** exponent / 3600 * 100


def _trimp(minutes: float, hr: float, rest: float, top: float) -> float:
    ratio = (hr - rest) / (top - rest)
    return minutes * ratio * 0.64 * math.exp(1.92 * ratio)


def hr_load(seconds: float, avg_hr: float, lthr: float, rest: float, top: float) -> float:
    if top <= rest or avg_hr <= rest:
        return 0.0
    return _trimp(seconds / 60, avg_hr, rest, top) / _trimp(60, lthr, rest, top) * 100


# A gap longer than this between two samples is a pause, not effort.
MAX_SAMPLE_GAP_S = 10.0


def hr_stream_load(
    times: list[float], hrs: list[float | None], lthr: float, rest: float, top: float
) -> float | None:
    """TRIMP integrated over the heart-rate stream, normalised so one hour at LTHR is 100.

    Interval sessions differ most from the average-based ``hr_load``: the exponential
    weighting makes a minute at 170 count far more than two minutes at 120.
    """
    if top <= rest or len(times) < 2:
        return None
    total = 0.0
    for i in range(1, len(times)):
        hr = hrs[i]
        dt = times[i] - times[i - 1]
        if hr is None or hr <= rest or dt <= 0:
            continue
        total += _trimp(min(dt, MAX_SAMPLE_GAP_S) / 60, hr, rest, top)
    return total / _trimp(60, lthr, rest, top) * 100


# --- the load per activity, as VibeSynergy counts it ---------------------------------------

# A Garmin multisport session is also stored as its parts; the parent would count twice.
PARENT_SPORTS = frozenset({"multi_sport", "multisport", "triathlon"})


@dataclass(frozen=True)
class Inputs:
    """The confirmed thresholds the load is computed from (Zones & thresholds)."""

    ftp: float | None
    css: float | None  # s per 100 m
    threshold_pace: float | None  # s per km
    lthr: dict[str, float]  # per family (ride, run, swim, other)
    max_hr: dict[str, float]
    rest_hr: float

    def key(self, fam: str) -> str:
        return (
            f"{self.ftp}|{self.css}|{self.threshold_pace}|{self.lthr.get(fam)}|"
            f"{self.top(fam)}|{self.rest_hr}"
        )

    def top(self, fam: str) -> float:
        """Max heart rate for a family; strength, Pilates and walking use the general one."""
        return self.max_hr.get(fam) or self.max_hr.get("other") or 0.0


def method_for(
    fam: str,
    *,
    np_watts: float | None,
    avg_power: float | None,
    avg_hr: float | None,
    speed: float | None,
    has_stream: bool,
    inputs: Inputs,
) -> str | None:
    """Which way the load of an activity is counted, in this order: power for rides with
    power (TSS); pace against CSS for swims (sTSS, wrist heart rate is unreliable in the
    water); heart rate, from the stream when there is one, else the average (TRIMP scaled so
    an hour at LTHR is 100); pace for runs without heart rate. None: nothing to count with."""
    if fam == "ride" and (np_watts or avg_power) and inputs.ftp:
        return "power"
    if fam == "swim" and speed and inputs.css:
        return "pace"
    lthr = inputs.lthr.get(fam) or inputs.lthr.get("run")
    if avg_hr and lthr and inputs.top(fam) > inputs.rest_hr:
        return "hr_stream" if has_stream else "hr"
    if fam == "run" and speed and inputs.threshold_pace:
        return "pace"
    return None


def load_of(
    method: str,
    fam: str,
    seconds: float,
    *,
    np_watts: float | None,
    avg_power: float | None,
    avg_hr: float | None,
    speed: float | None,
    inputs: Inputs,
    stream: tuple[list[float], list[float | None]] | None = None,
) -> float:
    lthr = inputs.lthr.get(fam) or inputs.lthr.get("run") or 0.0
    top = inputs.top(fam)
    if method == "power":
        return power_load(seconds, float(np_watts or avg_power or 0), float(inputs.ftp or 1))
    if method == "pace" and fam == "swim":
        return pace_load(seconds, float(speed or 0), 100 / float(inputs.css or 1), 3)
    if method == "pace":
        return pace_load(seconds, float(speed or 0), 1000 / float(inputs.threshold_pace or 1), 2)
    if method == "hr_stream" and stream is not None:
        found = hr_stream_load(stream[0], stream[1], lthr, inputs.rest_hr, top)
        if found is not None:
            return found
    return hr_load(seconds, float(avg_hr or 0), lthr, inputs.rest_hr, top)


def fingerprint(method: str | None, fam: str, row: dict[str, Any], inputs: Inputs) -> str:
    """What a stored load was computed from; a change in any of it means computing again."""
    import hashlib

    text = "|".join(
        str(row.get(k))
        for k in ("moving_s", "elapsed_s", "norm_power", "avg_power", "avg_hr", "avg_speed")
    )
    text += f"|{row.get('stream_key')}|{method}|{fam}|{inputs.key(fam)}|v2"
    return hashlib.sha256(text.encode()).hexdigest()[:24]


# Methods from most to least telling, to choose between two records of one session.
_QUALITY = {"power": 4, "hr_stream": 3, "pace": 2, "hr": 1, None: 0}


def without_duplicates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One session stored twice (e.g. a Zwift ride from Garmin and from Strava that were not
    merged) counts once: two activities of the same family that overlap for more than half of
    the shorter one keep the better-measured record."""
    keep: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda x: (x.get("start") is None, x.get("start") or 0)):
        start = item.get("start")
        twin = None
        if start is not None:
            for other in keep:
                if other["family"] != item["family"] or other.get("start") is None:
                    continue
                a0, a1 = other["start"].timestamp(), other["start"].timestamp() + other["seconds"]
                b0, b1 = start.timestamp(), start.timestamp() + item["seconds"]
                overlap = min(a1, b1) - max(a0, b0)
                if overlap > 0.5 * min(other["seconds"], item["seconds"]):
                    twin = other
                    break
        if twin is None:
            keep.append(item)
        elif _QUALITY.get(item["method"], 0) > _QUALITY.get(twin["method"], 0):
            keep[keep.index(twin)] = item
    return keep
