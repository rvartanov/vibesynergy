"""Durable job queue in SQLite: survives restarts, visible to the CLI, portal and MCP."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import job

BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_MAX = timedelta(hours=1)
# Never pruned: archive and history imports resume from their jobs (history.py).
KEEP_KINDS = ("history", "history_archive")
PRUNE_STATUSES = ("done", "cancelled")


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int


def backoff(attempts: int) -> timedelta:
    return min(BACKOFF_BASE * 2 ** (attempts - 1), BACKOFF_MAX)


class JobQueue:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enqueue(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        priority: int = 0,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        max_attempts: int = 5,
    ) -> int | None:
        """Add a job. Returns None when an active job with the same dedupe_key exists."""
        now = utc_now()
        try:
            with self._engine.begin() as conn:
                return conn.execute(
                    job.insert()
                    .values(
                        kind=kind,
                        payload=dict(payload),
                        status="queued",
                        priority=priority,
                        run_after=run_after or now,
                        attempts=0,
                        max_attempts=max_attempts,
                        dedupe_key=dedupe_key,
                        created_at=now,
                    )
                    .returning(job.c.id)
                ).scalar_one()
        except IntegrityError:
            return None

    def claim(
        self,
        worker_id: str,
        *,
        kinds: tuple[str, ...] | None = None,
        exclude_kinds: tuple[str, ...] = (),
    ) -> Job | None:
        """Atomically take the most urgent runnable job in this worker's lane."""
        now = utc_now()
        filters = []
        if kinds is not None:
            filters.append(job.c.kind.in_(kinds))
        if exclude_kinds:
            filters.append(job.c.kind.not_in(exclude_kinds))
        next_id = (
            select(job.c.id)
            .where(job.c.status == "queued", job.c.run_after <= now, *filters)
            .order_by(job.c.priority.desc(), job.c.id)
            .limit(1)
            .scalar_subquery()
        )
        with self._engine.begin() as conn:
            row = conn.execute(
                update(job)
                .where(job.c.id == next_id, job.c.status == "queued")
                .values(
                    status="running",
                    started_at=now,
                    locked_by=worker_id,
                    attempts=job.c.attempts + 1,
                )
                .returning(job.c.id, job.c.kind, job.c.payload, job.c.attempts, job.c.max_attempts)
            ).first()
        return Job(*row) if row else None

    def complete(self, job_id: int) -> None:
        self._finish(job_id, status="done", finished_at=utc_now(), last_error=None)

    def fail(self, job_id: int, error: str) -> None:
        with self._engine.connect() as conn:
            attempts, max_attempts = conn.execute(
                select(job.c.attempts, job.c.max_attempts).where(job.c.id == job_id)
            ).one()
        if attempts >= max_attempts:
            self._finish(job_id, status="dead", finished_at=utc_now(), last_error=error)
        else:
            self._finish(
                job_id,
                status="queued",
                run_after=utc_now() + backoff(attempts),
                locked_by=None,
                last_error=error,
            )

    def defer(self, job_id: int, run_after: datetime, error: str) -> None:
        """Re-queue a job for later without using up an attempt (e.g. a rate limit)."""
        with self._engine.begin() as conn:
            conn.execute(
                update(job)
                .where(job.c.id == job_id)
                .values(
                    status="queued",
                    run_after=run_after,
                    attempts=job.c.attempts - 1,
                    locked_by=None,
                    last_error=error,
                )
            )

    def requeue_abandoned(self, owner_alive: Callable[[str | None], bool | None]) -> int:
        """Recover a provably dead local owner; age alone never proves a job abandoned."""
        with self._engine.connect() as conn:
            running = conn.execute(
                select(job.c.id, job.c.locked_by, job.c.started_at).where(job.c.status == "running")
            ).all()
        recovered = 0
        for row in running:
            if owner_alive(row.locked_by) is not False:
                continue
            with self._engine.begin() as conn:
                recovered += conn.execute(
                    update(job)
                    .where(
                        job.c.id == row.id,
                        job.c.status == "running",
                        job.c.locked_by == row.locked_by,
                        job.c.started_at == row.started_at,
                    )
                    .values(
                        status="queued",
                        locked_by=None,
                        run_after=utc_now(),
                        attempts=func.max(0, job.c.attempts - 1),
                        last_error="Recovered after worker process ended",
                    )
                ).rowcount
        return recovered

    def counts(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(job.c.status, func.count()).group_by(job.c.status))
            return {status: n for status, n in rows}

    def prune(
        self, *, older_than: timedelta, statuses: tuple[str, ...], apply: bool = False
    ) -> dict[str, int]:
        """Finished jobs older than ``older_than`` per kind. Keeps the newest finished
        job of each kind (sync status reads it) and the history jobs (imports resume from
        them). Without ``apply`` it only counts."""
        cutoff = utc_now() - older_than
        with self._engine.begin() as conn:
            newest = (
                select(func.max(job.c.id))
                .where(job.c.finished_at.is_not(None))
                .group_by(job.c.kind)
            )
            where = (
                job.c.status.in_(statuses),
                func.coalesce(job.c.finished_at, job.c.created_at) < cutoff,
                job.c.kind.not_in(KEEP_KINDS),
                job.c.id.not_in(newest),
            )
            found = {
                kind: n
                for kind, n in conn.execute(
                    select(job.c.kind, func.count()).where(*where).group_by(job.c.kind)
                )
            }
            if apply and found:
                conn.execute(delete(job).where(*where))
        return found

    def recent(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = select(job).order_by(job.c.id.desc()).limit(limit)
        if status:
            query = query.where(job.c.status == status)
        with self._engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(query)]

    def _finish(self, job_id: int, **values: Any) -> None:
        with self._engine.begin() as conn:
            conn.execute(update(job).where(job.c.id == job_id).values(**values))
