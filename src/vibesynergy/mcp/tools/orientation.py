"""MCP tools: orientation (split from mcp/server.py)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.mcp.identity import current_author, current_label, current_scopes
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
        the AI coach, and things to remember (availability, fixed moments, equipment). Plus
        plan-calendar races with their priority, and contact_window: when an AI coach may
        reach the athlete (start, end, time zone, open_now, opens_at) and the one urgent
        safety exception; outside it write entries as usual but send him nothing. The
        training plan and its rules are in
        the knowledge base and intervals.icu.
        """
        from vibesynergy.coach_runner import contact

        snap = coach.snapshot(engine)
        return envelope(
            {
                **snap,
                "active_profile": coach.profile_binding(snap),
                "races": plan_q.calendar_races(engine, today()),
                # When an AI coach may reach the athlete (#119): keep to it.
                "contact_window": contact.state(rt),
                "note": coach.COACH_STATE_NOTE,
            }
        )

    @tool(READ, "read")
    def vibesynergy_coach_handbook(include_protocol: bool = True) -> dict[str, Any]:
        """Read this first when you coach. The coach protocol (docs/COACH_PROTOCOL.md) for
        any AI coach: roles and what needs the athlete's akkoord, the morning, evening, weekly and
        insight runs, tool order, idempotency keys, privacy rules for public text, replies
        and failure handling. Plus, generated from the harness registry and this install's
        settings: the task register (every harness task with its triggers, scheduled time
        as due, days, context chain, contract, what it writes alone and what needs
        approval, last run and missing runs), approval (roles, write policy per tool,
        approval_id), contact_window (when you may reach the athlete), the active
        knowledge-base articles (slugs to read with vibesynergy_kb_get), the active rule ids
        (cite them in kb_rules), this connection's scopes against what the coach loop
        needs, and connection: your role next to other AI coaches (the athlete's own agent,
        primary AI coach or advisory AI coach), what that role may write, and the author
        suffix for your idempotency keys. include_protocol=false
        returns only the index and protocol_version, for later runs.
        """
        from vibesynergy.mcp import roles

        role, author = roles.current(engine), current_author()
        suffix = roles.key_suffix(author)
        data, warnings = coach_handbook.handbook(
            engine,
            scopes=current_scopes(),
            include_protocol=include_protocol,
            rt=rt,
            connection={
                "role": role,
                "role_name": roles.ROLE_NAMES[role],
                "may": roles.ROLE_MAY[role],
                "author": author,
                "key_suffix": suffix,
                "idempotency_key_example": f"morning-{today().isoformat()}-{suffix}",
                "writes_daily_advice": role != roles.ADVISORY,
            },
        )
        return envelope(data, warnings=warnings)

    @tool(WRITE, "plan.propose")
    def vibesynergy_race_set_priority(
        source: str, source_id: str, priority: str | None = None
    ) -> dict[str, Any]:
        """Set one race to A, B or C (null: unset). Races are VibeSynergy's own (#111):
        source vibesynergy and source_id the race id, as vibesynergy_coach_state and
        vibesynergy_plan list them; a Garmin or intervals.icu id of the race's copy also
        works. The next A race is the primary race. A change in Garmin or intervals.icu
        never changes it."""
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
        """The intervals.icu calendar as cached in VibeSynergy: its copy of the plan, for
        checking that copy and for the vibesynergy_icu_* write tools (event_id, external_id,
        version). Not the plan: advice, reports and the AI coach read VibeSynergy's plan
        (vibesynergy_plan, vibesynergy_plan_model) and never this. Default today to +28 days.
        category: WORKOUT, NOTE, TARGET, RACE_A/B/C. refresh=true fetches intervals.icu first.
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
        """The training plan as VibeSynergy's own model, the only plan (source vibesynergy):
        version, the sessions in the window (default today to +13 days) with their steps as
        intervals.icu text, and the weeks it touches with their note and targets. Change it
        with vibesynergy_plan_session_set / _move / _skip and vibesynergy_plan_week_set /
        vibesynergy_plan_target_set; intervals.icu gets a copy within minutes."""
        from vibesynergy.query import plan_model

        first = _date(start) if start else today()
        last = _date(end) if end else first + timedelta(days=13)
        return envelope(plan_model.view(engine, first, last), span=Window(first, last))

    def _plan_write(
        name: str, arguments: dict[str, Any], reason: str, approval_id: str | None
    ) -> dict[str, Any]:
        """One plan tool call: written when it is approved for this connection's role,
        else stored as a proposal (query.plan_proposals, #118)."""
        from vibesynergy.mcp.scopes import scope_denied
        from vibesynergy.query import plan_model, plan_proposals

        author = current_author() or "mcp"
        held = plan_proposals.gate(
            engine,
            name,
            arguments,
            reason=reason,
            approval_id=approval_id,
            author=author,
            author_name=current_label(),
        )
        if held is not None:
            return envelope(held)
        if "plan.write" not in current_scopes():
            raise ToolError(scope_denied("plan.write"))
        out = plan_proposals.execute(engine, name, arguments, author=author, reason=reason)
        plan_model.mirror_soon(rt.jobs)
        return envelope(out)

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_session_set(
        reason: str,
        date: str,
        name: str,
        description: str,
        sport: str | None = None,
        session_id: str | None = None,
        slot: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or change one session in VibeSynergy's plan (only when vibesynergy_plan_model says
        source vibesynergy). description is intervals.icu step text (cue first, then steps);
        unknown steps are refused. The change reaches intervals.icu (and from there Zwift and
        Garmin) within minutes; changes made in intervals.icu itself are put back. Keep the
        week note and week targets in step (COACH-5). slot names a new session's id; slots
        ms1-swim, ms2-ride, ms3-run (ms<n>-<sport>, msb1-... for a second one that day) link
        that day's sessions into one multisport in that order: a triathlon, duathlon or
        brick, one Garmin multisport workout. approval_id: the athlete's approval, see
        vibesynergy_coach_handbook (connection); without it an AI coach's change is stored
        as a proposal (written: false). Only the primary AI coach adding a new session (no
        session_id) in a slot that held none, in a week that has its note, needs none."""
        return _plan_write(
            "vibesynergy_plan_session_set",
            {
                "date": date,
                "name": name,
                "description": description,
                "sport": sport,
                "session_id": session_id,
                "slot": slot,
            },
            reason,
            approval_id,
        )

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_session_move(
        reason: str, session_id: str, date: str, approval_id: str | None = None
    ) -> dict[str, Any]:
        """Move one planned session in VibeSynergy's plan to another day (only when
        vibesynergy_plan_model says source vibesynergy). Its intervals.icu copy moves within
        minutes. Keep the week's note and targets in step (COACH-5). approval_id: the
        athlete's approval; without it an AI coach's move is stored as a proposal."""
        return _plan_write(
            "vibesynergy_plan_session_move",
            {"session_id": session_id, "date": date},
            reason,
            approval_id,
        )

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_session_skip(
        reason: str, session_id: str, approval_id: str | None = None
    ) -> dict[str, Any]:
        """Take one planned session out of VibeSynergy's plan (only when vibesynergy_plan_model
        says source vibesynergy). Its intervals.icu copy is removed within minutes; the
        session stays in the plan's history. approval_id: the athlete's approval; without
        it an AI coach's change is stored as a proposal."""
        return _plan_write(
            "vibesynergy_plan_session_skip", {"session_id": session_id}, reason, approval_id
        )

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_week_set(
        reason: str, week: int, note: str, approval_id: str | None = None
    ) -> dict[str, Any]:
        """Change the note of one week in VibeSynergy's plan (its intent and structure; only
        when vibesynergy_plan_model says source vibesynergy). It reaches intervals.icu within
        minutes. Change the week's sessions and targets with it (COACH-5). approval_id: the
        athlete's approval; without it an AI coach's change is stored as a proposal."""
        return _plan_write(
            "vibesynergy_plan_week_set", {"week": week, "note": note}, reason, approval_id
        )

    @tool(ICU_WRITE, "plan.write")
    def vibesynergy_plan_target_set(
        reason: str,
        week: int,
        sport: str,
        time_s: float | None = None,
        distance_m: float | None = None,
        load: float | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Set one weekly target in VibeSynergy's plan: sport (run, ride, swim, …) with time in
        seconds, distance in metres and/or load; none of them removes the target. Only when
        vibesynergy_plan_model says source vibesynergy; it reaches intervals.icu within minutes.
        approval_id: the athlete's approval; without it an AI coach's change is stored as a
        proposal."""
        return _plan_write(
            "vibesynergy_plan_target_set",
            {
                "week": week,
                "sport": sport,
                "time_s": time_s,
                "distance_m": distance_m,
                "load": load,
            },
            reason,
            approval_id,
        )

    def _approved(name: str, approval_id: str | None) -> None:
        """An intervals.icu write: the athlete's own agent, or the primary AI coach with
        an approval reference (#118)."""
        from vibesynergy.query import plan_proposals

        plan_proposals.require_approval(engine, name, approval_id)

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
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Add one event to intervals.icu only. A reason is required. Not a bulk upsert.
        It is not in VibeSynergy's plan and never becomes a planned session or a race there:
        plan with vibesynergy_plan_session_set and friends instead.

        targets (TARGET events): time_target (s), load_target, distance_target (m).
        approval_id: required for the primary AI coach ('proposal:<id>' or 'reply:<id>');
        an advisory AI coach cannot use this tool.
        """
        _approved("vibesynergy_icu_create_event", approval_id)
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
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Change one plan event by its id. external_id only finds that id in the cache.

        expected_version is the version from the last read. The write is refused when
        the cached event, or intervals.icu, has moved on. approval_id: required for the
        primary AI coach; an advisory AI coach cannot use this tool.
        """
        _approved("vibesynergy_icu_update_event", approval_id)
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
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Move one plan event to another date, by its event id. approval_id: required for
        the primary AI coach; an advisory AI coach cannot use this tool."""
        _approved("vibesynergy_icu_move_event", approval_id)
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
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete one plan event by its id, after the version check. A reason is required.
        approval_id: required for the primary AI coach; an advisory AI coach cannot use
        this tool."""
        _approved("vibesynergy_icu_delete_event", approval_id)
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
    def vibesynergy_icu_revert(
        audit_id: int, reason: str, approval_id: str | None = None
    ) -> dict[str, Any]:
        """Undo one plan write. Restores the before snapshot through the API. approval_id:
        required for the primary AI coach; an advisory AI coach cannot use this tool."""
        _approved("vibesynergy_icu_revert", approval_id)
        return _icu(
            lambda client: icu_plan.revert(
                engine, client, audit_id=audit_id, reason=reason, author=current_author()
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
        safety: dict[str, Any] | None = None,
        task: str | None = None,
        harness: str | None = None,
        changed_because: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a typed AI coach entry. Author comes from the MCP connection, not this call.

        Every write is checked against the harness's output contract and REFUSED when it
        misses it (nothing is stored; the error says what to fix, then write again):
        for_date must be the day the entry is about (morning: today; evening: today or
        tomorrow); the summary must fit the task's word limit (morning, evening 70; the
        rest goes in details); and a daily advice (entry_type morning or evening, or task
        injury or checkin) must carry safety.
        safety: {"level": ok|ease|flag|refuse, "colour": green|amber|red, "reasons":
        [1-5 short sentences: what you weighed and why this level], "override_reason":
        one sentence, required when your level is milder than the rules' level}. colour
        follows level: ok=green, ease=amber, flag=amber, refuse=red. The rules' level is
        safety.floor in vibesynergy_coach_prompt(job, include_context=true) and
        safety.level in vibesynergy_today. This is a check of validity, not of approval.
        Role (#118, see connection in vibesynergy_coach_handbook): only the primary AI
        coach's entry (and the athlete's own agent's) is the daily advice. An advisory AI
        coach's morning or evening entry is stored as a second opinion, shown beside the
        advice in the same day thread; where it differs in safety level or a session
        decision, the athlete sees it flagged and the primary AI coach gets it in its next
        run. An advisory AI coach cannot write public_insight.
        idempotency_key: one per author, e.g. morning-2026-11-19-<author> (your author
        suffix is connection.key_suffix). Keys never collide between AI coaches: a key
        another author used is stored for you as <key>-<suffix>.
        task: the harness task this entry belongs to (morning, evening, weekly, monthly,
        block, race, test, thresholds, kb, injury, checkin); its contract then applies.
        harness: the harness.stamp from vibesynergy_coach_prompt, so the entry records which
        harness version it was written under (the result says whether it is current).
        changed_because (#115): one day has one advice thread. When the day the advice is
        for already has a version (day_thread in vibesynergy_coach_prompt(job,
        include_context=true), or advice.versions in vibesynergy_today), yours is the next
        version and must say {"previous": <id of the current version>, "reason": "what
        changes against it and why"}; without it, or citing an older version, the write is
        refused. Not needed for the first version of a day or for a second opinion.

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
                "athlete to allow publishing for this app (Settings → Sharing → Coach apps)."
            )
        day = _date(for_date)
        from vibesynergy.coach_runner import contract as harness_contract
        from vibesynergy.coach_runner import prompts as harness_prompts
        from vibesynergy.coach_runner import registry
        from vibesynergy.mcp import roles

        role = roles.current(engine)
        if role == roles.ADVISORY and public_insight and public_insight.strip():
            raise roles.RoleError(
                roles.refusal(
                    "vibesynergy_coach_write",
                    role,
                    "with public_insight publishes a line on Strava/Garmin, and an advisory "
                    "AI coach does not publish. Write the entry without public_insight.",
                )
            )

        under = harness_contract.current_run()
        if under:
            # The API runner: the harness knows the task and the day, not the model. The
            # task's contract applies to its report; anything else it writes on the way
            # (an extra note, an activity assessment) is checked as that entry type.
            wanted = registry.BY_ID[under["task"]].contract.entry_type
            task = under["task"] if wanted == entry_type and under["task"] != "insight" else None
        try:
            applies = harness_contract.task_for(entry_type, task)
            floor = _rules_floor(day) if (applies and applies.contract.safety) or safety else None
            # The day thread (#115): a daily advice for a day that already has a version is
            # the next version of it, and must build on that one. A second opinion is not.
            target = harness_contract.advice_target(
                entry_type,
                applies.id if applies else None,
                day,
                under["for_date"] if under else today(),
                safety is not None,
            )
            previous = (
                _thread_previous(target, idempotency_key)
                if target is not None and role != roles.ADVISORY
                else None
            )
            checked = harness_contract.check_write(
                entry_type=entry_type,
                summary=summary,
                for_date=day,
                today=under["for_date"] if under else today(),
                task=task,
                safety=safety,
                floor=floor,
                # Only the athlete's own agent acts on his request; an AI coach on its own
                # (the runner, a scheduled routine of an app) never softens a hard rule.
                autonomous=under is not None or role != roles.OWN,
                previous=previous,
                changed_because=changed_because,
            )
            # A runner's daily advice is written on the run's own day; the evening's counts
            # for tomorrow (#90), which the harness records as its target.
            if (
                under is not None
                and checked["task"] == under["task"]
                and under.get("target")
                and day != under["for_date"]
            ):
                raise harness_contract.ContractError(
                    f"Refused by the output contract: for_date must be "
                    f"{under['for_date'].isoformat()} for this {under['task']} run"
                )
        except harness_contract.ContractError as exc:
            raise ToolError(str(exc)) from exc
        current = (
            harness_prompts.harness_stamp(settings, checked["task"], engine)
            if checked["task"]
            else None
        )
        stamp = harness_contract.harness_meta(
            task=checked["task"],
            declared=harness,
            current=current,
            floor=floor,
            target=harness_contract.advice_target(
                entry_type, checked["task"], day, today(), checked["safety"] is not None
            ),
            role=role,
        )
        extra: dict[str, Any] = {"harness": stamp}
        if model_id:
            extra["model_id"] = model_id
        if checked["safety"]:
            extra["safety"] = checked["safety"]
        if checked.get("changed_because"):
            extra["changed_because"] = checked["changed_because"]
        if role == roles.ADVISORY:
            # An advisory entry always carries its author in the key, so the primary AI
            # coach's key for the day stays its own (morning-<date>-<author>).
            suffix = "-" + roles.key_suffix(current_author())
            if not idempotency_key.strip().endswith(suffix):
                idempotency_key = idempotency_key.strip() + suffix
        meta = coach_workouts.write_meta(rt, entry_type=entry_type, for_date=day, meta=extra)
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
        warnings = []
        if checked["task"] and not stamp["follows_current"] and stamp["via"] == "mcp":
            warnings.append(
                "This entry was not written under the current harness "
                f"({current}). Read vibesynergy_coach_prompt(job) first and pass its "
                "harness.stamp as harness."
            )
        if role == roles.ADVISORY and stamp.get("target"):
            warnings.append(
                "Stored as a second opinion: the daily advice is the primary AI coach's. "
                "The athlete sees yours beside it, flagged where it differs."
            )
        if under is None and stamp.get("target") and role != roles.ADVISORY:
            # An MCP coach's daily advice joins the same thread and the same notice as the
            # runner's (#115): one notice per day, linked to this stored version.
            from vibesynergy.query import daily_advice

            try:
                daily_advice.announce(rt, row["id"])
            except Exception:  # the entry is stored; the notice is not worth a failed write
                log.exception("announcing the advice version failed")
        return envelope({**row, "harness": stamp}, warnings=warnings)

    def _thread_previous(target, key: str) -> dict[str, Any] | None:
        from vibesynergy.query import daily_advice

        return daily_advice.previous_version(
            rt, target, key=key.strip() if key else None, author=current_author()
        )

    def _rules_floor(day) -> dict[str, Any] | None:
        """The rules' safety outcome for ``day``, to hold the AI coach's own against. ``None``
        when it cannot be computed: the shape is still checked, the comparison is not."""
        from vibesynergy.query import coach_brief

        try:
            found = coach_brief.today_brief(rt, day)["safety"]
        except Exception:
            return None
        return {"level": found.get("level"), "rules": found.get("rules") or []}

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
        threshold_change. Each entry includes kb_rules and its predictions. A daily advice
        version also has advice_version (#115): {date (the day it is for), version, of,
        status (current, replaced or invalid), current_id, replaced_by} and its
        changed_because, the same thread as the start page and vibesynergy_today. Newest
        first.
        """
        from vibesynergy.query import daily_advice

        span = window(period, None, None, "last_28d")
        rows = predictions.coach_entries(engine, span, entry_type)
        for row in rows:
            if row.get("entry_type") not in daily_advice.ADVICE_ENTRY_TYPES:
                continue
            try:
                row["advice_version"] = daily_advice.version_state(rt, row["id"])
            except LookupError:
                row["advice_version"] = None
            row["changed_because"] = daily_advice.changed_because(row)
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
        from vibesynergy.coach_runner import contract as harness_contract

        try:  # the same contract as vibesynergy_coach_write: a note, within its length
            harness_contract.check_write(
                entry_type="note", summary=text, for_date=today(), today=today()
            )
        except harness_contract.ContractError as exc:
            raise ToolError(str(exc)) from exc
        from vibesynergy.mcp import roles

        stamp = harness_contract.harness_meta(
            task=None, declared=None, current=None, floor=None, role=roles.current(engine)
        )
        row = coach_workouts.coach_write(
            engine,
            entry_type="note",
            summary=text,
            idempotency_key=None,
            for_date=today(),
            meta={"harness": stamp},
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
