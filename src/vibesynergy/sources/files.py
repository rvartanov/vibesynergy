"""Imported activity files: old head units, other devices, anything without an API.

``vibesynergy import files <dir or file>`` stores each FIT file by content hash. Files have no
API summary, so the activity row (and laps) come from the FIT file itself, keyed by its
fit_id. Files already archived by another route (the Garmin export) are skipped by hash.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from vibesynergy.core.timeutil import Window
from vibesynergy.fit.decode import map_fit
from vibesynergy.model import Record
from vibesynergy.sources.base import DatasetKind, DatasetSpec, Mapper, Source
from vibesynergy.sources.tracks import map_track
from vibesynergy.store.raw import RawRecord


class FilesSource(Source):
    id = "files"
    name = "Imported files (import only)"

    def datasets(self) -> Sequence[DatasetSpec]:
        return [
            DatasetSpec("fit", DatasetKind.FILE, "FIT files, by content hash"),
            DatasetSpec("gpx", DatasetKind.FILE, "GPX tracks, by content hash"),
            DatasetSpec("tcx", DatasetKind.FILE, "TCX activities, by content hash"),
        ]

    def fetch(self, dataset: DatasetSpec, window: Window | None):
        return iter(())  # import only

    def mappers(self) -> Mapping[str, Mapper]:
        timezone = self.ctx.settings.timezone

        def fit(raw: RawRecord) -> Iterable[Record]:
            return map_fit(raw.payload, timezone=timezone, with_activity=True)

        def track(raw: RawRecord) -> Iterable[Record]:
            return map_track(raw.payload, f"{raw.dataset}-{raw.item_key[:16]}", timezone=timezone)

        return {"fit": fit, "gpx": track, "tcx": track}
