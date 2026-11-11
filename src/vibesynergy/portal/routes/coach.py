"""Portal API routes: coach (split from portal/api.py). Order matters: api.py
registers these modules in sequence, so routes match exactly as before."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import Query
from fastapi.responses import JSONResponse

from vibesynergy.core.timeutil import Window
from vibesynergy.portal.api import (
    COMPARES,
    MAX_EXPLORER_METRICS,
    CoachLogRevert,
    KnowledgeArticleEdit,
    KnowledgeDecision,
    KnowledgeRuleEdit,
    PortalKit,
    PublicationRequest,
)
from vibesynergy.query import (
    analysis,
    annotations,
    assessments,
    catalog,
    coach_workouts,
    knowledge,
    periods,
    publication,
)
from vibesynergy.query import series as qs


def register(kit: PortalKit) -> None:
    router = kit.router
    rt = kit.rt
    engine = kit.engine
    today = kit.today
    envelope = kit.envelope

    @router.get("/coach/tasks")
    def coach_tasks_route() -> dict[str, Any]:
        from vibesynergy.core.timeutil import utc_now
        from vibesynergy.query import coach_tasks

        return envelope({"tasks": coach_tasks.status(engine, utc_now(), rt.settings.timezone)})

    @router.post("/coach/threshold-confirm/{entry_id}")
    def coach_threshold_confirm(entry_id: int) -> dict[str, Any]:
        try:
            return envelope(coach_workouts.confirm_threshold_change(engine, entry_id))
        except LookupError:
            return JSONResponse(status_code=404, content={"error": "proposal_not_found"})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @router.get("/activity-assessment")
    def activity_assessment(id: str) -> dict[str, Any]:
        """Private shared assessment and freshness. Reading never creates work."""
        return envelope(assessments.read(rt, id))

    @router.get("/current-coaching-state")
    def current_coaching_state(id: str | None = None) -> dict[str, Any]:
        """Read the current coaching version without creating assessment work."""
        return envelope(assessments.current_state(rt, id))

    @router.get("/activity-annotation-preview")
    def activity_annotation_preview(id: str, lang: str = "en") -> dict[str, Any]:
        """Additional public-safe block only; no provider calls or publication authority."""
        return envelope(annotations.preview(rt, id, language=lang))

    @router.get("/activity-publication-preview")
    def activity_publication_preview(
        id: str, destination: Literal["strava", "garmin"], lang: str = "en"
    ) -> dict[str, Any]:
        return envelope(publication.preview(rt, id, destination, language=lang))

    @router.post("/activity-publication")
    def activity_publication(request: PublicationRequest) -> dict[str, Any]:
        return envelope(
            publication.publish(
                rt,
                request.id,
                request.destination,
                request.preview_version,
                language=request.language,
            )
        )

    @router.post("/activity-publication/remove")
    def activity_publication_remove(request: PublicationRequest) -> dict[str, Any]:
        return envelope(
            publication.unpublish(
                rt,
                request.id,
                request.destination,
                request.preview_version,
                language=request.language,
            )
        )

    @router.get("/daily-coaching")
    def daily_coaching(date: str | None = None) -> dict[str, Any]:
        """Morning, evening, weekly and block reviews with freshness. Never creates work."""
        from datetime import date as _date

        from vibesynergy.query import daily_coach

        return envelope(daily_coach.read(rt, _date.fromisoformat(date) if date else None))

    @router.get("/knowledge")
    def knowledge_index() -> dict[str, Any]:
        """Articles and changes waiting for a decision."""
        return envelope(knowledge.portal_index(engine))

    @router.get("/knowledge/article")
    def knowledge_article(slug: str) -> dict[str, Any]:
        try:
            return envelope(knowledge.get_article(engine, slug))
        except LookupError:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    def _knowledge_decision(proposal_id: str, decide) -> dict[str, Any]:
        try:
            return envelope(decide(engine, proposal_id, actor="user"))
        except LookupError:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        except knowledge.KnowledgeError as exc:
            return JSONResponse(status_code=409, content={"error": exc.code})

    @router.post("/knowledge/proposal/approve")
    def knowledge_approve(request: KnowledgeDecision) -> dict[str, Any]:
        return _knowledge_decision(request.id, knowledge.approve_proposal)

    @router.post("/knowledge/proposal/reject")
    def knowledge_reject(request: KnowledgeDecision) -> dict[str, Any]:
        return _knowledge_decision(request.id, knowledge.reject_proposal)

    def _knowledge_write(write) -> Any:
        try:
            return envelope(write())
        except LookupError:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        except knowledge.KnowledgeError as exc:
            return JSONResponse(status_code=409, content={"error": exc.code})

    @router.get("/knowledge/methods")
    def knowledge_methods() -> dict[str, Any]:
        """Coach → Methods & rules: routine prompts, working method and evidence."""
        return envelope(knowledge.methods_index(engine))

    @router.get("/knowledge/archive")
    def knowledge_archive() -> dict[str, Any]:
        """Articles out of use. They stay in the change log and can be brought back."""
        return envelope({"articles": knowledge.archived_articles(engine)})

    @router.post("/knowledge/article/edit")
    def knowledge_article_edit(request: KnowledgeArticleEdit) -> Any:
        """Your own edit: saved at once as a new version (the change log keeps the old one)."""
        return _knowledge_write(
            lambda: knowledge.edit_article(
                engine,
                request.slug,
                author="user",
                expected_version=request.expected_version,
                title=request.title,
                body_md=request.body_md,
                status=request.status,
            )
        )

    @router.post("/knowledge/rule/edit")
    def knowledge_rule_edit(request: KnowledgeRuleEdit) -> Any:
        return _knowledge_write(
            lambda: knowledge.edit_rule(
                engine,
                request.id,
                author="user",
                expected_version=request.expected_version,
                text_value=request.text,
                active=request.active,
            )
        )

    @router.post("/knowledge/seed-prompts")
    def knowledge_seed_prompts() -> dict[str, Any]:
        """Copy docs/coach-prompts into the knowledge base; safe to repeat."""
        from vibesynergy.query import coach_prompts

        return envelope(
            {"prompts": coach_prompts.seed(engine, overlay=coach_prompts.overlay_dir(rt.settings))}
        )

    @router.get("/coach-log")
    def coach_log_entries(
        kind: str | None = None,
        target: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Every version of the knowledge, rules and coach memory, newest first."""
        from vibesynergy.query import coach_log

        return envelope(
            coach_log.entries(engine, kind=kind, target=target, before=before, limit=limit)
        )

    @router.get("/coach-log/diff")
    def coach_log_diff(kind: str, target: str, version: int) -> Any:
        from vibesynergy.query import coach_log

        try:
            return envelope(coach_log.diff(engine, kind, target, version))
        except LookupError:
            return JSONResponse(status_code=404, content={"error": "not_found"})

    @router.post("/coach-log/revert")
    def coach_log_revert(request: CoachLogRevert) -> Any:
        """Bring an older version back as the newest one. History is kept."""
        from vibesynergy.query import coach_log

        actor = "portal" if request.kind in coach_log.MEMORY_KINDS else "user"
        return _knowledge_write(
            lambda: coach_log.revert(
                engine, request.kind, request.target, request.version, actor=actor
            )
        )

    @router.get("/catalog")
    def catalog_page() -> dict[str, Any]:
        """Every metric with data (as the MCP tool vibesynergy_catalog), for the metric picker."""
        entries = catalog.catalogue(engine, coverage=True)
        return envelope({"metrics": list(entries.values())})

    @router.get("/series")
    def series_page(
        metrics: Annotated[list[str], Query()],
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        granularity: str | None = None,
        compare: str | None = None,
    ) -> dict[str, Any]:
        """Metrics explorer: the same aggregation as the MCP tool vibesynergy_series, plus
        summary statistics and, optionally, a comparison period shifted onto this one."""
        if not 1 <= len(metrics) <= MAX_EXPLORER_METRICS:
            raise ValueError(f"choose 1 to {MAX_EXPLORER_METRICS} metrics")
        catalog.check_metrics(engine, metrics)
        span = periods.resolve(period, start, end, today=today(), default="last_90d")
        first = qs.first_date(engine, metrics)
        if first and span.start < first <= span.end:  # "all time" starts at the first value
            span = Window(first, span.end)
        data = qs.series(engine, metrics, span, granularity=granularity or None, max_points=400)
        result: dict[str, Any] = {"series": data, "stats": qs.stats(engine, metrics, span)}
        if compare:
            if compare not in COMPARES:
                raise ValueError(f"compare must be one of {', '.join(COMPARES)}")
            other = analysis.previous_window(span, compare)
            shift = span.start - other.start
            before = qs.series(
                engine, metrics, other, granularity=data["granularity"], max_points=10_000
            )
            # Each comparison value is placed on the bucket it lines up with in this period.
            for row in before["rows"]:
                row[0] = str(qs.bucket(date.fromisoformat(row[0]) + shift, data["granularity"]))
            result["compare"] = {
                "kind": compare,
                "period": {"start": str(other.start), "end": str(other.end)},
                "series": before,
                "stats": qs.stats(engine, metrics, other),
            }
        return envelope(
            result,
            period={"start": str(span.start), "end": str(span.end)},
            units={m: catalog.KNOWN[m].unit for m in metrics if m in catalog.KNOWN},
        )

    @router.get("/schedule")
    def schedule_page(start: str, end: str) -> dict[str, Any]:
        """Training → Calendar: per day planned and done, per week against targets."""
        from vibesynergy.query import schedule

        return envelope(
            schedule.calendar(
                engine, date.fromisoformat(start), date.fromisoformat(end), today(), raw=rt.raw
            )
        )
