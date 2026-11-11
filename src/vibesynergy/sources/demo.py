"""Synthetic source used by tests and for trying VibeSynergy without accounts.

Enable with ``[sources.demo] enabled = true``. ``steps_offset`` changes the generated values,
which exercises the raw store's versioning.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Mapping, Sequence

from vibesynergy.core.timeutil import Window
from vibesynergy.model import DailyMetric, Record
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Mapper, Source
from vibesynergy.store.raw import RawRecord


def _seed(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")


class DemoSource(Source):
    id = "demo"
    name = "Demo (synthetic data)"

    def datasets(self) -> Sequence[DatasetSpec]:
        return [
            DatasetSpec(
                "daily",
                DatasetKind.DAILY,
                "Synthetic daily steps and resting heart rate",
                initial_days=14,
                refetch_days=1,
            ),
            DatasetSpec("profile", DatasetKind.SNAPSHOT, "Synthetic athlete profile"),
        ]

    def fetch(self, dataset: DatasetSpec, window: Window | None) -> Iterator[FetchedItem]:
        if dataset.name == "profile":
            yield FetchedItem("profile", {"name": "Demo Athlete", "ftp_w": 250, "max_hr": 188})
            return
        assert window is not None
        offset = int(self.ctx.config.get("steps_offset", 0))
        for day in window.days():
            seed = _seed(day.isoformat())
            yield FetchedItem(
                item_key=day.isoformat(),
                payload={
                    "date": day.isoformat(),
                    "steps": 4000 + seed % 8000 + offset,
                    "resting_hr": 44 + seed % 9,
                },
                request={"endpoint": "demo/daily", "date": day.isoformat()},
                window=Window(day, day),
            )

    def mappers(self) -> Mapping[str, Mapper]:
        return {"daily": map_daily}


def map_daily(raw: RawRecord) -> Iterable[Record]:
    data = raw.json()
    day = Window.parse(data["date"]).start
    yield DailyMetric(day, "daily.steps", float(data["steps"]), "count")
    yield DailyMetric(day, "hr.resting", float(data["resting_hr"]), "bpm")
