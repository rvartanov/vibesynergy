"""Composition root: builds the object graph shared by the CLI, worker, API and MCP."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine

from vibesynergy.core.config import Settings
from vibesynergy.normalize.normaliser import Normaliser
from vibesynergy.sources.base import Source
from vibesynergy.sources.registry import SourceRegistry
from vibesynergy.store import db
from vibesynergy.store.blobs import BlobStore
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.raw import RawStore
from vibesynergy.sync.engine import SyncEngine
from vibesynergy.sync.jobs import JobQueue
from vibesynergy.sync.worker import Worker, default_handlers


@dataclass
class Runtime:
    settings: Settings
    engine: Engine
    raw: RawStore
    registry: SourceRegistry
    parquet: ParquetStore
    normaliser: Normaliser
    sync: SyncEngine
    jobs: JobQueue
    scheduler: Any = None

    @classmethod
    def open(
        cls, settings: Settings, *, source_classes: Mapping[str, type[Source]] | None = None
    ) -> Runtime:
        engine = db.create_db_engine(settings.db_path)
        raw = RawStore(engine, BlobStore(settings.blob_dir))
        registry = SourceRegistry(settings, source_classes, raw=raw)
        parquet = ParquetStore(settings.parquet_dir)
        normaliser = Normaliser(engine, raw, registry, parquet)
        rt = cls(
            settings=settings,
            engine=engine,
            raw=raw,
            registry=registry,
            parquet=parquet,
            normaliser=normaliser,
            sync=SyncEngine(settings, engine, raw, registry, normaliser),
            jobs=JobQueue(engine),
        )
        rt.sync.on_change = rt.derive
        rt.sync.on_score = rt.score_predictions
        return rt

    def score_predictions(self) -> int:
        """Fill prediction outcomes from daily values. Safe to run more than once."""
        from vibesynergy.query.predictions import score_due

        return score_due(self.engine)

    def derive(self) -> None:
        """Rebuild the unified activity and daily tables (dedup, best-source rules)."""
        from vibesynergy.query import best_source

        best_source.rebuild(self.engine, self.settings)
        from vibesynergy.query import latency

        latency.processed(self.engine)
        from vibesynergy.query import journal

        journal.import_activity_notes(self.engine)
        from vibesynergy.query import plan_model

        plan_model.shadow_refresh(self.engine)
        from vibesynergy.query import agent_events

        agent_events.after_derive(self)

    def worker(self, *, lane: str = "all") -> Worker:
        from vibesynergy import alerts, dropfolder, history
        from vibesynergy.coach_runner import schedule as coach_schedule
        from vibesynergy.query import (
            agent_events,
            calendar_mirror,
            garmin_strength,
            publication_auto,
            publication_mirror,
        )

        handlers = default_handlers(self.sync, self.normaliser)
        handlers[agent_events.JOB] = lambda _job: agent_events.run(self)
        handlers["alerts"] = lambda _job: alerts.check(self)
        handlers[publication_mirror.JOB] = lambda _job: publication_mirror.run(self)
        handlers[publication_auto.JOB] = lambda _job: publication_auto.run(self)
        handlers[calendar_mirror.JOB] = lambda _job: calendar_mirror.run(self)
        handlers[garmin_strength.JOB] = lambda _job: garmin_strength.run(self)
        from vibesynergy.query import quote_sources

        handlers[quote_sources.JOB] = lambda _job: quote_sources.run(self)
        handlers["dropfolder"] = lambda _job: dropfolder.scan(self)
        from vibesynergy.query import plan_model

        handlers[plan_model.MIRROR_JOB] = lambda _job: plan_model.run_mirror(self.engine)
        handlers[coach_schedule.SCHEDULE_JOB] = lambda _job: coach_schedule.enqueue_due(self)
        handlers[coach_schedule.JOB] = lambda job: coach_schedule.handle(self, job)
        handlers[history.JOB] = lambda job: history.run_step(self, job.payload["fill"])
        handlers[history.ARCHIVE_JOB] = lambda job: history.import_archive(
            self, job.payload["fill"], attempt=job.attempts, max_attempts=job.max_attempts
        )
        if lane not in {"all", "sync", "webhook", "archive", "coach"}:
            raise ValueError("Unknown worker lane")
        # Strava notifications are fetch_one/tombstone. Their own lane claims them while a
        # source sync is already running; the sync lane can still claim them when it is free.
        # Coach runs take minutes, so they have a lane of their own and the sync lane skips them.
        webhook_kinds = ("fetch_one", "tombstone")
        kinds = {
            "archive": (history.ARCHIVE_JOB,),
            "webhook": webhook_kinds,
            "coach": coach_schedule.LANE_KINDS,
        }.get(lane)
        return Worker(
            self.jobs,
            handlers,
            kinds=kinds,
            exclude_kinds=(history.ARCHIVE_JOB, *coach_schedule.LANE_KINDS)
            if lane == "sync"
            else (),
        )

    def migrate(self) -> None:
        db.upgrade(self.engine)

    def schema_is_current(self) -> bool:
        return db.current_revision(self.engine) == db.head_revision()

    def close(self) -> None:
        self.engine.dispose()
