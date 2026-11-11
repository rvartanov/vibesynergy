"""The contract every source plugin implements.

A source describes its datasets, fetches raw items for a date window, and supplies mappers
that turn stored raw items into canonical records. Fetching and mapping are separate so the
canonical store can be rebuilt from the raw archive after a mapper fix.

Sources register through the ``vibesynergy.sources`` entry point group.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, ClassVar

from vibesynergy.core.config import Settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import Window
from vibesynergy.model import Record
from vibesynergy.store.raw import RawRecord, RawStore


class DatasetKind(StrEnum):
    DAILY = "daily"  # one item per calendar day
    RANGE = "range"  # items covering a date range, fetched per window
    ENTITY = "entity"  # discrete objects found within a window (activities, ECGs)
    SNAPSHOT = "snapshot"  # current state, no window (profile, gear, devices)
    FILE = "file"  # arrives through ``vibesynergy import``, never fetched by sync


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    kind: DatasetKind
    description: str
    initial_days: int = 7  # first incremental sync looks back this far; history is a backfill
    refetch_days: int = 0  # sources revise recent data; re-fetch this many days each sync
    history_start: date | None = None
    keep_history: bool = False  # every version stays mapped (zone changes), not just the latest
    default_enabled: bool = True  # off-by-default datasets are enabled per source in config
    lookahead_days: int = 0  # also fetch this far ahead of today (a training calendar)
    lookahead_months: int | None = None  # when set, the window ends this many months ahead
    min_age_minutes: int = 0  # a plain source sync leaves it alone until this old (rate limits)
    max_days: int | None = None  # a sync never reaches back further; older history is imported


@dataclass(frozen=True)
class FetchedItem:
    """One logical item. ``payload`` may be a zero-argument callable, which the engine calls
    only when it needs the content; with ``immutable`` set, an item already in the archive
    is not fetched again (original files never change)."""

    item_key: str
    payload: Any  # bytes, JSON-serialisable data, or a callable returning either
    request: Mapping[str, Any] = field(default_factory=dict)
    window: Window | None = None
    content_type: str | None = None  # defaults from the payload type
    immutable: bool = False
    revision: str | None = None  # cheap token; the same one as archived means: don't fetch again

    def resolve(self) -> Any:
        return self.payload() if callable(self.payload) else self.payload


@dataclass(frozen=True)
class SourceContext:
    settings: Settings
    config: Mapping[str, Any]
    secrets: SecretStore
    log: logging.Logger
    raw: RawStore | None = None


Mapper = Callable[[RawRecord], Iterable[Record]]


class Source(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    live_config: ClassVar[bool] = False  # enabled/selection set in the portal: read from disk

    def __init__(self, ctx: SourceContext) -> None:
        self.ctx = ctx

    @abstractmethod
    def datasets(self) -> Sequence[DatasetSpec]: ...

    @abstractmethod
    def fetch(self, dataset: DatasetSpec, window: Window | None) -> Iterator[FetchedItem]: ...

    def mappers(self) -> Mapping[str, Mapper]:
        return {}

    def fetch_one(self, dataset: str, key: str) -> Iterator[FetchedItem]:
        """Fetch a single item by key, for webhook events. Optional."""
        raise NotImplementedError(f"source {self.id!r} cannot fetch single items")

    def configured(self) -> bool:
        """Whether the source is set up well enough to sync (calendar chosen, login stored).
        Unconfigured sources are never scheduled and queued syncs for them end quietly."""
        return True

    def authenticate(self) -> None:
        """Interactive or token-based login; sources without auth need not override."""
        return None

    def dataset(self, name: str) -> DatasetSpec:
        for spec in self.datasets():
            if spec.name == name:
                return spec
        raise KeyError(f"source {self.id!r} has no dataset {name!r}")

    def keeps_history(self, name: str) -> bool:
        return any(spec.name == name and spec.keep_history for spec in self.datasets())

    def sync_datasets(self) -> list[DatasetSpec]:
        """Datasets a plain ``vibesynergy sync`` collects: fetchable and enabled.

        ``[sources.<id>] datasets = [...]`` limits the set; ``extra_datasets = [...]`` adds
        datasets that are off by default (e.g. nutrition, menstrual data).
        """
        only = self.ctx.config.get("datasets")
        extra = set(self.ctx.config.get("extra_datasets", []))
        return [
            spec
            for spec in self.datasets()
            if spec.kind is not DatasetKind.FILE
            and (spec.name in only if only else spec.default_enabled or spec.name in extra)
        ]
