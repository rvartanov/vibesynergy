"""Coach forecasts stored on a decision-log entry and scored from daily values.

``error`` is actual minus predicted. Bias is the mean error over an ISO week.
RMSE is the root mean square of that same error. A row is scored once, when
``daily_unified`` has a value for the target day, and later runs leave it alone.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select, update
from sqlalchemy.engine import Connection, Engine

from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.query import catalog
from vibesynergy.query.coach_workouts import COACH_ENTRY_TYPES, coach_row
from vibesynergy.store.schema import (
    activity_unified,
    coach_decision_log,
    coach_prediction,
    daily_unified,
)

_RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_FIELDS = frozenset({"metric", "horizon_days", "predicted", "ci_low", "ci_high", "model_id"})
_MAX_HORIZON_DAYS = 366


class CoachPredictionWrite(BaseModel):
    """One forecast supplied with ``vibesynergy_coach_write``."""

    model_config = ConfigDict(extra="forbid")
    metric: str
    horizon_days: int = Field(ge=0, le=_MAX_HORIZON_DAYS)
    predicted: float
    ci_low: float | None = None
    ci_high: float | None = None
    model_id: str | None = None


def ensure_metric(metric: str) -> str:
    """Reject anything that is not a metric id stored in ``daily_unified``."""
    if metric in catalog.DAILY_UNIFIED_METRICS:
        return metric
    if metric in catalog.COMPUTED:
        raise ValueError(
            f"metric {metric!r} is not stored as a daily value, so it cannot be scored"
        )
    raise ValueError(f"unknown metric {metric!r}")


def clean_rule_ids(ids: list) -> list[str]:
    """Rule ids stored on the entry. The knowledge base itself is a later piece."""
    if not isinstance(ids, list):
        raise ValueError("kb_rules must be a list of ids")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        text = raw.strip() if isinstance(raw, str) else ""
        if not text or not _RULE_ID.fullmatch(text):
            raise ValueError(f"invalid knowledge-rule id {raw!r}")
        if text in seen:
            raise ValueError(f"duplicate knowledge-rule id {text!r}")
        seen.add(text)
        cleaned.append(text)
    return cleaned


def normalize_predictions(
    predictions: list | None,
    *,
    for_date: date | None,
    model_id: str | None,
) -> list[dict[str, Any]]:
    """Target day, bounds and metric checks for one write. Empty stays empty."""
    if not predictions:
        return []
    if for_date is None:
        raise ValueError("for_date is required when predictions are set")
    prepared: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in predictions:
        item = _item_dict(raw)
        metric = ensure_metric(str(item.get("metric") or ""))
        horizon = _horizon(item.get("horizon_days"))
        key = (metric, horizon)
        if key in seen:
            raise ValueError(f"duplicate prediction for {metric} at horizon_days {horizon}")
        seen.add(key)
        predicted = _number(item.get("predicted"), "predicted")
        ci_low, ci_high = _interval(item.get("ci_low"), item.get("ci_high"), predicted)
        own_model = _model_id(item.get("model_id"))
        prepared.append(
            {
                "for_date": for_date + timedelta(days=horizon),
                "horizon_days": horizon,
                "metric": metric,
                "predicted": predicted,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "model_id": own_model or _model_id(model_id),
            }
        )
    return prepared


def insert_for_entry(conn: Connection, entry_id: int, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(coach_prediction.insert().values(entry_id=entry_id, **row))


def load_for_entry(conn: Connection, entry_id: int) -> list[dict[str, Any]]:
    found = conn.execute(
        select(coach_prediction)
        .where(coach_prediction.c.entry_id == entry_id)
        .order_by(coach_prediction.c.id)
    ).mappings()
    return [_public_prediction(row) for row in found]


def score_due(engine: Engine, *, now: datetime | None = None) -> int:
    """Fill actual, error and scored_at where the target day is already in daily_unified.

    Rows that already have scored_at are left as they were, including when the daily
    value changes later. Returns how many rows this call scored.
    """
    when = now or utc_now()
    if when.tzinfo is None:
        raise ValueError("scored_at requires a timezone-aware timestamp")
    scored = 0
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                coach_prediction.c.id,
                coach_prediction.c.predicted,
                daily_unified.c.value,
            )
            .select_from(coach_prediction)
            .join(coach_decision_log, coach_decision_log.c.id == coach_prediction.c.entry_id)
            .join(
                daily_unified,
                and_(
                    daily_unified.c.subject_id == coach_decision_log.c.subject_id,
                    daily_unified.c.date == coach_prediction.c.for_date,
                    daily_unified.c.metric == coach_prediction.c.metric,
                ),
            )
            .where(coach_prediction.c.scored_at.is_(None))
        ).all()
        for row in rows:
            actual = float(row.value)
            error = actual - float(row.predicted)
            result = conn.execute(
                update(coach_prediction)
                .where(
                    coach_prediction.c.id == row.id,
                    coach_prediction.c.scored_at.is_(None),
                )
                .values(actual=actual, error=error, scored_at=when)
            )
            scored += result.rowcount or 0
    return scored


def coach_entries(
    engine: Engine,
    span: Window,
    entry_type: str | None = None,
    *,
    subject_id: int = 1,
) -> list[dict[str, Any]]:
    """Typed coach entries in the period, newest first, with predictions attached."""
    if entry_type is not None and entry_type not in COACH_ENTRY_TYPES:
        names = ", ".join(sorted(COACH_ENTRY_TYPES))
        raise ValueError(f"entry_type must be one of {names}")
    stmt = (
        select(coach_decision_log)
        .where(
            coach_decision_log.c.subject_id == subject_id,
            coach_decision_log.c.entry_type.in_(tuple(sorted(COACH_ENTRY_TYPES))),
            coach_decision_log.c.for_date.between(span.start, span.end),
        )
        .order_by(coach_decision_log.c.for_date.desc(), coach_decision_log.c.id.desc())
    )
    if entry_type is not None:
        stmt = stmt.where(coach_decision_log.c.entry_type == entry_type)
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
        return [_published_entry(conn, row) for row in rows]


def activity_assessments(
    engine: Engine, activity_id: str, *, subject_id: int = 1
) -> list[dict[str, Any]]:
    """The coach's ``activity_assessment`` entries about one activity, newest first.

    An entry is about the activity when its ``activity_id`` or one of its activity
    ``evidence_refs`` resolves to the same merged activity. Entries written more than a
    day before the activity day cannot be about it. Raises ValueError for an unknown id.
    """
    from vibesynergy.query import views

    group = views.resolve_activity(engine, activity_id)

    def same(ref: Any) -> bool:
        if not isinstance(ref, str) or ":" not in ref:
            return False
        if ref == group:
            return True
        try:
            return views.resolve_activity(engine, ref) == group
        except ValueError:
            return False

    with engine.connect() as conn:
        day = conn.scalar(
            select(activity_unified.c.local_date).where(activity_unified.c.group_id == group)
        )
        if day is None:
            return []
        rows = (
            conn.execute(
                select(coach_decision_log)
                .where(
                    coach_decision_log.c.subject_id == subject_id,
                    coach_decision_log.c.entry_type == "activity_assessment",
                    coach_decision_log.c.for_date >= day - timedelta(days=1),
                )
                .order_by(coach_decision_log.c.at.desc(), coach_decision_log.c.id.desc())
            )
            .mappings()
            .all()
        )
        out = []
        for row in rows:
            meta = row["meta"] if isinstance(row["meta"], dict) else {}
            refs = [meta.get("activity_id"), *(meta.get("evidence_refs") or [])]
            if any(same(ref) for ref in refs):
                out.append(_published_entry(conn, row))
        return out


def prediction_scores(
    engine: Engine,
    span: Window,
    metric: str | None = None,
    *,
    subject_id: int = 1,
) -> dict[str, Any]:
    """Bias and RMSE per ISO week of the target date, for scored predictions only."""
    if metric is not None:
        ensure_metric(metric)
    stmt = (
        select(
            coach_prediction.c.for_date,
            coach_prediction.c.metric,
            coach_prediction.c.error,
        )
        .join(coach_decision_log, coach_decision_log.c.id == coach_prediction.c.entry_id)
        .where(
            coach_decision_log.c.subject_id == subject_id,
            coach_prediction.c.for_date.between(span.start, span.end),
            coach_prediction.c.scored_at.is_not(None),
            coach_prediction.c.error.is_not(None),
        )
        .order_by(coach_prediction.c.for_date, coach_prediction.c.metric, coach_prediction.c.id)
    )
    if metric is not None:
        stmt = stmt.where(coach_prediction.c.metric == metric)
    buckets: dict[tuple[str, str], list[float]] = {}
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            iso = row.for_date.isocalendar()
            week = f"{iso.year}-W{iso.week:02d}"
            buckets.setdefault((week, row.metric), []).append(float(row.error))
    weeks = []
    for (week, metric_id), errors in buckets.items():
        count = len(errors)
        bias = sum(errors) / count
        rmse = math.sqrt(sum(error * error for error in errors) / count)
        weeks.append(
            {
                "week": week,
                "metric": metric_id,
                "n": count,
                "bias": round(bias, 6),
                "rmse": round(rmse, 6),
            }
        )
    weeks.sort(key=lambda row: (row["week"], row["metric"]))
    return {"metric": metric, "weeks": weeks}


def _published_entry(conn: Connection, row) -> dict[str, Any]:
    data = coach_row(row)
    meta = data.get("meta") or {}
    data["kb_rules"] = list(meta.get("kb_rules") or [])
    data["predictions"] = load_for_entry(conn, data["id"])
    return data


def _public_prediction(row) -> dict[str, Any]:
    data = dict(row)
    if data.get("for_date") is not None:
        data["for_date"] = data["for_date"].isoformat()
    scored_at = data.get("scored_at")
    if scored_at is not None:
        data["scored_at"] = scored_at.isoformat()
    return data


def _item_dict(raw: Any) -> dict:
    if hasattr(raw, "model_dump"):
        return raw.model_dump()
    if not isinstance(raw, dict):
        raise ValueError("each prediction must be an object")
    extra = set(raw) - _FIELDS
    if extra:
        names = ", ".join(sorted(extra))
        raise ValueError(f"unknown prediction fields: {names}")
    return raw


def _horizon(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"horizon_days must be a whole number from 0 to {_MAX_HORIZON_DAYS}")
    if value < 0 or value > _MAX_HORIZON_DAYS:
        raise ValueError(f"horizon_days must be a whole number from 0 to {_MAX_HORIZON_DAYS}")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _interval(low: Any, high: Any, predicted: float) -> tuple[float | None, float | None]:
    if low is None and high is None:
        return None, None
    if low is None or high is None:
        raise ValueError("ci_low and ci_high are set together")
    ci_low = _number(low, "ci_low")
    ci_high = _number(high, "ci_high")
    if ci_low > ci_high:
        raise ValueError("ci_low must be less than or equal to ci_high")
    if not ci_low <= predicted <= ci_high:
        raise ValueError("predicted must lie between ci_low and ci_high")
    return ci_low, ci_high


def _model_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("model_id must be a string")
    text = value.strip()
    return text or None
