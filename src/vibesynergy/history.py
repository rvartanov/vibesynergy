"""Filling history ("the filler"): a source's past, fetched by VibeSynergy itself.

Two ways, one list of runs (table ``history_fill``, shown on Settings → Sources → Status):

- **API**: month by month, oldest first, through the job queue at backfill priority, so the
  regular syncs and webhooks go first. Each month is one job; progress is saved after it, so
  a restart continues instead of fetching everything again. A month where every dataset hit
  the source's rate limit waits until the limit resets; other failures are tried again every
  30 minutes, and after 6 in a row the run is stuck. Months where only some datasets
  succeeded are fetched once more at the end.
- **Archive**: an account export (Garmin's data export, Strava's archive) imported in a
  thread of its own, because it can take a while and the job worker should keep running
  the regular syncs. One archive at a time; after a restart it starts again, and files it
  already archived are skipped.

Strava has no API route: its API budget is for new activities only, so its past comes from
the archive. Runs of that kind left over from before are cancelled, never deleted.

A run that finishes or gets stuck sends an alert, when alerts are set up.
"""

from __future__ import annotations

import logging
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update

from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.core.user_datetime import (
    format_user_date,
    format_user_datetime,
    format_user_month_year,
)
from vibesynergy.sources.base import DatasetKind
from vibesynergy.store.schema import activity, daily_metric, history_fill, job

log = logging.getLogger(__name__)

JOB = "history"
PRIORITY = -1  # after the regular syncs (0) and re-syncs (10)
STUCK_AFTER = 6
RETRY_AFTER = timedelta(minutes=30)
ACTIVE = ("running", "waiting", "paused")
ARCHIVES = {
    "garmin_export": "Garmin account export",
    "strava_export": "Strava account archive",
}

# How to get each source's export (the archive route). Sources without one fill over the API.
EXPORTS = {
    "garmin": {
        "archive": "garmin_export",
        "url": "https://www.garmin.com/account/datamanagement/exportdata/",
        "steps": "On garmin.com: Account → Data Management → Export Your Data → Request Data "
        "Export. Garmin emails a download link, usually within a day (sometimes a few days).",
    },
    "strava": {
        "archive": "strava_export",
        "url": "https://support.strava.com/hc/en-us/articles/216918437-Exporting-your-Data-and-Bulk-Export",
        "steps": "On strava.com: Settings → My Account → Download or delete your account → "
        "Get started → Request your archive. Strava emails a download link, usually within "
        "a few hours.",
    },
}
# Sources whose API is never used for history: the reason shown when someone tries.
API_HISTORY_OFF = {
    "strava": "Strava's API is kept for new activities. Import the Strava archive for the past.",
}
RECENT_DAYS = 14  # a period this recent is a normal sync, not history

# Recommend the export when the API would take longer than this, or would run into a daily
# request limit (more than a day then, however few requests are left).
EXPORT_ABOVE_HOURS = 3


# Starting and controlling runs ------------------------------------------------------------


def history_datasets(rt: Any, source: str) -> list[str]:
    """What a fill fetches: the source's regular datasets, except current-state snapshots."""
    specs = rt.registry.get(source).sync_datasets()
    return [s.name for s in specs if s.kind not in (DatasetKind.SNAPSHOT, DatasetKind.FILE)]


def suggest(rt: Any, source: str, today: date) -> dict[str, Any] | None:
    """A sensible period to fill: from when the account started (Strava tells; else ten
    years back) to the day before what the regular syncs already have. None when there's
    nothing before that."""
    states = [rt.sync.state(source, name) for name in history_datasets(rt, source)]
    firsts = [s["synced_from"] for s in states if s and s["synced_from"]]
    end = min(firsts) - timedelta(days=1) if firsts else today
    start = _account_start(rt, source) or date(today.year - 10, 1, 1)
    if start > end:
        return None
    return {"source": source, "start": str(start), "end": str(end), "months": _months(start, end)}


def estimate(rt: Any, source: str, today: date) -> dict[str, Any]:
    """How long filling a source over its API would take, from what VibeSynergy already knows
    (no API calls), and whether its account export is the better route.

    Sources with daily datasets (Garmin): one call per dataset per day of history, plus the
    monthly ones, at the configured request interval; activities add a few seconds each.
    Strava has no API route (see ``API_HISTORY_OFF``): only its archive.
    """
    if source in API_HISTORY_OFF:
        return {
            "source": source,
            "period": None,
            "activities": None,
            "hours": None,
            "per_year_hours": None,
            "daily_limit": False,
            "export": EXPORTS.get(source),
            "api_off": True,
            "recommend": "export",
        }
    spec_by_name = {s.name: s for s in rt.registry.get(source).sync_datasets()}
    names = history_datasets(rt, source)
    daily = sum(spec_by_name[n].kind is DatasetKind.DAILY for n in names)
    monthly = sum(spec_by_name[n].kind is DatasetKind.RANGE for n in names)
    period = suggest(rt, source, today)
    out: dict[str, Any] = {
        "source": source,
        "period": period,
        "activities": None,
        "hours": None,
        "per_year_hours": None,
        "daily_limit": False,
        "export": EXPORTS.get(source),
        "api_off": False,
    }
    if daily or monthly:
        interval = float(rt.registry.get(source).ctx.config.get("request_interval", 1.0))
        per_year = (365 * daily + 12 * monthly) * interval / 3600
        out["per_year_hours"] = round(per_year, 1)
        if period:
            years = period["months"] / 12
            out["hours"] = round(per_year * years, 1)
    if out["hours"] is None:
        out["recommend"] = None  # nothing left to fill that we know of: no advice
    else:
        long = out["hours"] > EXPORT_ABOVE_HOURS or out["daily_limit"]
        out["recommend"] = "export" if out["export"] and long else "api"
    return out


def api_history_refused(source: str, start: date, today: date) -> str | None:
    """Why a period can't be fetched over the API (None: it can)."""
    if source in API_HISTORY_OFF and start < today - timedelta(days=RECENT_DAYS):
        return API_HISTORY_OFF[source]
    return None


def cancel_api_history(rt: Any) -> int:
    """Cancel API runs for sources that no longer fill over the API (a run that was queued,
    running or waiting before the change). Nothing already fetched is touched."""
    cut = [
        f
        for f in fills(rt)
        if f["method"] == "api"
        and f["source"] in API_HISTORY_OFF
        and f["status"] in (*ACTIVE, "stuck")
    ]
    for fill in cut:
        _update(
            rt,
            fill["id"],
            status="cancelled",
            wait_until=None,
            finished_at=utc_now(),
            message="cancelled: this source's API is only used for new activities",
        )
    # Old queued jobs would otherwise occupy the worker after an upgrade. Preserve their
    # audit rows, but make them ineligible for another claim.
    with rt.engine.begin() as conn:
        pending = conn.execute(
            select(job.c.id, job.c.payload).where(
                job.c.kind == JOB, job.c.status.in_(("queued", "running"))
            )
        ).all()
        old_ids = set(
            conn.execute(
                select(history_fill.c.id).where(
                    history_fill.c.source.in_(API_HISTORY_OFF), history_fill.c.method == "api"
                )
            ).scalars()
        )
        job_ids = [row.id for row in pending if row.payload.get("fill") in old_ids]
        if job_ids:
            conn.execute(
                update(job)
                .where(job.c.id.in_(job_ids))
                .values(status="cancelled", finished_at=utc_now(), locked_by=None)
            )
    return len(cut)


def start_api(
    rt: Any,
    source: str,
    start: date,
    end: date,
    *,
    today: date,
    datasets: list[str] | None = None,
) -> dict[str, Any]:
    if source in API_HISTORY_OFF:
        raise ValueError(API_HISTORY_OFF[source])
    if source not in rt.registry.enabled_ids():
        raise ValueError(f"{source!r} is not an enabled source")
    available = history_datasets(rt, source)
    if not available:
        raise ValueError(f"{source} has no history to fetch")
    if datasets and set(datasets) - set(available):
        raise ValueError(f"{source} datasets to fill: {', '.join(available)}")
    if start > end:
        raise ValueError("the start date is after the end date")
    if end > today:
        raise ValueError("the end date is in the future")
    if any(f["source"] == source and f["status"] in ACTIVE for f in fills(rt)):
        raise ValueError(f"{source} is already being filled; pause or cancel that run first")
    fill_id = _insert(
        rt,
        source=source,
        method="api",
        status="running",
        start=start,
        end=end,
        next_month=start.replace(day=1),
        datasets=datasets,
        total=_months(start, end),
        message="queued",
    )
    _enqueue(rt, fill_id)
    return get(rt, fill_id)


def act(rt: Any, fill_id: int, action: str) -> dict[str, Any]:
    """pause | resume | cancel | retry (fetch the partial months again)."""
    fill = get(rt, fill_id)
    status = fill["status"]
    if action == "cancel":
        if status not in (*ACTIVE, "stuck"):
            raise ValueError(f"this run is {status}")
        _update(rt, fill_id, status="cancelled", finished_at=utc_now(), message="cancelled")
        if fill["method"] == "archive" and fill["file"]:
            from vibesynergy import dropfolder

            dropfolder.delete_archive(rt.settings, Path(fill["file"]))
        return get(rt, fill_id)
    if fill["method"] != "api":
        raise ValueError("an archive import can only be cancelled")
    if action in ("resume", "retry") and fill["source"] in API_HISTORY_OFF:
        raise ValueError(API_HISTORY_OFF[fill["source"]])
    if action == "pause":
        if status not in ("running", "waiting"):
            raise ValueError(f"this run is {status}")
        _update(rt, fill_id, status="paused", message="paused")
    elif action == "resume":
        if status not in ("paused", "stuck"):
            raise ValueError(f"this run is {status}")
        _update(rt, fill_id, status="running", failures=0, wait_until=None, message="resumed")
        _enqueue(rt, fill_id)
    elif action == "retry":
        if status in ACTIVE or not fill["partial"]:
            raise ValueError("there are no partial months to fetch again")
        _update(
            rt,
            fill_id,
            status="running",
            retry=list(fill["partial"]),
            partial=[],
            retried=True,
            failures=0,
            finished_at=None,
            message="fetching partial months again",
        )
        _enqueue(rt, fill_id)
    else:
        raise ValueError("action must be pause, resume, cancel or retry")
    return get(rt, fill_id)


# The API run: one month per job -------------------------------------------------------------


def run_step(rt: Any, fill_id: int) -> None:
    """The job handler: fetch the next month of a run, save progress, queue the next job."""
    fill = _row(rt, fill_id)
    if fill is None or fill["status"] not in ("running", "waiting"):
        return  # paused, cancelled or finished meanwhile
    if fill["source"] in API_HISTORY_OFF:
        cancel_api_history(rt)  # a job queued before the change: no request is made
        return
    now = utc_now()
    if fill["wait_until"] and fill["wait_until"] > now + timedelta(minutes=1):
        return  # an older job; the one for after the wait is queued
    month = _current_month(fill)
    if month is None:
        if fill["partial"] and not fill["retried"]:
            _update(rt, fill_id, retry=list(fill["partial"]), partial=[], retried=True)
            _enqueue(rt, fill_id)
        else:
            _finish(rt, fill)
        return
    chunk = Window(max(month, fill["start"]), min(_month_end(month), fill["end"]))
    began = time.monotonic()
    try:
        datasets = fill["datasets"] or history_datasets(rt, fill["source"])
        results = rt.sync.sync(fill["source"], datasets, chunk)
        crash = None
    except Exception as exc:  # e.g. the source was disabled or its login expired
        log.exception("history fill %s: %s failed", fill_id, chunk)
        results, crash = [], f"{type(exc).__name__}: {exc}"
    busy = fill["busy_s"] + time.monotonic() - began
    failed = [r for r in results if r.error]
    label = format_user_month_year(month)

    if results and len(failed) < len(results):  # progress, even if partial
        retrying = bool(fill["retry"])
        partial = [m for m in fill["partial"] if m != _key(month)]
        if failed:
            partial.append(_key(month))
        values: dict[str, Any] = {
            "status": "running",
            "partial": partial,
            "failures": 0,
            "wait_until": None,
            "busy_s": busy,
            "last_error": "; ".join(f"{r.dataset}: {r.error}" for r in failed) or None,
            "message": f"{label} done" + (f" ({len(failed)} datasets failed)" if failed else ""),
        }
        if retrying:
            values["retry"] = fill["retry"][1:]
        else:
            values["next_month"] = _next_month(month)
            values["done"] = fill["done"] + 1
        _save_step(rt, fill_id, values)
    elif results and all(r.retry_at for r in failed):
        until = max(r.retry_at for r in failed)
        values = {
            "status": "waiting",
            "wait_until": until,
            "busy_s": busy,
            "last_error": failed[0].error,
            "message": f"waiting for the API limit to reset ({label} next)",
        }
        _save_step(rt, fill_id, values, run_after=until)
    else:
        failures = fill["failures"] + 1
        error = crash or "; ".join(f"{r.dataset}: {r.error}" for r in failed) or "nothing fetched"
        values = {"failures": failures, "busy_s": busy, "last_error": error}
        if failures >= STUCK_AFTER:
            values.update(status="stuck", message=f"stuck at {label} after {failures} tries")
            if _save_step(rt, fill_id, values, requeue=False):
                _notify(rt, get(rt, fill_id))
        else:
            again = now + RETRY_AFTER
            values.update(
                status="running",
                message=f"{label} failed ({failures} of {STUCK_AFTER} tries); again at "
                f"{format_user_datetime(again)}",
            )
            _save_step(rt, fill_id, values, run_after=again)


def _save_step(
    rt: Any,
    fill_id: int,
    values: dict[str, Any],
    *,
    run_after: datetime | None = None,
    requeue: bool = True,
) -> bool:
    """Save a month's outcome and queue the next job; returns whether the run was still
    going. When it was paused or cancelled while the month ran, the progress is kept but the
    pause or cancel stands (no status change, no next job)."""
    now = utc_now()
    with rt.engine.begin() as conn:
        applied = conn.execute(
            update(history_fill)
            .where(history_fill.c.id == fill_id, history_fill.c.status.in_(("running", "waiting")))
            .values(updated_at=now, **values)
        ).rowcount
        if not applied:
            progress = {
                k: v for k, v in values.items() if k not in ("status", "wait_until", "message")
            }
            conn.execute(
                update(history_fill)
                .where(history_fill.c.id == fill_id)
                .values(updated_at=now, **progress)
            )
    if applied and requeue:
        _enqueue(rt, fill_id, run_after=run_after)
    return bool(applied)


def _current_month(fill: dict[str, Any]) -> date | None:
    if fill["retry"]:
        return date.fromisoformat(f"{fill['retry'][0]}-01")
    month = fill["next_month"]
    return month if month and month <= fill["end"] else None


def _finish(rt: Any, fill: dict[str, Any]) -> None:
    left = fill["partial"]
    message = f"done: {fill['done']} months" + (
        f"; still partial: {', '.join(left)}" if left else ""
    )
    _update(rt, fill["id"], status="done", finished_at=utc_now(), message=message)
    _notify(rt, get(rt, fill["id"]))


# Archive imports ------------------------------------------------------------------------------

ARCHIVE_JOB = "history_archive"
ARCHIVE_FAILURE = "Import failed; the file was removed. Upload it again."
ARCHIVE_FAILURE_RETAINED = "Import failed. Check the file and try again."
ARCHIVE_RETRY = "Import interrupted; trying again."


class _Cancelled(Exception):
    pass


def start_archive(rt: Any, path: Path, kind: str | None = None) -> dict[str, Any]:
    """Import an account export zip in the background; returns the run."""
    from vibesynergy.imports import archive_kind

    kind = kind or archive_kind(path)
    if kind not in ARCHIVES:
        raise ValueError(f"{path.name} is not a Garmin or Strava account export")
    for fill in fills(rt):
        if fill["file"] == str(path) and fill["status"] in ACTIVE:
            return fill
    fill_id = _insert(
        rt, source=kind, method="archive", status="running", file=str(path), message="queued"
    )
    _enqueue_archive(rt, fill_id)
    return get(rt, fill_id)


def resume_archives(rt: Any) -> int:
    """Reconcile crash leftovers and ensure each active import has a queued job."""
    from vibesynergy import dropfolder

    with rt.engine.connect() as conn:
        archive_jobs = conn.execute(
            select(job.c.payload, job.c.status).where(job.c.kind == ARCHIVE_JOB).order_by(job.c.id)
        ).all()
    latest = {row.payload.get("fill"): row.status for row in archive_jobs}
    resumed = 0
    for fill in fills(rt):
        if fill["method"] != "archive":
            continue
        status = latest.get(fill["id"])
        if fill["status"] in ("done", "failed", "cancelled") or status in ("dead", "done"):
            removed = bool(fill["file"]) and dropfolder.delete_archive(
                rt.settings, Path(fill["file"])
            )
            if fill["status"] == "running":
                log.error(
                    "archive import failed; uploaded file removed"
                    if removed
                    else "archive import failed; file retained outside upload directory"
                )
                _update(
                    rt,
                    fill["id"],
                    status="failed",
                    last_error=None,
                    message=ARCHIVE_FAILURE if removed else ARCHIVE_FAILURE_RETAINED,
                    finished_at=utc_now(),
                )
            continue
        if fill["status"] == "running":
            _enqueue_archive(rt, fill["id"])
            resumed += 1
    return resumed


def _enqueue_archive(rt: Any, fill_id: int) -> None:
    rt.jobs.enqueue(
        ARCHIVE_JOB,
        {"fill": fill_id},
        priority=PRIORITY,
        dedupe_key=f"history-archive-{fill_id}",
        max_attempts=STUCK_AFTER,
    )


def import_archive(
    rt: Any, fill_id: int, *, attempt: int = 1, max_attempts: int = STUCK_AFTER
) -> None:
    from vibesynergy import dropfolder, imports

    importers = {
        "garmin_export": imports.import_garmin_export,
        "strava_export": imports.import_strava_export,
    }

    fill = _row(rt, fill_id)
    if fill is None:
        return
    path = Path(fill["file"])
    if fill["status"] == "cancelled":  # cancelled while waiting for its turn
        dropfolder.delete_archive(rt.settings, path)
        return
    if fill["status"] != "running":
        return
    _update(rt, fill_id, message="importing")
    began = time.monotonic()

    def progress(text: str, done: int | None = None, total: int | None = None) -> None:
        if (_row(rt, fill_id) or {}).get("status") == "cancelled":
            raise _Cancelled
        counts = {} if done is None else {"done": done, "total": total}
        _update(rt, fill_id, message=f"importing: {text}", **counts)

    try:
        results = importers[fill["source"]](rt, path, progress=progress)
    except _Cancelled:
        dropfolder.delete_archive(rt.settings, path)
        return
    except Exception as exc:
        corrupt = isinstance(exc, (zipfile.BadZipFile, zipfile.LargeZipFile))
        final = corrupt or attempt >= max_attempts
        if final:
            removed = dropfolder.delete_archive(rt.settings, path)
            log.error(
                "archive import failed; uploaded file removed"
                if removed
                else "archive import failed; file retained outside upload directory"
            )
            _update(
                rt,
                fill_id,
                status="failed",
                last_error=None,
                message=ARCHIVE_FAILURE if removed else ARCHIVE_FAILURE_RETAINED,
                failures=attempt,
                busy_s=time.monotonic() - began,
                finished_at=utc_now(),
            )
            _notify(rt, get(rt, fill_id))
            return
        _update(
            rt, fill_id, message=ARCHIVE_RETRY, failures=attempt, busy_s=time.monotonic() - began
        )
        raise RuntimeError("archive import retry") from exc
    new = sum(r.new_versions for r in results)
    known = sum(r.skipped for r in results)
    errors = sum(r.normalise_errors for r in results)
    message = ("done: " if new else "done, nothing new: ") + summary(rt, fill["source"])
    if known:
        message += f"; {_count(known, 'file was', 'files were')} already here"
    if errors:
        message += f"; {errors:,} could not be read (kept in the raw archive)"
    dropfolder.delete_archive(rt.settings, path)
    _update(
        rt,
        fill_id,
        status="done",
        done=(_row(rt, fill_id) or {}).get("total") or new,  # a counted archive: all of it
        file=str(path),
        busy_s=time.monotonic() - began,
        message=message,
        finished_at=utc_now(),
    )
    _notify(rt, get(rt, fill_id))


def summary(rt: Any, source: str) -> str:
    """What an export source now holds: activities with their date range, and days of daily
    data (Garmin's export also has sleep, steps, resting heart rate, …)."""
    with rt.engine.connect() as conn:
        count, first, last = conn.execute(
            select(
                func.count(), func.min(activity.c.local_date), func.max(activity.c.local_date)
            ).where(activity.c.source == source)
        ).one()
        days = conn.execute(
            select(func.count(func.distinct(daily_metric.c.date))).where(
                daily_metric.c.source == source
            )
        ).scalar()
    parts = [_count(count, "activity", "activities")]
    if count:
        parts[0] += f" from {format_user_date(first)} to {format_user_date(last)}"
    if days:
        parts.append(f"daily data for {_count(days, 'day', 'days')}")
    return ", ".join(parts)


# Reading ------------------------------------------------------------------------------------


def fills(rt: Any) -> list[dict[str, Any]]:
    """Every run, newest first, with progress and an ETA where one can be given."""
    with rt.engine.connect() as conn:
        rows = conn.execute(select(history_fill).order_by(history_fill.c.id.desc())).mappings()
        return [_out(dict(r)) for r in rows]


def get(rt: Any, fill_id: int) -> dict[str, Any]:
    row = _row(rt, fill_id)
    if row is None:
        raise ValueError(f"no history fill {fill_id}")
    return _out(row)


def _out(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items()}
    for key in ("start", "end", "next_month"):
        out[key] = str(out[key]) if out[key] else None
    for key in ("wait_until", "created_at", "updated_at", "finished_at"):
        out[key] = out[key].isoformat() if out[key] else None
    out["name"] = ARCHIVES.get(row["source"], row["source"])
    total, done = row["total"], row["done"]
    out["progress"] = round(done / total, 3) if total else None
    remaining = (total - done) + len(row["retry"]) if total else None
    out["eta_s"] = (
        round(row["busy_s"] / done * remaining)
        if row["method"] == "api" and row["status"] in ("running", "waiting") and done
        else None
    )
    return out


# Helpers ------------------------------------------------------------------------------------


def _account_start(rt: Any, source: str) -> date | None:
    if source != "strava":
        return None
    try:
        for raw_id in rt.raw.current_ids("strava", "profile"):
            created = ((rt.raw.load(raw_id).json() or {}).get("athlete") or {}).get("created_at")
            if created:
                return date.fromisoformat(str(created)[:10])
    except Exception:  # a hint only
        log.debug("no Strava account start", exc_info=True)
    return None


def _insert(rt: Any, **values: Any) -> int:
    now = utc_now()
    defaults = {
        "partial": [],
        "retry": [],
        "retried": False,
        "failures": 0,
        "done": 0,
        "busy_s": 0.0,
        "created_at": now,
        "updated_at": now,
    }
    with rt.engine.begin() as conn:
        return conn.execute(
            history_fill.insert().values(**{**defaults, **values}).returning(history_fill.c.id)
        ).scalar_one()


def _update(rt: Any, fill_id: int, **values: Any) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            update(history_fill)
            .where(history_fill.c.id == fill_id)
            .values(updated_at=utc_now(), **values)
        )


def _row(rt: Any, fill_id: int) -> dict[str, Any] | None:
    with rt.engine.connect() as conn:
        row = conn.execute(select(history_fill).where(history_fill.c.id == fill_id)).mappings()
        found = row.first()
    return dict(found) if found else None


def _enqueue(rt: Any, fill_id: int, run_after: datetime | None = None) -> None:
    rt.jobs.enqueue(JOB, {"fill": fill_id}, priority=PRIORITY, run_after=run_after)


def _notify(rt: Any, fill: dict[str, Any]) -> None:
    from vibesynergy import alerts

    subject = f"VibeSynergy: filling {fill['name']} {fill['status']}"
    body = fill["message"] or ""
    if fill["last_error"] and fill["status"] != "done":
        body += f"\nLast error: {fill['last_error']}"
    try:
        alerts.deliver(rt, subject, body)
    except Exception:  # an alert must never break the run
        log.exception("sending the history alert failed")


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _key(month: date) -> str:
    return month.strftime("%Y-%m")


def _month_end(month: date) -> date:
    return _next_month(month) - timedelta(days=1)


def _next_month(month: date) -> date:
    return (month.replace(day=1) + timedelta(days=32)).replace(day=1)


def _months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month + 1
