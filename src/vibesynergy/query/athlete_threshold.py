"""Athlete training thresholds: propose, confirm, and read current values for workout generation."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError

from vibesynergy.core.timeutil import utc_now
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
