"""Start page blocks beyond the dashboard reply: body and measurements, what is coming
up, and the best running efforts over a chosen period. Thin: the work is in
``vibesynergy.query.start_page``. Shared views reach these through ``portal.share.PAGES``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter

from vibesynergy.core.timeutil import today_in
from vibesynergy.query import start_page, views
from vibesynergy.runtime import Runtime


def build_start_router(rt: Runtime) -> APIRouter:
    router = APIRouter()
    engine = rt.engine

    def today() -> date:
        return today_in(rt.settings.timezone)

    def reply(data: Any) -> dict[str, Any]:
        return {
            "data": data,
            "meta": {"timezone": rt.settings.timezone, "freshness": views.freshness(engine)},
        }

    @router.get("/body-summary")
    def body_summary(date: str | None = None) -> dict[str, Any]:
        """Weight with its trend, latest body composition and blood pressure, with dates."""
        when = _day(date) if date else today()
        if when > today():
            raise ValueError("choose a date up to today")
        return reply(start_page.body(engine, when))

    @router.get("/upcoming")
    def upcoming() -> dict[str, Any]:
        """Planned workouts and races, today and the next six days."""
        return reply(start_page.upcoming(engine, today()))

    @router.get("/best-runs")
    def best_runs(period: str = start_page.BEST_RUN_PERIODS[0]) -> dict[str, Any]:
        """Fastest running times over one of the start page's periods, with its dates."""
        return reply(start_page.best_runs(engine, rt.parquet, period, today()))

    @router.get("/bests")
    def bests(sport: str = "run", period: str = start_page.BEST_RUN_PERIODS[0]) -> dict[str, Any]:
        """Best efforts per sport or all together, over one of the start page's periods."""
        return reply(start_page.bests(engine, rt.parquet, sport, period, today()))

    return router


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not a date: {text!r} (use YYYY-MM-DD)") from exc
