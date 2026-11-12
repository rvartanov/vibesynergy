"""Portal API routes: views (split from portal/api.py). Order matters: api.py
registers these modules in sequence, so routes match exactly as before."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from vibesynergy.core.timeutil import Window
from vibesynergy.i18n import translator
from vibesynergy.portal.api import (
    HYPNOGRAM_NIGHTS,
    MAX_SLEEP_NIGHTS,
    SLEEP_METRICS,
    TRAINING_SPORTS,
    PlanSessionChange,
    PortalKit,
    _date,
)
from vibesynergy.query import (
    activity_effort,
    coach,
    dashboard,
    periods,
    planning,
    training,
    training_plan,
    views,
    workouts,
)
from vibesynergy.query import series as qs


def register(kit: PortalKit) -> None:
    router = kit.router
    rt = kit.rt
    engine = kit.engine
    settings = kit.settings
    today = kit.today
    envelope = kit.envelope

    @router.get("/weather")
    def weather_route() -> dict[str, Any]:
        """The daily forecast for the calendar's coming days: Open-Meteo for the chosen
        place or where you last trained outside, cached for an hour; empty, never an error."""
        from vibesynergy.query import activity_weather

        found = activity_weather.forecast(rt)
        where = found.get("location") or {}
        # The page needs only which place; coordinates stay out (the calendar can be shared).
        return envelope(
            {**found, "location": {"kind": where.get("kind"), "name": where.get("name")}}
        )

    @router.get("/weather/places")
    def weather_places(q: str = "", lang: str = "nl") -> dict[str, Any]:
        """Places for Settings → System → Weather on the calendar, found by name."""
        from vibesynergy.query import activity_weather

        language = lang if lang in ("nl", "en") else "nl"
        return envelope({"places": activity_weather.places(q, language)})

    def _own_plan_leads() -> None:
        """Sessions change here only in VibeSynergy's own plan; say which case it is not."""
        from vibesynergy.query import plan_model

        if plan_model.current_plan_id(engine) is None:
            raise ValueError("There is no plan yet.")
        if plan_model.source(engine) != "vibesynergy":
            raise ValueError("intervals.icu leads the plan: change it there")

    @router.post("/plan/session/move")
    def plan_session_move(request: PlanSessionChange) -> dict[str, Any]:
        """Move a planned session from the calendar while VibeSynergy leads."""
        from vibesynergy.query import plan_model

        _own_plan_leads()
        if not request.date:
            raise ValueError("date is required")
        out = plan_model.move_session(
            engine,
            session_id=request.session_id,
            day=_date(request.date),
            reason="moved in the calendar",
            author="portal",
        )
        plan_model.mirror_soon(rt.jobs)
        return envelope(out)

    @router.post("/plan/session/skip")
    def plan_session_skip(request: PlanSessionChange) -> dict[str, Any]:
        """Take a planned session out of the plan from the calendar."""
        from vibesynergy.query import plan_model

        _own_plan_leads()
        out = plan_model.skip_session(
            engine, session_id=request.session_id, reason="skipped in the calendar", author="portal"
        )
        plan_model.mirror_soon(rt.jobs)
        return envelope(out)

    @router.get("/activity-weather")
    def activity_weather_route(id: str, lang: str = "en") -> dict[str, Any]:
        """The weather of one outdoor session, or null indoors and when unknown."""
        from vibesynergy.query import activity_weather

        return envelope(activity_weather.for_activity(rt, id, lang))

    @router.get("/schedule/weather")
    def schedule_weather(date: str, lang: str = "en") -> dict[str, Any]:
        """The weather of each outdoor session on one day: Garmin's, else Open-Meteo
        at its start and hour. Indoor sessions have none."""
        from vibesynergy.query import activity_weather

        day = _date(date)
        return envelope({"date": date, "activities": activity_weather.for_day(rt, day, lang)})

    @router.get("/planning")
    def planning_page(week: str | None = None, range: int = 90) -> dict[str, Any]:
        """Planning & progress page: the chosen week against the plan, the trend over
        the weeks and phases, fitness (intervals.icu's), the projection to each race, and
        the plan itself (weeks, phases, races, next nine days)."""
        chosen = None
        if week:
            try:
                chosen = date.fromisoformat(week)
            except ValueError as exc:
                raise ValueError("week must be a date (YYYY-MM-DD)") from exc
        from vibesynergy.query import horizon

        data = planning.page(
            engine,
            rt.raw,
            today(),
            week=chosen,
            athlete=settings.athlete,
            fitness_days=range,
            parquet=rt.parquet,
        )
        # Are the coming days on the Garmin? The dashboard's warnings link here.
        # Only with a plan and a connected Garmin: without either there is nothing to check.
        guard = horizon.for_runtime(rt, today())
        on_garmin = bool(guard and guard.get("has_plan") and rt.registry.source_enabled("garmin"))
        data["horizon"] = guard if on_garmin else None
        from vibesynergy.query import plan_model

        data["plan_source"] = plan_model.source(engine)  # who leads the plan
        return envelope(data)

    @router.get("/race-predictions")
    def race_predictions_page() -> dict[str, Any]:
        """Fitness → Form: predicted race times for 5 km to 50 km (``race_predictions``)."""
        from vibesynergy.query import race_predictions

        return envelope(race_predictions.race_predictions(engine, rt.parquet, today()))

    @router.get("/form")
    def form_page(range: int = 90) -> dict[str, Any]:
        """Fitness → Form: fitness, fatigue and form and the projection to the races."""
        return envelope(
            planning.form(
                engine,
                rt.raw,
                today(),
                athlete=settings.athlete,
                fitness_days=range,
                parquet=rt.parquet,
            )
        )

    @router.get("/plan/builder/draft")
    def plan_builder_draft() -> dict[str, Any]:
        """A first plan from the builder's settings: nothing is written."""
        from vibesynergy.query import plan_builder

        return envelope(plan_builder.draft(engine, settings.data_dir, today()))

    @router.get("/plan/builder")
    def plan_builder_settings() -> dict[str, Any]:
        """The plan builder's settings, the current plan as defaults."""
        from vibesynergy.query import plan_builder

        return envelope(plan_builder.settings(engine, settings.data_dir, today()))

    @router.get("/plan/booklet")
    def plan_booklet(lang: str = "nl") -> Response:
        """The coach booklet: the plan on paper, readable without VibeSynergy."""
        from vibesynergy.query import booklet

        page = booklet.render(booklet.data(engine, today()), "nl" if lang == "nl" else "en")
        return Response(page, media_type="text/html; charset=utf-8")

    @router.get("/plan/export.csv")
    def plan_export() -> Response:
        """The plan as CSV: week notes and sessions, for a spreadsheet."""
        from vibesynergy.query import plan_model

        return Response(
            plan_model.export_csv(engine),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="plan-{today()}.csv"'},
        )

    @router.get("/plan/export.xlsx")
    def plan_export_xlsx(lang: str = "nl") -> Response:
        """The plan as an Excel file (in place of the Google Sheet)."""
        from vibesynergy.query import plan_export

        return Response(
            plan_export.xlsx(
                plan_export.rows(engine),
                "en" if lang == "en" else "nl",
                plan_export.extras(engine, today(), "en" if lang == "en" else "nl"),
            ),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="plan-{today()}.xlsx"'},
        )

    @router.get("/plan/export.pdf")
    def plan_export_pdf(lang: str = "nl") -> Response:
        """The plan as a PDF to keep or print, week by week."""
        from vibesynergy.query import plan_export

        return Response(
            plan_export.pdf(
                plan_export.rows(engine),
                "en" if lang == "en" else "nl",
                sections=plan_export.extras(engine, today(), "en" if lang == "en" else "nl"),
            ),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="plan-{today()}.pdf"'},
        )

    @router.get("/training-plan")
    def training_plan_page() -> dict[str, Any]:
        """Training plan page: the whole intervals.icu plan, read-only (weeks, phases,
        races, and the workouts of the next nine days)."""
        return envelope(training_plan.overview(engine, today()))

    @router.get("/training")
    def training_page(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        sport: str | None = None,
        load: str = "vibesynergy",
        include_coaching: bool = True,
    ) -> dict[str, Any]:
        """Training page: load (CTL/ATL/TSB), weekly volume per sport, time in heart-rate,
        power and pace zones, and fitness markers; the same functions as the MCP training tools."""
        if sport and sport not in TRAINING_SPORTS:
            raise ValueError(f"sport must be one of {', '.join(TRAINING_SPORTS)}")
        span = periods.resolve(period, start, end, today=today(), default="last_90d")
        first = qs.first_date(engine, ["activity.count"])
        if first and span.start < first <= span.end:
            span = Window(first, span.end)
        granularity = "week" if len(span) <= 400 else "month"
        zone_kinds = {
            kind: views.intensity_distribution(
                engine,
                span,
                basis=kind,
                include_coaching=include_coaching,
                parquet=rt.parquet,
                athlete=settings.athlete,
            )
            for kind in ("hr", "power", "pace")
        }
        load_payload = training.training_load(
            engine,
            span,
            sport=sport,
            load=load,
            athlete=settings.athlete,
            include_coaching=include_coaching,
            raw=rt.raw,
            parquet=rt.parquet,
        )
        # The Form tile matches the dashboard when this view ends today.
        if sport is None and load == "vibesynergy" and include_coaching and span.end == today():
            snapshot = training.current_load(rt, today())
            load_payload["today"] = snapshot
            if load_payload.get("current"):
                for key in ("ctl", "atl", "tsb", "form", "form_zone", "form_pct", "ctl_ramp_7d"):
                    load_payload["current"][key] = snapshot[key]
        return envelope(
            {
                "load": load_payload,
                "volume": training.volume(
                    engine, span, granularity=granularity, include_coaching=include_coaching
                ),
                "granularity": granularity,
                "zones": zone_kinds["hr"],
                "zone_kinds": zone_kinds,
                "fitness": training.fitness(engine, span),
            },
            period={"start": str(span.start), "end": str(span.end)},
        )

    @router.get("/dashboard")
    def dashboard_page(request: Request, lang: str = "en") -> dict[str, Any]:
        """Dashboard: verdict, needs attention, plan and race, this week, trends, injuries;
        its sentences in the portal's language."""
        data = dashboard.dashboard(
            engine,
            today(),
            timezone=settings.timezone,
            athlete=settings.athlete,
            sources=rt.registry.enabled_ids(),
            gear=workouts.gear(engine, rt.raw, today())["gear"],
            tr=translator(lang),
            include_calendar=not request.scope.get("vibesynergy_share"),
            rt=rt,
        )
        # The assessment panel still reads advice once. This is only the shared profile
        # identity, so dashboard, activity advice, publication and daily reviews match.
        data["active_profile"] = coach.profile_binding(
            coach.snapshot(engine, raw=rt.raw, timezone=settings.timezone)
        )
        completed = data.get("completed")
        if isinstance(completed, dict) and isinstance(completed.get("activities"), list):
            activity_effort.attach_full(engine, rt.raw, completed["activities"])
        return envelope(data)

    @router.get("/dashboard-metric-status")
    def dashboard_metric_status() -> dict[str, Any]:
        """Private, deferred source/dataset status for the dashboard's recovery evidence."""
        end = today()
        window = Window(end - timedelta(days=13), end)
        return envelope(
            {
                metric: views.selected_metric_status(engine, metric, window)
                for metric in (
                    "recovery.readiness",
                    "hrv.night_avg",
                    "hr.resting",
                    "sleep.duration",
                )
            }
        )

    @router.get("/recovery")
    def recovery_page(
        period: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Recovery page: HRV with your normal band, resting heart rate, readiness,
        Body Battery and stress per day, plus the morning check-ins; vibesynergy_recovery's
        series."""
        from sqlalchemy import select as _select

        from vibesynergy.store.schema import checkin

        span = periods.resolve(period, start, end, today=today(), default="last_28d")
        if len(span) > 400:
            raise ValueError("choose a period of at most 400 days")
        with engine.connect() as conn:
            rows = conn.execute(
                _select(checkin)
                .where(checkin.c.date.between(span.start, span.end))
                .order_by(checkin.c.date.desc())
            ).mappings()
            checkins = [
                {
                    k: (str(v) if k == "date" else v)
                    for k, v in dict(r).items()
                    if k not in ("subject_id", "updated_at")
                }
                for r in rows
            ]
        return envelope(
            {"series": views.recovery(engine, span), "checkins": checkins},
            period={"start": str(span.start), "end": str(span.end)},
        )

    @router.get("/sleep")
    def sleep_page(
        period: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Sleep page: every night with its stages (hypnograms), regularity, and the nightly
        metrics as a series; the same query as the MCP tool vibesynergy_sleep."""
        span = periods.resolve(period, start, end, today=today(), default="last_28d")
        first = qs.first_date(engine, ["sleep.duration"])
        if first and span.start < first <= span.end:
            span = Window(first, span.end)
        if len(span) > MAX_SLEEP_NIGHTS:
            raise ValueError(f"choose a period of at most {MAX_SLEEP_NIGHTS} days")
        data = views.sleep(
            engine, span, stages=len(span) <= HYPNOGRAM_NIGHTS, timezone=settings.timezone
        )
        data["series"] = qs.series(
            engine,
            SLEEP_METRICS,
            span,
            granularity="day" if len(span) <= 120 else "week",
            max_points=400,
        )
        return envelope(data, period={"start": str(span.start), "end": str(span.end)})
