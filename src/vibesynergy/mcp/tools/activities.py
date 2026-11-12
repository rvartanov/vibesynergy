"""MCP tools: activities (split from mcp/server.py)."""

from __future__ import annotations

from typing import Any

from vibesynergy.mcp.server import (
    READ,
    WRITE,
    ToolKit,
    _date,
)
from vibesynergy.query import (
    views,
)


def register(kit: ToolKit) -> None:
    rt = kit.rt
    engine = kit.engine
    tool = kit.tool
    window = kit.window
    envelope = kit.envelope
    diary_changed = kit.diary_changed

    # Activities -------------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_activities(
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        sport: str | None = None,
        text: str | None = None,
        min_distance_km: float | None = None,
        min_duration_min: float | None = None,
        source: str | None = None,
        sort: str = "date",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search activities (one row per real session, merged across sources) with totals
        for the whole match. sort: date, distance, duration, load, np, avg_hr, ascent
        (descending). Default: the last 20 of all time. Ids work in vibesynergy_activity."""
        span = window(period, start, end, "all")
        data = views.activities(
            engine,
            span,
            sport=sport,
            text=text,
            min_distance_m=min_distance_km * 1000 if min_distance_km else None,
            min_duration_s=min_duration_min * 60 if min_duration_min else None,
            source=source,
            sort=sort,
            limit=min(limit, 200),
            offset=offset,
        )
        from vibesynergy.query import activity_effort

        activity_effort.attach_compact(engine, rt.raw, data["activities"])
        return envelope(data, span=span)

    @tool(READ, "read")
    def vibesynergy_activity(id: str) -> dict[str, Any]:
        """One activity in depth: merged summary, the copies from each source (with their
        titles, devices, descriptions), laps, time in HR/power zones, which stream
        channels exist, and effort: RPE 1-10 and Feel 1-5 (1 = Strong, 5 = Weak, the
        intervals.icu scale) with their source (garmin, intervals_icu or vibesynergy).
        id: e.g. 'garmin:123' or 'strava:456'."""
        from vibesynergy.query import activity_effort

        detail = views.activity_detail(engine, rt.parquet, id)
        detail["effort"] = activity_effort.for_activity(engine, rt.raw, id)
        return envelope(detail)

    @tool(WRITE, "diary.write")
    def vibesynergy_activity_set_effort(
        id: str, rpe: int | None = None, feel: int | None = None
    ) -> dict[str, Any]:
        """Save how an activity went, as the user said it: rpe 1-10 (1 Nothing at all … 10
        Max effort) and/or feel 1-5 (1 Strong, 2 Good, 3 Normal, 4 Poor, 5 Weak). The
        user's entry wins over what Garmin recorded (that stays visible as the other
        value); nothing is written to intervals.icu."""
        from vibesynergy.query import activity_effort

        return diary_changed(activity_effort.set_effort(engine, rt.raw, id, rpe=rpe, feel=feel))

    @tool(WRITE, "diary.write")
    def vibesynergy_activity_set_coaching(id: str, is_coaching: bool | None) -> dict[str, Any]:
        """Whether an activity is a training you give (kept out of your own load). Sets its
        session purpose: true = coached_by_me, false = training, null = automatic from the
        title ("training geven"). Same as vibesynergy_activity_set_purpose."""
        from vibesynergy.query import coaching

        return envelope(coaching.set_override(engine, id, is_coaching))

    @tool(READ, "read")
    def vibesynergy_activity_assessment(id: str) -> dict[str, Any]:
        """The coach's assessment of one activity, read from the coach feed.

        assessment is the newest activity_assessment entry written with
        vibesynergy_coach_write about this activity (activity_id or an evidence_ref);
        decision_log lists all of them, newest first. state is ready or missing. Never
        runs a model or queues work.
        """
        from vibesynergy.query import assessments

        return envelope(assessments.read(rt, id))

    @tool(READ, "read")
    def vibesynergy_daily_coaching(date: str = "today") -> dict[str, Any]:
        """Saved morning decision, evening review, weekly review and block evaluation for a
        day, with freshness and the fixed safety outcome. Read-only: it never runs AI or
        queues work; a stale review has not considered newer inputs."""
        from vibesynergy.query import daily_coach

        return envelope(daily_coach.read(rt, None if date == "today" else _date(date)))

    @tool(READ, "read")
    def vibesynergy_current_coaching_state(id: str | None = None) -> dict[str, Any]:
        """Current profile, evidence and saved assessment versions for the latest or a
        specified activity. Read-only; stale advice never counts as current."""
        from vibesynergy.query import assessments

        return envelope(assessments.current_state(rt, id))

    @tool(READ, "read")
    def vibesynergy_activity_streams(
        id: str, channels: list[str] | None = None, max_points: int = 500
    ) -> dict[str, Any]:
        """Per-second data of an activity (hr, power, speed, cadence, alt, dist, lat, lon,
        temp, developer fields such as dev_Form Power), averaged into at most max_points
        samples, with elapsed seconds as the time axis. Speed samples remain m/s;
        speed_unit names the sport's display notation."""
        data = views.activity_streams(
            engine, rt.parquet, id, channels=channels, max_points=min(max_points, 5000)
        )
        return envelope(data, method=f"mean over every {data['resampled_every']} samples")
