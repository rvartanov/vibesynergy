"""Sync engine: fetch → raw archive → normalise, with per-dataset state.

One dataset failing never stops the others; its error is recorded in ``dataset_state``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.config import RETIRED_SOURCES, Settings
from vibesynergy.core.errors import RateLimited
from vibesynergy.core.timeutil import Window, add_months, today_in, utc_now
from vibesynergy.normalize.normaliser import Normaliser
from vibesynergy.query import latency
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Source
from vibesynergy.sources.registry import SourceRegistry
from vibesynergy.store.raw import RawStore, encode_payload
from vibesynergy.store.schema import dataset_state

log = logging.getLogger(__name__)


class SyncTimeout(Exception):
    """A sync job exceeded its configured wall-clock limit."""


def _check_sync_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise SyncTimeout()


@dataclass
class DatasetResult:
    source: str
    dataset: str
    window: Window | None
    fetched: int = 0
    skipped: int = 0  # immutable items already archived
    new_versions: int = 0
    records: int = 0
    normalise_errors: int = 0
    error: str | None = None
    retry_at: datetime | None = None  # set when the error was a rate limit
    raw_ids: list[str] = field(default_factory=list)


class SyncEngine:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        raw: RawStore,
        registry: SourceRegistry,
        normaliser: Normaliser,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._raw = raw
        self._registry = registry
        self._normaliser = normaliser
        # Called after a sync, fetch or import that changed canonical data (derived tables).
        self.on_change: Callable[[], None] | None = None
        # Called after every sync, including one that wrote nothing, so open predictions
        # can pick up a daily value that was already stored.
        self.on_score: Callable[[], None] | None = None

    def skip_reason(self, source_id: str) -> str | None:
        """Why a queued sync ends quietly: the source was retired, turned off or isn't set
        up."""
        if source_id in RETIRED_SOURCES:
            return "retired"
        if source_id not in self._registry.available():
            return None  # unknown: let the sync fail loudly
        if source_id not in self._registry.enabled_ids():
            return "disabled"
        if not self._registry.get(source_id).configured():
            return "not set up"
        return None

    def sync(
        self,
        source_id: str,
        datasets: Sequence[str] | None = None,
        window: Window | None = None,
        *,
        deadline: float | None = None,
    ) -> list[DatasetResult]:
        source = self._registry.get(source_id)
        specs = [source.dataset(n) for n in datasets] if datasets else source.sync_datasets()
        if not datasets:  # a plain sync: leave recently fetched, slowly changing datasets be
            specs = [s for s in specs if not self._fresh(source.id, s)]
        results = []
        for spec in specs:
            _check_sync_deadline(deadline)
            results.append(self.sync_dataset(source, spec, window, deadline=deadline))
        if source_id == "intervals_icu":
            try:
                from vibesynergy.core.timeutil import today_in
                from vibesynergy.query import icu_notes

                icu_notes.retry_recent(
                    self._engine, self._settings.data_dir, today_in(self._settings.timezone)
                )
            except Exception:
                log.exception("intervals.icu private notes retry failed")
        self.changed(results)
        if self.on_score is not None:
            try:
                self.on_score()
            except Exception:
                log.exception("scoring coach predictions failed")
        return results

    def changed(self, results: Sequence[DatasetResult]) -> None:
        """Refresh derived data if any result wrote canonical records."""
        if self.on_change and any(r.records or r.new_versions for r in results):
            try:
                self.on_change()
            except Exception:
                log.exception("updating derived data failed")

    def sync_dataset(
        self,
        source: Source,
        spec: DatasetSpec,
        window: Window | None = None,
        *,
        deadline: float | None = None,
    ) -> DatasetResult:
        lock = (
            source.ctx.secrets.lock("calendar-read-write")
            if (source.id, spec.name) == ("garmin", "scheduled_workouts")
            else nullcontext()
        )
        with lock:
            return self._sync_dataset(source, spec, window, deadline=deadline)

    def _sync_dataset(
        self,
        source: Source,
        spec: DatasetSpec,
        window: Window | None,
        *,
        deadline: float | None = None,
    ) -> DatasetResult:
        if spec.kind is DatasetKind.FILE:
            result = DatasetResult(source.id, spec.name, None)
            result.error = "imported with `vibesynergy import`, not fetched by sync"
            return result
        if spec.kind is DatasetKind.SNAPSHOT:
            window = None
        elif window is None:
            window = self.plan_window(source.id, spec)
        result = DatasetResult(source.id, spec.name, window)
        started = utc_now()
        if (source.id, spec.name) == ("garmin", "scheduled_workouts"):
            # An interrupted/ongoing fetch must not leave the previous read writable.
            with self._engine.begin() as conn:
                conn.execute(
                    update(dataset_state)
                    .where(
                        dataset_state.c.source == source.id, dataset_state.c.dataset == spec.name
                    )
                    .values(cursor=None)
                )
        try:
            _check_sync_deadline(deadline)
            for item in source.fetch(spec, window):
                _check_sync_deadline(deadline)
                if source.id in {"garmin", "strava"} and spec.name == "activities":
                    latency.stamp(self._engine, source.id, item.item_key, fetch_started_at=started)
                self.ingest(source.id, spec.name, item, result)
        except SyncTimeout:
            log.warning("sync %s.%s timed out", source.id, spec.name)
            _failed(result, SyncTimeout("sync timed out"))
        except Exception as exc:
            log.exception("sync %s.%s failed", source.id, spec.name)
            _failed(result, exc)
        self.record_state(result, started)
        return result

    def _fresh(self, source_id: str, spec: DatasetSpec) -> bool:
        if not spec.min_age_minutes:
            return False
        state = self.state(source_id, spec.name)
        done = state and state["last_success_at"]
        if not done:
            return False
        done = done if done.tzinfo else done.replace(tzinfo=UTC)
        return utc_now() - done < timedelta(minutes=spec.min_age_minutes)

    def fetch_entity(self, source_id: str, dataset: str, key: str) -> DatasetResult:
        """Fetch one item (a webhook said it changed) and normalise it."""
        source = self._registry.get(source_id)
        result = DatasetResult(source_id, dataset, None)
        try:
            for item in source.fetch_one(dataset, key):
                self.ingest(source_id, dataset, item, result)
        except Exception as exc:
            log.exception("fetch %s.%s %s failed", source_id, dataset, key)
            _failed(result, exc)
        self.changed([result])
        return result

    def tombstone(self, source_id: str, dataset: str, key: str) -> DatasetResult:
        """Record that the source deleted an item. The raw history stays; the canonical
        rows go, because the tombstone version maps to nothing."""
        result = DatasetResult(source_id, dataset, None)
        if self._raw.current_id(source_id, dataset, key):
            self.ingest(source_id, dataset, FetchedItem(key, {"deleted": True}), result)
        self.changed([result])
        return result

    def ingest(
        self, source_id: str, dataset: str, item: FetchedItem, result: DatasetResult
    ) -> None:
        """Archive one item; process new content and retry unfinished existing mappings."""
        if item.immutable and (raw_id := self._raw.current_id(source_id, dataset, item.item_key)):
            result.skipped += 1
            if self._raw.needs_normalising(raw_id):
                self._normalise(raw_id, result)
                if dataset == "activities" and source_id in {"strava", "garmin"}:
                    latency.fetched(self._engine, source_id, item.item_key, utc_now())
            return
        if item.revision:
            known = self._raw.current_request(source_id, dataset, item.item_key) or {}
            if known.get("revision") == item.revision:
                result.skipped += 1  # unchanged since it was fetched
                raw_id = self._raw.current_id(source_id, dataset, item.item_key)
                if raw_id and self._raw.needs_normalising(raw_id):
                    self._normalise(raw_id, result)
                    if dataset == "activities" and source_id in {"strava", "garmin"}:
                        latency.fetched(self._engine, source_id, item.item_key, utc_now())
                return
        payload, content_type = encode_payload(item.resolve())
        stored = self._raw.put(
            source=source_id,
            dataset=dataset,
            item_key=item.item_key,
            payload=payload,
            content_type=item.content_type or content_type,
            request=item.request,
            window=item.window,
        )
        result.fetched += 1
        if dataset == "activities" and source_id in {"strava", "garmin"}:
            latency.stamp(self._engine, source_id, item.item_key, fetched_at=utc_now())
        if stored.is_new:
            result.new_versions += 1
        if stored.is_new or self._raw.needs_normalising(stored.id):
            self._normalise(stored.id, result)
        result.raw_ids.append(stored.id)
        if dataset == "activities" and source_id in {"strava", "garmin"}:
            latency.fetched(self._engine, source_id, item.item_key, utc_now())

    def _normalise(self, raw_id: str, result: DatasetResult) -> None:
        outcome = self._normaliser.normalise(raw_id)
        result.records += outcome.records
        result.normalise_errors += outcome.error is not None

    def plan_window(self, source_id: str, spec: DatasetSpec) -> Window:
        """Incremental window: from the last synced day (minus refetch) up to today, or up to
        ``lookahead_months`` (else ``lookahead_days``) ahead for a training calendar."""
        today = today_in(self._settings.timezone)
        state = self.state(source_id, spec.name)
        if state and state["synced_until"]:
            start = min(state["synced_until"], today) - timedelta(days=spec.refetch_days)
        else:
            start = today - timedelta(days=spec.initial_days - 1)
        if spec.history_start:
            start = max(start, spec.history_start)
        if spec.max_days is not None:
            start = max(start, today - timedelta(days=spec.max_days))
        if spec.lookahead_months is not None:
            end = add_months(today, spec.lookahead_months)
        else:
            end = today + timedelta(days=spec.lookahead_days)
        return Window(min(start, today), end)

    def state(self, source_id: str, dataset: str) -> dict | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(dataset_state).where(
                    dataset_state.c.source == source_id, dataset_state.c.dataset == dataset
                )
            ).first()
        return dict(row._mapping) if row else None

    def states(self) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(dataset_state)
                .where(dataset_state.c.source.not_in(RETIRED_SOURCES))
                .order_by(dataset_state.c.source, dataset_state.c.dataset)
            )
            return [dict(r._mapping) for r in rows]

    def record_state(self, result: DatasetResult, attempted_at) -> None:
        values: dict = {"last_attempt_at": attempted_at, "last_error": result.error}
        if (result.source, result.dataset) == ("garmin", "scheduled_workouts"):
            values["cursor"] = None
            if (
                not result.error
                and not result.normalise_errors
                and result.window
                and result.raw_ids
            ):
                values["cursor"] = {
                    "calendar_coverage": {
                        "start": result.window.start.isoformat(),
                        "end": result.window.end.isoformat(),
                        "completed_at": utc_now().isoformat(),
                        "raw_ids": result.raw_ids,
                    }
                }
        if result.error is None:
            values["last_success_at"] = utc_now()
        stmt = insert(dataset_state).values(source=result.source, dataset=result.dataset, **values)
        update_set = dict(values)
        if result.error is None and result.window:
            # Coverage only grows; a backfill of old dates must not move synced_until back.
            stmt = insert(dataset_state).values(
                source=result.source,
                dataset=result.dataset,
                synced_from=result.window.start,
                synced_until=result.window.end,
                **values,
            )
            update_set["synced_from"] = _least(
                dataset_state.c.synced_from, stmt.excluded.synced_from
            )
            update_set["synced_until"] = _greatest(
                dataset_state.c.synced_until, stmt.excluded.synced_until
            )
        with self._engine.begin() as conn:
            conn.execute(
                stmt.on_conflict_do_update(index_elements=["source", "dataset"], set_=update_set)
            )


def _least(current, new):
    return func.coalesce(func.min(current, new), new)


def _greatest(current, new):
    return func.coalesce(func.max(current, new), new)


def _failed(result: DatasetResult, exc: Exception) -> None:
    result.error = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, RateLimited):
        result.retry_at = exc.retry_at
