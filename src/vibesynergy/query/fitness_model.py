"""Fitness, fatigue and form: Banister's performance management model, VibeSynergy's only one.

- Load per activity: ``own_load`` (power, pace or heart rate; an hour at threshold is 100),
  per day the sum of ``training.activity_loads``.
- Fitness (CTL) and fatigue (ATL) are exponentially weighted averages of the daily load with
  42- and 7-day time constants: ``ctl += (load - ctl) * (1 - e^(-1/42))``.
- Form (TSB) is the same day's fitness minus fatigue; the ramp rate is the change in fitness
  over 7 days.
- Form zones are Joe Friel's, on form as a percentage of fitness: transition above +20 %,
  fresh +5 to +20 %, grey zone -10 to +5 %, optimal -30 to -10 %, high risk below -30 %.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

CTL_DAYS = 42
ATL_DAYS = 7
K_CTL = 1 - math.exp(-1 / CTL_DAYS)
K_ATL = 1 - math.exp(-1 / ATL_DAYS)
# Lower bound of each form zone in % of fitness, high to low (Friel).
FORM_ZONES = (
    (20.0, "transition"),
    (5.0, "fresh"),
    (-10.0, "grey"),
    (-30.0, "optimal"),
    (None, "high_risk"),
)


def form_pct(ctl: float | None, tsb: float | None) -> float | None:
    """Form as a percentage of fitness."""
    if ctl is None or tsb is None or ctl <= 0:
        return None
    return round(tsb / ctl * 100, 1)


def form_zone(ctl: float | None, tsb: float | None) -> str | None:
    pct = form_pct(ctl, tsb)
    if pct is None:
        return None
    for low, name in FORM_ZONES:
        if low is None or pct > low:
            return name
    return None  # pragma: no cover


def legacy_form(tsb: float | None) -> str | None:
    """The older absolute-TSB band other modules still read (safety rules, dashboard)."""
    if tsb is None:
        return None
    if tsb > 5:
        return "fresh"
    if tsb > -10:
        return "neutral"
    if tsb > -30:
        return "optimal training"
    return "overreaching risk"


def step(ctl: float, atl: float, load: float) -> tuple[float, float]:
    """One day of the model."""
    return ctl + (load - ctl) * K_CTL, atl + (load - atl) * K_ATL


def project(
    ctl: float, atl: float, start: date, loads: dict[date, float], end: date
) -> list[dict[str, Any]]:
    """Fitness, fatigue and form from the day after ``start`` through ``end`` if the
    athlete does exactly ``loads`` (a planned load per day; days without one rest)."""
    out = []
    day = start + timedelta(days=1)
    while day <= end:
        ctl, atl = step(ctl, atl, loads.get(day, 0.0))
        tsb = ctl - atl
        out.append(
            {
                "date": str(day),
                "load": round(loads.get(day, 0.0), 1),
                "ctl": round(ctl, 1),
                "atl": round(atl, 1),
                "tsb": round(tsb, 1),
                "form_pct": form_pct(ctl, tsb),
                "form_zone": form_zone(ctl, tsb),
            }
        )
        day += timedelta(days=1)
    return out
