"""Turns raw items into canonical records using the owning source's mappers."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, Table, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.timeutil import utc_now
from vibesynergy.model import (
    Activity,
    ActivityLap,
    ActivityZoneTime,
    BodyMeasurement,
    CalendarSpan,
    DailyMetric,
    FitFile,
    IcuEvent,
    PlannedItem,
    Record,
    SleepSession,
    SleepStage,
    Stream,
    TrainingPlan,
    WellnessSamples,
    ZoneSet,
)
from vibesynergy.sources.registry import SourceRegistry
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.raw import RawStore
from vibesynergy.store.schema import (
    activity,
    activity_lap,
    activity_zone_time,
    body_measurement,
    daily_metric,
    fit_file,
    icu_event,
    planned_item,
    sleep_session,
    sleep_stage,
    training_plan,
    zone_set,
)

log = logging.getLogger(__name__)

# Record type -> (table, primary key columns used for upserts).
TABLES: dict[type, tuple[Table, tuple[str, ...]]] = {
    DailyMetric: (daily_metric, ("subject_id", "date", "metric", "source")),
    Activity: (activity, ("source", "source_id")),
    ActivityLap: (activity_lap, ("source", "activity_source_id", "lap_index")),
    ActivityZoneTime: (activity_zone_time, ("source", "activity_source_id", "kind", "zone")),
    SleepSession: (sleep_session, ("source", "source_id")),
    SleepStage: (sleep_stage, ("source", "session_source_id", "start_utc")),
    BodyMeasurement: (body_measurement, ("source", "source_id", "metric")),
    ZoneSet: (zone_set, ("source", "kind", "sport", "valid_from")),
    PlannedItem: (planned_item, ("source", "source_id")),
    TrainingPlan: (training_plan, ("source", "source_id")),
    FitFile: (fit_file, ("raw_item_id", "fit_id")),
    IcuEvent: (icu_event, ("id",)),
}

# Canonical tables whose rows point back to the raw item they came from.
_PROVENANCE_TABLES = tuple(table for table, _ in TABLES.values())


@dataclass(frozen=True)
class NormaliseResult:
    records: int
    error: str | None = None


class Normaliser:
    def __init__(
        self,
        engine: Engine,
        raw: RawStore,
        registry: SourceRegistry,
        parquet: ParquetStore,
    ) -> None:
        self._engine = engine
        self._raw = raw
        self._registry = registry
        self._parquet = parquet

    def normalise(self, raw_id: str) -> NormaliseResult:
        """Map one raw item. Errors are recorded on the item, not raised."""
        item = self._raw.load(raw_id)
        source = self._registry.get(item.source, require_enabled=False)
        mapper = source.mappers().get(item.dataset)
        if mapper is None:  # archived for completeness; nothing maps it yet
            self._raw.mark_normalised(raw_id)
            return NormaliseResult(0)
        keep_history = source.keeps_history(item.dataset)
        try:
            records = list(mapper(item))
            files = self._write_files(records, source=item.source)
            with self._engine.begin() as conn:
                if item.supersedes and not keep_history:
                    _delete_from(conn, item.supersedes)
                _delete_from(conn, item.id)
                count = write_records(conn, records, source=item.source, raw_item_id=item.id)
        except Exception as exc:
            log.exception("normalising %s (%s.%s) failed", raw_id, item.source, item.dataset)
            self._raw.mark_normalised(raw_id, error=f"{type(exc).__name__}: {exc}")
            return NormaliseResult(0, error=str(exc))
        self._raw.mark_normalised(raw_id)
        return NormaliseResult(count + files)

    def rebuild(self, source_id: str, dataset: str | None = None) -> tuple[int, int]:
        """Re-map raw items of a source; returns (items, errors).

        Datasets that keep history (zone sets, …) re-map every version, oldest first;
        all others only the current version of each item.
        """
        source = self._registry.get(source_id, require_enabled=False)
        items = errors = 0
        for spec in source.datasets():
            if dataset and spec.name != dataset:
                continue
            ids = (
                self._raw.all_ids(source_id, spec.name)
                if spec.keep_history
                else self._raw.current_ids(source_id, spec.name)
            )
            for raw_id in ids:
                items += 1
                errors += self.normalise(raw_id).error is not None
        return items, errors

    def drop(self, source_id: str, dataset: str) -> tuple[int, int, int]:
        """Remove what a dataset's raw items were mapped to: canonical rows, streams and
        wellness files. The raw archive is kept, so ``rebuild`` can bring it all back.
        Returns (raw items, rows, files) removed."""
        source = self._registry.get(source_id, require_enabled=False)
        mapper = source.mappers().get(dataset)
        items = rows = files = 0
        for raw_id in self._raw.all_ids(source_id, dataset):
            items += 1
            if mapper is not None:  # the records name the files they were written to
                try:
                    records = list(mapper(self._raw.load(raw_id)))
                except Exception:
                    records = []
                for record in records:
                    if isinstance(record, Stream):
                        path = self._parquet.stream_path(record.fit_id)
                    elif isinstance(record, WellnessSamples):
                        path = self._parquet.wellness_path(source_id, record.key)
                    else:
                        continue
                    if path.exists():
                        path.unlink()
                        files += 1
            with self._engine.begin() as conn:
                rows += _delete_from(conn, raw_id)
        return items, rows, files

    def _write_files(self, records: list[Record], *, source: str) -> int:
        written = 0
        for record in records:
            if isinstance(record, Stream):
                self._parquet.write_stream(record)
                written += 1
            elif isinstance(record, WellnessSamples):
                self._parquet.write_wellness(source, record)
                written += 1
        return written


def _delete_from(conn: Connection, raw_item_id: str) -> int:
    removed = 0
    for table in _PROVENANCE_TABLES:
        removed += conn.execute(delete(table).where(table.c.raw_item_id == raw_item_id)).rowcount
    return removed


def _row(record: Record, *, source: str, raw_item_id: str, now) -> dict:
    row = {f.name: getattr(record, f.name) for f in dataclasses.fields(record)}
    for key, value in row.items():
        if isinstance(value, tuple):
            row[key] = list(value)
    # icu_event.source is plan|other, not the plugin id. The table is only this source.
    extra: dict = {"raw_item_id": raw_item_id, "updated_at": now}
    if not isinstance(record, IcuEvent):
        extra["source"] = source
    row.update(extra)
    return row


def write_records(
    conn: Connection, records: Iterable[Record], *, source: str, raw_item_id: str
) -> int:
    """Upsert records into their tables; later records win over earlier ones with the same
    key. File-backed records (streams, wellness samples) are skipped here. A CalendarSpan
    then removes the planned items on its dates that this raw item no longer lists."""
    now = utc_now()
    records = list(records)
    grouped: dict[type, dict[tuple, dict]] = {}
    for record in records:
        if type(record) not in TABLES:
            continue
        _, key_cols = TABLES[type(record)]
        row = _row(record, source=source, raw_item_id=raw_item_id, now=now)
        grouped.setdefault(type(record), {})[tuple(row[c] for c in key_cols)] = row
    count = 0
    for record_type, keyed in grouped.items():
        table, key_cols = TABLES[record_type]
        rows = list(keyed.values())
        batch = max(1, _MAX_PARAMS // len(rows[0]))  # stay under SQLite's parameter limit
        for start in range(0, len(rows), batch):
            stmt = insert(table).values(rows[start : start + batch])
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=list(key_cols),
                    set_={c: stmt.excluded[c] for c in rows[0] if c not in key_cols},
                )
            )
        count += len(rows)
    for span in (r for r in records if isinstance(r, CalendarSpan)):
        # Items this reply lists were just upserted with its raw_item_id; the rest are stale.
        covered = planned_item.c.date.between(span.start, span.end)
        if span.include_overlapping:
            covered = or_(
                covered,
                (planned_item.c.date < span.start)
                & (planned_item.c.extra["end"].as_string() > span.start.isoformat()),
            )
        stale = (
            (planned_item.c.source == source)
            & covered
            & (planned_item.c.raw_item_id != raw_item_id)
        )
        has_adaptive = any(
            r.kind == "plan_workout" and span.start <= r.date <= span.end
            for r in records
            if isinstance(r, PlannedItem)
        )
        if source == "garmin" and not has_adaptive:
            # Garmin sometimes omits the entire adaptive batch while the plan remains
            # active. Preserve only known workouts of that plan; a reply containing any
            # adaptive replacements, or an ended/replaced plan, reconciles normally.
            active_plans = (
                conn.execute(
                    select(training_plan.c.source_id).where(
                        training_plan.c.source == source,
                        or_(
                            training_plan.c.status.is_(None),
                            func.lower(training_plan.c.status).notin_(
                                ("completed", "cancelled", "canceled", "ended", "abandoned")
                            ),
                        ),
                        or_(
                            training_plan.c.start_date.is_(None),
                            training_plan.c.start_date <= span.end,
                        ),
                        or_(
                            training_plan.c.end_date.is_(None),
                            training_plan.c.end_date >= span.start,
                        ),
                    )
                )
                .scalars()
                .all()
            )
            if active_plans:
                stale &= ~(
                    (planned_item.c.kind == "plan_workout")
                    & planned_item.c.plan_id.in_(active_plans)
                )
        conn.execute(delete(planned_item).where(stale))
    return count


_MAX_PARAMS = 30000
