"""MCP tools: analysis (split from mcp/server.py)."""

from __future__ import annotations

import logging
from typing import Any

from vibesynergy.mcp.server import (
    READ,
    ToolKit,
    _excluding,
)
from vibesynergy.query import (
    analysis,
    catalog,
    journal,
    training,
    views,
)

log = logging.getLogger(__name__)


def register(kit: ToolKit) -> None:
    engine = kit.engine
    tool = kit.tool
    window = kit.window
    envelope = kit.envelope

    # Analysis ---------------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_period(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        compare_to: str = "previous",
    ) -> dict[str, Any]:
        """Digest of a period ("how was my week/month"): totals per sport and averages of
        sleep, HRV, resting HR, readiness, steps and weight, with deltas against the
        previous period of the same length (compare_to: previous, same_last_year, none).
        Default period last_7d."""
        span = window(period, start, end, "last_7d")
        before = None if compare_to == "none" else analysis.previous_window(span, compare_to)
        data = training.period_digest(engine, span, before)
        if before:
            data["previous_period"] = {"start": str(before.start), "end": str(before.end)}
        return envelope(data, span=span)

    @tool(READ, "read")
    def vibesynergy_trend(
        metric: str,
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        method: str = "theil_sen",
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Is a metric changing, and how fast? Slope per week with a 95% confidence
        interval, change over the period, R² and a one-line summary. method: theil_sen
        (robust, default) or linear. Default period last_90d. exclude_tags leaves out
        days with those diary tags (e.g. ["sick"])."""
        catalog.check_metrics(engine, [metric])
        span = window(period, start, end, "last_90d")
        data = analysis.trend(engine, metric, span, method=method, exclude_tags=exclude_tags)
        return envelope(data, span=span, metrics=[metric], method=_excluding(exclude_tags))

    @tool(READ, "read")
    def vibesynergy_compare_periods(
        metrics: list[str],
        period_a: str,
        period_b: str | None = None,
        baseline: str = "previous",
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Compare metrics between two periods: n, mean, median of each, difference,
        relative %, Cohen's d and a Mann-Whitney p-value. period_b defaults to the period
        before period_a (baseline: previous or same_last_year). exclude_tags leaves out
        days with those diary tags in both periods (e.g. ["sick", "travel"])."""
        catalog.check_metrics(engine, metrics)
        a = window(period_a, None, None, "last_28d")
        if period_b:
            b = window(period_b, None, None, "last_28d")
        else:
            b = analysis.previous_window(a, baseline)
        data = {
            "period_a": {"start": str(a.start), "end": str(a.end)},
            "period_b": {"start": str(b.start), "end": str(b.end)},
            "metrics": analysis.compare(engine, metrics, a, b, exclude_tags=exclude_tags),
        }
        return envelope(data, metrics=metrics, method=_excluding(exclude_tags))

    @tool(READ, "read")
    def vibesynergy_correlate(
        x: str,
        y: str,
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        max_lag_days: int = 0,
        method: str = "spearman",
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Correlation between two daily metrics, optionally with y lagged 0..max_lag_days
        days after x (e.g. training load today vs HRV tomorrow; pain.max vs
        activity.load). Returns r, n and p per lag. method: spearman (default) or
        pearson. Default period last_365d. exclude_tags leaves out days with those diary
        tags (e.g. ["sick"])."""
        catalog.check_metrics(engine, [x, y])
        span = window(period, start, end, "last_365d")
        lags = range(0, min(max(max_lag_days, 0), 14) + 1)
        data = analysis.correlate(
            engine, x, y, span, lags=lags, method=method, exclude_tags=exclude_tags
        )
        return envelope(data, span=span, metrics=[x, y], method=_excluding(exclude_tags))

    @tool(READ, "read")
    def vibesynergy_distribution(
        metric: str,
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        bins: int | None = None,
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Histogram and quantiles of a daily metric (default period last_365d).
        exclude_tags leaves out days with those diary tags (e.g. ["sick"])."""
        catalog.check_metrics(engine, [metric])
        span = window(period, start, end, "last_365d")
        data = analysis.distribution(engine, metric, span, bins=bins, exclude_tags=exclude_tags)
        return envelope(data, span=span, metrics=[metric], method=_excluding(exclude_tags))

    @tool(READ, "read")
    def vibesynergy_anomalies(
        metrics: list[str] | None = None,
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        threshold: float = 2.0,
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Unusual days: values more than `threshold` standard deviations from the
        preceding 60-day mean (default metrics HRV, resting HR, sleep score, readiness,
        weight; default period last_28d). Days tagged sick in the diary don't count
        towards that mean (exclude_tags replaces the list; [] = keep every day). Each
        anomaly has `diary` when the diary explains the day (notes, check-in, pain):
        mention it instead of guessing a cause."""
        metrics = metrics or list(views.DAY_BASELINE_METRICS)
        catalog.check_metrics(engine, metrics)
        span = window(period, start, end, "last_28d")
        tags = journal.BASELINE_EXCLUDE_TAGS if exclude_tags is None else exclude_tags
        found = analysis.anomalies(engine, metrics, span, threshold=threshold, exclude_tags=tags)
        return envelope(
            {"anomalies": found}, span=span, metrics=metrics, method=_excluding(tags, "baseline")
        )
