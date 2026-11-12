"""Runs queued jobs. Job kinds map to handlers; unknown kinds fail permanently."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime

from vibesynergy.core.timeutil import Window
from vibesynergy.normalize.normaliser import Normaliser
from vibesynergy.sync.engine import DatasetResult, SyncEngine, SyncTimeout
from vibesynergy.sync.jobs import Job, JobQueue
from vibesynergy.sync.scheduler import sync_job_timeout_seconds

log = logging.getLogger(__name__)

Handler = Callable[[Job], None]


class JobError(Exception):
    def __init__(self, message: str, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at  # defer without consuming an attempt, e.g. rate limit or budget


def _raise_failures(results: list[DatasetResult]) -> None:
    failed = [r for r in results if r.error or r.normalise_errors]
    if not failed:
        return
    problems = []
    for result in failed:
        if result.error:
            problems.append(f"{result.dataset}: {result.error}")
        if result.normalise_errors:
            problems.append(
                f"{result.dataset}: {result.normalise_errors} raw items failed to normalise"
            )
    message = "; ".join(problems)
    waits = [r.retry_at for r in failed if r.retry_at and not r.normalise_errors]
    retry_at = max(waits) if len(waits) == len(failed) else None
    raise JobError(message, retry_at)


def default_handlers(sync: SyncEngine, normaliser: Normaliser) -> dict[str, Handler]:
    def run_sync(job: Job) -> None:
        p = job.payload
        if reason := sync.skip_reason(p["source"]):
            log.info("sync for %s skipped: %s", p["source"], reason)
            return
        window = Window.parse(p["window"]) if p.get("window") else None
        datasets = [p["dataset"]] if p.get("dataset") else None
        deadline = None
        if timeout := sync_job_timeout_seconds(sync._settings):
            deadline = time.monotonic() + timeout
        try:
            _raise_failures(sync.sync(p["source"], datasets, window, deadline=deadline))
        except SyncTimeout as exc:
            raise JobError("sync timed out") from exc

    def run_normalise(job: Job) -> None:
        _, errors = normaliser.rebuild(job.payload["source"], job.payload.get("dataset"))
        if errors:
            raise JobError(f"{errors} raw items failed to normalise")

    def run_fetch_one(job: Job) -> None:
        p = job.payload
        if p["source"] == "strava" and p["dataset"] == "activities":
            from vibesynergy.core.timeutil import utc_now
            from vibesynergy.query import latency

            latency.stamp(sync._engine, "strava", str(p["key"]), fetch_started_at=utc_now())
        _raise_failures([sync.fetch_entity(p["source"], p["dataset"], str(p["key"]))])

    def run_tombstone(job: Job) -> None:
        p = job.payload
        _raise_failures([sync.tombstone(p["source"], p["dataset"], str(p["key"]))])

    return {
        "sync": run_sync,
        "normalise": run_normalise,
        "fetch_one": run_fetch_one,
        "tombstone": run_tombstone,
    }


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        *,
        kinds: tuple[str, ...] | None = None,
        exclude_kinds: tuple[str, ...] = (),
    ) -> None:
        self._queue = queue
        self._handlers = handlers
        self._kinds = kinds
        self._exclude_kinds = exclude_kinds
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def run_once(self) -> bool:
        """Run one job if one is due. Returns whether a job ran."""
        recover_stale_jobs(self._queue)
        job = self._queue.claim(
            self.worker_id, kinds=self._kinds, exclude_kinds=self._exclude_kinds
        )
        if job is None:
            return False
        handler = self._handlers.get(job.kind)
        try:
            if handler is None:
                raise JobError(f"no handler for job kind {job.kind!r}")
            handler(job)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            retry_at = getattr(exc, "retry_at", None)
            if retry_at is not None:
                log.info("job %s (%s) deferred; again at %s", job.id, job.kind, retry_at)
                self._queue.defer(job.id, retry_at, error)
            else:
                log.warning("job %s (%s) failed: %s", job.id, job.kind, exc)
                self._queue.fail(job.id, error)
        else:
            self._queue.complete(job.id)
        return True

    def run(self, poll_seconds: float = 5.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        delay = poll_seconds
        max_delay = max(poll_seconds * 64, 300.0)
        while not stop.is_set():
            try:
                if not self.run_once():
                    stop.wait(poll_seconds)
                delay = poll_seconds
            except Exception:
                log.exception("worker %s loop failed", self.worker_id)
                stop.wait(delay)
                delay = min(delay * 2, max_delay)


def recover_stale_jobs(queue: JobQueue, *, startup: bool = False) -> int:
    """Re-queue jobs still marked running after a prior worker process died.

    ``startup`` is for the one call ``serve`` makes before its workers start. At that moment
    this process runs no job, so a job owned by its own pid was left by an earlier run: in a
    container the service has the same pid on every start. A container also gets a new
    hostname when it is recreated (an update), and nothing else works on its database, so
    there every job still marked running is one the previous container left behind."""
    if not startup:
        return queue.requeue_abandoned(_owner_alive)
    in_container = os.environ.get("VIBESYNERGY_CONTAINER") == "1"
    own = f"{socket.gethostname()}:{os.getpid()}"

    def alive(owner: str | None) -> bool | None:
        if in_container or owner == own:
            return False
        return _owner_alive(owner)

    return queue.requeue_abandoned(alive)


def _owner_alive(owner: str | None) -> bool | None:
    """Legacy host:pid worker IDs. Unknown/remote owners are not safe to reclaim."""
    host, separator, pid_text = (owner or "").rpartition(":")
    if not separator or host != socket.gethostname():
        return None
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True
