"""One versioned user context and coaching policy. No inference or external writes here.

Since the policy holds only how the coach talks to the athlete and the athlete's own
notes; the context holds only what the coach should remember (availability, fixed moments,
preferences, equipment). The training plan and its rules live in the knowledge base and in
intervals.icu; races and their priorities come from the plan calendar.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.store.schema import coach_event, coach_head, coach_revision

SCHEMA_VERSION = 1
# 2: the method, priorities, phases, sport order, weekly time, event sources, manual
# races, source priorities and the internal coach's assessment schedule are gone.
# 3: the Garmin Coach boundary is gone (Claude is the only coach).
POLICY_VERSION = "3"
BOUNDARIES = [
    "Use measured evidence and confirmed user context; distinguish uncertainty and inference.",
    "Do not diagnose, invent measurements or infer exercise load from calendar commitments.",
    "Preserve personal events and human-written activity text.",
    "Settings never grant external-write permission or bypass workout approval and ownership.",
    (
        "Private context stays out of public annotations; apply the "
        "shared safety engine before actions."
    ),
]
# Where the coach finds what these settings no longer hold. Returned by vibesynergy_coach_state.
COACH_STATE_NOTE = (
    "Only the athlete's communication preferences, notes for the coach and things to "
    "remember (availability, fixed moments, equipment) live here. The training plan and its "
    "rules are in the knowledge base (the plan article, intervals-icu-workout-authoring, "
    "claude-coach-loop) and in intervals.icu. Race priorities are the races listed here, "
    "from the plan calendar and the intervals.icu RACE_A/B/C category."
)
# Keys older stored documents may still hold. They are dropped on read and on the next save;
# nothing reads them any more. Any other unknown key is still an error.
RETIRED_POLICY_KEYS = frozenset(
    {
        "method",
        "priority",
        "athlete",
        "phase",
        "sport_priority",
        "sport_methods",
        "week_minutes",
        "usage_budget",
        "garmin_inputs",
        "assessments",
    }
)
RETIRED_CONTEXT_KEYS = frozenset({"event_sources", "manual_events", "source_priorities", "goals"})
# Instructions that try to cancel a fixed bound are kept as text and explained, not applied.
_INSTRUCTION_CONFLICTS = (
    (
        "safety",
        (
            r"\bignore (the )?(safety|pain|injury|illness)\b",
            r"\b(skip|don'?t see|do not see) (a |the )?(doctor|physiotherapist|clinician)\b",
            r"\bdiagnos(e|is)\b",
            r"\btrain through (the )?(pain|injury)\b",
            r"\bno safety (check|rule)",
        ),
        (
            "Safety still applies. This note cannot override pain, illness "
            "or the advice to see a clinician."
        ),
    ),
    (
        "privacy",
        (
            r"\bignore privacy\b",
            r"\b(publish|include|put|share).{0,48}\b(hrv|sleep score|weight|injury|diary)\b",
            r"\b(hrv|sleep score|weight|injury|diary).{0,48}\b(public|strava|annotation)\b",
        ),
        "Private health details stay out of public activity text.",
    ),
    (
        "approval",
        (
            r"\b(skip|without|no) (your |my )?approval\b",
            r"\bignore (ownership|approval)\b",
        ),
        "Workout changes still need your approval.",
    ),
    (
        "evidence",
        (
            r"\b(invent|make up|fabricate) (the )?(numbers|pace|heart rate|distance)\b",
            r"\bignore (the )?(measurements|evidence|data)\b",
        ),
        "Measured results stay the facts. This note cannot replace them.",
    ),
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _without(data: Any, retired: frozenset[str]) -> Any:
    if isinstance(data, dict) and retired & data.keys():
        return {key: value for key, value in data.items() if key not in retired}
    return data


class Policy(StrictModel):
    """How the coach talks to the athlete, plus the athlete's own notes for the coach."""

    instructions: str = Field(default="", max_length=2000)
    language: Literal["auto", "en", "nl"] = "auto"
    detail: Literal["concise", "standard", "detailed"] = "concise"
    tone: Literal["direct", "supportive"] = "direct"

    @model_validator(mode="before")
    @classmethod
    def ignore_retired(cls, data: Any) -> Any:
        return _without(data, RETIRED_POLICY_KEYS)


class ContextLink(StrictModel):
    """A pointer at an existing diary, equipment or event row. Not a copy of that row."""

    entity: Literal["note", "injury", "gear", "manual_event", "provider_event"]
    ref: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:@+/-]+$")


class Constraint(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    kind: Literal["availability", "preference", "commitment", "equipment", "self_report"]
    text: str = Field(min_length=1, max_length=2000)
    # "coach": written by the coach over MCP; the coach may change only these.
    source: Literal["user", "verified_migration", "coach"] = "user"
    origin: Literal["user_statement", "measurement", "coach_inference"] = "user_statement"
    active: bool = True
    days: list[int] = Field(default_factory=list, max_length=7)
    links: list[ContextLink] = Field(default_factory=list, max_length=8)
    archive_key: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    imported_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def provenance(self) -> Constraint:
        if self.origin == "measurement" and not self.links:
            raise ValueError("a measurement must point at an existing record")
        if len({(link.entity, link.ref) for link in self.links}) != len(self.links):
            raise ValueError("context links must be unique")
        weekdays = set(self.days)
        if len(weekdays) != len(self.days) or any(day not in range(1, 8) for day in weekdays):
            raise ValueError("days must be unique weekdays from Monday to Sunday")
        return self


class Context(StrictModel):
    """What the coach should remember: availability, fixed moments, preferences, equipment."""

    constraints: list[Constraint] = Field(default_factory=list, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def ignore_retired(cls, data: Any) -> Any:
        return _without(data, RETIRED_CONTEXT_KEYS)

    @model_validator(mode="after")
    def unique_ids(self) -> Context:
        if len({e.id for e in self.constraints}) != len(self.constraints):
            raise ValueError("context entry IDs must be unique")
        archive_keys = [item.archive_key for item in self.constraints if item.archive_key]
        if len(archive_keys) != len(set(archive_keys)):
            raise ValueError("archive keys must be unique")
        return self


class Conflict(ValueError):
    """The user must reload before applying an edit based on an older revision."""


def _effective_context(values: dict) -> dict:
    """What coaching reads. Retired notes stay in the payload so a later import cannot
    recreate them, and they do not steer advice."""
    return {**values, "constraints": [row for row in values["constraints"] if row["active"]]}


def instruction_conflicts(text: str) -> list[dict]:
    """Which fixed bounds an instruction tries to cancel, and the bound that still wins."""
    if not isinstance(text, str) or not text.strip():
        return []
    folded = text.casefold()
    found = []
    for bound, patterns, explanation in _INSTRUCTION_CONFLICTS:
        if any(re.search(pattern, folded) for pattern in patterns):
            found.append({"bound": bound, "explanation": explanation})
    return found


def policy_conflicts(payload: dict) -> list[dict]:
    """Conflicts to show next to the notes. They do not change the stored fingerprint."""
    if not isinstance(payload, dict):
        return []
    text = payload.get("instructions")
    return instruction_conflicts(text if isinstance(text, str) else "")


def profile_binding(snapshot: dict | None) -> dict:
    """The active profile identity shared by dashboard, advice, publication and MCP."""
    snapshot = snapshot or {}
    documents = snapshot.get("documents") if isinstance(snapshot.get("documents"), dict) else {}
    policy = documents.get("policy") if isinstance(documents.get("policy"), dict) else {}
    effective = policy.get("effective") if isinstance(policy.get("effective"), dict) else {}
    return {
        "profile_version": snapshot.get("version"),
        "policy_version": effective.get("policy_version"),
        "policy_revision": policy.get("version"),
        "policy_fingerprint": policy.get("fingerprint"),
    }


def fingerprint(data: Any) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def catalogue() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "boundaries": BOUNDARIES,
        "policy_defaults": Policy().model_dump(),
    }


def default_revision() -> dict:
    """Stable active baseline until the user activates a saved revision.

    Version zero is tied to this catalogue version and never writes a revision on read.
    Existing installations with no selected policy therefore receive usable defaults.
    """
    checked = preview("policy", Policy().model_dump())
    return {"version": 0, "schema_version": SCHEMA_VERSION, **checked}


def preview(kind: str, payload: dict) -> dict:
    if kind == "policy":
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("instructions", ""), str)
            and len(payload.get("instructions", "")) > 2000
        ):
            raise ValueError("Keep coaching instructions within 2,000 characters.")
        values = Policy.model_validate(payload).model_dump()
        effective = {**values, "policy_version": POLICY_VERSION, "boundaries": BOUNDARIES}
    elif kind == "context":
        values = Context.model_validate(payload).model_dump()
        effective = _effective_context(values)
    else:
        raise ValueError("kind must be policy or context")
    return {"payload": values, "effective": effective, "fingerprint": fingerprint(effective)}


def _scope(table, subject_id: int, kind: str):
    if type(subject_id) is not int or subject_id < 1 or kind not in ("policy", "context"):
        raise ValueError("invalid coach document scope")
    return (table.c.subject_id == subject_id) & (table.c.kind == kind)


def _revision(conn, subject_id, kind, version):
    if version is None:
        return None
    row = (
        conn.execute(
            select(coach_revision).where(
                _scope(coach_revision, subject_id, kind), coach_revision.c.version == version
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def state(engine: Engine, kind: str, *, subject_id: int = 1) -> dict:
    with engine.connect() as conn:
        if not conn.in_transaction():
            conn.exec_driver_sql("BEGIN")
        head = (
            conn.execute(select(coach_head).where(_scope(coach_head, subject_id, kind)))
            .mappings()
            .first()
        )
        version, active = (head["version"], head["active_version"]) if head else (0, None)
        history = (
            conn.execute(
                select(
                    coach_revision.c.version,
                    coach_revision.c.created_at,
                    coach_revision.c.actor,
                    coach_revision.c.reason,
                    coach_revision.c.fingerprint,
                )
                .where(_scope(coach_revision, subject_id, kind))
                .order_by(coach_revision.c.version.desc())
                .limit(50)
            )
            .mappings()
            .all()
        )
        return {
            "version": version,
            "active_version": active if active is not None else (0 if kind == "policy" else None),
            "draft": _revision(conn, subject_id, kind, version),
            "active": _revision(conn, subject_id, kind, active)
            or (default_revision() if kind == "policy" and active is None else None),
            "history": [dict(r) for r in history],
        }


def _validate_change(expected_version, actor, reason):
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected_version must be a nonnegative integer")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 100:
        raise ValueError("actor is required (at most 100 characters)")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 1000):
        raise ValueError("reason must be at most 1000 characters")


def save_draft(
    engine: Engine,
    kind: str,
    payload: dict,
    *,
    expected_version: int,
    reason: str | None = None,
    actor: str = "user",
    subject_id: int = 1,
) -> dict:
    if kind == "context":
        # A partial edit keeps the rest of the active context.
        with engine.connect() as conn:
            head = (
                conn.execute(select(coach_head).where(_scope(coach_head, subject_id, kind)))
                .mappings()
                .first()
            )
            if head and head["active_version"]:
                active = _revision(conn, subject_id, kind, head["active_version"])
                if active and isinstance(active.get("payload"), dict):
                    payload = {**_without(active["payload"], RETIRED_CONTEXT_KEYS), **payload}
    checked = preview(kind, payload)
    _validate_change(expected_version, actor, reason)
    scope = _scope(coach_head, subject_id, kind)
    version = expected_version + 1
    with engine.begin() as conn:
        conn.execute(
            insert(coach_head)
            .values(subject_id=subject_id, kind=kind, version=0)
            .on_conflict_do_nothing()
        )
        result = conn.execute(
            coach_head.update()
            .where(scope, coach_head.c.version == expected_version)
            .values(version=version)
        )
        if result.rowcount != 1:
            raise Conflict("coach settings changed; reload and review before saving")
        conn.execute(
            coach_revision.insert().values(
                subject_id=subject_id,
                kind=kind,
                version=version,
                schema_version=SCHEMA_VERSION,
                **checked,
                created_at=utc_now(),
                actor=actor,
                reason=reason or "",
            )
        )
    return state(engine, kind, subject_id=subject_id)


def activate(
    engine: Engine,
    kind: str,
    *,
    expected_version: int,
    reason: str | None = None,
    actor: str = "user",
    subject_id: int = 1,
) -> dict:
    _validate_change(expected_version, actor, reason)
    if expected_version == 0:
        raise ValueError("save and preview a draft first")
    with engine.begin() as conn:
        # Taking the write lock before reading ensures another save cannot race activation.
        revision = conn.execute(
            select(coach_revision.c.payload).where(
                _scope(coach_revision, subject_id, kind),
                coach_revision.c.version == expected_version,
            )
        ).scalar_one_or_none()
        if revision is None:
            raise Conflict("coach settings changed; reload and review before activation")
        preview(kind, revision)
        result = conn.execute(
            coach_head.update()
            .where(_scope(coach_head, subject_id, kind), coach_head.c.version == expected_version)
            .values(active_version=expected_version)
        )
        if result.rowcount != 1:
            raise Conflict("coach settings changed; reload and review before activation")
        previous = conn.execute(
            select(coach_event.c.version)
            .where(_scope(coach_event, subject_id, kind), coach_event.c.action == "activate")
            .order_by(coach_event.c.id.desc())
            .limit(1)
        ).scalar()
        if previous != expected_version:
            conn.execute(
                coach_event.insert().values(
                    subject_id=subject_id,
                    kind=kind,
                    version=expected_version,
                    action="activate",
                    actor=actor,
                    reason=reason or "",
                    created_at=utc_now(),
                )
            )
    return state(engine, kind, subject_id=subject_id)


def restore_draft(
    engine: Engine,
    kind: str,
    version: int,
    *,
    expected_version: int,
    reason: str | None = None,
    actor: str = "user",
    subject_id: int = 1,
) -> dict:
    with engine.connect() as conn:
        old = _revision(conn, subject_id, kind, version)
    if not old:
        raise ValueError("revision not found")
    return save_draft(
        engine,
        kind,
        old["payload"],
        expected_version=expected_version,
        reason=reason,
        actor=actor,
        subject_id=subject_id,
    )


def athlete_notes(engine: Engine, *, subject_id: int = 1) -> str:
    """The athlete's notes for a coach. Coaches can read them. They cannot write them."""
    active = state(engine, "policy", subject_id=subject_id).get("active") or {}
    payload = active.get("payload") or {}
    return str(payload.get("instructions") or "")


def athlete_for_coach(engine: Engine, settings: Any, *, subject_id: int = 1) -> dict[str, Any]:
    """Profile, thresholds, plan-calendar races and notes. Notes are the athlete's words."""
    from vibesynergy.query import athlete_threshold, plan, thresholds

    current = athlete_threshold.current_thresholds(engine)
    run = current.get("run") or {}

    def pick(metric: str) -> dict[str, Any] | None:
        row = run.get(metric)
        if not row:
            return None
        return {"value": row["value"], "unit": row["unit"], "sport": row["sport"]}

    return {
        "profile": {
            "timezone": settings.timezone,
            "units": settings.units,
            "sex": settings.athlete.get("sex"),
            "max_hr": settings.athlete.get("max_hr"),
            "resting_hr": settings.athlete.get("resting_hr"),
            # The age, not the date of birth: enough for masters rules and max-HR
            # estimates, and nothing more personal than needed.
            "age": age_on(settings.athlete.get("birth_date"), today_in(settings.timezone)),
        },
        "notes": athlete_notes(engine, subject_id=subject_id),
        "thresholds": {
            "lthr": pick("lthr"),
            "threshold_pace": pick("threshold_pace"),
            # Every confirmed value per sport (bike, run, swim, general); zones and
            # platform values: vibesynergy_thresholds.
            "by_sport": {
                sport: {
                    metric: {k: row[k] for k in ("value", "unit", "method", "date_set")}
                    for metric, row in metrics.items()
                }
                for sport, metrics in thresholds.current_values(engine).items()
            },
        },
        "races": plan.calendar_races(engine, today_in(settings.timezone)),
    }


def snapshot(engine: Engine, *, subject_id: int = 1, raw=None, timezone: str = "UTC") -> dict:
    """The active documents and their combined fingerprint, read in one snapshot.

    ``raw`` and ``timezone`` are accepted for older callers; nothing here reads them since
    the Garmin goal and event inputs left.
    """
    del raw, timezone
    with engine.connect() as conn:
        if not conn.in_transaction():
            conn.exec_driver_sql("BEGIN")  # explicit read snapshot on SQLite
        heads = (
            conn.execute(select(coach_head).where(coach_head.c.subject_id == subject_id))
            .mappings()
            .all()
        )
        documents = {
            h["kind"]: _revision(conn, subject_id, h["kind"], h["active_version"]) for h in heads
        }
    active = {
        k: {
            "version": r["version"],
            "fingerprint": r["fingerprint"],
            "effective": r["effective"],
        }
        for k, r in documents.items()
        if r
    }
    if "policy" not in active:
        baseline = default_revision()
        active["policy"] = {
            "version": 0,
            "fingerprint": baseline["fingerprint"],
            "effective": baseline["effective"],
        }
    return {"documents": active, "version": fingerprint(active)}


def apply_active(
    engine: Engine,
    kind: str,
    payload: dict,
    *,
    expected_version: int,
    reason: str | None = None,
    actor: str = "user",
    subject_id: int = 1,
) -> dict:
    """Save and activate in one step for immediate settings."""
    saved = save_draft(
        engine,
        kind,
        payload,
        expected_version=expected_version,
        reason=reason,
        actor=actor,
        subject_id=subject_id,
    )
    return activate(
        engine,
        kind,
        expected_version=saved["version"],
        reason=reason,
        actor=actor,
        subject_id=subject_id,
    )


def coaching_race_context(
    engine: Engine,
    today: date,
    *,
    subject_id: int = 1,
    raw=None,
    profile: dict | None = None,
) -> dict:
    """Next race, plan phase and tune-ups from the plan calendar.

    The same projection the start page uses: Garmin and intervals.icu races with their
    stored or RACE_A/B/C priority. ``subject_id``, ``raw`` and ``profile`` are accepted for
    older callers and not read.
    """
    from vibesynergy.query import plan

    del subject_id, raw, profile
    phase = plan.race_phase(engine, today)
    return {
        "next_race": phase.get("next_race"),
        "plan_phase": phase.get("plan_phase"),
        "supporting": phase.get("supporting") or [],
    }


WORKOUT_EQUIPMENT_RULE = (
    "Prescribe only items that are confirmed; for 'unknown', ask the athlete first."
)


def workout_equipment_context(engine: Engine, location_key: str) -> dict:
    """Equipment rows that may appear in a workout prompt for one location (scenario 10)."""
    from vibesynergy.query import equipment as equipment_query

    items = equipment_query.prescribable_items(engine, location_key)
    return {
        "location_key": location_key,
        "rule": WORKOUT_EQUIPMENT_RULE,
        "items": items,
    }


def workout_prompt_context(
    engine: Engine,
    location_key: str,
    *,
    subject_id: int = 1,
    raw=None,
) -> dict:
    """Assemble workout-facing coach context: user equipment notes plus the location register."""
    profile = snapshot(engine, subject_id=subject_id, raw=raw)
    context = profile.get("documents", {}).get("context", {}).get("effective") or {}
    equipment_notes = [
        row
        for row in context.get("constraints") or []
        if row.get("kind") == "equipment" and row.get("active", True)
    ]
    register = workout_equipment_context(engine, location_key)
    return {
        "equipment_register": register,
        "equipment_context_entries": equipment_notes,
    }


def age_on(birth_date: str | None, day) -> int | None:
    """Whole years on ``day`` for an ISO birth date, or None when it is not set."""
    if not birth_date:
        return None
    from datetime import date

    born = date.fromisoformat(birth_date)
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


MemoryKind = Literal["availability", "preference", "commitment", "equipment", "self_report"]


def memory_write(
    engine: Engine,
    action: Literal["add", "update", "retire"],
    *,
    reason: str,
    actor: str,
    item_id: str | None = None,
    kind: MemoryKind | None = None,
    text: str | None = None,
    days: list[int] | None = None,
    origin: Literal["user_statement", "coach_inference"] = "user_statement",
    subject_id: int = 1,
) -> dict:
    """The coach keeps "what to remember" itself: add, update or retire one item.

    The coach changes only items it wrote (source "coach"); the athlete's own items stay
    theirs. Every change is a new active revision with the actor and reason, so the Change log
    shows it with a diff and a revert.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("say in one line why this belongs in memory")
    current = state(engine, "context", subject_id=subject_id)
    active = current.get("active") or {}
    items = [dict(row) for row in (active.get("payload") or {}).get("constraints") or []]
    if action == "add":
        if not text or kind is None:
            raise ValueError("add needs kind and text")
        new_id = "coach-" + hashlib.sha256(f"{kind}:{text}".encode()).hexdigest()[:10]
        if any(row["id"] == new_id for row in items):
            raise ValueError("this item is already in memory")
        items.append(
            {
                "id": new_id,
                "kind": kind,
                "text": text,
                "source": "coach",
                "origin": origin,
                "active": True,
                "days": days or [],
            }
        )
        changed = new_id
    else:
        found = next((row for row in items if row["id"] == item_id), None)
        if found is None:
            raise ValueError(f"no memory item {item_id!r}")
        if found.get("source") != "coach":
            raise ValueError("this item is the athlete's own; propose the change to them instead")
        if action == "retire":
            found["active"] = False
        else:
            if text is not None:
                found["text"] = text
            if kind is not None:
                found["kind"] = kind
            if days is not None:
                found["days"] = days
            found["origin"] = origin
        changed = str(item_id)
    result = apply_active(
        engine,
        "context",
        {"constraints": items},
        expected_version=current["version"],
        reason=reason.strip(),
        actor=actor,
        subject_id=subject_id,
    )
    return {"item_id": changed, "action": action, "version": result.get("version")}
