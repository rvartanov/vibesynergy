"""Athlete training thresholds: propose, confirm, and read current values for workout generation."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Engine, desc, select, update
from sqlalchemy.exc import IntegrityError

from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import session_purpose
from vibesynergy.store.schema import athlete_threshold

METRICS = frozenset(
    {
        "max_hr",
        "lthr",
        "threshold_pace",
        "threshold_power",
        "ftp",
        "css",
        "vo2max",
        "resting_hr_baseline",
        "hrv_baseline",
    }
)
# test: a dedicated test; race: from a race; field: from a training session; manual: the
# athlete typed it; device_auto: a device estimate; estimate: the coach's estimate.
METHODS = frozenset({"test", "race", "field", "manual", "device_auto", "estimate"})
STATUSES = frozenset({"current", "stale", "proposed", "superseded", "dismissed"})
HR_ZONE_METRICS = frozenset({"max_hr", "lthr"})


class ThresholdError(ValueError):
    """Invalid threshold state or duplicate current row."""


def _validate_metric(metric: str) -> str:
    key = metric.strip().lower()
    if key not in METRICS:
        raise ValueError(f"metric must be one of {', '.join(sorted(METRICS))}")
    return key


def _validate_method(method: str) -> str:
    key = method.strip().lower()
    if key not in METHODS:
        raise ValueError(f"method must be one of {', '.join(sorted(METHODS))}")
    return key


def _row_dict(row) -> dict[str, Any]:
    data = dict(row)
    for key in ("date_set", "next_review"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    for key in ("created_at", "updated_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


def _append_history(existing: list | None, event: dict[str, Any]) -> list:
    history = list(existing or [])
    history.append(event)
    return history


def propose_threshold(
    engine: Engine,
    *,
    sport: str,
    metric: str,
    value: float,
    unit: str,
    method: str = "estimate",
    source_device: str | None = None,
    confidence: float | None = None,
    date_set: date | None = None,
    next_review: date | None = None,
    history: list | None = None,
) -> dict[str, Any]:
    """Insert a proposed threshold row (not visible to workout generation)."""
    metric = _validate_metric(metric)
    method = _validate_method(method)
    sport_key = sport.strip().lower()
    now = utc_now()
    payload_history = _append_history(
        history,
        {
            "at": now.isoformat(),
            "event": "proposed",
            "value": value,
            "unit": unit,
            "method": method,
        },
    )
    with engine.begin() as conn:
        row_id = conn.execute(
            athlete_threshold.insert().values(
                sport=sport_key,
                metric=metric,
                value=value,
                unit=unit,
                method=method,
                source_device=source_device,
                confidence=confidence,
                date_set=date_set,
                next_review=next_review,
                status="proposed",
                history=payload_history,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        row = (
            conn.execute(select(athlete_threshold).where(athlete_threshold.c.id == row_id))
            .mappings()
            .one()
        )
    return _row_dict(row)


def confirm_threshold(
    engine: Engine,
    row_id: int,
    *,
    confirmed_at: date | None = None,
) -> dict[str, Any]:
    """Promote a proposed row to current; supersede the previous current row."""
    now = utc_now()
    with engine.begin() as conn:
        proposed = (
            conn.execute(select(athlete_threshold).where(athlete_threshold.c.id == row_id))
            .mappings()
            .first()
        )
        if not proposed:
            raise LookupError("threshold row not found")
        if proposed["status"] == "current":
            return _row_dict(proposed)
        if proposed["status"] != "proposed":
            raise ThresholdError(f"threshold row {row_id} is not proposed")
        current = (
            conn.execute(
                select(athlete_threshold).where(
                    athlete_threshold.c.sport == proposed["sport"],
                    athlete_threshold.c.metric == proposed["metric"],
                    athlete_threshold.c.status == "current",
                )
            )
            .mappings()
            .first()
        )
        if current:
            conn.execute(
                update(athlete_threshold)
                .where(athlete_threshold.c.id == current["id"])
                .values(
                    status="superseded",
                    superseded_by=row_id,
                    updated_at=now,
                    history=_append_history(
                        current["history"],
                        {
                            "at": now.isoformat(),
                            "event": "superseded",
                            "by": row_id,
                        },
                    ),
                )
            )
        history = _append_history(
            proposed["history"],
            {
                "at": now.isoformat(),
                "event": "confirmed",
                "confirmed_on": (confirmed_at or now.date()).isoformat(),
            },
        )
        try:
            conn.execute(
                update(athlete_threshold)
                .where(athlete_threshold.c.id == row_id)
                .values(status="current", history=history, updated_at=now)
            )
        except IntegrityError as exc:
            raise ThresholdError(
                "another current threshold exists for this sport and metric"
            ) from exc
        row = (
            conn.execute(select(athlete_threshold).where(athlete_threshold.c.id == row_id))
            .mappings()
            .one()
        )
    return _row_dict(row)


def list_thresholds(
    engine: Engine,
    *,
    sport: str | None = None,
    metric: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    query = select(athlete_threshold).order_by(desc(athlete_threshold.c.id))
    if sport:
        query = query.where(athlete_threshold.c.sport == sport.strip().lower())
    if metric:
        query = query.where(athlete_threshold.c.metric == _validate_metric(metric))
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(sorted(STATUSES))}")
        query = query.where(athlete_threshold.c.status == status)
    with engine.connect() as conn:
        return [_row_dict(row) for row in conn.execute(query).mappings()]


def current_thresholds(engine: Engine) -> dict[str, dict[str, Any]]:
    """Map sport -> metric -> row for status=current only (workout generation entry point)."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(athlete_threshold).where(athlete_threshold.c.status == "current")
        ).mappings()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            out.setdefault(row["sport"], {})[row["metric"]] = _row_dict(row)
    return out


def workout_generation_thresholds(engine: Engine) -> dict[str, dict[str, Any]]:
    """Alias used by coach workout code: never reads proposed or superseded rows."""
    return current_thresholds(engine)


def record_activity_field_estimate(
    engine: Engine,
    *,
    sport: str,
    metric: str,
    value: float,
    unit: str,
    purpose: str | None,
    method: str = "field",
    source_device: str | None = None,
) -> dict[str, Any] | None:
    """Propose a threshold from an activity only when session purpose allows it."""
    if not session_purpose.threshold_eligible(purpose):
        return None
    return propose_threshold(
        engine,
        sport=sport,
        metric=metric,
        value=value,
        unit=unit,
        method=method,
        source_device=source_device,
        history=[{"event": "activity_signal"}],
    )


def hr_metrics_from_row(row: dict[str, Any]) -> dict[str, float]:
    """Extract HR values from a confirmed threshold row for Garmin zone writes."""
    metric = row.get("metric")
    if metric not in HR_ZONE_METRICS:
        return {}
    return {metric: float(row["value"])}
