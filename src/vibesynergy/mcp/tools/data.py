"""MCP tools: data management (split from mcp/server.py)."""

from __future__ import annotations

import logging
from typing import Any

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.server import (
    OPEN,
    READ,
    ToolKit,
    _date,
)
from vibesynergy.query import (
    catalog,
    views,
)

log = logging.getLogger(__name__)


def register(kit: ToolKit) -> None:
    rt = kit.rt
    engine = kit.engine
    tool = kit.tool
    window = kit.window
    envelope = kit.envelope

    # Data management --------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_sync_status(source: str | None = None) -> dict[str, Any]:
        """Per dataset: fetch timestamps/errors, current archive processing errors/pending
        counts, and fetched range. Fetch success is not proof of complete metric coverage."""
        return envelope(views.sync_status(engine, source))

    @tool(READ, "read")
    def vibesynergy_data_quality(limit: int = 200) -> dict[str, Any]:
        """Read-only audit of Strava/Garmin activity linking: unlinked near-duplicates and
        same-source groups that should not exist."""
        from vibesynergy.query import data_quality

        return envelope(data_quality.activity_quality(engine, limit=limit))

    @tool(READ, "read")
    def vibesynergy_latency(limit: int = 20) -> dict[str, Any]:
        """Recent activity delivery stages and delays, including finish to first dashboard query.
        Provider upload before notification is outside VibeSynergy's control; absent stages are
        null.
        """
        from vibesynergy.query import latency

        return envelope(latency.recent(engine, limit))

    @tool(READ, "read")
    def vibesynergy_coverage(
        metrics: list[str],
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Days with data per month for each metric (default period all), to spot gaps
        before an analysis."""
        catalog.check_metrics(engine, metrics)
        span = window(period, start, end, "all")
        return envelope(views.coverage(engine, metrics, span), span=span, metrics=metrics)

    @tool(READ, "read")
    def vibesynergy_metric_status(
        metric: str,
        source: str,
        dataset: str,
        start: str,
        end: str,
        previous_version: str | None = None,
    ) -> dict[str, Any]:
        """Observed daily metric values, units, coverage and source-stage freshness.
        A matching previous_version yields unchanged only for a current comparison."""
        span = Window(_date(start), _date(end))
        return envelope(
            views.metric_status(
                engine, metric, source, dataset, span, previous_version=previous_version
            ),
            span=span,
            metrics=[metric],
        )

    @tool(OPEN, "sync")
    def vibesynergy_refresh(source: str | None = None, period: str = "today") -> dict[str, Any]:
        """Queue an immediate sync (default: all sources, today). Returns job ids; the data
        arrives within minutes. Use when the user says data is missing or stale."""
        span = window(period, None, None, "today")
        sources = [source] if source else rt.registry.enabled_ids()
        jobs = {}
        for sid in sources:
            payload = {"source": sid, "dataset": None, "window": str(span)}
            jobs[sid] = rt.jobs.enqueue(
                "sync", payload, priority=10, dedupe_key=f"sync:{sid}:None:{span}"
            )
        return envelope({"queued": jobs}, span=span)
