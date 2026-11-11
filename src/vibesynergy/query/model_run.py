"""Stored personal-model fits and the parameter diff against the previous run.

The diff is computed at write time against the latest run of the same ``model_id``
whose ``started_at`` is at or before this run. A later backfill does not rewrite
diffs already stored on runs that were written first.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, desc, select

from vibesynergy.core.ids import new_id
from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import model_run

_MAX_MODEL_ID = 200
_MAX_REPORT = 200_000
_MAX_PARAMS = 200_000


def write_run(
    engine: Engine,
    *,
    model_id: str,
    params: Any,
    report: str,
    started_at: datetime | str | None = None,
    finished_at: datetime | str | None = None,
    created_by: str | None = None,
    timezone: str = "UTC",
) -> dict[str, Any]:
    """Insert one run and store the parameter diff against the previous run."""
    model = _model_id(model_id)
    body = _report(report)
    fitted = _params(params)
    started, finished = _span(started_at, finished_at, timezone)
    author = _created_by(created_by)
    with engine.begin() as conn:
        previous = (
            conn.execute(
                select(model_run)
                .where(
                    model_run.c.model_id == model,
                    model_run.c.started_at <= started,
                )
                .order_by(desc(model_run.c.started_at), desc(model_run.c.id))
                .limit(1)
            )
            .mappings()
            .first()
        )
        diff = None
        if previous is not None:
            diff = _params_diff(previous["id"], _stored_params(previous["params"]), fitted)
        run_id = new_id("mrun")
        conn.execute(
            model_run.insert().values(
                id=run_id,
                model_id=model,
                started_at=started,
                finished_at=finished,
                params=fitted,
                report=body,
                diff=diff,
                created_by=author,
            )
        )
        row = conn.execute(select(model_run).where(model_run.c.id == run_id)).mappings().one()
    return _public(row)


def list_runs(
    engine: Engine,
    *,
    model_id: str,
    start: date,
    end: date,
    timezone: str,
) -> list[dict[str, Any]]:
    """Runs of one model whose start falls on a local date in ``start``..``end``."""
    model = _model_id(model_id)
    if end < start:
        raise ValueError(f"window end {end} is before start {start}")
    first, last = _utc_bounds(start, end, timezone)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(model_run)
                .where(
                    model_run.c.model_id == model,
                    model_run.c.started_at >= first,
                    model_run.c.started_at < last,
                )
                .order_by(desc(model_run.c.started_at), desc(model_run.c.id))
            )
            .mappings()
            .all()
        )
    return [_public(row) for row in rows]


def _params_diff(previous_id: str, old: dict, new: dict) -> dict[str, Any]:
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    changed: dict[str, Any] = {}
    _walk("", old, new, added, removed, changed)
    return {
        "previous_id": previous_id,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _walk(
    path: str,
    old: Any,
    new: Any,
    added: dict[str, Any],
    removed: dict[str, Any],
    changed: dict[str, Any],
) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in old:
                added[child] = new[key]
            elif key not in new:
                removed[child] = old[key]
            else:
                _walk(child, old[key], new[key], added, removed, changed)
        return
    if old != new:
        changed[path or "$"] = {"from": old, "to": new}


def _model_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("model_id is required")
    model = value.strip()
    if len(model) > _MAX_MODEL_ID:
        raise ValueError("model_id is too long")
    return model


def _report(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("report must be text")
    if len(value) > _MAX_REPORT:
        raise ValueError("report is too large")
    return value


def _params(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("params must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("params must be a JSON object")
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("params must be JSON") from exc
    if len(encoded) > _MAX_PARAMS:
        raise ValueError("params are too large")
    return json.loads(encoded)


def _stored_params(value: Any) -> dict:
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
        raise ValueError("stored params must be a JSON object")
    if isinstance(value, dict):
        return value
    raise ValueError("stored params must be a JSON object")


def _span(
    started_at: datetime | str | None,
    finished_at: datetime | str | None,
    timezone: str,
) -> tuple[datetime, datetime]:
    now = utc_now()
    if started_at is None and finished_at is None:
        return now, now
    if started_at is None:
        finished = _parse_dt(finished_at, timezone)
        return finished, finished
    started = _parse_dt(started_at, timezone)
    finished = started if finished_at is None else _parse_dt(finished_at, timezone)
    if finished < started:
        raise ValueError("finished_at is before started_at")
    return started, finished


def _parse_dt(value: datetime | str | None, timezone: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = "" if value is None else str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"timestamps look like 2026-10-26T08:30:00+02:00, got {value!r}"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.astimezone(UTC)


def _utc_bounds(start: date, end: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    first = datetime.combine(start, datetime.min.time(), tzinfo=zone).astimezone(UTC)
    last = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(
        UTC
    )
    return first, last


def _created_by(explicit: str | None) -> str:
    if explicit is not None:
        author = explicit.strip()
        if not author:
            raise ValueError("created_by is required")
        return author[:200]
    try:
        from vibesynergy.mcp.identity import current_author

        return current_author()
    except ImportError:
        return "user"


def _public(row) -> dict[str, Any]:
    data = dict(row)
    for key in ("started_at", "finished_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    data["params"] = _stored_params(data.get("params"))
    if isinstance(data.get("diff"), str):
        data["diff"] = json.loads(data["diff"])
    return data
