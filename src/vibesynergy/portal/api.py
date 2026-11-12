"""JSON API behind the portal pages. Thin: parse parameters, call ``vibesynergy.query``.

Responses look like the MCP tools' envelope: ``{"data": …, "meta": {…}}``; values are SI
(seconds, metres, kilograms, m/s) and the pages format them for display.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from vibesynergy.core.timeutil import today_in
from vibesynergy.portal.tiles import DEFAULT_MAX_AGE_DAYS, DEFAULT_UPSTREAM, TileCache
from vibesynergy.query import (
    views,
)
from vibesynergy.runtime import Runtime

log = logging.getLogger(__name__)

LOCAL_TILES = "tiles/{z}/{x}/{y}.png"  # the server's tile cache, relative to the page

# The key numbers on the Today page, each with a 14-day trend.
TODAY_METRICS = [
    "recovery.readiness",
    "sleep.score",
    "sleep.duration",
    "hrv.night_avg",
    "hr.resting",
    "recovery.body_battery.max",
    "recovery.body_battery.min",
    "recovery.stress.avg",
    "body.weight",
    "daily.steps",
    "activity.duration",
]
TREND_DAYS = 14

# The Day page's timeline: all-day samples, averaged per INTRADAY_MINUTES.
INTRADAY_METRICS = ["hr.sample", "stress.sample", "bodybattery.sample"]
INTRADAY_MINUTES = 5


class PublicationRequest(BaseModel):
    id: str
    destination: Literal["strava", "garmin"]
    preview_version: str
    language: Literal["en", "nl"] = "en"


class KnowledgeDecision(BaseModel):
    id: str


class KnowledgeArticleEdit(BaseModel):
    slug: str
    expected_version: int
    title: str | None = None
    body_md: str | None = None
    status: Literal["active", "retired"] | None = None


class KnowledgeRuleEdit(BaseModel):
    id: str
    expected_version: int
    text: str | None = None
    active: bool | None = None


class GearEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "spare", "retired"] | None = None
    life_km: float | None = Field(default=None, gt=0, lt=100_000)
    note: str | None = Field(default=None, max_length=1000)


class ActivityGearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=3, max_length=200)
    ref: str | None = Field(default=None, max_length=100)  # None: the source's; "none": none


class GearDefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sport: str = Field(min_length=1, max_length=60)
    ref: str | None = Field(default=None, max_length=100)  # None removes the default


class PlanSessionChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=3, max_length=200)
    date: str | None = Field(default=None, max_length=10)  # move: the new day


class GearServiceAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: str = Field(min_length=1, max_length=80)
    every_km: float | None = Field(default=None, gt=0, lt=100_000)
    every_days: int | None = Field(default=None, gt=0, lt=3660)
    km_now: float | None = Field(default=None, ge=0)


class GearServiceDone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    km_now: float | None = Field(default=None, ge=0)


class CoachLogRevert(BaseModel):
    kind: str
    target: str
    version: int


class ActivityEffortRequest(BaseModel):
    id: str
    rpe: int | None = None
    feel: int | None = None
    # Which of rpe/feel to change; a named field that is null clears VibeSynergy's entry.
    fields: list[Literal["rpe", "feel"]] | None = None


class ActivityPurposeRequest(BaseModel):
    id: str
    purpose: Literal["training", "recovery", "leisure", "coached_by_me", "race"] | None = None
    reason: str | None = None


class RacePriorityRequest(BaseModel):
    source: str
    source_id: str
    priority: Literal["A", "B", "C"] | None = None


MAX_EXPLORER_METRICS = 6

# Settings → Sources → Status: the coverage map's rows, and job priorities. Webhooks use 10 and
# scheduled syncs 0; a long re-sync runs below both, so it never holds up the regular updates.
COVERAGE_METRICS = [
    "sleep.score",
    "hrv.night_avg",
    "hr.resting",
    "daily.steps",
    "recovery.body_battery.max",
    "body.weight",
    "bp.systolic",
    "activity.count",
]
# Sleep page: per-night series, and hypnograms only for shorter periods (one row per night).
SLEEP_METRICS = [
    "sleep.score",
    "sleep.duration",
    "sleep.deep",
    "sleep.light",
    "sleep.rem",
    "sleep.awake",
    "sleep.avg_hrv",
    "sleep.avg_hr",
    "sleep.avg_respiration",
]
HYPNOGRAM_NIGHTS = 31

# Body page: weight and composition from the scale, blood pressure from the monitor.
BODY_METRICS = [
    "body.weight",
    "body.fat_pct",
    "body.fat_mass",
    "body.muscle_mass",
    "body.lean_mass",
    "body.water_mass",
    "body.bone_mass",
    "bp.systolic",
    "bp.diastolic",
    "bp.pulse",
]
MAX_SLEEP_NIGHTS = 3660

# SQL notebook: rows returned at most (as vibesynergy_sql).
MAX_SQL_ROWS = 5000

# Correlations page: lags shown, y up to a week after x.
MAX_LAG = 7

# Training page: sport filters match activity sports by substring, like the MCP tools.
TRAINING_SPORTS = ("run", "cycl", "walk", "swim")
RESYNC_PRIORITY = 10
BACKFILL_PRIORITY = -1
COMPARES = ("previous", "same_last_year")


def upstream_tiles(settings) -> str:
    """The tile server the cache fills from (``[portal] tiles``; OpenStreetMap by default)."""
    return str(settings.portal.get("tiles", DEFAULT_UPSTREAM))


def tile_cache_enabled(settings) -> bool:
    return bool(settings.portal.get("tile_cache", True))


def tile_url(settings) -> str:
    """The tile address the map in the browser uses: the local cache unless it is off."""
    return LOCAL_TILES if tile_cache_enabled(settings) else upstream_tiles(settings)


def tile_cache(settings) -> TileCache:
    return TileCache(
        settings.data_dir / "tiles",
        upstream_tiles(settings),
        max_age_days=float(settings.portal.get("tile_max_age_days", DEFAULT_MAX_AGE_DAYS)),
    )


@dataclass(frozen=True)
class PortalKit:
    """What the route modules need from build_router."""

    router: APIRouter
    rt: Any
    engine: Any
    settings: Any
    today: Callable[[], date]
    envelope: Callable[..., dict[str, Any]]
    diary_changed: Callable[..., dict[str, Any]]


def build_router(rt: Runtime) -> APIRouter:
    router = APIRouter()
    engine, settings = rt.engine, rt.settings

    def today() -> date:
        return today_in(settings.timezone)

    def envelope(data: Any, **meta: Any) -> dict[str, Any]:
        return {
            "data": data,
            "meta": {
                "timezone": settings.timezone,
                "freshness": views.freshness(engine),
                **meta,
            },
        }

    def diary_changed(data: Any) -> dict[str, Any]:
        return envelope(data)

    from vibesynergy.portal.routes import coach as routes_coach
    from vibesynergy.portal.routes import core as routes_core
    from vibesynergy.portal.routes import data as routes_data
    from vibesynergy.portal.routes import health as routes_health
    from vibesynergy.portal.routes import views as routes_views

    kit = PortalKit(
        router=router,
        rt=rt,
        engine=engine,
        settings=settings,
        today=today,
        envelope=envelope,
        diary_changed=diary_changed,
    )
    routes_core.register(kit)
    routes_coach.register(kit)
    routes_views.register(kit)
    routes_health.register(kit)
    routes_data.register(kit)

    return router


class SqlRequest(BaseModel):
    query: str
    limit: int = 500


class NoteRequest(BaseModel):
    id: str | None = None
    text: str | None = None
    date: str | None = None
    end: str | None = None
    tags: list[str] | None = None
    delete: bool = False
    # A new note about one activity ("garmin:123"); it also shows on that activity.
    activity_id: str | None = None


class CheckinRequest(BaseModel):
    date: str | None = None
    sleep_quality: int | None = None
    soreness: int | None = None
    mood: int | None = None
    stress: int | None = None
    motivation: int | None = None
    note: str | None = None


class InjuryRequest(BaseModel):
    injury_id: str | None = None
    body_part: str | None = None
    side: str | None = None
    kind: str | None = None
    title: str | None = None
    date: str | None = None
    pain: int | None = None
    timing: str | None = None
    affects_movement: bool | None = None
    note: str | None = None
    status: str | None = None
    notes: str | None = None


class UploadStart(BaseModel):
    name: str
    size: int


class UploadFinish(BaseModel):
    expect: Literal["garmin_export", "strava_export"] | None = None


# A zip uploaded on one source's card that isn't that source's export.
WRONG_ARCHIVE = {
    "garmin_export": "This isn't the Garmin export. Choose the zip Garmin emailed you "
    "(it has a DI_CONNECT folder).",
    "strava_export": "This isn't the Strava export. Choose the zip Strava emailed you "
    "(it has activities.csv).",
}


class ResyncRequest(BaseModel):
    source: str
    dataset: str | None = None
    start: str
    end: str


def _jobs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = ("id", "kind", "payload", "status", "priority", "attempts", "max_attempts", "last_error")
    times = ("created_at", "started_at", "finished_at", "run_after")
    return [
        {
            **{k: r[k] for k in keep},
            **{k: r[k].isoformat() if r[k] else None for k in times},
        }
        for r in rows
    ]


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not a date: {value!r} (use YYYY-MM-DD)") from exc
