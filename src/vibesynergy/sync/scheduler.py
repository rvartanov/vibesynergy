"""Periodic syncs: enqueues an incremental sync per enabled source at its interval.

Configured under ``[schedule]`` in minutes (0 turns it off)::

    [schedule]
    garmin = 15
    withings = 60     # webhooks deliver new data within a minute; this is a safety net
    strava = 60
    intervals_icu = 60
    "garmin.scheduled_workouts" = 15   # one dataset more often: the training calendar
    dropfolder = 5    # the drop folder check
    alerts = 15       # the alert check
    sync_job_timeout_minutes = 20   # wall-clock limit for one sync job (0 = off)
    http_timeout_seconds = 60       # per-request connect+read for source HTTP clients

Jobs share the dedupe key used by ``vibesynergy sync --queue``, so a sync that is still queued
or running is never queued twice.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from vibesynergy.alerts import ALERT_MINUTES
from vibesynergy.core.config import Settings
from vibesynergy.dropfolder import DROP_MINUTES
from vibesynergy.sources.registry import SourceRegistry
from vibesynergy.sync.jobs import JobQueue

log = logging.getLogger(__name__)

DEFAULT_SYNC_JOB_TIMEOUT_MINUTES = 20

DEFAULT_MINUTES = {
    "garmin": 15,  # a new activity shows up within a quarter of an hour
    "caldav": 15,
    "withings": 60,
    "strava": 60,
    "intervals_icu": 60,
    # Plan changes made on the watch or in Garmin Connect show up sooner than the full sync.
    "garmin.scheduled_workouts": 15,
}
SERVICE_MINUTES = {"dropfolder": DROP_MINUTES, "alerts": ALERT_MINUTES}


def minutes_for(settings: Settings, key: str) -> float:
    """The interval for a source, ``source.dataset`` or service check (0: off)."""
    default = DEFAULT_MINUTES.get(key, SERVICE_MINUTES.get(key, 0))
    try:
        return max(0.0, float(settings.schedule.get(key, default)))
    except (TypeError, ValueError):
        return float(default)


def sync_job_timeout_seconds(settings: Settings) -> float | None:
    """Wall-clock limit for one ``sync`` job (0 in config turns it off)."""
    raw = settings.schedule.get("sync_job_timeout_minutes", DEFAULT_SYNC_JOB_TIMEOUT_MINUTES)
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        minutes = float(DEFAULT_SYNC_JOB_TIMEOUT_MINUTES)
    if minutes <= 0:
        return None
    return minutes * 60.0


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        registry: SourceRegistry,
        jobs: JobQueue,
        *,
        clock: Callable[[], float] = time.monotonic,
        services: dict[str, Callable[[], bool]] | None = None,
    ) -> None:
        self._services = services or {}
        self._settings = settings
        self._registry = registry
        self._jobs = jobs
        self._clock = clock
        self._next: dict[str, float] = {}

    def _configured(self, sid: str) -> bool:
        try:
            return self._registry.get(sid).configured()
        except Exception:
            log.exception("checking whether %s is set up failed", sid)
            return False

    def intervals(self) -> dict[str, float]:
        """Minutes per enabled source, and per ``source.dataset`` that has its own interval
        (only while that dataset is collected). Zero or no interval: not scheduled."""
        enabled = {sid for sid in self._registry.enabled_ids() if self._configured(sid)}
        keys = {*DEFAULT_MINUTES, *self._settings.schedule} - set(SERVICE_MINUTES)
        out = {}
        for key in sorted(keys):
            sid, _, dataset = key.partition(".")
            if sid not in enabled or not (minutes := minutes_for(self._settings, key)):
                continue
            if dataset:
                synced = {d.name for d in self._registry.get(sid).sync_datasets()}
                if dataset not in synced:
                    continue
            out[key] = minutes
        return out

    def run_once(self) -> list[str]:
        """Enqueue the syncs that are due, and the alert check when alerts are set up;
        returns what was queued."""
        now = self._clock()
        queued = []
        cfg = self._settings.alerts
        drop = minutes_for(self._settings, "dropfolder")
        if drop and now >= self._next.get("dropfolder", 0):
            self._next["dropfolder"] = now + drop * 60
            if self._jobs.enqueue("dropfolder", {}, dedupe_key="dropfolder"):
                queued.append("dropfolder")
        wanted = cfg.get("enabled", True) and cfg.get("email_to")
        check = minutes_for(self._settings, "alerts")
        if wanted and check and now >= self._next.get("alerts", 0):
            self._next["alerts"] = now + check * 60
            if self._jobs.enqueue("alerts", {}, dedupe_key="alerts"):
                queued.append("alerts")
        for key, wanted_service in self._services.items():
            if wanted_service() and now >= self._next.get(key, 0):
                self._next[key] = now + 5 * 60
                if self._jobs.enqueue(key, {}, dedupe_key=key, priority=9):
                    queued.append(key)
        for key, minutes in self.intervals().items():
            if now < self._next.get(key, 0):
                continue
            self._next[key] = now + minutes * 60
            sid, _, dataset = key.partition(".")
            payload = {"source": sid, "dataset": dataset or None, "window": None}
            dedupe = f"sync:{sid}:{dataset or None}:None"
            if self._jobs.enqueue("sync", payload, dedupe_key=dedupe):
                queued.append(key)
        return queued

    def reload(self) -> None:
        """Follow the intervals on the current settings from the next tick.

        A shorter interval is not left waiting on the previous one. Saving does not
        queue a sync by itself.
        """
        now = self._clock()
        active = dict(self.intervals())
        for key in SERVICE_MINUTES:
            minutes = minutes_for(self._settings, key)
            if minutes:
                active[key] = minutes
        for key in list(self._next):
            if key not in active and key not in self._services:
                del self._next[key]
        for key, minutes in active.items():
            wait = minutes * 60.0
            current = self._next.get(key)
            self._next[key] = now + wait if current is None else min(current, now + wait)

    def run(self, stop: threading.Event, tick: float = 30.0) -> None:
        while not stop.is_set():
            try:
                for sid in self.run_once():
                    log.info("scheduled sync for %s", sid)
            except Exception:
                log.exception("scheduler tick failed")
            stop.wait(tick)
