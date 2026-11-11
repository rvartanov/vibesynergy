"""VibeSynergy MCP server (docs/MCP.md): tools, resources and the guide for AI clients.

Tool handlers are thin: parse parameters, call ``vibesynergy.query``, wrap the result in the
common envelope ``{"data": …, "meta": {units, timezone, period, sources, freshness, …}}``.
Run over stdio (``vibesynergy mcp``) or streamable HTTP inside ``vibesynergy serve``.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic_core import to_jsonable_python

from vibesynergy import __version__
from vibesynergy.core.timeutil import Window, today_in, utc_now
from vibesynergy.mcp.identity import current_connection, current_scopes
from vibesynergy.mcp.scopes import READ_SCOPES, scope_denied
from vibesynergy.query import (
    catalog,
    coach,
    coach_handbook,
    periods,
    sql,
    views,
)
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import audit_log

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolKit:
    """What the tool modules need from build_server: the server and its helpers."""

    server: Any
    rt: Any
    engine: Any
    settings: Any
    tool: Callable[[ToolAnnotations, str], Callable[[Callable], Callable]]
    today: Callable[[], date]
    window: Callable[..., Window]
    envelope: Callable[..., dict[str, Any]]
    diary_changed: Callable[..., dict[str, Any]]
    _icu: Callable[..., dict[str, Any]]


READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
OPEN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
ICU_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
ICU_DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
_READ_AUDIT_KEEP = timedelta(days=180)
_last_read_prune = 0.0
_AUDIT_TARGET_KEYS = ("id", "activity_id", "injury_id", "for_date", "date", "slug", "rule_id")


def _audit_target(arguments: dict) -> str:
    for key in _AUDIT_TARGET_KEYS:
        value = arguments.get(key)
        if value:
            return str(value)[:200]
    return ""


def _audit_arguments(arguments: dict, scope: str) -> dict:
    """Writes keep their arguments. Reads keep only short values, never a SQL query.

    Arguments become plain JSON first: tools take Pydantic models (predictions on
    vibesynergy_coach_write, rules on vibesynergy_kb_upsert), which the JSON column cannot store
    as they are. Anything else that is not JSON becomes its text.
    """
    arguments = to_jsonable_python(arguments, fallback=str)
    if scope not in READ_SCOPES:
        return arguments
    small: dict[str, Any] = {}
    for key, value in arguments.items():
        if key == "query":
            small["query_chars"] = len(str(value))
            continue
        text = value if isinstance(value, str) else str(value)
        small[key] = value if len(text) <= 80 else text[:77] + "..."
    return small


GUIDE = """\
Coaching? Call vibesynergy_coach_handbook first (also the resource vibesynergy://coach/handbook).
It is the coach protocol for any AI: roles and what needs the athlete's approval, the
morning, evening, weekly and insight runs, which tools to call in which order, idempotency
keys, privacy rules for public text, and the current knowledge-base articles and rule ids.

VibeSynergy is the user's personal health and training archive (Garmin, Withings, Strava and
imported files), combined into one consistent view.

How to use it well:
- Start with vibesynergy_overview when you do not know what is there. vibesynergy_catalog lists
  metric ids (e.g. sleep.score, hrv.night_avg, body.weight) with units and coverage.
- "How should I train today?" is vibesynergy_today (one call). The week, including the weekend,
  is vibesynergy_week. vibesynergy_plan with this_week is that same Monday to Sunday. Other tools
  still treat this_week as Monday through today.
- For one day use vibesynergy_day; for sleep, recovery or body use those tools; for any metric
  over time use vibesynergy_series (it aggregates server-side; ask for weeks or months over
  long periods) and vibesynergy_stats for summary statistics.
- Activities: vibesynergy_activities to search/list, vibesynergy_activity for one in depth,
  vibesynergy_activity_streams for per-second data (downsampled to max_points).
- Periods: today, yesterday, last_7d, last_14d, last_28d, last_90d, last_365d, week
  (Mon-Sun), this_week (Mon-today), last_week, last_<n>d, last_<n>m, next_<n>d, next_<n>m,
  this_month, last_month, this_year, ytd, all, 2026-W38, 2026-09, 2026, or start/end dates.
- Values are SI: seconds, metres, kilograms, m/s. Convert for the user (pace = 1000/speed
  s/km). Dates are the user's local dates; sleep belongs to the morning it ends.
- Each daily value comes from the best source (e.g. weight from Withings, sleep from
  Garmin); vibesynergy_explain shows the alternatives and where a number came from.
- Every response has meta.warnings; mention them when they matter (missing days, stale
  sync). Use vibesynergy_sync_status if data seems missing, vibesynergy_refresh to fetch now.
- The user's own diary is here too: vibesynergy_notes (notes with tags like sick or travel,
  morning check-ins) and vibesynergy_injuries (pain log with warning flags). Check them before
  explaining odd numbers; anomalies and baselines carry the day's diary context. Baselines
  leave out sick days; statistics tools take exclude_tags. Check-in scores (checkin.*) and
  the day's worst pain (pain.max) are metrics like any other, e.g. for vibesynergy_correlate.
  Save what the user tells you with vibesynergy_add_note, vibesynergy_checkin or
  vibesynergy_log_injury, only after they said or confirmed it.
- Personal model fits: vibesynergy_model_run_write stores parameters and a markdown
  report. vibesynergy_model_runs lists runs of one model. The parameter diff against
  the previous run of that model is stored when the run is written.
- Coach entries: vibesynergy_coach_write stores morning, evening, note, activity_assessment
  or threshold_change. Optional predictions (metric, horizon_days, predicted, ci_low,
  ci_high, model_id) are scored from the daily value on the target day; error is
  actual minus predicted. Optional kb_rules stores rule ids on that entry.
  vibesynergy_coach_entries reads the entries back. vibesynergy_prediction_scores returns
  bias and RMSE per ISO week. An activity_assessment with activity_id and public_insight
  puts that one public sentence in the activity's Strava/Garmin block;
  vibesynergy_activity_insights lists recent activities still missing one; vibesynergy_events
  lists new activities (activity.ready) and activities the athlete made public on Strava
  (activity.published, the moment to write the insight) for agents that poll.
- Knowledge: vibesynergy_kb_search and vibesynergy_kb_get read articles, rules and sources.
  vibesynergy_coach_prompt gives a routine its current instructions (the athlete edits them).
  vibesynergy_kb_upsert writes a new version and keeps the old ones. vibesynergy_kb_propose_change
  only suggests a change; the athlete approves it in the portal. Pass revert_to to copy an
  older article version forward.
- This is personal data for the user's own analysis, not medical advice. Never diagnose an
  injury; relay the warning flags and suggest a physiotherapist or doctor for real pain.
"""


def build_server(rt: Runtime) -> MCPServer:
    server = MCPServer(
        name="vibesynergy",
        title="VibeSynergy",
        description="Personal health and training data: sleep, recovery, body, activities.",
        instructions=GUIDE,
        version=__version__,
    )
    engine, settings = rt.engine, rt.settings
    # Tool name -> required scope, for in-process callers (the API coach runner) that offer
    # a model only the tools its scopes allow.
    tool_scopes: dict[str, str] = {}
    server.vibesynergy_tool_scopes = tool_scopes  # type: ignore[attr-defined]

    def audit(name: str, arguments: dict, outcome: str, seconds: float, scope: str) -> None:
        """Every tool call is recorded: who called, which scope, and what it touched.

        Write arguments are kept. Reads keep only short arguments. Failures to record
        never break the call. Read rows older than 180 days are dropped; writes stay.
        """
        global _last_read_prune
        try:
            connection = current_connection()
            elapsed_ms = round(seconds * 1000)
            target = _audit_target(arguments)
            now = utc_now()
            with engine.begin() as conn:
                conn.execute(
                    audit_log.insert().values(
                        at=now,
                        actor="mcp",
                        action=name,
                        detail={
                            "arguments": _audit_arguments(arguments, scope),
                            "outcome": outcome[:300],
                            "ms": elapsed_ms,
                        },
                        client_id=connection.client,
                        author=connection.author,
                        scope=scope,
                        target=target,
                        outcome=outcome[:300],
                        ms=elapsed_ms,
                    )
                )
                if time.monotonic() - _last_read_prune > 3600:
                    _last_read_prune = time.monotonic()
                    conn.execute(
                        audit_log.delete().where(
                            audit_log.c.scope.in_(tuple(READ_SCOPES)),
                            audit_log.c.at < now - _READ_AUDIT_KEEP,
                        )
                    )
        except Exception:
            log.exception("could not write the audit log")

    def tool(annotations: ToolAnnotations, scope: str) -> Callable[[Callable], Callable]:
        """Register a tool and require ``scope`` on the caller's token.

        Input problems (unknown metric, bad period or id) raise ValueError; those
        messages are written for the model, so they are passed on as ToolError.
        Anything else stays a crash whose details remain in the server log.
        A local caller with no token (stdio, tests) has every scope.
        """

        def register(fn: Callable) -> Callable:
            tool_scopes[fn.__name__] = scope

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                started, outcome = time.monotonic(), "ok"
                try:
                    if scope not in current_scopes():
                        outcome = "denied"
                        raise ToolError(scope_denied(scope))
                    return fn(*args, **kwargs)
                except ToolError as exc:
                    if outcome == "ok":
                        outcome = f"error: {exc}"
                    raise
                except ValueError as exc:
                    outcome = f"error: {exc}"
                    raise ToolError(str(exc)) from exc
                except Exception as exc:
                    outcome = f"crash: {type(exc).__name__}"
                    raise
                finally:
                    audit(fn.__name__, kwargs, outcome, time.monotonic() - started, scope)

            return server.tool(annotations=annotations)(wrapper)

        return register

    def today() -> date:
        return today_in(settings.timezone)

    def window(period: str | None, start: str | None, end: str | None, default: str) -> Window:
        return periods.resolve(period, start, end, today=today(), default=default)

    def envelope(
        data: Any,
        *,
        span: Window | None = None,
        metrics: list[str] | None = None,
        method: str | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        known = catalog.KNOWN
        meta: dict[str, Any] = {
            "timezone": settings.timezone,
            "freshness": views.freshness(engine),
        }
        if span:
            meta["period"] = {"start": str(span.start), "end": str(span.end)}
        if metrics:
            meta["units"] = {m: known[m].unit for m in metrics if m in known}
        if method:
            meta["method"] = method
        meta["warnings"] = warnings or []
        return {"data": data, "meta": meta}

    def diary_changed(data: Any, **meta: Any) -> dict[str, Any]:
        return envelope(data, **meta)

    def _icu(call: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient, IntervalsIcuError

        try:
            client = IntervalsIcuClient.from_env()
        except IntervalsIcuError as exc:
            raise ToolError(str(exc)) from exc
        try:
            return envelope(call(client))
        except IntervalsIcuError as exc:
            raise ToolError(str(exc)) from exc

    from vibesynergy.mcp.tools import activities as tools_activities
    from vibesynergy.mcp.tools import analysis as tools_analysis
    from vibesynergy.mcp.tools import data as tools_data
    from vibesynergy.mcp.tools import days as tools_days
    from vibesynergy.mcp.tools import diary as tools_diary
    from vibesynergy.mcp.tools import harness as tools_harness
    from vibesynergy.mcp.tools import knowledge as tools_knowledge
    from vibesynergy.mcp.tools import orientation as tools_orientation
    from vibesynergy.mcp.tools import power as tools_power
    from vibesynergy.mcp.tools import series as tools_series
    from vibesynergy.mcp.tools import training as tools_training
    from vibesynergy.mcp.tools import wellness as tools_wellness

    kit = ToolKit(
        server=server,
        rt=rt,
        engine=engine,
        settings=settings,
        tool=tool,
        today=today,
        window=window,
        envelope=envelope,
        diary_changed=diary_changed,
        _icu=_icu,
    )

    tools_orientation.register(kit)
    tools_harness.register(kit)
    tools_days.register(kit)
    tools_series.register(kit)
    tools_activities.register(kit)
    tools_wellness.register(kit)
    tools_analysis.register(kit)
    tools_training.register(kit)
    tools_data.register(kit)
    tools_diary.register(kit)
    tools_knowledge.register(kit)
    tools_power.register(kit)

    # Prompts (they only orchestrate tools; no hidden logic) ------------------------------

    @server.prompt()
    def daily_check_in(date: str = "today") -> str:
        """Today's readiness, sleep and recovery against the personal baseline."""
        return (
            f"Give me a daily check-in for {date}. Use vibesynergy_day and vibesynergy_baselines, "
            f"and "
            "vibesynergy_training_load for form. Say how recovered I am compared with my normal, "
            "what stands out, and whether to train hard, easy or rest. Be brief."
        )

    @server.prompt()
    def weekly_review(week: str = "last_week") -> str:
        """Volume, load and form, sleep and recovery, highlights, next week's focus."""
        return (
            f"Review my training week ({week}). Use vibesynergy_period, vibesynergy_volume, "
            "vibesynergy_training_load, vibesynergy_sleep and vibesynergy_anomalies. Cover "
            "volume per "
            "sport, load and form, sleep and recovery, highlights and concerns, and one or two "
            "priorities for next week."
        )

    @server.prompt()
    def activity_review(activity_id: str) -> str:
        """Execution, laps, zones and heart-rate drift of one activity."""
        return (
            f"Review activity {activity_id}. Use vibesynergy_activity and, where useful, "
            "vibesynergy_activity_streams. Describe pacing or power over the laps, time in zones, "
            "heart-rate drift, and how it compares with similar recent sessions "
            "(vibesynergy_activities with the same sport)."
        )

    @server.prompt()
    def trend_investigation(metric: str, period: str = "last_365d") -> str:
        """Trend, unusual values and possible drivers of one metric."""
        return (
            f"Investigate the trend of {metric} over {period}. Use vibesynergy_trend, "
            "vibesynergy_series (weekly), vibesynergy_anomalies and vibesynergy_correlate with "
            "plausible "
            "drivers (training load, sleep, weight). State uncertainty and sample sizes; "
            "correlation is not causation."
        )

    @server.prompt()
    def race_readiness(race_date: str, sport: str, distance: str) -> str:
        """Form trajectory, taper and realistic target before a race."""
        return (
            f"I race {distance} ({sport}) on {race_date}. Use vibesynergy_training_load, "
            "vibesynergy_fitness, vibesynergy_bests and vibesynergy_volume to judge my form "
            "trajectory, "
            "whether my taper is on track, and what result is realistic."
        )

    @server.prompt()
    def body_composition_review(period: str = "last_90d") -> str:
        """Trend weight and composition."""
        return (
            f"Review my body composition over {period} with vibesynergy_body and vibesynergy_trend "
            "on body.weight. Focus on the trend rather than day-to-day noise. Informational "
            "only."
        )

    @server.prompt()
    def sleep_review(period: str = "last_28d") -> str:
        """Sleep quality, regularity and its relation to training."""
        return (
            f"Review my sleep over {period}: vibesynergy_sleep, vibesynergy_series for sleep.score "
            "and sleep.duration, and vibesynergy_correlate between activity.load and next-day "
            "sleep.score (max_lag_days=1). Mention regularity and anything unusual."
        )

    @server.prompt()
    def data_health() -> str:
        """Sync state, gaps and what to fix."""
        return (
            "Check the health of my VibeSynergy data with vibesynergy_sync_status and "
            "vibesynergy_coverage for the main metrics. List problems and gaps and what to do "
            "about each."
        )

    # Resources --------------------------------------------------------------------------

    @server.resource("vibesynergy://guide", mime_type="text/markdown")
    def guide() -> str:
        """How to use VibeSynergy's tools well."""
        return GUIDE

    @server.resource("vibesynergy://coach/handbook", mime_type="text/markdown")
    def coach_handbook_resource() -> str:
        """The coach protocol for any AI coach (same text as vibesynergy_coach_handbook)."""
        return coach_handbook.protocol_text() or (
            f"{coach_handbook.PROTOCOL_DOC} is not part of this install."
        )

    @server.resource("vibesynergy://metrics", mime_type="application/json")
    def metrics_resource() -> dict:
        """The metric catalogue."""
        return {"metrics": list(catalog.catalogue(engine).values())}

    @server.resource("vibesynergy://schema", mime_type="application/json")
    def schema_resource() -> dict:
        """Tables for vibesynergy_sql."""
        return {"dialect": "SQLite", "tables": sql.TABLES}

    @server.resource("vibesynergy://athlete/notes", mime_type="application/json")
    def athlete_notes_resource() -> dict:
        """Notes the athlete wrote for their coach. Read-only."""
        return {"notes": coach.athlete_notes(engine)}

    return server


def _excluding(tags: Any, what: str = "values") -> str | None:
    """The method note for results that left out tagged days."""
    tags = [t for t in (tags or []) if t]
    if not tags:
        return None
    return f"days tagged {', '.join(tags)} in the diary are left out of the {what}"


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"dates look like 2026-09-24, got {text!r}") from exc
