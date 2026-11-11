"""Portal API routes: core (split from portal/api.py). Order matters: api.py
registers these modules in sequence, so routes match exactly as before."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import JSONResponse

from vibesynergy import __version__
from vibesynergy.core.timeutil import Window
from vibesynergy.portal.api import (
    INTRADAY_METRICS,
    INTRADAY_MINUTES,
    TODAY_METRICS,
    TREND_DAYS,
    ActivityEffortRequest,
    ActivityGearRequest,
    ActivityPurposeRequest,
    GearDefaultRequest,
    GearEdit,
    GearServiceAdd,
    GearServiceDone,
    PortalKit,
    _date,
    tile_url,
)
from vibesynergy.query import (
    activity_effort,
    catalog,
    coach_workouts,
    periods,
    training,
    views,
    workouts,
)
from vibesynergy.query import plan as plan_q
from vibesynergy.query import series as qs


def register(kit: PortalKit) -> None:
    router = kit.router
    rt = kit.rt
    engine = kit.engine
    settings = kit.settings
    today = kit.today
    envelope = kit.envelope

    @router.get("/about")
    def about_version() -> dict[str, Any]:
        """The running version, commit, schema and last deploy. Read-only."""
        from vibesynergy.query import about

        return envelope(about.about(rt))

    @router.get("/meta")
    def meta(request: Request) -> dict[str, Any]:
        """What every page needs once: today's date, units, sources, the metric catalogue;
        behind a share link also what it shows (title, pages, whether it may write)."""
        status = views.sync_status(engine)
        link = request.scope.get("vibesynergy_share")
        return envelope(
            {
                "version": __version__,
                "today": str(today()),
                "units": settings.units,
                "weather_units": {"temperature": settings.temperature, "wind": settings.wind},
                "sources": sorted({d["source"] for d in status["datasets"]}),
                "problems": len(status["problems"]),
                "metrics": {m.id: m.to_dict() for m in catalog.KNOWN.values()},
                "tiles": tile_url(settings),
                "share": link.public() if link else None,
            }
        )

    @router.get("/today")
    def today_page(request: Request, date: str | None = None) -> dict[str, Any]:
        """The day page's summary (fast; the timeline comes from /day): the day's numbers
        with baselines, 14-day trends and what was planned for the day."""
        when = _date(date) if date else today()
        span = Window(when - timedelta(days=TREND_DAYS - 1), when)
        day = views.day(engine, when)
        activity_effort.attach_full(engine, rt.raw, day["activities"])
        baselines = {m: b for m in TODAY_METRICS if (b := qs.baseline(engine, m, when)) is not None}
        trend = qs.series(engine, TODAY_METRICS, span, granularity="day", max_points=TREND_DAYS)
        calendar = plan_q.plan(
            engine,
            Window(when, when),
            today(),
            include_calendar=not request.scope.get("vibesynergy_share"),
            include_sync_status=False,
        )
        return envelope(
            {
                "date": str(when),
                "day": day,
                "baselines": baselines,
                "trend": trend,
                "units": {m: catalog.KNOWN[m].unit for m in TODAY_METRICS},
                "plan": calendar["items"],
                "not_in_plan": calendar["not_in_plan"],
            },
            period={"start": str(span.start), "end": str(span.end)},
        )

    @router.get("/day")
    def day_page(date: str | None = None) -> dict[str, Any]:
        """The Day page: everything for one date, with a timeline in local minutes since
        midnight (0-1440) for the intraday samples, the night's sleep stages and activities."""
        when = _date(date) if date else today()
        tz = ZoneInfo(settings.timezone)
        midnight = datetime.combine(when, time(), tzinfo=tz)
        day = views.day(engine, when)
        nights = views.sleep(engine, Window(when, when), stages=True)["nights"]
        intraday = training.intraday(
            rt.parquet,
            when,
            settings.timezone,
            metrics=INTRADAY_METRICS,
            resample_min=INTRADAY_MINUTES,
        )

        def minute(iso: str | None) -> float | None:
            if not iso:
                return None
            moment = datetime.fromisoformat(iso)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return round((moment - midnight).total_seconds() / 60, 1)

        stages = [
            {"start": minute(s["start_utc"]), "end": minute(s["end_utc"]), "stage": s["stage"]}
            for s in (nights[0].get("stages", []) if nights else [])
        ]
        activities = [
            {
                "id": (a["sources"] or [a["group_id"]])[0],
                "name": a["name"],
                "sport": a["sport"],
                "start": minute(a["start_utc"]),
                "end": round(
                    (minute(a["start_utc"]) or 0) + (a["elapsed_s"] or a["moving_s"] or 0) / 60, 1
                ),
            }
            for a in day["activities"]
            if a["start_utc"]
        ]
        return envelope(
            {
                "date": str(when),
                "day": day,
                "timeline": {
                    "minutes_per_sample": INTRADAY_MINUTES,
                    "columns": intraday["columns"],
                    "rows": intraday["rows"],
                    "sleep_stages": stages,
                    "activities": activities,
                },
                "units": {m: catalog.KNOWN[m].unit for m in catalog.KNOWN},
            },
            period={"start": str(when), "end": str(when)},
        )

    @router.get("/activities")
    def activities_page(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        sport: str | None = None,
        text: str | None = None,
        sort: str = "date",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Activities list: the same search as the MCP tool vibesynergy_activities."""
        span = periods.resolve(period, start, end, today=today(), default="all")
        data = views.activities(
            engine,
            span,
            sport=sport or None,
            text=text or None,
            sort=sort,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
        data["sports"] = views.sports(engine)
        activity_effort.attach_compact(engine, rt.raw, data["activities"])
        return envelope(data, period={"start": str(span.start), "end": str(span.end)})

    @router.get("/activity")
    def activity_page(id: str, max_points: int = 1500) -> dict[str, Any]:
        """One activity: the merged summary, each source's copy, laps, zones, and its
        streams (averaged into at most max_points samples) for the map and charts."""
        detail = views.activity_detail(engine, rt.parquet, id)
        streams = None
        if detail["streams"]:
            streams = views.activity_streams(
                engine, rt.parquet, id, max_points=max(50, min(max_points, 5000))
            )
        detail["effort"] = activity_effort.for_activity(engine, rt.raw, id)
        try:
            detail["gear"] = workouts.activity_gear_of(engine, rt.raw, id, today())
        except Exception:  # the gear picker must never break the activity page
            detail["gear"] = None
        try:
            from vibesynergy.query import own_load

            detail["own_load"] = own_load.for_activity(engine, rt.raw, rt.parquet, id)
        except Exception:  # a comparison must never break the activity page
            detail["own_load"] = None
        detail["multisport"] = _multisport_of(detail["activity"])
        return envelope({**detail, "stream_data": streams})

    def _multisport_of(act: dict[str, Any]) -> dict[str, Any] | None:
        """The holder's parts, or the holder a part belongs to."""
        day = date.fromisoformat(str(act["local_date"])[:10])
        rows = views.activities(engine, Window(day, day), limit=50)["activities"]
        me = next((r for r in rows if r["group_id"] == act["group_id"]), None)
        if me is None:
            return None
        by_id = {r["group_id"]: r for r in rows}
        brief = ("group_id", "name", "sport", "moving_s", "distance_m")
        if me.get("parts"):
            parts = [by_id[i] for i in me["parts"] if i in by_id]
            return {"parts": [{k: p.get(k) for k in brief} for p in parts]}
        if me.get("part_of") in by_id:
            holder = by_id[me["part_of"]]
            return {"holder": {k: holder.get(k) for k in brief}}
        return None

    @router.post("/activity-effort")
    def activity_effort_save(request: ActivityEffortRequest) -> dict[str, Any]:
        """Save RPE (1-10) and/or Feel (1-5, 1 = Strong) when no platform has a value."""
        return envelope(
            activity_effort.set_effort(
                engine,
                rt.raw,
                request.id,
                rpe=request.rpe,
                feel=request.feel,
                fields=set(request.fields) if request.fields is not None else None,
            )
        )

    @router.post("/activity-purpose")
    def activity_purpose(request: ActivityPurposeRequest) -> dict[str, Any]:
        from vibesynergy.query import session_purpose

        try:
            return envelope(
                session_purpose.set_override(
                    engine, request.id, request.purpose, reason=request.reason
                )
            )
        except LookupError:
            return JSONResponse(status_code=404, content={"error": "activity_not_found"})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @router.get("/inbox")
    def inbox_route() -> dict[str, Any]:
        """What waits on you: decisions first, then notices."""
        from vibesynergy.query import inbox

        return envelope(inbox.inbox(rt, today()))

    @router.get("/inbox/count")
    def inbox_count_route() -> dict[str, Any]:
        from vibesynergy.query import inbox

        return envelope({"count": inbox.count(engine)})

    @router.get("/gear")
    def gear_route() -> dict[str, Any]:
        return envelope(workouts.gear(engine, rt.raw, today()))

    @router.post("/gear-default")
    def gear_default_route(request: GearDefaultRequest) -> dict[str, Any]:
        """Default gear per sport, from today on."""
        return envelope(workouts.set_gear_default(engine, request.sport, request.ref, today()))

    @router.post("/activity-gear")
    def activity_gear_route(request: ActivityGearRequest) -> dict[str, Any]:
        """Change the gear of one activity; it counts in the totals at once."""
        return envelope(workouts.set_activity_gear(engine, request.id, request.ref))

    @router.post("/gear/service/{service_id}/done")
    def gear_service_done(service_id: str, request: GearServiceDone) -> dict[str, Any]:
        workouts.service_done(engine, service_id, km_now=request.km_now, today=today())
        return envelope({"done": service_id})

    @router.post("/gear/service/{service_id}/remove")
    def gear_service_remove(service_id: str) -> dict[str, Any]:
        workouts.remove_service(engine, service_id)
        return envelope({"removed": service_id})

    @router.post("/gear/{ref}/service")
    def gear_service_add(ref: str, request: GearServiceAdd) -> dict[str, Any]:
        service_id = workouts.add_service(
            engine,
            unquote(ref),
            request.what,
            every_km=request.every_km,
            every_days=request.every_days,
            km_now=request.km_now,
            today=today(),
        )
        return envelope({"id": service_id})

    @router.post("/gear/{ref}")
    def gear_edit(ref: str, request: GearEdit) -> dict[str, Any]:
        try:
            return envelope(workouts.set_gear(engine, unquote(ref), **request.model_dump()))
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @router.get("/search")
    def search_route(q: str = "") -> dict[str, Any]:
        """One search over activities, diary notes, coach messages, knowledge and gear."""
        from vibesynergy.query import search

        return envelope(search.search(rt, q[:200]))

    @router.get("/coach/feed")
    def coach_feed_route() -> dict[str, Any]:
        from vibesynergy.query import coach_replies

        entries = coach_workouts.coach_feed(rt)
        replies = coach_replies.for_entries(engine, [e["id"] for e in entries if e.get("id")])
        for entry in entries:
            entry["replies"] = replies.get(entry.get("id"), [])
        return envelope({"entries": entries})
