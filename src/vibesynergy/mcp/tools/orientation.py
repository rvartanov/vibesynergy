"""MCP tools: orientation (split from mcp/server.py)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.mcp.identity import current_author, current_scopes
from vibesynergy.mcp.server import (
    ICU_DELETE,
    ICU_WRITE,
    READ,
    WRITE,
    ToolKit,
    _date,
)
from vibesynergy.query import (
    best_source,
    catalog,
    coach,
    coach_handbook,
    coach_workouts,
    icu_plan,
    model_run,
    predictions,
    sql,
    thresholds,
    views,
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
    _icu = kit._icu

    # Orientation ------------------------------------------------------------------------

    @tool(READ, "read")
    def vibesynergy_coach_state() -> dict[str, Any]:
        """The athlete's coaching preferences: advice language, detail and tone, notes for
        the coach, and things to remember (availability, fixed moments, equipment). Plus
        plan-calendar races with their priority. The training plan and its rules are in
        the knowledge base and intervals.icu.
        """
        snap = coach.snapshot(engine)
        return envelope(
            {
                **snap,
                "active_profile": coach.profile_binding(snap),
                "races": plan_q.calendar_races(engine, today()),
                "note": coach.COACH_STATE_NOTE,
            }
        )

    @tool(READ, "read")
    def vibesynergy_coach_handbook(include_protocol: bool = True) -> dict[str, Any]:
        """Read this first when you coach. The coach protocol (docs/COACH_PROTOCOL.md) for
        any AI: roles and what needs the athlete's akkoord, the morning, evening, weekly and
        insight runs, tool order, idempotency keys, privacy rules for public text, replies
        and failure handling. Plus the task register (each coach task with its trigger,
        inputs, output, what you decide alone, last run and missing runs), the active
        knowledge-base articles (slugs to read with vibesynergy_kb_get), the active rule ids
        (cite them in kb_rules), and this connection's scopes against what the coach loop
        needs. include_protocol=false
        returns only the index and protocol_version, for later runs.
        """
        data, warnings = coach_handbook.handbook(
            engine,
            scopes=current_scopes(),
            include_protocol=include_protocol,
            timezone=settings.timezone,
        )
        return envelope(data, warnings=warnings)

    @tool(WRITE, "plan.propose")
    def vibesynergy_race_set_priority(
        source: str, source_id: str, priority: str | None = None
    ) -> dict[str, Any]:
        """Set one race to A, B or C. Null uses the default again.

        A Garmin primary race stays A until you choose. Calendar sync keeps the choice.
        """
        from vibesynergy.query.race_priority import set_priority

        try:
            saved = set_priority(engine, source, source_id, priority)
        except (LookupError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return envelope(saved)

    @tool(READ, "read")
    def vibesynergy_icu_events(
        start: str | None = None,
        end: str | None = None,
        category: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """The intervals.icu plan as cached in VibeSynergy, with event_id, external_id and
        version for the vibesynergy_icu_* write tools. Default today to +28 days. category:
        WORKOUT, NOTE, TARGET, RACE_A/B/C. refresh=true fetches intervals.icu first.
        """
        first = _date(start) if start else today_in(settings.timezone)
        last = _date(end) if end else first + timedelta(days=28)
        if last < first:
            raise ToolError("end is before start.")
        if refresh:
            try:
                rt.sync.sync("intervals_icu", ["events"], Window(first, last))
            except Exception as exc:  # report, then still return the cache
                raise ToolError(f"intervals.icu sync failed: {exc}") from exc
        return envelope(
            {"events": icu_plan.list_events(engine, first, last, category)},
            span=Window(first, last),
        )

    @tool(READ, "read")
    def vibesynergy_weather(days: int = 9) -> dict[str, Any]:
        """Daily weather forecast (Open-Meteo, up to 14 days) for the place the athlete chose in
        Settings (location.kind "chosen"), else where they last trained outside, for outdoor or
        indoor choices. Independent of intervals.icu.

        Per day: temperature, wind and gusts (m/s), rain/snow (mm) and flags (rain, snow,
        gusts, cold). The flags are plain thresholds; decide outdoor or Zwift yourself.
        """
        from vibesynergy.query import activity_weather

        found = activity_weather.forecast(rt)
        days = max(1, min(int(days), 14))
        return envelope({**found, "days": found["days"][:days]})

    @tool(READ, "read")
    def vibesynergy_plan_model(start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """The training plan as VibeSynergy's own model: who leads (source intervals or
        vibesynergy), version, the sessions in the window (default today to +13 days) with
        their steps as intervals.icu text, and the weeks it touches with their note and
        targets. While source is intervals, keep writing with the vibesynergy_icu_* tools; when
        it is vibesynergy, write with vibesynergy_plan_session_set / _move / _skip and
        vibesynergy_plan_week_set / vibesynergy_plan_target_set."""
        from vibesynergy.query import plan_model

        first = _date(start) if start else today()
        last = _date(end) if end else first + timedelta(days=13)
        return envelope(plan_model.view(engine, first, last), span=Window(first, last))

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_session_set(
        reason: str,
        date: str,
        name: str,
        description: str,
        sport: str | None = None,
        session_id: str | None = None,
        slot: str | None = None,
    ) -> dict[str, Any]:
        """Create or change one session in VibeSynergy's plan (only when vibesynergy_plan_model says
        source vibesynergy). description is intervals.icu step text (cue first, then steps);
        unknown steps are refused. The change reaches intervals.icu (and from there Zwift and
        Garmin) within minutes; changes made in intervals.icu itself are put back. Keep the
        week note and week targets in step (COACH-5)."""
        from vibesynergy.query import plan_model

        if plan_model.source(engine) != "vibesynergy":
            raise ToolError(
                "intervals.icu still leads the plan: use vibesynergy_icu_create_event or "
                "vibesynergy_icu_update_event"
            )
        try:
            out = plan_model.set_session(
                engine,
                session_id=session_id,
                day=_date(date),
                name=name,
                sport=sport,
                description=description,
                reason=reason,
                author=current_author() or "mcp",
                slot=slot,
            )
            plan_model.mirror_soon(rt.jobs)
            return envelope(out)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def _vibesynergy_leads() -> None:
        from vibesynergy.query import plan_model

        if plan_model.source(engine) != "vibesynergy":
            raise ToolError(
                "intervals.icu still leads the plan: change the week note or target there "
                "with vibesynergy_icu_update_event"
            )

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_session_move(reason: str, session_id: str, date: str) -> dict[str, Any]:
        """Move one planned session in VibeSynergy's plan to another day (only when
        vibesynergy_plan_model says source vibesynergy). Its intervals.icu copy moves within
        minutes. Keep the week's note and targets in step (COACH-5)."""
        from vibesynergy.query import plan_model

        _vibesynergy_leads()
        try:
            out = plan_model.move_session(
                engine,
                session_id=session_id,
                day=_date(date),
                reason=reason,
                author=current_author() or "mcp",
            )
            plan_model.mirror_soon(rt.jobs)
            return envelope(out)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_session_skip(reason: str, session_id: str) -> dict[str, Any]:
        """Take one planned session out of VibeSynergy's plan (only when vibesynergy_plan_model
        says source vibesynergy). Its intervals.icu copy is removed within minutes; the
        session stays in the plan's history."""
        from vibesynergy.query import plan_model

        _vibesynergy_leads()
        try:
            out = plan_model.skip_session(
                engine, session_id=session_id, reason=reason, author=current_author() or "mcp"
            )
            plan_model.mirror_soon(rt.jobs)
            return envelope(out)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_week_set(reason: str, week: int, note: str) -> dict[str, Any]:
        """Change the note of one week in VibeSynergy's plan (its intent and structure; only
        when vibesynergy_plan_model says source vibesynergy). It reaches intervals.icu within
        minutes. Change the week's sessions and targets with it (COACH-5)."""
        from vibesynergy.query import plan_model

        _vibesynergy_leads()
        try:
            out = plan_model.set_week(
                engine, week=week, note=note, reason=reason, author=current_author() or "mcp"
            )
            plan_model.mirror_soon(rt.jobs)
            return envelope(out)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_target_set(
        reason: str,
        week: int,
        sport: str,
        time_s: float | None = None,
        distance_m: float | None = None,
        load: float | None = None,
    ) -> dict[str, Any]:
        """Set one weekly target in VibeSynergy's plan: sport (run, ride, swim, …) with time in
        seconds, distance in metres and/or load; none of them removes the target. Only when
        vibesynergy_plan_model says source vibesynergy; it reaches intervals.icu within minutes."""
        from vibesynergy.query import plan_model

        _vibesynergy_leads()
        try:
            out = plan_model.set_target(
                engine,
                week=week,
                sport=sport,
                time_s=time_s,
                distance_m=distance_m,
                load=load,
                reason=reason,
                author=current_author() or "mcp",
            )
            plan_model.mirror_soon(rt.jobs)
            return envelope(out)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_icu_create_event(
        reason: str,
        category: str,
        start_date: str,
        name: str,
        sport: str | None = None,
        description: str | None = None,
        external_id: str | None = None,
        end_date: str | None = None,
        for_week: bool = False,
        tags: list[str] | None = None,
        targets: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Add one intervals.icu plan event. A reason is required. Not a bulk upsert.

        targets (TARGET events): time_target (s), load_target, distance_target (m).
        """
        return _icu(
            lambda client: icu_plan.create_event(
                engine,
                client,
                reason=reason,
                category=category,
                start_date=_date(start_date),
                name=name,
                sport=sport,
                description=description,
                external_id=external_id,
                end_date=_date(end_date) if end_date else None,
                for_week=for_week,
                tags=tags,
                targets=targets,
                author=current_author(),
            )
        )

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_icu_update_event(
        reason: str,
        expected_version: str,
        event_id: str | None = None,
        external_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        category: str | None = None,
        sport: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change one plan event by its id. external_id only finds that id in the cache.

        expected_version is the version from the last read. The write is refused when
        the cached event, or intervals.icu, has moved on.
        """
        changes = {
            "name": name,
            "description": description,
            "category": category,
            "type": sport,
            "tags": tags,
        }
        return _icu(
            lambda client: icu_plan.update_event(
                engine,
                client,
                reason=reason,
                expected_version=expected_version,
                changes=changes,
                event_id=event_id,
                external_id=external_id,
                author=current_author(),
            )
        )

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_icu_move_event(
        reason: str,
        expected_version: str,
        start_date: str,
        event_id: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Move one plan event to another date, by its event id."""
        return _icu(
            lambda client: icu_plan.move_event(
                engine,
                client,
                reason=reason,
                expected_version=expected_version,
                start_date=_date(start_date),
                event_id=event_id,
                external_id=external_id,
                author=current_author(),
            )
        )

    @tool(ICU_DELETE, "plan.write")
    def vibesynergy_icu_delete_event(
        reason: str,
        expected_version: str,
        event_id: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete one plan event by its id, after the version check. A reason is required."""
        return _icu(
            lambda client: icu_plan.delete_event(
                engine,
                client,
                reason=reason,
                expected_version=expected_version,
                event_id=event_id,
                external_id=external_id,
                author=current_author(),
            )
        )

    @tool(ICU_DELETE, "plan.write")
    def vibesynergy_icu_revert(audit_id: int, reason: str) -> dict[str, Any]:
        """Undo one plan write. Restores the before snapshot through the API."""
        return _icu(
            lambda client: icu_plan.revert(
                engine, client, audit_id=audit_id, reason=reason, author=current_author()
            )
        )

    @tool(ICU_DELETE, "plan.write")
    def vibesynergy_icu_resolve_duplicate(
        keep: str,
        reason: str,
        expected_version: str,
        event_id: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Choose one side of a dubbel pair. Nothing is deleted until this is called.

        keep=garmin keeps the Garmin Coach session: the intervals.icu event is deleted and
        audited. keep=plan keeps the intervals.icu workout; Garmin Coach cannot be deleted
        from here, so that session is marked for the athlete.
        """
        return _icu(
            lambda client: icu_plan.resolve_duplicate(
                engine,
                client,
                keep=keep,
                reason=reason,
                expected_version=expected_version,
                event_id=event_id,
                external_id=external_id,
                author=current_author(),
            )
        )

    @tool(WRITE, "coach.write")
    def vibesynergy_coach_write(
        entry_type: coach_workouts.CoachEntryType,
        summary: str,
        idempotency_key: str,
        for_date: str,
        caveat: str | None = None,
        details: str | None = None,
        evidence_refs: list[str] | None = None,
        valid_until: str | None = None,
        model_id: str | None = None,
        predictions: list[predictions.CoachPredictionWrite] | None = None,
        kb_rules: list[str] | None = None,
        activity_id: str | None = None,
        public_insight: str | None = None,
        sessions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Write a typed AI coach entry. Author comes from the MCP connection, not this call.

        sessions (morning, evening, weekly): your verdict per planned session, shown as a
        status with one symbol and colour each: [{title, decision, note}], decision one of
        keep, adjust, skip, done, missed, proposal; note at most one short line.

        predictions are optional forecasts scored later from daily values. kb_rules stores
        knowledge-rule ids on the entry. Both can be omitted.
        activity_assessment only: activity_id links the entry to one activity (group id or
        garmin:/strava: id). public_insight is the one or two sentences that may appear in
        the activity's public Strava/Garmin description ("Persoonlijke AI inzicht"); max
        300 characters, one paragraph, no [ ] /* */. Never put health, diary, injury or
        other private details in it. The newest entry with public_insight for an activity
        wins; "" withdraws the line. Published blocks are updated within minutes when
        automatic publication is on. Because this line becomes public, it needs the
        ``publish`` permission; without it, write the entry without public_insight.
        """
        if public_insight and public_insight.strip() and "publish" not in current_scopes():
            raise ToolError(
                "public_insight ends up in the public Strava/Garmin description, and this app "
                "isn't allowed to publish. Write the entry without public_insight, or ask the "
                "athlete to allow publishing for this app (Settings → Coach apps)."
            )
        day = _date(for_date)
        meta = coach_workouts.write_meta(
            rt,
            entry_type=entry_type,
            for_date=day,
            meta={"model_id": model_id} if model_id else None,
        )
        try:
            row = coach_workouts.coach_write(
                engine,
                entry_type=entry_type,
                summary=summary,
                caveat=caveat,
                details=details,
                evidence_refs=evidence_refs,
                idempotency_key=idempotency_key,
                for_date=day,
                valid_until=datetime.fromisoformat(valid_until) if valid_until else None,
                meta=meta,
                predictions=predictions,
                kb_rules=kb_rules,
                activity_id=activity_id,
                public_insight=public_insight,
                sessions=sessions,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return envelope(row)

    @tool(READ, "read")
    def vibesynergy_activity_insights(days: int = 2) -> dict[str, Any]:
        """Activities of the last days (default 2, max 14) with a Strava copy: whether each
        has a public coach insight, its Strava publication state and strava_public (false
        while still private). missing lists the ids still without one, public or not, for
        vibesynergy_coach_write(activity_id=..., public_insight=...)."""
        from vibesynergy.query import publication_insight

        try:
            return envelope(publication_insight.recent(rt, days=days))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @tool(READ, "read")
    def vibesynergy_events(
        since: str | None = None, types: list[str] | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """VibeSynergy's events for agents that poll instead of receiving webhooks, oldest
        first: {type, version, id, occurred_at, data}. since is an ISO time (default the
        last 24 hours); pass next_since back to continue. types: activity.ready (new
        merged activities with ids, date, sport and public_insight_missing) and
        activity.published (the athlete made the Strava copy public: title, description and
        gear are final, so write the public insight now). Events carry
        ids only; read details with vibesynergy_activity and write back with
        vibesynergy_coach_write(activity_id=..., public_insight=...)."""
        from vibesynergy.query import agent_events

        try:
            return envelope(agent_events.recent(rt, since_at=since, types=types, limit=limit))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @tool(READ, "read")
    def vibesynergy_coach_entries(
        period: str | None = None,
        entry_type: str | None = None,
    ) -> dict[str, Any]:
        """Coach entries from vibesynergy_coach_write for a period (default last_28d).

        entry_type limits the list to morning, evening, note, activity_assessment or
        threshold_change. Each entry includes kb_rules and its predictions.
        """
        span = window(period, None, None, "last_28d")
        rows = predictions.coach_entries(engine, span, entry_type)
        return envelope({"entries": rows}, span=span)

    @tool(READ, "read")
    def vibesynergy_prediction_scores(
        period: str | None = None,
        metric: str | None = None,
    ) -> dict[str, Any]:
        """Bias and RMSE of scored predictions, one row per ISO week (default last_90d).

        error is actual minus predicted. bias is the mean error. rmse is the root mean
        square error. Open predictions, still waiting on a daily value, are left out.
        metric must be a daily value VibeSynergy stores.
        """
        span = window(period, None, None, "last_90d")
        data = predictions.prediction_scores(engine, span, metric)
        return envelope(data, span=span, metrics=[metric] if metric else None)

    @tool(WRITE, "plan.propose")
    def vibesynergy_coach_threshold_propose(
        sport: str,
        metric: str,
        value: float,
        unit: str,
        summary: str,
        idempotency_key: str,
        method: str = "estimate",
        activity_id: str | None = None,
        date_set: str | None = None,
    ) -> dict[str, Any]:
        """Propose a new threshold, e.g. after a test or race; nothing changes until the
        athlete takes it over on Training → Zones & thresholds.

        sport: bike, run, swim (or general for resting_hr_baseline / hrv_baseline).
        metric: ftp, threshold_power, lthr, max_hr, threshold_pace, css, vo2max,
        resting_hr_baseline, hrv_baseline. Values in W, bpm, s/km (run pace) or s/100m
        (CSS); m/s and min/km are converted. method: test, race, field, device_auto or
        estimate. activity_id: the test or race (group id or garmin:/strava: id).
        date_set: when it was measured (YYYY-MM-DD, default today). summary is what the
        athlete reads next to the proposal. Never pushed to Garmin or intervals.icu by
        itself: the athlete chooses that when taking it over.
        """
        try:
            row = coach_workouts.propose_threshold_change(
                engine,
                sport=sport,
                metric=metric,
                value=value,
                unit=unit,
                summary=summary,
                idempotency_key=idempotency_key,
                method=method,
                activity_id=activity_id,
                date_set=_date(date_set) if date_set else None,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return envelope(row)

    @tool(READ, "read")
    def vibesynergy_thresholds() -> dict[str, Any]:
        """Confirmed thresholds per sport (the values plans must use), their zones (Friel HR,
        power, pace/CSS), what Garmin and intervals.icu hold now with mismatches, measured
        references (resting HR, HRV, VO2max) and open proposals. Proposed values are not in
        sports.*.values until the athlete takes them over."""
        return envelope(thresholds.for_coach(engine, today=today()))

    @tool(WRITE, "coach.write")
    def vibesynergy_coach_log(text: str, kind: str = "note") -> dict[str, Any]:
        """Append a note to the coach feed."""
        if kind != "note":
            raise ToolError("vibesynergy_coach_log only accepts kind 'note'")
        row = coach_workouts.coach_write(
            engine,
            entry_type="note",
            summary=text,
            idempotency_key=None,
            for_date=today(),
        )
        return envelope({"logged": True, "kind": kind, "entry": row})

    @tool(WRITE, "coach.write")
    def vibesynergy_coach_memory_write(
        action: Literal["add", "update", "retire"],
        reason: str,
        item_id: str | None = None,
        kind: Literal["availability", "preference", "commitment", "equipment", "self_report"]
        | None = None,
        text: str | None = None,
        days: list[int] | None = None,
        origin: Literal["user_statement", "coach_inference"] = "user_statement",
    ) -> dict[str, Any]:
        """Keep "what to remember" yourself: add, update or retire one memory item.

        Add an item when the athlete says something durable (injury history, a preference, a
        fixed commitment, equipment) or a pattern repeats; origin=coach_inference for your
        own conclusion. days are weekdays 1 (Monday) to 7. You can change or retire only
        items you wrote (ids from vibesynergy_coach_state with source "coach"); the athlete's own
        items are theirs. reason is one line on why it belongs in memory. Every change is a
        version in Coach → Change log, with a revert.
        """
        try:
            row = coach.memory_write(
                engine,
                action,
                reason=reason,
                actor=current_author() or "mcp",
                item_id=item_id,
                kind=kind,
                text=text,
                days=days,
                origin=origin,
            )
        except (ValueError, coach.Conflict) as exc:
            raise ToolError(str(exc)) from exc
        return envelope(row)

    @tool(WRITE, "coach.write")
    def vibesynergy_model_run_write(
        model_id: str,
        params: dict[str, Any],
        report: str,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        """Store one personal-model fit: parameters and a markdown report.

        The difference in parameters against the previous run of the same model is
        stored on the row. The author is this connection.
        """
        row = model_run.write_run(
            engine,
            model_id=model_id,
            params=params,
            report=report,
            started_at=started_at,
            finished_at=finished_at,
            timezone=settings.timezone,
        )
        return envelope(row)

    @tool(READ, "read")
    def vibesynergy_model_runs(
        model_id: str,
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Runs of one personal model in a period (default all), newest first.

        Each run includes the parameter diff stored against the run before it.
        """
        span = window(period, start, end, "all")
        rows = model_run.list_runs(
            engine,
            model_id=model_id,
            start=span.start,
            end=span.end,
            timezone=settings.timezone,
        )
        return envelope({"model_id": model_id, "runs": rows}, span=span)

    @tool(WRITE, "diary.write")
    def vibesynergy_activity_set_purpose(id: str, purpose: str | None = None) -> dict[str, Any]:
        """Set session purpose: training, recovery, leisure, coached_by_me, race, or null (auto)."""
        from vibesynergy.query import session_purpose

        try:
            stats = session_purpose.set_override(engine, id, purpose)
        except (LookupError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return envelope({"activity_id": id, "purpose": purpose, **stats})

    @tool(READ, "read")
    def vibesynergy_overview() -> dict[str, Any]:
        """What is in the store: sources, the last 7 days of key metrics, sync problems.
        Includes the next race and current plan phase (including taper), when recorded.
        Start here when you do not yet know what data exists."""
        data = views.overview(engine, today())
        warnings = [f"{p['dataset']}: {p['error']}" for p in data["sync_problems"]]
        return envelope(data, warnings=warnings)

    @tool(READ, "read")
    def vibesynergy_catalog(
        query: str | None = None, domain: str | None = None, include_coverage: bool = True
    ) -> dict[str, Any]:
        """Search the metric catalogue: ids, labels, units, domain, default aggregation and
        coverage (first/last date, % of days). Use the ids in other tools."""
        entries = catalog.catalogue(engine, coverage=include_coverage)
        if domain:
            entries = {k: v for k, v in entries.items() if v["domain"] == domain}
        if query:
            q = query.lower()
            entries = {
                k: v for k, v in entries.items() if q in k.lower() or q in v["label"].lower()
            }
        return envelope({"metrics": list(entries.values())})

    @tool(READ, "read.expert")
    def vibesynergy_schema() -> dict[str, Any]:
        """Tables available to vibesynergy_sql, with their columns and units."""
        return envelope({"dialect": "SQLite", "tables": sql.TABLES})

    @tool(READ, "read")
    def vibesynergy_explain(metric: str, date: str) -> dict[str, Any]:
        """Where a daily value came from: the source shown, every source's value, the raw
        record behind each and the best-source rule that chose between them."""
        return envelope(
            views.explain(
                engine,
                metric,
                _date(date),
                rt.settings.best_source,
                best_source.load(rt.settings.data_dir),
            )
        )
