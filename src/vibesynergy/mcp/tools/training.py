"""MCP tools: training (split from mcp/server.py)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.server import (
    OPEN,
    READ,
    ToolKit,
    _date,
)
from vibesynergy.query import (
    activity_analysis,
    calendar_mirror,
    coach_brief,
    planning,
    training,
    workouts,
)
from vibesynergy.query import plan as plan_q

log = logging.getLogger(__name__)


def register(kit: ToolKit) -> None:
    rt = kit.rt
    engine = kit.engine
    settings = kit.settings
    tool = kit.tool
    today = kit.today
    window = kit.window
    envelope = kit.envelope

    # Training ---------------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_training_load(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        sport: str | None = None,
        load: str = "vibesynergy",
        include_coaching: bool = True,
    ) -> dict[str, Any]:
        """Fitness, fatigue and form (CTL/ATL/TSB, the performance management chart) from
        daily training load, with the current form band and 7-day CTL ramp. load:
        vibesynergy (default, the one the portal and coach use: per activity power (TSS)
        for rides with power, pace against CSS for swims, else the heart-rate stream
        (TRIMP, an hour at LTHR is 100), with the thresholds valid that day; form =
        same-day CTL - ATL, form_zone on form % of fitness: transition, fresh, grey,
        optimal, high_risk), trimp (Banister TRIMP from duration and average HR), tss
        (from power and FTP; only activities with power) or garmin (Garmin's per-activity
        load). Default last_90d."""
        span = window(period, start, end, "last_90d")
        data = training.training_load(
            engine,
            span,
            sport=sport,
            load=load,
            athlete=settings.athlete,
            include_coaching=include_coaching,
            raw=rt.raw,
            parquet=rt.parquet,
        )
        return envelope(data, span=span, method="CTL 42-day and ATL 7-day EWMA of daily load")

    @tool(READ, "read")
    def vibesynergy_volume(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        granularity: str = "week",
        by: str = "sport",
        include_coaching: bool = True,
    ) -> dict[str, Any]:
        """Training volume per week/month/year and sport: count, moving time (s),
        distance (m), ascent (m), load, with totals. by: sport or all. Default
        last_90d."""
        span = window(period, start, end, "last_90d")
        data = training.volume(
            engine, span, granularity=granularity, by=by, include_coaching=include_coaching
        )
        return envelope(data, span=span)

    @tool(READ, "read")
    def vibesynergy_intervals(id: str) -> dict[str, Any]:
        """The laps of one activity as work and recovery (split by power, else speed): per
        lap time, distance, source speed (m/s), sport_units, HR and power; the averages; and
        the fade from the first to the last work interval in percent. A session whose laps
        are all within 15% of each other is 'steady'. id: e.g. 'garmin:123'."""
        return envelope(workouts.intervals(engine, rt.parquet, id))

    @tool(READ, "read")
    def vibesynergy_activity_analyze(
        id: str,
        start: str | None = None,
        end: str | None = None,
        last_minutes: float | None = None,
    ) -> dict[str, Any]:
        """A window of one activity against the rest of it, from the per-second streams:
        duration, distance, source speed (m/s), sport_units, average and max heart rate and
        power, normalized power, cadence, climbing, efficiency factor, heart-rate drift,
        aerobic decoupling and time in heart-rate zones. The window: start and end as
        elapsed time (3600, '1:00:00', '45min') or distance ('10km'), or last_minutes;
        the whole activity without them. E.g. 'my HR drift in the last hour':
        last_minutes=60. id: e.g. 'garmin:123'."""
        data = activity_analysis.analyze(
            engine, rt.parquet, id, start=start, end=end, last_minutes=last_minutes
        )
        return envelope(data, method="per-second streams; NP = 30 s rolling, 4th-power mean")

    @tool(READ, "read")
    def vibesynergy_similar_activities(
        id: str,
        by: str = "profile",
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Other sessions like this one, best match first, each with a similarity score
        (0-1) and its differences from this one (vs_this). by: 'profile' (same kind of
        sport, similar duration, distance and heart rate), 'route' (the same course by GPS)
        or 'workout' (the same number and length of work intervals). Default period: all.
        Ideal for 'am I getting faster on this loop'. id: e.g. 'garmin:123'."""
        span = window(period, start, end, "all") if (period or start or end) else None
        data = activity_analysis.similar(engine, rt.parquet, id, by=by, window=span, limit=limit)
        return envelope(data, span=span) if span else envelope(data)

    @tool(READ, "read")
    def vibesynergy_activity_annotation_preview(id: str, language: str = "en") -> dict[str, Any]:
        """Preview an additional numeric activity-description block from a current saved
        assessment. Language en/nl. Never publishes, runs AI, or changes existing text.
        Live target, scope, ownership and human-edit verification remain required."""
        from vibesynergy.query import annotations

        return envelope(annotations.preview(rt, id, language=language))

    @tool(READ, "read")
    def vibesynergy_activity_publication_preview(
        id: str, destination: str, language: str = "en"
    ) -> dict[str, Any]:
        """Read a Garmin or Strava description and preview the exact proposed text."""
        from vibesynergy.query import publication

        return envelope(publication.preview(rt, id, destination, language=language))

    @tool(OPEN, "publish")
    def vibesynergy_activity_publish(
        id: str, destination: str, preview_version: str, language: str = "en"
    ) -> dict[str, Any]:
        """Publish one previewed description, then verify it from the provider."""
        from vibesynergy.query import publication

        return envelope(
            publication.publish(rt, id, destination, preview_version, language=language)
        )

    @tool(OPEN, "publish")
    def vibesynergy_activity_unpublish(
        id: str, destination: str, preview_version: str, language: str = "en"
    ) -> dict[str, Any]:
        """Remove an unchanged VibeSynergy section and verify the provider text."""
        from vibesynergy.query import publication

        return envelope(
            publication.unpublish(rt, id, destination, preview_version, language=language)
        )

    @tool(READ, "read")
    def vibesynergy_compare_activities(ids: list[str]) -> dict[str, Any]:
        """2 to 5 activities side by side (date, distance, time, speed, HR, power, load,
        calories) with each one's difference from the first listed."""
        return envelope(workouts.compare(engine, rt.parquet, ids))

    @tool(READ, "read")
    def vibesynergy_gear() -> dict[str, Any]:
        """Bikes and shoes (from Strava) with Strava's total distance, how often VibeSynergy saw
        them used and when last, and a warning for running shoes past 600 km (usual
        replacement range 600-800 km). 'other_sources' reports Garmin's own gear collection
        (not yet merged in; see 'note'). 'references' cites the raw gear snapshots.
        'activity_gear' lists Garmin gear ids used on activities, without distance or model."""
        return envelope(workouts.gear(engine, rt.raw, today()))

    @tool(READ, "read")
    def vibesynergy_workout_library(
        template_id: str | None = None,
        lang: Literal["nl", "en"] = "nl",
        params: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """The workout library: ready workouts for run, ride, swim and strength
        (AA, MT-A, MT-B, MS, SM as a [garmin] block without weights: add weight= per
        exercise from the athlete's history), relative targets only (% FTP, % LTHR, zones).
        Without template_id: the catalogue (id, sport, purpose, phases, load
        easy/moderate/hard, params with defaults, the easier alternative). With template_id
        (and params to change the defaults): the workout's name, intervals.icu type and
        text and planned seconds, ready for vibesynergy_icu_create_event. Fill day 9 from here
        rather than typing steps.
        """
        from vibesynergy.query import workout_library

        if template_id is None:
            return envelope({"templates": workout_library.catalogue(lang)})
        try:
            return envelope(workout_library.render(template_id, lang, **(params or {})))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @tool(READ, "read")
    def vibesynergy_exercises() -> dict[str, Any]:
        """Strength exercises (category + specific variant) your synced Garmin sessions used,
        matched against Garmin's own workout-editor catalog, with how many sessions and the
        first/last date seen. 'known' is false for an identity Garmin's catalog does not name
        (shown with its enum turned into readable text instead). 'freshness' is the Garmin
        activities collection. 'catalog.fingerprint' identifies the unique catalog rows."""
        return envelope(workouts.exercises(engine))

    @tool(READ, "read")
    def vibesynergy_today(date: str | None = None) -> dict[str, Any]:
        """One day for a coach: check-in, safety, today's session, the race, and the same
        verdict the dashboard shows. Default today. Prefer this over several smaller reads."""
        when = _date(date) if date else today()
        data = coach_brief.today_brief(rt, when)
        return envelope(data, span=Window(when, when), warnings=data["warnings"])

    @tool(READ, "read")
    def vibesynergy_week(week: str | None = None) -> dict[str, Any]:
        """Monday to Sunday, planned against what was done, including Saturday and Sunday.
        ``week`` is this_week (default) or an ISO week such as 2026-W42. Each session is
        done, changed, missed, today or upcoming. Also the week's plan note, its time
        targets per sport with time done, and Garmin workouts that are not in the plan."""
        day = today()
        name = (week or "this_week").strip()
        if not name or name.lower() in {"this_week", "week"}:
            span = plan_q.calendar_week_window(day)
        else:
            span = window(name, None, None, "this_week")
        return envelope(coach_brief.week_brief(rt, span.start, span.end, day), span=span)

    @tool(READ, "read")
    def vibesynergy_plan_vs_done(
        start: str | None = None, end: str | None = None, weeks: int = 12
    ) -> dict[str, Any]:
        """Plan against done per Monday-to-Sunday week and per training phase, the
        same numbers as the Planning & progress page. Per week: goal_s per sport
        (intervals.icu's weekly TARGET time, else the planned workouts; basis says which),
        done_s, done_pct, planned and done load (intervals.icu's), counts of done /
        changed / missed / extra workouts, sessions_done_pct (done of the planned workouts
        whose day passed) and compliance_avg (intervals.icu's compliance of the paired
        ones). start/end are dates (their Mondays count); default the ``weeks`` weeks
        ending with this one (at most 52). Per-workout detail of one week: vibesynergy_week."""
        first = _date(start) if start else None
        last = _date(end) if end else None
        data = planning.plan_vs_done(
            engine, rt.raw, today(), first=first, last=last, weeks=max(1, min(weeks, 52))
        )
        span = Window(_date(data["start"]), _date(data["end"]))
        return envelope(data, span=span)

    @tool(READ, "read")
    def vibesynergy_projection() -> dict[str, Any]:
        """A projection, not a measurement: fitness, fatigue and form (CTL/ATL/TSB and
        intervals.icu's form zone) per day from today to the last upcoming race if the
        plan is done as written, through intervals.icu's model from today's values, and
        the projected values on each race day. Planned load per day: the intervals.icu
        workout's load; a workout without one gets its planned time at the recent load per
        hour; a week with only a weekly target gets that target spread over its remaining
        days. ``basis`` counts each kind and ``assumptions`` names them; say so when you
        quote it. Changes as soon as the plan changes."""
        data = planning.projection(
            engine, rt.raw, today(), athlete=settings.athlete, parquet=rt.parquet
        )
        return envelope(data, span=Window(_date(data["start"]), _date(data["end"])))

    @tool(READ, "read")
    def vibesynergy_plan(
        period: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """The training calendar. From plan_start on, the intervals.icu calendar is the
        plan (plan_source intervals_icu): its workouts are the items, a Garmin workout that
        intervals.icu pushed is folded into one as garmin_copies, and any other Garmin
        workout is listed under not_in_plan and never counted. Earlier days use Garmin
        (and its coaching plan). Each planned workout says whether it was done (matched to
        an activity of the same sport that day), changed, or missed; also today's session,
        adherence, weeks (the weekly intervals.icu note and time targets per sport, with
        time done), and upcoming events and races with days to go. An intervals.icu race on
        the day of a Garmin race is that race (intervals_id). Race priority: a stored
        choice, else a Garmin primary race is A, else the intervals.icu RACE_A/B/C letter;
        priority_origin is saved, derived or intervals. this_week and week are Monday to
        Sunday. Default: the last 7 days and the next 28."""
        named = (period or "").strip().lower()
        if named in {"this_week", "week"} and not start and not end:
            span = plan_q.calendar_week_window(today())
        elif period or start or end:
            span = window(period, start, end, "last_7d")
        else:
            now = today()
            span = Window(now - timedelta(days=7), now + timedelta(days=28))
        data = plan_q.plan(engine, span, today())
        data["calendar_mirror"] = {
            **data["calendar_mirror"],
            "enabled": calendar_mirror.mirror_active(rt.settings),
        }
        return envelope(data, span=span)

    @tool(READ, "read")
    def vibesynergy_fitness(
        period: str | None = None, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        """Fitness markers over time as change points: VO2max run/ride, fitness age, race
        predictions (s), and lactate threshold HR / FTP per sport from the zone history.
        Default last_365d."""
        span = window(period, start, end, "last_365d")
        return envelope(training.fitness(engine, span), span=span)

    @tool(READ, "read")
    def vibesynergy_bests(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        sport: str | None = None,
        kind: str = "power",
    ) -> dict[str, Any]:
        """Best efforts, each with the activity and date it came from. kind=power: best
        average power for 5 s to 60 min (activities with power). kind=pace: fastest running
        time for 400 m, 1 km, mile, 5 km, 10 km and half marathon from the distance stream
        (stretches faster than running, e.g. a bike or car mid-activity, are left out).
        kind=overview: every sport (sport all, run, bike, swim or other): longest session,
        fastest run times, best power, fastest swim session over 1000 m, and for all sports
        together the most hours in a week and the biggest day load. Default period all
        (pace and overview: last_365d)."""
        if kind == "pace":
            span = window(period, start, end, "last_365d")
            data = training.pace_curve(engine, rt.parquet, span, sport=sport or "run")
            return envelope(data, span=span)
        if kind == "overview":
            from vibesynergy.query import bests

            span = window(period, start, end, "last_365d")
            try:
                data = bests.overview(engine, rt.parquet, span, sport=sport or "all")
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            return envelope(data, span=span)
        if kind != "power":
            raise ValueError("kind must be power, pace or overview")
        span = window(period, start, end, "all")
        return envelope(training.power_curve(engine, rt.parquet, span, sport=sport), span=span)

    @tool(READ, "read")
    def vibesynergy_intraday(
        date: str | None = None, metrics: list[str] | None = None, resample_min: int = 5
    ) -> dict[str, Any]:
        """All-day samples of one day (heart rate, stress, steps, SpO2, respiration, sleep
        HR) averaged per resample_min minutes, in local time. Default today."""
        when = _date(date) if date else today()
        data = training.intraday(
            rt.parquet, when, settings.timezone, metrics=metrics, resample_min=max(1, resample_min)
        )
        warnings = [] if data["rows"] else ["no intraday samples for this day"]
        return envelope(data, span=Window(when, when), warnings=warnings)
