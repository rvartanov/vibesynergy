"""MCP tools: wellness and body (split from mcp/server.py)."""

from __future__ import annotations

from typing import Any

from vibesynergy.mcp.server import (
    READ,
    ToolKit,
)
from vibesynergy.query import (
    coach,
    views,
)


def register(kit: ToolKit) -> None:
    rt = kit.rt
    engine = kit.engine
    settings = kit.settings
    tool = kit.tool
    window = kit.window
    envelope = kit.envelope

    # Wellness and body ------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_sleep(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        stages: bool = False,
    ) -> dict[str, Any]:
        """Nights (default last_7d): times, duration, stages, score from the best source,
        with a period summary and regularity (mean bedtime / wake time / midpoint in local
        time and their standard deviation in minutes). stages=true adds the hypnogram
        segments."""
        span = window(period, start, end, "last_7d")
        data = views.sleep(engine, span, stages=stages, timezone=settings.timezone)
        return envelope(data, span=span)

    @tool(READ, "read")
    def vibesynergy_recovery(
        period: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Daily HRV with baseline band, resting HR, readiness, body battery, stress, sleep
        score and training load side by side (default last_14d)."""
        span = window(period, start, end, "last_14d")
        return envelope(views.recovery(engine, span), span=span)

    @tool(READ, "read")
    def vibesynergy_body(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        metrics: list[str] | None = None,
    ) -> dict[str, Any]:
        """Body measurements with time and source, trend and statistics (default last_90d;
        metrics default body.weight, body.fat_pct, body.muscle_mass). Informational only."""
        metrics = metrics or ["body.weight", "body.fat_pct", "body.muscle_mass"]
        span = window(period, start, end, "last_90d")
        return envelope(
            views.body(engine, span, metrics),
            span=span,
            metrics=metrics,
            warnings=["informational, not a medical assessment"],
        )

    @tool(READ, "read")
    def vibesynergy_blood_pressure(
        period: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Blood-pressure readings with local time and source, morning/evening averages and
        the guideline category of each reading (default last_90d). Informational only."""
        span = window(period, start, end, "last_90d")
        return envelope(
            views.blood_pressure(engine, span, settings.timezone),
            span=span,
            warnings=["informational, not a diagnosis"],
        )

    @tool(READ, "read")
    def vibesynergy_intensity_distribution(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        basis: str = "hr",
        include_coaching: bool = True,
    ) -> dict[str, Any]:
        """Time in each heart-rate, power or pace zone per week and in total, counted by
        VibeSynergy from each activity's stream with the zones of that day (heart rate: five
        Friel zones from LTHR, 5a-5c as zone 5; power: seven Coggan zones from FTP, rides;
        pace: seven Friel zones from threshold pace, runs). Heart rate also includes the
        low / moderate / high split and a polarisation index. Default last_90d."""
        span = window(period, start, end, "last_90d")
        return envelope(
            views.intensity_distribution(
                engine,
                span,
                basis=basis,
                include_coaching=include_coaching,
                parquet=rt.parquet,
                athlete=settings.athlete,
            ),
            span=span,
        )

    @tool(READ, "read")
    def vibesynergy_profile() -> dict[str, Any]:
        """Current heart-rate and power zones and thresholds per sport, with their history
        (valid_from dates), so old activities can be judged against the zones of the time."""
        return envelope(views.profile(engine))

    @tool(READ, "read")
    def vibesynergy_athlete() -> dict[str, Any]:
        """Profile, thresholds, plan-calendar races, and notes for the coach.
        The notes are the athlete's own words. This tool cannot change them."""
        return envelope(coach.athlete_for_coach(engine, settings))
