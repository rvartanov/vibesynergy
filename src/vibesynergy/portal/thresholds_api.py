"""Training → Zones & thresholds: read the overview, set a value, take over or dismiss a
coach proposal, and (only when the athlete ticks it) set the values in Garmin and
intervals.icu. Thin: everything lives in ``vibesynergy.query.thresholds`` and ``threshold_push``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from vibesynergy.core.timeutil import today_in
from vibesynergy.query import threshold_push, thresholds, views
from vibesynergy.runtime import Runtime


class Push(BaseModel):
    model_config = ConfigDict(extra="forbid")

    garmin: bool = False
    intervals: bool = False


class SetValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sport: str
    metric: str
    value: float
    method: str = "manual"
    date_set: date | None = None
    activity_id: str | None = None
    push: Push | None = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    push: Push | None = None


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sport: str
    push: Push


def build_thresholds_router(rt: Runtime) -> APIRouter:
    router = APIRouter()
    engine = rt.engine

    def today() -> date:
        return today_in(rt.settings.timezone)

    def reply(data: Any) -> dict[str, Any]:
        return {
            "data": data,
            "meta": {"timezone": rt.settings.timezone, "freshness": views.freshness(engine)},
        }

    def bad(exc: Exception, status: int = 400) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": str(exc)})

    def pushed(sport: str, push: Push | None) -> list[dict[str, Any]]:
        if not push:
            return []
        targets = [
            name
            for name, wanted in (("garmin", push.garmin), ("intervals", push.intervals))
            if wanted
        ]
        return threshold_push.push(rt, sport, targets) if targets else []

    def overview(results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        data = thresholds.overview(engine, today=today())
        data["push_results"] = results or []
        return reply(data)

    @router.get("/thresholds")
    def read() -> dict[str, Any]:
        return overview()

    @router.post("/thresholds/set")
    def set_value(body: SetValue) -> Any:
        try:
            row = thresholds.set_value(
                engine,
                sport=body.sport,
                metric=body.metric,
                value=body.value,
                method=body.method,
                date_set=body.date_set,
                activity_id=body.activity_id,
                today=today(),
            )
        except LookupError as exc:
            return bad(exc, 404)
        except ValueError as exc:
            return bad(exc)
        return overview(pushed(row["sport"], body.push))

    @router.post("/thresholds/{row_id}/accept")
    def accept(row_id: int, body: Decision | None = None) -> Any:
        try:
            row = thresholds.accept(engine, row_id)
        except LookupError as exc:
            return bad(exc, 404)
        except ValueError as exc:
            return bad(exc)
        return overview(pushed(row["sport"], body.push if body else None))

    @router.post("/thresholds/{row_id}/dismiss")
    def dismiss(row_id: int) -> Any:
        try:
            thresholds.dismiss(engine, row_id)
        except LookupError as exc:
            return bad(exc, 404)
        except ValueError as exc:
            return bad(exc)
        return overview()

    @router.post("/thresholds/push")
    def push(body: PushRequest) -> Any:
        sport, _ = thresholds.canonical(body.sport, "lthr")
        if sport not in thresholds.SPORTS:
            return bad(ValueError("sport must be bike, run or swim"))
        return overview(pushed(sport, body.push))

    return router
