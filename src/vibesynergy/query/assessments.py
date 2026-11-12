"""Durable activity assessment snapshots, identity reconciliation and read-only freshness.

No LLM calls or external actions. Consumers share this registry; only an explicit worker
may prepare/complete a revision. Reading never queues work. Private interpretation is never
used to construct the public projection.
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, literal_column, or_, select, tuple_, union_all

from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.core.user_datetime import format_user_local_time_hhmm
from vibesynergy.query import (
    activity_analysis,
    coach,
    evidence,
    journal,
    plan,
    series,
    snapshot,
    training,
    views,
)
from vibesynergy.store.schema import (
    activity,
    activity_link,
    activity_unified,
    checkin,
    daily_metric,
    daily_unified,
    injury,
    note,
    planned_item,
    raw_item,
)

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1
ANALYSIS_VERSION = 2
# Prompt 4 clarifies per-sport method overrides while keeping the JSON contract unchanged.
PROMPT_VERSION = 4
# The input version still hashes prompt 1: a prompt wording change must not make every stored
# assessment stale (which would queue a new AI call per recent activity). Only changed
# evidence does that. Stored revisions keep the prompt_version they were written with.
INPUT_PROMPT_VERSION = 1
# Observation window, not a claim of complete coverage or a physiological safety threshold.
CONTEXT_DAYS = 42
METRICS = (
    "sleep.score",
    "sleep.duration",
    "hrv.night_avg",
    "hr.resting",
    "recovery.readiness",
    "recovery.body_battery.max",
    "recovery.stress.avg",
)


class ChangedInputs(ValueError):
    pass


# Day-stable sections of the semantic view. Cleaned once per day on the bulk path.
_STABLE_SECTIONS = frozenset(
    {
        "schema_version",
        "analysis_version",
        "prompt_version",
        "daily",
        "recent_activities",
        "coverage",
        "plan",
        "journal",
        "coach",
    }
)
_VERSION_PLACEHOLDERS = {
    "recorded": {},
    "evidence": {"facts_version": ""},
    "laps": [],
    "zones": [],
    "source_status": [],
    "comparison": {"matches": []},
}


def _semantic_view(inputs: dict) -> dict:
    """Semantic sections before bookkeeping is stripped. Shared by both digest paths."""
    coach_inputs = inputs["coach"]
    comparison = inputs["comparison"]
    # Derived display and coach context must not make stored assessments stale.
    return {
        **{k: v for k, v in inputs.items() if k not in ("sport_units", "coach_context")},
        "laps": [
            {k: v for k, v in lap.items() if k != "sport_units"} for lap in inputs.get("laps", [])
        ],
        "prompt_version": INPUT_PROMPT_VERSION,
        "evidence": {"facts_version": inputs["evidence"]["facts_version"]},
        "coach": {
            "documents": {
                kind: revision["effective"] for kind, revision in coach_inputs["documents"].items()
            },
        },
        # Candidate source aliases can change when an identical provider duplicate is added.
        "comparison": {
            **comparison,
            "matches": [
                {k: v for k, v in item.items() if k != "id"} for item in comparison["matches"]
            ],
        },
    }


def _cleaned_stable(stable_json: dict) -> dict:
    """Cleaned day-stable sections. ``stable_json`` has already been through ``_json``."""
    viewed = _semantic_view({**_VERSION_PLACEHOLDERS, **stable_json})
    return {
        key: snapshot.semantic(value) for key, value in viewed.items() if key in _STABLE_SECTIONS
    }


def _semantic(inputs: dict, *, cleaned_stable: dict | None = None) -> dict:
    """What decides the input version, per top-level section; derived from stored inputs only,
    so the version and the decision log's 'what changed' can never disagree.

    ``cleaned_stable`` is the bulk-path cache of the day-stable sections. It must be the
    ``snapshot.semantic`` of those same sections, so the digest matches a full ``_semantic`` call.
    """
    viewed = _semantic_view(inputs)
    if not cleaned_stable:
        return snapshot.semantic(viewed)
    semantic = dict(cleaned_stable)
    for key, value in viewed.items():
        if key not in cleaned_stable:
            semantic[key] = snapshot.semantic(value)
    return semantic


def _on_same_day(value, day: date) -> bool:
    if isinstance(value, date):
        return value == day
    return str(value)[:10] == day.isoformat()


def _volume_parts(reader, day: date):
    """Moving time and the ramp the safety rules read. Shared with the bulk advice guard."""
    recent = training.volume(
        reader, Window(day - timedelta(days=6), day), granularity="day", by="all"
    )["totals"].get("all", {})
    previous = training.volume(
        reader,
        Window(day - timedelta(days=34), day - timedelta(days=7)),
        granularity="day",
        by="all",
    )["totals"].get("all", {})
    recent_s = recent.get("moving_s", 0)
    usual_s = previous.get("moving_s", 0) / 4
    ramp = round(100 * (recent_s / usual_s - 1), 1) if usual_s else None
    return recent, recent_s, usual_s, ramp


def _recovery_context(reader, day: date, daily: list) -> dict:
    today = {r["metric"]: r["value"] for r in daily if _on_same_day(r["date"], day)}
    return {
        "sleep_score": today.get("sleep.score"),
        "sleep_duration_s": today.get("sleep.duration"),
        "hrv_night_avg": today.get("hrv.night_avg"),
        "hrv_baseline": series.baseline(reader, "hrv.night_avg", day, window_days=41),
        "readiness": today.get("recovery.readiness"),
        "body_battery_max": today.get("recovery.body_battery.max"),
    }


def _coach_context(reader, rt, activity_id, detail, day, daily, planned_today):
    """Compact interpretation aids. All underlying evidence already has a versioned home."""
    laps = detail["laps"]
    zones = detail["zones"]
    analysis = None
    if detail["streams"]:
        try:
            window = activity_analysis.analyze(reader, rt.parquet, activity_id)["window"]
            analysis = {
                k: window.get(k)
                for k in ("pace_s_per_km", "avg_hr", "avg_power", "hr_drift_bpm", "decoupling_pct")
                if window.get(k) is not None
            }
        except ValueError:
            pass  # A short or incomplete stream has no meaningful two-half comparison.
    recent, recent_s, usual_s, ramp = _volume_parts(reader, day)
    # One load model everywhere: intervals.icu's.
    load = training.training_load(
        reader,
        Window(day - timedelta(days=34), day),
        load="vibesynergy",
        athlete=rt.settings.athlete,
        raw=getattr(rt, "raw", None),
        parquet=getattr(rt, "parquet", None),
    )
    load_rows = load["rows"]
    load_available = bool((load.get("hr_profile") or {}).get("max_hr"))
    last_7d_load = round(sum(row[1] for row in load_rows[-7:]), 1) if load_available else None
    usual_7d_load = round(sum(row[1] for row in load_rows[:-7]) / 4, 1) if load_available else None
    return {
        "session": {
            "lap_count": len(laps),
            "splits": [
                {k: lap.get(k) for k in ("elapsed_s", "distance_m", "avg_hr", "avg_power")}
                for lap in laps[:12]
            ],
            "zones_s": [{k: zone.get(k) for k in ("kind", "zone", "seconds")} for zone in zones],
            "stream_analysis": analysis,
        },
        "plan_on_day": [
            {k: item.get(k) for k in ("title", "sport", "status")} for item in planned_today
        ],
        "week": {
            "last_7d_moving_s": recent_s,
            "usual_7d_moving_s": round(usual_s),
            "volume_ramp_pct": ramp,
            "last_7d_sessions": recent.get("count", 0),
            "last_7d_load": last_7d_load,
            "usual_7d_load": usual_7d_load,
            "load_ramp_pct": (
                round(100 * (last_7d_load / usual_7d_load - 1), 1) if usual_7d_load else None
            ),
            "ctl_ramp_7d": load["current"]["ctl_ramp_7d"] if load_available else None,
        },
        "recovery": _recovery_context(reader, day, daily),
    }


def _foreign_subject(conn, subject_id: int):
    return conn.execute(
        union_all(
            *[
                select(table.c.subject_id).where(table.c.subject_id != subject_id)
                for table in (activity_unified, daily_unified, planned_item, note, checkin, injury)
            ]
        ).limit(1)
    ).first()


def _query_daily(conn, subject_id: int, span: Window) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            select(daily_unified)
            .where(
                daily_unified.c.subject_id == subject_id,
                daily_unified.c.date.between(span.start, span.end),
                daily_unified.c.metric.in_(METRICS),
            )
            .order_by(daily_unified.c.date, daily_unified.c.metric)
        ).mappings()
    ]


def _query_recent(conn, subject_id: int, span: Window) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            select(*[activity_unified.c[k] for k in evidence.FIELDS])
            .where(
                activity_unified.c.subject_id == subject_id,
                activity_unified.c.local_date.between(span.start, span.end),
            )
            .order_by(activity_unified.c.start_utc, activity_unified.c.sport)
        ).mappings()
    ]


def _in_span(rows: list[dict], span: Window, key: str) -> list[dict]:
    return [row for row in rows if span.start <= row[key] <= span.end]


def _activity_raw_ids(conn, primary_source: str, source_refs: list[str]) -> set:
    source_ids = [
        ref.split(":", 1)[1] for ref in source_refs if ref.split(":", 1)[0] == primary_source
    ]
    if not source_ids:
        return set()
    return set(
        conn.execute(
            select(activity.c.raw_item_id).where(
                activity.c.source == primary_source,
                activity.c.source_id.in_(source_ids),
            )
        ).scalars()
    )


def _daily_raw_ids(conn, subject_id: int, span: Window) -> set:
    return set(
        conn.execute(
            select(daily_metric.c.raw_item_id)
            .join(
                daily_unified,
                and_(
                    *[
                        daily_metric.c[key] == daily_unified.c[key]
                        for key in ("subject_id", "date", "metric", "source")
                    ]
                ),
            )
            .where(
                daily_unified.c.subject_id == subject_id,
                daily_unified.c.date.between(span.start, span.end),
                daily_unified.c.metric.in_(METRICS),
            )
        ).scalars()
    )


def _planned_raw_ids(conn, subject_id: int, items: list[dict]) -> set:
    pairs = [(item["source"], item["source_id"]) for item in items]
    if not pairs:
        return set()
    return set(
        conn.execute(
            select(planned_item.c.raw_item_id).where(
                planned_item.c.subject_id == subject_id,
                tuple_(planned_item.c.source, planned_item.c.source_id).in_(pairs),
            )
        ).scalars()
    )


def _datasets_for(conn, raw_ids: set) -> set:
    ids = [item for item in raw_ids if item is not None]
    if not ids:
        return set()
    return set(
        conn.execute(
            select(raw_item.c.source, raw_item.c.dataset).where(raw_item.c.id.in_(ids))
        ).all()
    )


def _project_source_status(dataset_rows: list[dict]) -> list[dict]:
    return [
        {
            "source": d["source"],
            "dataset": d["dataset"],
            "fetch_status": d["fetch_status"],
            "processing_state": d["processing"]["state"],
            "processing_pending": bool(d["processing"]["pending"]),
            "last_success_at": d["last_success_at"],
            "last_received_at": d["processing"]["last_received_at"],
        }
        for d in dataset_rows
    ]


def _comparison_inputs(
    reader,
    rt,
    activity_id: str,
    day: date,
    *,
    candidates=None,
    group=None,
    target=None,
    windowed: bool = False,
):
    # Retrospective: do not compare to activities completed afterwards.
    comparison = activity_analysis.similar(
        reader,
        rt.parquet,
        activity_id,
        window=None if windowed else Window(day - timedelta(days=365), day - timedelta(days=1)),
        limit=5,
        candidates=candidates,
        group=group,
        target=target,
    )
    comparison.pop("activity", None)
    for item in comparison["matches"]:
        item.pop("name", None)
    return comparison


def _coverage(span: Window, daily: list) -> dict:
    return {
        "from": span.start,
        "through": span.end,
        "observed_days": {
            metric: len({r["date"] for r in daily if r["metric"] == metric}) for metric in METRICS
        },
        "note": ("Observed days, not proof of complete source coverage; missing is not zero."),
    }


def _stable_inputs(profile: dict, parts: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "prompt_version": PROMPT_VERSION,
        "daily": parts["daily"],
        "recent_activities": parts["recent"],
        "coverage": _coverage(parts["span"], parts["daily"]),
        "plan": parts["calendar"],
        "journal": parts["journal"],
        "coach": profile,
    }


def _calendar_inputs(reader, rt, day: date, profile: dict) -> tuple[dict, list]:
    calendar = plan.plan(
        reader, Window(day, day + timedelta(days=7)), day, include_sync_status=False
    )
    planned_today = calendar["today"]
    race_context = coach.coaching_race_context(reader, day, raw=rt.raw, profile=profile)
    built = {
        **{key: race_context[key] for key in ("next_race", "plan_phase", "supporting")},
        "items": sorted(
            (
                {
                    **{
                        key: item[key]
                        for key in (
                            "source",
                            "source_id",
                            "date",
                            "kind",
                            "sport",
                            "plan_id",
                            "is_race",
                            "primary",
                            "status",
                            "write_authority",
                            "completion_match",
                            "done_by",
                        )
                        if key in item
                    },
                    "timing": {
                        key: item["extra"][key]
                        for key in ("start", "end", "all_day", "timezone")
                        if key in (item.get("extra") or {})
                    },
                }
                for item in calendar["items"]
                if item["kind"] in plan.TRAINING_KINDS | {"commitment"}
            ),
            key=lambda item: (item["date"], item["kind"], item["source"], item["source_id"]),
        ),
    }
    return built, planned_today


def _activity_pool(conn) -> list[dict]:
    """One ``activity_unified`` scan, in local-date index order, for in-memory similarity."""
    rowid = literal_column("rowid").label("_rowid")
    return [
        dict(row)
        for row in conn.execute(
            select(rowid, activity_unified).order_by(activity_unified.c.local_date, rowid)
        ).mappings()
    ]


class _SharedDigest:
    """Reads shared by every activity digest inside one ``stored_summaries_for_refs`` call."""

    def __init__(self, rt, conn, days: list[date], subject_id: int, refs: list[str]):
        self.rt = rt
        self.conn = conn
        self.reader = snapshot.SnapshotReader(conn)
        self.subject_id = subject_id
        self.profile = coach.snapshot(self.reader, raw=rt.raw)
        status = views.sync_status(self.reader)
        self.sync_by_key = {(d["source"], d["dataset"]): d for d in status["datasets"]}
        self.activities = _activity_pool(conn)
        self._activity_dates = [row["local_date"] for row in self.activities]
        self._by_group = {row["group_id"]: row for row in self.activities}
        self._family_of = {}
        self._rows_by_family: dict = {}
        for row in self.activities:
            fam = plan.family(row.get("sport"))
            self._family_of[row["group_id"]] = fam
            self._rows_by_family.setdefault(fam, []).append(row)
        self._family_dates = {
            fam: [row["local_date"] for row in rows] for fam, rows in self._rows_by_family.items()
        }
        self._group_by_ref = {
            f"{source}:{source_id}": group_id
            for source, source_id, group_id in conn.execute(
                select(activity_link.c.source, activity_link.c.source_id, activity_link.c.group_id)
            )
        }
        idents = set()
        for ref in refs:
            group = self._group_by_ref.get(ref)
            row = self._by_group.get(group) if group else None
            if row is None:
                continue
            idents.update(tuple(key.split(":", 1)) for key in row["sources"])
        self.laps_by, self.zones_by = views.lap_zone_index(conn, idents)
        self.foreign_subject = _foreign_subject(conn, subject_id)
        self._bundle: dict = {}
        self._raw_cache: dict = {}
        self._dataset_cache: dict = {}
        if days:
            wide = Window(min(days) - timedelta(days=CONTEXT_DAYS - 1), max(days))
            self._daily_rows = _query_daily(conn, subject_id, wide)
            self._recent_rows = _query_recent(conn, subject_id, wide)
            self._covered = wide
        else:
            self._daily_rows = []
            self._recent_rows = []
            self._covered = None
        self._warm_streams(refs)

    def _warm_streams(self, refs: list[str]) -> None:
        paths = []
        seen: set[str] = set()
        for ref in refs:
            group = self._group_by_ref.get(ref)
            row = self._by_group.get(group) if group else None
            key = row.get("stream_key") if row else None
            if not key or key in seen:
                continue
            seen.add(key)
            path = self.rt.parquet.stream_path(key)
            if path.is_file():
                paths.append(path)
        evidence.warm_stream_digests(paths)

    def _covers(self, span: Window) -> bool:
        covered = self._covered
        return bool(covered and span.start >= covered.start and span.end <= covered.end)

    def group_for(self, ref: str) -> str:
        group = self._group_by_ref.get(ref)
        if group is None or group not in self._by_group:
            raise ValueError(f"no activity {ref!r} (see vibesynergy_activities)")
        return group

    def similarity_rows(self, day: date, group: str) -> list[dict]:
        """Same-sport rows in the year before ``day``, in local-date index order.

        ``similar`` applies this same family rule. Pre-slicing keeps that order and
        drops other sports before the per-activity score.
        """
        start = day - timedelta(days=365)
        end = day - timedelta(days=1)
        fam = self._family_of.get(group)
        if fam is None:
            sport = self._by_group[group].get("sport")
            lo = bisect_left(self._activity_dates, start)
            hi = bisect_right(self._activity_dates, end)
            return [
                row
                for row in self.activities[lo:hi]
                if row["group_id"] != group and row.get("sport") == sport
            ]
        dates = self._family_dates.get(fam, [])
        rows = self._rows_by_family.get(fam, [])
        lo = bisect_left(dates, start)
        hi = bisect_right(dates, end)
        return [row for row in rows[lo:hi] if row["group_id"] != group and row.get("sport")]

    def stable_sections(self, parts: dict) -> tuple[dict, dict]:
        """JSON-ready day-stable inputs, and those sections already cleaned for the digest."""
        cached = parts.get("_stable")
        if cached is None:
            stable_json = snapshot.json_safe(_stable_inputs(self.profile, parts))
            cached = (stable_json, _cleaned_stable(stable_json))
            parts["_stable"] = cached
        return cached

    def bundle(self, subject_id: int, day: date) -> dict:
        key = (subject_id, day)
        cached = self._bundle.get(key)
        if cached is not None:
            return cached
        span = Window(day - timedelta(days=CONTEXT_DAYS - 1), day)
        if subject_id == self.subject_id and self._covers(span):
            daily = _in_span(self._daily_rows, span, "date")
            recent = _in_span(self._recent_rows, span, "local_date")
        else:
            daily = _query_daily(self.conn, subject_id, span)
            recent = _query_recent(self.conn, subject_id, span)
        calendar, planned_today = _calendar_inputs(self.reader, self.rt, day, self.profile)
        cached = {
            "span": span,
            "daily": daily,
            "recent": recent,
            "journal": journal.context(self.reader, day),
            "calendar": calendar,
            "planned_today": planned_today,
        }
        self._bundle[key] = cached
        return cached

    def source_status(self, datasets: set) -> list[dict]:
        rows = [self.sync_by_key[key] for key in sorted(datasets) if key in self.sync_by_key]
        return _project_source_status(rows)

    def raw_ids(self, primary_source: str, source_refs: list[str], subject_id: int, span, items):
        activity_key = (primary_source, tuple(source_refs))
        if activity_key not in self._raw_cache:
            self._raw_cache[activity_key] = _activity_raw_ids(
                self.conn, primary_source, source_refs
            )
        daily_key = ("daily", subject_id, span.start, span.end)
        if daily_key not in self._raw_cache:
            self._raw_cache[daily_key] = _daily_raw_ids(self.conn, subject_id, span)
        planned_key = (
            "planned",
            subject_id,
            tuple((item["source"], item["source_id"]) for item in items),
        )
        if planned_key not in self._raw_cache:
            self._raw_cache[planned_key] = _planned_raw_ids(self.conn, subject_id, items)
        return (
            set(self._raw_cache[activity_key])
            | set(self._raw_cache[daily_key])
            | set(self._raw_cache[planned_key])
        )

    def datasets_for(self, raw_ids: set) -> set:
        ids = tuple(sorted(item for item in raw_ids if item is not None))
        if ids not in self._dataset_cache:
            self._dataset_cache[ids] = _datasets_for(self.conn, set(ids))
        return self._dataset_cache[ids]


def _digest_capture(rt, conn, activity_id: str, *, shared: _SharedDigest | None = None) -> dict:
    """Inputs that decide ``input_version``. No coach context and no second facts read.

    ``_semantic`` drops ``coach_context``. The single-activity path adds that context
    afterwards; the bulk path never builds it.
    """
    reader = shared.reader if shared is not None else snapshot.SnapshotReader(conn)
    if shared is not None:
        detail = views.digest_detail(
            shared._by_group[shared.group_for(activity_id)],
            shared.laps_by,
            shared.zones_by,
            rt.parquet,
        )
    else:
        detail = views.activity_detail(reader, rt.parquet, activity_id)
    a = detail["activity"]
    # Existing daily/diary/plan queries currently describe the local primary subject only.
    # Refuse another subject until all underlying contracts are account-scoped.
    if a["subject_id"] != 1:
        raise ValueError("assessment context is unavailable for this subject")
    foreign = (
        shared.foreign_subject if shared is not None else _foreign_subject(conn, a["subject_id"])
    )
    if foreign:
        raise ValueError("multi-subject assessment context is not supported yet")
    day = date.fromisoformat(a["local_date"])
    if shared is not None:
        parts = shared.bundle(a["subject_id"], day)
        span = parts["span"]
        daily = parts["daily"]
        recent = parts["recent"]
        personal = parts["journal"]
        calendar = parts["calendar"]
        planned_today = parts["planned_today"]
        profile = shared.profile
        candidates = shared.similarity_rows(day, a["group_id"])
        target = shared._by_group.get(a["group_id"])
    else:
        span = Window(day - timedelta(days=CONTEXT_DAYS - 1), day)
        daily = _query_daily(conn, a["subject_id"], span)
        recent = _query_recent(conn, a["subject_id"], span)
        profile = coach.snapshot(reader, raw=rt.raw)
        calendar, planned_today = _calendar_inputs(reader, rt, day, profile)
        personal = journal.context(reader, day)
        candidates = None
        target = None
    comparison = _comparison_inputs(
        reader,
        rt,
        activity_id,
        day,
        candidates=candidates,
        group=a["group_id"],
        target=target,
        windowed=candidates is not None,
    )
    if shared is not None:
        raw_ids = shared.raw_ids(
            a["primary_source"],
            detail["evidence"]["source_refs"],
            a["subject_id"],
            span,
            calendar["items"],
        )
        datasets = shared.datasets_for(raw_ids)
        source_status = shared.source_status(datasets)
    else:
        raw_ids = _activity_raw_ids(conn, a["primary_source"], detail["evidence"]["source_refs"])
        raw_ids.update(_daily_raw_ids(conn, a["subject_id"], span))
        raw_ids.update(_planned_raw_ids(conn, a["subject_id"], calendar["items"]))
        datasets = _datasets_for(conn, raw_ids)
        source_status = _project_source_status(
            views.sync_status(reader, datasets=datasets)["datasets"]
        )
    recorded = {k: a.get(k) for k in evidence.FIELDS}
    from vibesynergy.query.units import presentation

    varying = {
        "recorded": recorded,
        "sport_units": a.get("sport_units") or presentation(a.get("sport"), a),
        "evidence": detail["evidence"],
        "laps": detail["laps"],
        "zones": [{k: z.get(k) for k in evidence.ZONE_FIELDS} for z in detail["zones"]],
        "source_status": source_status,
        "comparison": comparison,
    }
    if shared is not None:
        # Stable sections were JSON-encoded and cleaned once for this day.
        stable_json, cleaned_stable = shared.stable_sections(parts)
        inputs = {**stable_json, **snapshot.json_safe(varying)}
        semantic = _semantic(inputs, cleaned_stable=cleaned_stable)
    else:
        inputs = snapshot.json_safe(
            {
                **_stable_inputs(
                    profile,
                    {
                        "span": span,
                        "daily": daily,
                        "recent": recent,
                        "calendar": calendar,
                        "journal": personal,
                    },
                ),
                **varying,
            }
        )
        semantic = _semantic(inputs)
    return {
        "input_version": evidence.digest(semantic),
        "inputs": inputs,
        "source_refs": detail["evidence"]["source_refs"],
        "subject_id": a["subject_id"],
        "group_id": a["group_id"],
        "_reader": reader,
        "_detail": detail,
        "_day": day,
        "_daily": daily,
        "_planned_today": planned_today,
    }


def _capture(rt, conn, activity_id: str) -> dict:
    built = _digest_capture(rt, conn, activity_id, shared=None)
    reader = built.pop("_reader")
    detail = built.pop("_detail")
    day = built.pop("_day")
    daily = built.pop("_daily")
    planned_today = built.pop("_planned_today")
    built["inputs"]["coach_context"] = snapshot.json_safe(
        _coach_context(reader, rt, activity_id, detail, day, daily, planned_today)
    )
    # Parquet isn't part of SQLite: detect changes during the snapshot as well.
    if (
        views.activity_detail(reader, rt.parquet, activity_id)["evidence"]["facts_version"]
        != detail["evidence"]["facts_version"]
    ):
        raise ChangedInputs("activity stream changed during capture")
    return built


def capture(rt, activity_id: str) -> dict:
    with rt.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN")
        return _capture(rt, conn, activity_id)


def read(rt, activity_id: str) -> dict:
    """The coach's assessment of an activity, from the coach feed.

    ``assessment`` is the newest ``activity_assessment`` entry the coach wrote about this
    activity with ``vibesynergy_coach_write``; ``decision_log`` holds all of them, newest
    first. ``state`` is ``ready`` or ``missing``. Internal (model-run) assessments are
    retired, so ``coaching_state.reason`` stays ``retired``. Reading never creates work.
    """
    from vibesynergy.query import predictions

    try:
        entries = predictions.activity_assessments(rt.engine, activity_id)
    except ValueError:
        entries = []
    return {
        "state": "ready" if entries else "missing",
        "source": "coach_feed",
        "assessment": entries[0] if entries else None,
        "needs_revision": False,
        "input_version": None,
        "activity": activity_summary(rt, activity_id),
        "decision_log": entries,
        "controls": {"profile_active": False},
        "coaching_state": unavailable_state("retired", activity_id),
    }


def activity_summary(rt, activity_id: str) -> dict | None:
    """Name, sport, local start, distance, duration and the user's own description."""
    try:
        group = views.resolve_activity(rt.engine, activity_id)
    except ValueError:
        return None
    with rt.engine.connect() as conn:
        row = (
            conn.execute(select(activity_unified).where(activity_unified.c.group_id == group))
            .mappings()
            .first()
        )
        if row is None:
            return None
        description = None
        for key in row["sources"]:
            source, source_id = key.split(":", 1)
            extra = conn.scalar(
                select(activity.c.extra).where(
                    activity.c.source == source, activity.c.source_id == source_id
                )
            )
            description = journal.personal_text((extra or {}).get("description"))
            if description:
                break
    start = row["start_utc"]
    if row["tz_offset_s"] is not None:
        local = start + timedelta(seconds=row["tz_offset_s"])
    else:
        local = start.astimezone(ZoneInfo(rt.settings.timezone))
    return {
        "id": group,
        "name": row["name"],
        "sport": row["sport"],
        "local_date": local.date().isoformat(),
        "local_time": format_user_local_time_hhmm(local),
        "distance_m": row["distance_m"],
        "duration_s": row["moving_s"] or row["elapsed_s"],
        "avg_speed": row["avg_speed"],
        "description": description,
    }


def stored_summaries_for_refs(rt, conn, subject_id: int, refs: list[str]) -> dict[str, dict]:
    """No internal assessment is stored. Every ref is missing."""
    del rt, conn, subject_id
    empty = {"state": "missing", "input_version": None, "advice": None}
    return {ref: dict(empty) for ref in refs if ref}


def stored_for_activity(rt, activity_id: str) -> dict:
    """No internal assessment is stored."""
    del rt, activity_id
    return {
        "state": "missing",
        "input_version": None,
        "advice": None,
        "needs_revision": False,
        "assessment": None,
    }


def own_training():
    """the athlete's own sessions. Coaching sessions given to others are never auto-assessed or
    shown as the latest assessed workout (they stay readable by explicit id)."""
    return or_(activity_unified.c.kind.is_(None), activity_unified.c.kind != "coaching")


def current_state(rt, activity_id: str | None = None) -> dict:
    """Profile and race context. Internal assessments are not read."""
    del activity_id
    profile = coach.snapshot(rt.engine, raw=rt.raw)
    active = coach.profile_binding(profile)
    return {
        **plan.race_phase(rt.engine, today_in(rt.settings.timezone)),
        "contract_version": 1,
        "activity_id": None,
        "profile": _profile_versions(profile["documents"]),
        "profile_version": active["profile_version"],
        "active_profile": active,
        "bound_versions": {
            **active,
            "prompt_version": None,
            "evidence_version": None,
        },
        "needs_revision": False,
        "evidence_version": None,
        "input_version": None,
        "assessment_id": None,
        "assessment_version": None,
        "current_assessment_id": None,
        "freshness": "missing",
        "error": None,
        "assessed_at": None,
        "last_decision": None,
        "reason": "retired",
    }


def unavailable_state(reason: str, activity_id: str | None = None) -> dict:
    """A cheap, non-authoritative result for an optional dashboard coaching panel."""
    return {
        "contract_version": 1,
        "activity_id": activity_id,
        "profile": {},
        "profile_version": None,
        "active_profile": coach.profile_binding({}),
        "bound_versions": bound_versions(None),
        "needs_revision": False,
        "evidence_version": None,
        "input_version": None,
        "assessment_id": None,
        "assessment_version": None,
        "current_assessment_id": None,
        "freshness": "unavailable",
        "error": None,
        "assessed_at": None,
        "last_decision": None,
        "reason": reason,
    }


def _profile_versions(documents: dict) -> dict:
    return {
        kind: {"version": document["version"], "fingerprint": document["fingerprint"]}
        for kind, document in documents.items()
    }


def bound_versions(inputs: dict | None) -> dict:
    """Profile, policy, prompt and evidence versions captured with one assessment.

    Derived on read from the stored snapshot so a wording change here does not rewrite
    history or move the input version.
    """
    inputs = inputs or {}
    coach_snap = inputs.get("coach") if isinstance(inputs.get("coach"), dict) else {}
    evidence_block = inputs.get("evidence") if isinstance(inputs.get("evidence"), dict) else {}
    return {
        **coach.profile_binding(coach_snap),
        "prompt_version": inputs.get("prompt_version"),
        "evidence_version": evidence_block.get("facts_version"),
    }
