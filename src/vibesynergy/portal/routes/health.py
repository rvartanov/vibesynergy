"""Portal API routes: health (split from portal/api.py). Order matters: api.py
registers these modules in sequence, so routes match exactly as before."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import Query, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from vibesynergy import dropfolder, history, imports, uploads
from vibesynergy.core.timeutil import Window
from vibesynergy.i18n import translator
from vibesynergy.portal.api import (
    BACKFILL_PRIORITY,
    BODY_METRICS,
    COVERAGE_METRICS,
    MAX_LAG,
    MAX_SQL_ROWS,
    RESYNC_PRIORITY,
    TRAINING_SPORTS,
    PortalKit,
    RacePriorityRequest,
    ResyncRequest,
    SqlRequest,
    UploadStart,
    _date,
    _jobs,
    log,
)
from vibesynergy.query import (
    analysis,
    best_source,
    calendar_mirror,
    catalog,
    latency,
    periods,
    sql,
    training,
    views,
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

    @router.get("/body")
    def body_page(
        period: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Body page: every weigh-in and blood pressure reading, daily composition series and
        period statistics; the same query as the MCP tool vibesynergy_body."""
        span = periods.resolve(period, start, end, today=today(), default="last_365d")
        first = qs.first_date(engine, ["body.weight", "bp.systolic"])
        if first and span.start < first <= span.end:
            span = Window(first, span.end)
        data = views.body(engine, span, BODY_METRICS)
        # One reading per measurement: the best source's (Garmin mirrors Withings weigh-ins).
        present: dict[str, set[str]] = {}
        for r in data["measurements"]:
            present.setdefault(r["metric"], set()).add(r["source"])
        chosen = {}
        for metric, found in present.items():
            order = best_source.source_order(settings, metric)
            chosen[metric] = min(found, key=lambda s: order.index(s) if s in order else len(order))
        data["measurements"] = [
            r for r in data["measurements"] if r["source"] == chosen[r["metric"]]
        ]
        latest = {}
        for m in ("body.weight", "body.fat_pct", "body.muscle_mass", "bp.systolic"):
            rows = [r for r in data["measurements"] if r["metric"] == m]
            if rows:
                latest[m] = rows[-1]
        data["latest"] = latest
        return envelope(
            data,
            period={"start": str(span.start), "end": str(span.end)},
            units={m: catalog.KNOWN[m].unit for m in BODY_METRICS if m in catalog.KNOWN},
        )

    @router.get("/correlate")
    def correlate_page(
        x: str,
        y: str,
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        lag: int = 0,
        method: str = "spearman",
        exclude_tags: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        """Correlations page: r per lag 0..MAX_LAG (as the MCP tool vibesynergy_correlate) and
        the day pairs behind the chosen lag, for the scatter plot; ``exclude_tags`` leaves
        out days with those diary tags."""
        catalog.check_metrics(engine, [x, y])
        if not 0 <= lag <= MAX_LAG:
            raise ValueError(f"lag must be 0 to {MAX_LAG} days")
        span = periods.resolve(period, start, end, today=today(), default="last_365d")
        data = analysis.correlate(
            engine,
            x,
            y,
            span,
            lags=range(0, MAX_LAG + 1),
            method=method,
            exclude_tags=exclude_tags,
        )
        shift = timedelta(days=lag)
        values = qs.daily_values(
            engine, [x, y], Window(span.start, span.end + shift), exclude_tags=exclude_tags
        )
        vx, vy = values[x], values[y]
        data["lag"] = lag
        data["points"] = [
            [str(d), vx[d], vy[d + shift]]
            for d in sorted(vx)
            if span.start <= d <= span.end and d + shift in vy
        ]
        return envelope(
            data,
            period={"start": str(span.start), "end": str(span.end)},
            units={m: catalog.KNOWN[m].unit for m in (x, y) if m in catalog.KNOWN},
        )

    @router.get("/power_curve")
    def power_curve_page(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        sport: str = "cycl",
    ) -> dict[str, Any]:
        """Best average power per duration (slow over long periods: every power stream is
        read), for one sport at a time: running and cycling power don't compare."""
        if sport not in TRAINING_SPORTS:
            raise ValueError(f"sport must be one of {', '.join(TRAINING_SPORTS)}")
        span = periods.resolve(period, start, end, today=today(), default="last_90d")
        data = training.power_curve(engine, rt.parquet, span, sport=sport)
        return envelope(data, period={"start": str(span.start), "end": str(span.end)})

    @router.get("/files")
    def files_page() -> dict[str, Any]:
        """The drop folder and what was imported from it lately."""
        return envelope(
            {
                "folder": str(dropfolder.folder(settings)),
                "history": dropfolder.history(settings),
                "every_minutes": dropfolder.DROP_MINUTES,
            }
        )

    @router.post("/files/upload")
    async def files_upload(files: list[UploadFile], lang: str | None = None) -> dict[str, Any]:
        """Upload activity files (FIT, GPX, TCX or a zip): written to the drop folder in
        pieces (never held in memory) and imported straight away. An account export zip
        is recognised and imported in the background, whatever its size."""
        tr = translator(lang)
        if not files:
            raise ValueError(tr("choose one or more files"))
        for upload in files:
            name = upload.filename or "upload"
            size = upload.size or 0
            try:
                uploads.check_space(dropfolder.folder(settings), size)
            except uploads.NotEnoughSpace as exc:
                raise ValueError(
                    tr(
                        "There isn't enough free space for this file. "
                        "Free up {gb} GB and try again.",
                        gb=max(1, -(-exc.short // 1024**3)),
                    )
                ) from None
            target = dropfolder.upload_path(settings, name)
            part = target.with_name(f".{target.name}.part")  # the drop folder skips dot files
            try:
                with part.open("wb") as fh:
                    while chunk := await upload.read(1024 * 1024):
                        await run_in_threadpool(fh.write, chunk)
                big = part.stat().st_size > dropfolder.MAX_BYTES
                if big and not (
                    name.lower().endswith(".zip")
                    and await run_in_threadpool(imports.archive_kind, part)
                ):
                    raise ValueError(
                        tr(
                            "{file} is too large to import as loose files. If it's a "
                            "Garmin or Strava export, upload it under History.",
                            file=name,
                        )
                    )
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            part.rename(target)
        results = await run_in_threadpool(dropfolder.scan, rt, settle_s=0)
        return envelope({"results": results})

    _CALENDAR_MAX_DAYS = 3660
    _AGENDA_MAX_DAYS = 366

    _CALENDAR_MIN_YEAR = 1990

    @router.get("/calendar")
    def calendar_page(
        request: Request,
        year: int | None = None,
        metric: str = "activity.duration",
        period: str | None = None,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        agenda: str | None = None,
        agenda_start: str | None = None,
        agenda_end: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Calendar heatmap: daily values for one metric in a span (``load`` is TRIMP), plus
        events. Agenda range uses ``agenda`` or ``agenda_start``/``agenda_end`` (legacy
        ``start``/``end``)."""
        tdy = today()

        def _in_bounds(d: date) -> bool:
            return _CALENDAR_MIN_YEAR <= d.year <= tdy.year + 1

        if from_ or to:
            if not (from_ and to):
                raise ValueError("Choose both start and end dates for the heatmap")
            span = Window(_date(from_), _date(to))
        elif period:
            span = periods.resolve(period, None, None, today=tdy, default=str(tdy.year))
        elif year:
            if not _CALENDAR_MIN_YEAR <= year <= tdy.year + 1:
                raise ValueError("choose a year with data")
            span = Window(date(year, 1, 1), date(year, 12, 31))
        else:
            y = tdy.year
            span = Window(date(y, 1, 1), date(y, 12, 31))
        if not _in_bounds(span.start) or not _in_bounds(span.end):
            raise ValueError("choose a year with data")
        if len(span) > _CALENDAR_MAX_DAYS:
            raise ValueError(f"Choose a range of up to {_CALENDAR_MAX_DAYS} days")
        heatmap_year = span.start.year if span.start.year == span.end.year else year or tdy.year
        if metric == "load":
            profile = training.hr_profile(engine, settings.athlete)
            values = {
                d: round(v, 1) for d, v in training.daily_trimp(engine, span, profile).items()
            }
        else:
            catalog.check_metrics(engine, [metric])
            values = qs.daily_values(engine, [metric], span)[metric]
        shared = bool(request.scope.get("vibesynergy_share"))
        events = [
            i
            for i in plan_q.plan(engine, span, tdy, include_calendar=False)["items"]
            if i["kind"] == "event"
        ]
        leg_start = agenda_start or start
        leg_end = agenda_end or end
        if agenda:
            agenda_span = periods.resolve(agenda, None, None, today=tdy, default="next_14d")
        elif leg_start or leg_end:
            if not (leg_start and leg_end):
                raise ValueError("Choose both start and end dates for the agenda")
            agenda_span = periods.resolve(None, leg_start, leg_end, today=tdy)
        else:
            agenda_span = periods.resolve("next_14d", today=tdy)
        if len(agenda_span) > _AGENDA_MAX_DAYS:
            raise ValueError(f"Choose an agenda range of up to {_AGENDA_MAX_DAYS + 1} days")
        agenda_start_d, agenda_end_d = agenda_span.start, agenda_span.end
        agenda = (
            plan_q.plan(engine, Window(agenda_start_d, agenda_end_d), tdy) if not shared else None
        )
        first = qs.first_date(engine, ["activity.count", "hr.resting", "body.weight"])
        year_lo = first.year if first else heatmap_year
        years = list(range(year_lo, tdy.year + 1))
        if heatmap_year not in years:
            years.append(heatmap_year)
            years.sort()
        return envelope(
            {
                "year": heatmap_year,
                "span": {"start": str(span.start), "end": str(span.end)},
                "agenda": agenda["items"] if agenda else None,
                "calendar_sync": agenda["calendar_sync"] if agenda else None,
                "schedule_sync": agenda["schedule_sync"] if agenda else None,
                "calendar_mirror": agenda["calendar_mirror"] if agenda else None,
                "mirror_active": calendar_mirror.mirror_active(rt.settings) if agenda else False,
                "agenda_window": {"start": str(agenda_start_d), "end": str(agenda_end_d)},
                "metric": metric,
                "days": {str(d): v for d, v in sorted(values.items())},
                "events": [
                    {"date": e["date"], "title": e["title"], "is_race": e["is_race"]}
                    for e in events
                ],
                "years": years,
                **({} if shared else {"races": plan_q.calendar_races(engine, tdy)}),
            }
        )

    @router.post("/race-priority")
    def save_race_priority(request: RacePriorityRequest) -> dict[str, Any]:
        """Store A, B or C for one race. Null clears the choice. Sync does not overwrite it."""
        from vibesynergy.query.race_priority import set_priority

        try:
            saved = set_priority(engine, request.source, request.source_id, request.priority)
            return envelope(saved)
        except LookupError:
            return JSONResponse(status_code=404, content={"error": "race_not_found"})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @router.get("/pace_curve")
    def pace_curve_page(
        period: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Fastest running times for 400 m to the half marathon (reads every run's stream)."""
        span = periods.resolve(period, start, end, today=today(), default="last_365d")
        data = training.pace_curve(engine, rt.parquet, span)
        return envelope(data, period={"start": str(span.start), "end": str(span.end)})

    @router.get("/data")
    def data_page() -> dict[str, Any]:
        """Data & sync: sources, every dataset's sync state, problems, the job queue and
        the raw archive per dataset (as `vibesynergy status` and vibesynergy_sync_status)."""
        status = views.sync_status(engine)
        enabled = set(rt.registry.enabled_ids())
        sources = [
            {
                "id": sid,
                "name": cls.name,
                "enabled": sid in enabled,
                "configured": rt.registry.get(sid, require_enabled=False).configured(),
                "datasets": [
                    {"name": d.name, "kind": d.kind.value, "description": d.description}
                    for d in rt.registry.get(sid, require_enabled=False).sync_datasets()
                ],
            }
            for sid, cls in rt.registry.available().items()
            if sid in enabled or any(d["source"] == sid for d in status["datasets"])
        ]
        unset = {s["id"] for s in sources if s["enabled"] and not s["configured"]}
        problems = [p for p in status["problems"] if p["source"] not in unset]
        archive = [
            {**r, "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None}
            for r in rt.raw.stats()
        ]
        return envelope(
            {
                "sources": sources,
                "datasets": status["datasets"],
                "problems": problems,
                "status_semantics": status["semantics"],
                "jobs": {"counts": rt.jobs.counts(), "recent": _jobs(rt.jobs.recent(limit=40))},
                "archive": archive,
                "latency": latency.recent(engine, 1),
            }
        )

    @router.get("/latency")
    def latency_page(limit: int = 20) -> dict[str, Any]:
        """Private, read-only source and dashboard delivery stages."""
        return envelope(latency.recent(engine, limit))

    @router.get("/coverage")
    def coverage_page(months: int = 24) -> dict[str, Any]:
        """Days with data per month for the key metrics (the coverage map)."""
        months = max(1, min(months, 240))
        end = today()
        first_month = (end.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
        found = qs.first_date(engine, COVERAGE_METRICS)
        start = max(first_month, found.replace(day=1)) if found else first_month
        data = views.coverage(engine, COVERAGE_METRICS, Window(start, end))
        return envelope(data, period={"start": str(start), "end": str(end)})

    @router.get("/sql/schema")
    def sql_schema() -> dict[str, Any]:
        """The tables the SQL notebook can query, as the MCP tool vibesynergy_schema."""
        return envelope({"dialect": "SQLite", "tables": sql.TABLES})

    @router.post("/sql")
    def sql_query(request: SqlRequest) -> dict[str, Any]:
        """SQL notebook: one read-only SELECT over the documented tables (the same runner as
        the MCP tool vibesynergy_sql: no writes, row and time limits)."""
        limit = max(1, min(request.limit, MAX_SQL_ROWS))
        started = datetime.now(UTC)
        data = sql.run(settings.db_path, request.query, limit=limit)
        data["seconds"] = round((datetime.now(UTC) - started).total_seconds(), 3)
        return envelope(data)

    @router.get("/metric-status")
    def metric_status_page(
        metric: str,
        source: str,
        dataset: str,
        start: str,
        end: str,
        previous_version: str | None = None,
    ) -> dict[str, Any]:
        """Source/dataset-specific metric coverage and stage freshness."""
        return envelope(
            views.metric_status(
                engine,
                metric,
                source,
                dataset,
                Window(date.fromisoformat(start), date.fromisoformat(end)),
                previous_version=previous_version,
            )
        )

    @router.post("/resync")
    def resync(request: ResyncRequest) -> dict[str, Any]:
        """Queue a sync of one source (optionally one dataset) for a period. Up to a month
        runs first thing; longer periods are split into months that run in the background,
        oldest first, like `vibesynergy backfill`."""
        if request.source not in rt.registry.enabled_ids():
            raise ValueError(f"{request.source!r} is not an enabled source")
        source = rt.registry.get(request.source)
        if request.dataset and request.dataset not in {d.name for d in source.datasets()}:
            raise ValueError(f"{request.source} has no dataset {request.dataset!r}")
        first, last = _date(request.start), _date(request.end)
        if first > last:
            raise ValueError("the start date is after the end date")
        if last > today():
            raise ValueError("the end date is in the future")
        if reason := history.api_history_refused(request.source, first, today()):
            raise ValueError(reason)
        span = Window(first, last)
        chunks = (
            [span]
            if len(span) <= 31
            else [Window(max(m.start, first), min(m.end, last)) for m in span.months()]
        )
        priority = RESYNC_PRIORITY if len(chunks) == 1 else BACKFILL_PRIORITY
        queued = []
        for chunk in chunks:
            payload = {"source": request.source, "dataset": request.dataset, "window": str(chunk)}
            job_id = rt.jobs.enqueue(
                "sync",
                payload,
                priority=priority,
                dedupe_key=f"sync:{request.source}:{request.dataset}:{chunk}",
            )
            queued.append({"window": str(chunk), "job": job_id})
        return envelope({"queued": queued, "background": len(chunks) > 1})

    # History: the filler (API runs month by month, account archives in the background).

    @router.get("/history")
    def history_page() -> dict[str, Any]:
        """Every history fill with its progress, and per enabled source that can be filled:
        the suggested period, how long the API would take, whether it has an export, and
        which of the two is recommended."""
        sources = []
        for sid in rt.registry.enabled_ids():
            try:
                if history.history_datasets(rt, sid):
                    sources.append(history.estimate(rt, sid, today()))
            except Exception:  # a source that can't say; it just isn't offered here
                log.exception("history estimate for %s failed", sid)
        return envelope(
            {
                "fills": history.fills(rt),
                "sources": sources,
                "archives": history.ARCHIVES,
                "exports": history.EXPORTS,
                "max_upload_gb": uploads.MAX_BYTES // 1024**3,
            }
        )

    @router.post("/history/start")
    def history_start(request: ResyncRequest) -> dict[str, Any]:
        """Start filling a source's history for a period, in the background."""
        fill = history.start_api(
            rt,
            request.source,
            _date(request.start),
            _date(request.end),
            today=today(),
            datasets=[request.dataset] if request.dataset else None,
        )
        return envelope(fill)

    @router.post("/history/upload")
    async def history_upload(request: Request) -> dict[str, Any]:
        """Upload an account export zip (Garmin's data export or Strava's archive) as the
        request body, streamed to the drop folder, then imported in the background."""
        name = unquote(request.headers.get("x-filename") or "archive.zip")
        if not name.lower().endswith(".zip"):
            raise ValueError("choose the export's .zip file")
        target = dropfolder.upload_path(settings, name)
        part = target.with_name(f".{target.name}.part")  # the drop folder skips dot files
        size = 0
        try:
            with part.open("wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > uploads.MAX_BYTES:
                        raise ValueError(f"larger than {uploads.MAX_BYTES // 1024**3} GB")
                    await run_in_threadpool(fh.write, chunk)
            kind = await run_in_threadpool(imports.archive_kind, part)
            if kind is None:
                raise ValueError(
                    f"{name} isn't a Garmin or Strava account export; activity files go "
                    "under Import files"
                )
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        part.rename(target)
        return envelope(history.start_archive(rt, target, kind))

    # Resumable uploads: the page sends the zip in chunks and continues after an interruption.

    @router.post("/history/uploads")
    def history_upload_start(request: UploadStart, lang: str | None = None) -> dict[str, Any]:
        uploads.clean(settings)
        try:
            return envelope(uploads.start(settings, request.name, request.size))
        except uploads.NotEnoughSpace as exc:
            raise ValueError(
                translator(lang)(
                    "There isn't enough free space for this file. Free up {gb} GB and try again.",
                    gb=max(1, -(-exc.short // 1024**3)),
                )
            ) from None

    @router.get("/history/uploads/{upload_id}")
    def history_upload_status(upload_id: str) -> dict[str, Any]:
        return envelope(uploads.status(settings, upload_id))
