"""The coach feed: entries the coach writes over MCP, and threshold changes.

VibeSynergy's own workout proposals (propose → approve → Garmin push) were removed; the plan flows
intervals.icu → Garmin.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Engine, desc, select, update
from sqlalchemy.exc import IntegrityError

from vibesynergy.core.timeutil import utc_now
from vibesynergy.mcp.identity import read_author
from vibesynergy.query import athlete_threshold
from vibesynergy.store.schema import coach_decision_log

# What vibesynergy_coach_write accepts and publishes in its schema. Weekly and block
# reviews stay on the review rows; they are not decision-log entry types.
COACH_ENTRY_TYPES = frozenset(
    {
        "activity_assessment",
        "morning",
        "evening",
        "note",
        "threshold_change",
    }
)
CoachEntryType = Literal[
    "activity_assessment",
    "morning",
    "evening",
    "note",
    "threshold_change",
]
COACH_FEED_ENTRY_TYPES = frozenset(
    {
        "activity_assessment",
        "morning",
        "evening",
        "note",
        "threshold_change",
        "plan_change",
    }
)


def _resolve_author(author: str | None) -> str:
    if author is not None:
        return author
    try:
        from vibesynergy.mcp.identity import current_author

        return current_author()
    except ImportError:
        return "user"


def _format_coach_text(summary: str, caveat: str | None = None) -> str:
    text = (summary or "").strip()
    if caveat and str(caveat).strip():
        line = str(caveat).strip()
        text = f"{text}\n{line}" if text else line
    return text[:4000]


def entry_freshness(valid_until: datetime | None, *, now: datetime | None = None) -> str:
    when = now or utc_now()
    if valid_until is None:
        return "current"
    return "stale" if when > valid_until else "current"


def write_meta(
    rt,
    *,
    entry_type: str,
    for_date: date | None,
    meta: dict | None = None,
    subject_id: int = 1,
) -> dict:
    """Version stamps for coach entries (freshness and needs_revision on read)."""
    from vibesynergy.query import coach, daily_coach

    out = dict(meta or {})
    snap = coach.snapshot(rt.engine, subject_id=subject_id, timezone=rt.settings.timezone)
    out["profile_version"] = snap["version"]
    documents = snap["documents"]
    if policy := documents.get("policy"):
        out["policy_version"] = policy["version"]
    if context := documents.get("context"):
        out["context_version"] = context["fingerprint"]
    if for_date and entry_type in ("morning", "evening"):
        cap = daily_coach.capture(rt, for_date, entry_type)
        out["evidence_version"] = cap["input_version"]
    return out


def _parse_valid_until(entry: dict) -> datetime | None:
    value = entry.get("valid_until")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def coach_entry_freshness(
    entry: dict,
    coach_snap: dict,
    rt,
    *,
    subject_id: int = 1,
) -> str:
    if entry_freshness(_parse_valid_until(entry)) == "stale":
        return "stale"
    meta = entry.get("meta") or {}
    if meta.get("profile_version") and meta["profile_version"] != coach_snap["version"]:
        return "needs_revision"
    documents = coach_snap["documents"]
    policy = documents.get("policy")
    if (
        policy
        and meta.get("policy_version") is not None
        and meta["policy_version"] != policy["version"]
    ):
        return "needs_revision"
    context = documents.get("context")
    if (
        context
        and meta.get("context_version")
        and meta["context_version"] != context["fingerprint"]
    ):
        return "needs_revision"
    stored_evidence = meta.get("evidence_version")
    day = entry.get("for_date")
    kind = entry.get("entry_type")
    if stored_evidence and day and kind in ("morning", "evening"):
        from vibesynergy.query import daily_coach

        current = daily_coach.capture(rt, date.fromisoformat(day), kind)["input_version"]
        if stored_evidence != current:
            return "needs_revision"
    return "current"


def enrich_coach_entry(
    rt,
    entry: dict,
    coach_snap: dict,
    *,
    subject_id: int = 1,
    safety_by_day: dict[str, dict] | None = None,
) -> dict:
    from vibesynergy.query import daily_coach

    meta = dict(entry.get("meta") or {})
    text = entry.get("text") or ""
    lines = text.split("\n")
    advice_text = lines[0] if lines else ""
    caveat = lines[1] if len(lines) > 1 else ""
    entry_text = text
    advice_mode = "advice"
    safety_line = None
    safety_rules: list[str] = []
    day = entry.get("for_date")
    entry_kind = entry.get("entry_type")
    safety_kind = entry_kind if entry_kind in ("morning", "evening") else "morning"
    # Morning and evening advice stay as the coach wrote them. A flag or refuse day adds
    # one safety line. A plain note is never rewritten.
    if day and entry_kind in ("morning", "evening"):
        if safety_by_day is not None and day in safety_by_day:
            safety = safety_by_day[day]
        else:
            safety = daily_coach.current_safety(rt, date.fromisoformat(day))
            if safety_by_day is not None:
                safety_by_day[day] = safety
        safety_rules = list(safety["rules"])
        if safety["level"] == "refuse":
            safety_line = daily_coach.fixed_safety_summary(rt, safety_kind, "refuse")
            advice_mode = "safety_override"
        elif safety["level"] == "flag":
            safety_line = daily_coach.fixed_safety_summary(rt, safety_kind, "flag")
            advice_mode = "advice_with_safety"
    freshness = coach_entry_freshness(entry, coach_snap, rt, subject_id=subject_id)
    return {
        **entry,
        "meta": meta,
        "advice_text": advice_text,
        "caveat": caveat,
        "entry_text": meta.get("entry_text", entry_text),
        "advice_mode": advice_mode,
        "safety_line": safety_line,
        "safety_rules": safety_rules,
        "freshness": freshness,
    }


# The coach's verdict per planned session: one shared set of words, shown with the
# same symbol and colour everywhere (portal, mail).
SESSION_DECISIONS = ("keep", "adjust", "skip", "done", "missed", "proposal")


def clean_sessions(sessions: list) -> list[dict[str, str]]:
    """[{title, decision, note?}] with a known decision; at most 12, short texts."""
    out = []
    for item in sessions[:12]:
        if not isinstance(item, dict):
            raise ValueError("each session is {title, decision, note}")
        title = str(item.get("title") or "").strip()[:120]
        decision = str(item.get("decision") or "").strip()
        if not title or decision not in SESSION_DECISIONS:
            raise ValueError(f"session decision must be one of {', '.join(SESSION_DECISIONS)}")
        row = {"title": title, "decision": decision}
        note = str(item.get("note") or "").strip()[:200]
        if note:
            row["note"] = note
        out.append(row)
    return out


def coach_write(
    engine: Engine,
    *,
    entry_type: str,
    summary: str,
    caveat: str | None = None,
    details: str | None = None,
    evidence_refs: list | None = None,
    idempotency_key: str | None = None,
    for_date: date | None = None,
    valid_until: datetime | None = None,
    author: str | None = None,
    subject_id: int = 1,
    meta: dict | None = None,
    predictions: list | None = None,
    kb_rules: list | None = None,
    activity_id: str | None = None,
    public_insight: str | None = None,
    sessions: list | None = None,
) -> dict[str, Any]:
    if entry_type not in COACH_ENTRY_TYPES:
        raise ValueError(f"entry_type must be one of {', '.join(sorted(COACH_ENTRY_TYPES))}")
    link: dict[str, str] = {}
    if activity_id is not None or public_insight is not None:
        from vibesynergy.query import publication_insight

        if entry_type != publication_insight.ENTRY_TYPE:
            raise ValueError("activity_id and public_insight belong to an activity_assessment")
        if not activity_id:
            raise ValueError("public_insight needs the activity_id it belongs to")
        link["activity_id"] = publication_insight.resolve_link(engine, activity_id)
        if public_insight is not None:
            link["public_insight"] = publication_insight.validate(public_insight)
    if idempotency_key is not None and not str(idempotency_key).strip():
        raise ValueError("idempotency_key must not be blank")
    from vibesynergy.query import predictions as prediction_store

    resolved_author = _resolve_author(author)
    payload = dict(meta or {})
    if resolved_author.startswith("client:") and not payload.get("author_name"):
        try:
            from vibesynergy.mcp.identity import current_label

            label = current_label().strip()
        except ImportError:
            label = ""
        if label:
            payload["author_name"] = label
    if details:
        payload["details"] = details
    if evidence_refs:
        payload["evidence_refs"] = evidence_refs
    if sessions:
        payload["sessions"] = clean_sessions(sessions)
    payload.update(link)
    text = _format_coach_text(summary, caveat)
    if not text.strip():
        raise ValueError("summary is required")
    prepared = prediction_store.normalize_predictions(
        predictions,
        for_date=for_date,
        model_id=payload.get("model_id") if isinstance(payload.get("model_id"), str) else None,
    )
    if kb_rules is not None:
        payload["kb_rules"] = prediction_store.clean_rule_ids(kb_rules)
    now = utc_now()
    with engine.begin() as conn:
        if idempotency_key:
            existing = (
                conn.execute(
                    select(coach_decision_log).where(
                        coach_decision_log.c.idempotency_key == idempotency_key
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                return _published_coach_row(conn, existing)
        try:
            row_id = conn.execute(
                coach_decision_log.insert().values(
                    subject_id=subject_id,
                    at=now,
                    kind="note",
                    author=resolved_author,
                    entry_type=entry_type,
                    idempotency_key=idempotency_key,
                    for_date=for_date,
                    valid_until=valid_until,
                    text=text,
                    meta=payload,
                )
            ).inserted_primary_key[0]
        except IntegrityError:
            if not idempotency_key:
                raise
            existing = (
                conn.execute(
                    select(coach_decision_log).where(
                        coach_decision_log.c.idempotency_key == idempotency_key
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                return _published_coach_row(conn, existing)
            raise
        if prepared:
            prediction_store.insert_for_entry(conn, row_id, prepared)
        row = (
            conn.execute(select(coach_decision_log).where(coach_decision_log.c.id == row_id))
            .mappings()
            .one()
        )
        return _published_coach_row(conn, row)


def _published_coach_row(conn, row) -> dict[str, Any]:
    from vibesynergy.query.predictions import load_for_entry

    data = coach_row(row)
    data["predictions"] = load_for_entry(conn, data["id"])
    return data


def coach_row(row) -> dict[str, Any]:
    data = dict(row)
    if "author" in data:
        data["author"] = read_author(data["author"])
    for key in ("at", "valid_until"):
        if data.get(key):
            data[key] = data[key].isoformat()
    if data.get("for_date"):
        data["for_date"] = data["for_date"].isoformat()
    data["freshness"] = entry_freshness(
        row["valid_until"] if isinstance(row.get("valid_until"), datetime) else None
    )
    return data


# Old private names. An open branch (the coach runner) still imports ``_coach_row``;
# drop these once nothing does.
_coach_row = coach_row
_coach_entry_freshness = coach_entry_freshness
_enrich_coach_entry = enrich_coach_entry


def coach_feed(rt, *, subject_id: int = 1, limit: int = 80) -> list[dict[str, Any]]:
    from vibesynergy.query import coach

    engine = rt.engine
    coach_snap = coach.snapshot(engine, subject_id=subject_id, timezone=rt.settings.timezone)
    with engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                select(coach_decision_log)
                .where(
                    coach_decision_log.c.subject_id == subject_id,
                    coach_decision_log.c.entry_type.in_(tuple(COACH_FEED_ENTRY_TYPES)),
                )
                .order_by(desc(coach_decision_log.c.id))
                .limit(limit)
            ).mappings()
        ]
    safety_by_day: dict[str, dict] = {}
    return [
        enrich_coach_entry(
            rt,
            coach_row(row),
            coach_snap,
            subject_id=subject_id,
            safety_by_day=safety_by_day,
        )
        for row in rows
    ]


def workout_generation_thresholds(engine: Engine) -> dict[str, dict[str, Any]]:
    """Thresholds that may influence generated workouts (confirmed current values only)."""
    return athlete_threshold.workout_generation_thresholds(engine)


def propose_threshold_change(
    engine: Engine,
    *,
    sport: str,
    metric: str,
    value: float,
    unit: str,
    summary: str,
    idempotency_key: str,
    subject_id: int = 1,
    method: str = "estimate",
    activity_id: str | None = None,
    date_set: date | None = None,
) -> dict[str, Any]:
    """The AI coach proposes a threshold change; invisible to workout gen until confirmed.

    ``activity_id`` links the test or race it came from; ``date_set`` is when it was
    measured (default today). Nothing goes to Garmin or intervals.icu: the athlete takes
    the value over on the Zones & thresholds page and chooses there where else to set it.
    """
    from vibesynergy.query import thresholds

    sport, metric = thresholds.canonical(sport, metric)
    value = thresholds.to_canonical(sport, metric, value, unit)
    thresholds.validate(sport, metric, value)
    unit = thresholds.unit_for(sport, metric)
    group_id = thresholds._resolve_activity(engine, activity_id)
    with engine.connect() as conn:
        existing = (
            conn.execute(
                select(coach_decision_log).where(
                    coach_decision_log.c.idempotency_key == idempotency_key
                )
            )
            .mappings()
            .first()
        )
        if existing:
            return coach_row(existing)
    proposed = athlete_threshold.propose_threshold(
        engine,
        sport=sport,
        metric=metric,
        value=value,
        unit=unit,
        method=method,
        date_set=date_set or utc_now().date(),
        history=[{"event": "coach_proposal", "activity_id": group_id}] if group_id else None,
    )
    meta = {
        "status": "proposed",
        "sport": sport,
        "metric": metric,
        "value": value,
        "unit": unit,
        "method": proposed["method"],
        "athlete_threshold_id": proposed["id"],
    }
    if group_id:
        meta["activity_id"] = group_id
    return coach_write(
        engine,
        entry_type="threshold_change",
        summary=summary,
        idempotency_key=idempotency_key,
        for_date=utc_now().date(),
        meta=meta,
        subject_id=subject_id,
    )


def confirm_threshold_change(
    engine: Engine,
    entry_id: int,
    *,
    subject_id: int = 1,
) -> dict[str, Any]:
    """the athlete confirms a proposed threshold; flags workouts for regeneration.

    Confirming never writes to Garmin or intervals.icu; that is an explicit choice on the
    Zones & thresholds page (``threshold_push``).
    """
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(coach_decision_log).where(
                    coach_decision_log.c.id == entry_id,
                    coach_decision_log.c.subject_id == subject_id,
                    coach_decision_log.c.entry_type == "threshold_change",
                )
            )
            .mappings()
            .first()
        )
    if not row:
        raise LookupError("threshold proposal not found")
    meta = dict(row["meta"] or {})
    if meta.get("status") == "confirmed":
        return pending_workout_regeneration(engine, subject_id=subject_id)
    if meta.get("status") != "proposed":
        raise ValueError("threshold proposal is not awaiting confirmation")
    threshold_id = meta.get("athlete_threshold_id")
    if threshold_id is None:
        raise ValueError("threshold proposal has no athlete_threshold_id")
    athlete_threshold.confirm_threshold(engine, int(threshold_id))
    regen_from = utc_now().date()
    regen_until = regen_from + timedelta(days=7)
    meta.update(
        {
            "status": "confirmed",
            "confirmed_at": utc_now().isoformat(),
            "regenerate_workouts_from": regen_from.isoformat(),
            "regenerate_workouts_until": regen_until.isoformat(),
        }
    )
    with engine.begin() as conn:
        conn.execute(
            update(coach_decision_log).where(coach_decision_log.c.id == entry_id).values(meta=meta)
        )
    return pending_workout_regeneration(engine, subject_id=subject_id)


def pending_workout_regeneration(engine: Engine, *, subject_id: int = 1) -> dict[str, Any]:
    """Workouts in the regen window after a confirmed threshold change."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(coach_decision_log)
            .where(
                coach_decision_log.c.subject_id == subject_id,
                coach_decision_log.c.entry_type == "threshold_change",
            )
            .order_by(desc(coach_decision_log.c.id))
        ).mappings()
        pending = []
        for row in rows:
            meta = row["meta"] or {}
            if meta.get("status") != "confirmed":
                continue
            pending.append(
                {
                    "entry_id": row["id"],
                    "sport": meta.get("sport"),
                    "metric": meta.get("metric"),
                    "regenerate_workouts_from": meta.get("regenerate_workouts_from"),
                    "regenerate_workouts_until": meta.get("regenerate_workouts_until"),
                }
            )
    return {"pending_regeneration": pending}
