"""MCP tools: days and periods (split from mcp/server.py)."""

from __future__ import annotations

import logging
from typing import Any

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.server import (
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
    today = kit.today
    window = kit.window
    envelope = kit.envelope

    # Days and periods -------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_day(date: str | None = None) -> dict[str, Any]:
        """Everything about one day (default today): metrics by domain, the night's sleep,
        activities, measurements, next race, plan phase and key values against the
        60-day personal baseline."""
        from vibesynergy.query import activity_effort

        when = _date(date) if date else today()
        data = views.day(engine, when)
        activity_effort.attach_compact(engine, rt.raw, data["activities"])
        warnings = [] if data["metrics"] else [f"no data for {when}; see vibesynergy_sync_status"]
        return envelope(data, span=Window(when, when), warnings=warnings)

    @tool(READ, "read")
    def vibesynergy_calendar(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        metrics: list[str] | None = None,
    ) -> dict[str, Any]:
        """One row per day, one column per metric (default: sleep score, HRV, resting HR,
        activities, training time) for scanning a few weeks. Default period last_28d."""
        metrics = metrics or [
            "sleep.score",
            "hrv.night_avg",
            "hr.resting",
            "activity.count",
            "activity.duration",
        ]
        catalog.check_metrics(engine, metrics)
        span = window(period, start, end, "last_28d")
        return envelope(views.calendar(engine, span, metrics), span=span, metrics=metrics)
