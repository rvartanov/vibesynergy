"""MCP tools: time series (split from mcp/server.py)."""

from __future__ import annotations

import logging
from typing import Any

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.server import (
    READ,
    ToolKit,
    _date,
    _excluding,
)
from vibesynergy.query import (
    catalog,
    journal,
    views,
)
from vibesynergy.query import series as qs

log = logging.getLogger(__name__)


def register(kit: ToolKit) -> None:
    engine = kit.engine
    tool = kit.tool
    today = kit.today
    window = kit.window
    envelope = kit.envelope

    # Time series ------------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_series(
        metrics: list[str],
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        granularity: str | None = None,
        agg: dict[str, str] | None = None,
        sport: str | None = None,
        max_points: int = 400,
    ) -> dict[str, Any]:
        """Any metrics over time on one axis, aggregated server-side. granularity: day,
        week, month, year (default: by period length). agg per metric: mean, median, sum,
        min, max, count, first, last, std, p10..p90 (default from the catalogue). Each row
        has n (days behind the value). sport filters activity.* metrics."""
        catalog.check_metrics(engine, metrics)
        span = window(period, start, end, "last_90d")
        data = qs.series(
            engine,
            metrics,
            span,
            granularity=granularity,
            agg=agg,
            sport=sport,
            max_points=max_points,
        )
        empty = [m for m, n in data["days_with_data"].items() if n == 0]
        return envelope(
            data,
            span=span,
            metrics=metrics,
            method=f"{data['granularity']} buckets; aggregation per metric in data.agg",
            warnings=[f"no data for {m} in this period" for m in empty],
        )

    @tool(READ, "read")
    def vibesynergy_stats(
        metrics: list[str],
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Descriptive statistics of daily values: n, mean, median, std, min, max,
        percentiles, first/last value and change. Default period last_90d.
        exclude_tags leaves out days with those diary tags (e.g. ["sick", "travel"])."""
        catalog.check_metrics(engine, metrics)
        span = window(period, start, end, "last_90d")
        return envelope(
            qs.stats(engine, metrics, span, exclude_tags=exclude_tags),
            span=span,
            metrics=metrics,
            method=_excluding(exclude_tags),
        )

    @tool(READ, "read")
    def vibesynergy_baselines(
        metrics: list[str] | None = None,
        date: str | None = None,
        window_days: int = 60,
        exclude_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Is a value normal for this person? Baseline mean/std over the preceding
        window_days, the day's value and its z-score (default: HRV, resting HR, sleep
        score, readiness, weight; default date today). Days tagged sick in the diary are
        left out of the baseline; exclude_tags replaces that list ([] = keep every day).
        data.diary is what the diary says about the day, if anything."""
        metrics = metrics or list(views.DAY_BASELINE_METRICS)
        catalog.check_metrics(engine, metrics)
        when = _date(date) if date else today()
        tags = journal.BASELINE_EXCLUDE_TAGS if exclude_tags is None else exclude_tags
        data: dict[str, Any] = {
            m: qs.baseline(engine, m, when, window_days=window_days, exclude_tags=tags)
            for m in metrics
        }
        missing = [m for m, v in data.items() if v is None]
        data["diary"] = journal.context(engine, when)
        return envelope(
            data,
            span=Window(when, when),
            metrics=metrics,
            method=_excluding(tags, "baseline"),
            warnings=[f"{m}: no value that day or under 7 days of history" for m in missing],
        )
