"""Statistical analysis over daily values (docs/MCP.md §3.3): trend, comparison,
correlation, distribution, anomalies.

Pure Python on purpose: a lifetime of daily values is a few thousand points, and the
methods are standard. Every result carries n and a plain-language summary; p-values use
normal approximations, which is stated in the result.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Engine

from vibesynergy.core.timeutil import Window
from vibesynergy.query import journal
from vibesynergy.query import series as qs

_Z95 = 1.959964


def _norm_sf(z: float) -> float:
    """Two-sided p-value for a standard normal statistic."""
    return math.erfc(abs(z) / math.sqrt(2))


def _ordered(values: dict[date, float]) -> tuple[list[date], list[float]]:
    days = sorted(values)
    return days, [values[d] for d in days]


# Trend --------------------------------------------------------------------------------------


def trend(
    engine: Engine,
    metric: str,
    window: Window,
    *,
    method: str = "theil_sen",
    exclude_tags: Iterable[str] | None = None,
) -> dict:
    days, ys = _ordered(
        qs.daily_values(engine, [metric], window, exclude_tags=exclude_tags)[metric]
    )
    n = len(ys)
    if n < 5:
        return {"n": n, "summary": f"too few values for a trend ({n}; need at least 5)"}
    xs = [(d - days[0]).days for d in days]
    if method == "linear":
        slope, intercept, se = _ols(xs, ys)
        ci = (slope - _Z95 * se, slope + _Z95 * se) if se is not None else (None, None)
    elif method == "theil_sen":
        slope, intercept, ci = _theil_sen(xs, ys)
    else:
        raise ValueError("method must be theil_sen or linear")
    fitted = [intercept + slope * x for x in xs]
    r2 = _r2(ys, fitted)
    per_week = slope * 7
    unit_ci = [None if c is None else round(c * 7, 4) for c in ci]
    change = slope * (xs[-1] - xs[0])
    direction = (
        "rising"
        if unit_ci[0] and unit_ci[0] > 0
        else ("falling" if unit_ci[1] is not None and unit_ci[1] < 0 else "no clear trend")
    )
    return {
        "n": n,
        "method": method,
        "slope_per_week": round(per_week, 4),
        "slope_per_week_ci95": unit_ci,
        "change_over_period": round(change, 4),
        "start_fitted": round(fitted[0], 3),
        "end_fitted": round(fitted[-1], 3),
        "r2": round(r2, 3) if r2 is not None else None,
        "direction": direction,
        "summary": f"{metric}: {per_week:+.3g} per week (95% CI {unit_ci[0]} to {unit_ci[1]}), "
        f"{direction}, n={n}",
    }


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float, float | None]:
    n = len(xs)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my, None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=True)]
    se = math.sqrt(sum(r * r for r in resid) / (n - 2) / sxx) if n > 2 else None
    return slope, intercept, se


def _theil_sen(xs: list[float], ys: list[float]) -> tuple[float, float, tuple]:
    """Median of pairwise slopes; CI from Sen (1968) with the normal approximation.
    Long series are thinned to about 1,500 evenly spaced points."""
    if len(xs) > 1500:
        step = len(xs) / 1500
        idx = [int(i * step) for i in range(1500)]
        xs, ys = [xs[i] for i in idx], [ys[i] for i in idx]
    slopes = sorted(
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    )
    slope = statistics.median(slopes)
    intercept = statistics.median(y - slope * x for x, y in zip(xs, ys, strict=True))
    n, m = len(xs), len(slopes)
    c = _Z95 * math.sqrt(n * (n - 1) * (2 * n + 5) / 18)
    lo, hi = int((m - c) / 2), int((m + c) / 2) + 1
    ci = (slopes[max(lo, 0)], slopes[min(hi, m - 1)]) if m else (None, None)
    return slope, intercept, ci


def _r2(ys: list[float], fitted: list[float]) -> float | None:
    my = statistics.fmean(ys)
    total = sum((y - my) ** 2 for y in ys)
    if total == 0:
        return None
    return 1 - sum((y - f) ** 2 for y, f in zip(ys, fitted, strict=True)) / total


# Comparison ---------------------------------------------------------------------------------


def previous_window(window: Window, baseline: str) -> Window:
    if baseline == "same_last_year":
        return Window(
            window.start.replace(year=window.start.year - 1),
            window.end.replace(year=window.end.year - 1),
        )
    length = len(window)
    return Window(window.start - timedelta(days=length), window.start - timedelta(days=1))


def compare(
    engine: Engine,
    metrics: list[str],
    a: Window,
    b: Window,
    *,
    exclude_tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    va = qs.daily_values(engine, metrics, a, exclude_tags=exclude_tags)
    vb = qs.daily_values(engine, metrics, b, exclude_tags=exclude_tags)
    out = {}
    for metric in metrics:
        xa, xb = list(va[metric].values()), list(vb[metric].values())
        entry: dict[str, Any] = {"a": _summary(xa), "b": _summary(xb)}
        if xa and xb:
            ma, mb = statistics.fmean(xa), statistics.fmean(xb)
            entry["difference"] = round(ma - mb, 4)
            entry["relative_pct"] = round(100 * (ma - mb) / mb, 2) if mb else None
            if len(xa) > 1 and len(xb) > 1:
                pooled = math.sqrt(
                    (
                        (len(xa) - 1) * statistics.variance(xa)
                        + (len(xb) - 1) * statistics.variance(xb)
                    )
                    / (len(xa) + len(xb) - 2)
                )
                entry["cohens_d"] = round((ma - mb) / pooled, 3) if pooled else None
                entry["mann_whitney_p"] = round(_mann_whitney_p(xa, xb), 4)
        if min(len(xa), len(xb)) < 7:
            entry["note"] = "small sample: fewer than 7 days in a period"
        out[metric] = entry
    return out


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
    }


def _mann_whitney_p(a: list[float], b: list[float]) -> float:
    """Two-sided p (normal approximation with tie correction)."""
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks = [0.0] * len(combined)
    i, ties = 0, 0.0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        t = j - i + 1
        ties += t**3 - t
        i = j + 1
    na, nb = len(a), len(b)
    ra = sum(r for r, (_, g) in zip(ranks, combined, strict=True) if g == 0)
    u = ra - na * (na + 1) / 2
    n = na + nb
    sigma = math.sqrt(na * nb / 12 * ((n + 1) - ties / (n * (n - 1))))
    return _norm_sf((u - na * nb / 2) / sigma) if sigma else 1.0


# Correlation --------------------------------------------------------------------------------


def correlate(
    engine: Engine,
    x: str,
    y: str,
    window: Window,
    *,
    lags: range = range(0, 1),
    method: str = "spearman",
    exclude_tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Correlation of x on day d with y on day d + lag, per lag."""
    if method not in ("spearman", "pearson"):
        raise ValueError("method must be spearman or pearson")
    ext = Window(window.start, window.end + timedelta(days=max(lags.stop - 1, 0)))
    values = qs.daily_values(engine, [x, y], ext, exclude_tags=exclude_tags)
    vx, vy = values[x], values[y]
    results = []
    for lag in lags:
        pairs = [
            (vx[d], vy[d + timedelta(days=lag)])
            for d in vx
            if window.start <= d <= window.end and d + timedelta(days=lag) in vy
        ]
        n = len(pairs)
        if n < 5:
            results.append({"lag_days": lag, "n": n, "r": None})
            continue
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        if method == "spearman":
            xs, ys = _ranks(xs), _ranks(ys)
        r = _pearson(xs, ys)
        p = None
        if r is not None and abs(r) < 1 and n > 3:
            t = r * math.sqrt((n - 2) / (1 - r * r))
            p = round(_norm_sf(t), 4)
        results.append({"lag_days": lag, "n": n, "r": None if r is None else round(r, 3), "p": p})
    return {
        "method": method,
        "by_lag": results,
        "caution": "Correlation is not causation; p-values use a normal approximation and are "
        "not corrected for testing several lags.",
    }


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


# Distribution and anomalies -----------------------------------------------------------------


def distribution(
    engine: Engine,
    metric: str,
    window: Window,
    *,
    bins: int | None = None,
    exclude_tags: Iterable[str] | None = None,
) -> dict:
    values = sorted(
        qs.daily_values(engine, [metric], window, exclude_tags=exclude_tags)[metric].values()
    )
    if not values:
        return {"n": 0}
    bins = bins or max(5, min(30, round(math.sqrt(len(values)))))
    lo, hi = values[0], values[-1]
    width = (hi - lo) / bins or 1.0
    counts = [0] * bins
    for v in values:
        counts[min(int((v - lo) / width), bins - 1)] += 1
    return {
        "n": len(values),
        "histogram": [
            {"from": round(lo + i * width, 4), "to": round(lo + (i + 1) * width, 4), "count": c}
            for i, c in enumerate(counts)
        ],
        "quantiles": {
            f"p{p}": round(qs.percentile(values, p), 4) for p in (5, 10, 25, 50, 75, 90, 95)
        },
    }


def anomalies(
    engine: Engine,
    metrics: list[str],
    window: Window,
    *,
    threshold: float = 2.0,
    baseline_days: int = 60,
    exclude_tags: Iterable[str] | None = journal.BASELINE_EXCLUDE_TAGS,
) -> list[dict[str, Any]]:
    """Days whose value is more than ``threshold`` standard deviations from the mean of
    the preceding ``baseline_days`` (at least 14 days of history). Days tagged with
    ``exclude_tags`` (default: sick) don't count towards that mean, but are still checked
    themselves. Each anomaly carries the diary's context for that day, if any: notes,
    the check-in and pain logged, so a low HRV during a cold explains itself."""
    ext = Window(window.start - timedelta(days=baseline_days), window.end)
    values = qs.daily_values(engine, metrics, ext)
    skip = journal.tagged_days(engine, ext, list(exclude_tags or []))
    found = []
    for metric in metrics:
        by_day = values[metric]
        for d in sorted(by_day):
            if d < window.start:
                continue
            history = [
                by_day[h]
                for h in by_day
                if d - timedelta(days=baseline_days) <= h < d and h not in skip
            ]
            if len(history) < 14:
                continue
            mean, std = statistics.fmean(history), statistics.stdev(history)
            if not std:
                continue
            z = (by_day[d] - mean) / std
            if abs(z) >= threshold:
                found.append(
                    {
                        "date": str(d),
                        "metric": metric,
                        "value": round(by_day[d], 4),
                        "baseline_mean": round(mean, 4),
                        "z": round(z, 2),
                        "direction": "high" if z > 0 else "low",
                    }
                )
    context: dict[str, dict[str, Any] | None] = {}
    for a in found:
        if a["date"] not in context:
            context[a["date"]] = journal.context(engine, date.fromisoformat(a["date"]))
        if context[a["date"]]:
            a["diary"] = context[a["date"]]
    return sorted(found, key=lambda a: (a["date"], a["metric"]))
