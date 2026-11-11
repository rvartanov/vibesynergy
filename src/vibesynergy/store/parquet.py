"""Parquet datasets for high-volume data: activity streams and intraday wellness samples.

Layout under ``<data_dir>/parquet``::

    streams/<fit_id>.parquet                     one file per original FIT file
    wellness/source=<source>/<key>.parquet       one file per raw item (a day, a FIT file)

Files are derived from the raw archive and rewritten whole on re-normalisation, so they can
be deleted and rebuilt at any time. Query them with DuckDB or Polars over the glob.

There are tens of thousands of wellness files (one per monitoring FIT file), so reading one
day doesn't open them all: ``wellness_files`` picks the files whose time range overlaps,
from an index of each file's first and last sample (``wellness/_index.json``, read from the
files' footers and refreshed for new or changed files only).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from vibesynergy.model import Stream, WellnessSamples

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str) -> str:
    return _SAFE.sub("_", name)


INDEX = "_index.json"


class ParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: dict[str, list[int]] = {}  # relative path -> [mtime_ns, size, min, max]
        self._index_stamp: tuple | None = None  # the folders' mtimes when the index was checked
        self._index_lock = threading.Lock()

    def stream_path(self, fit_id: str) -> Path:
        return self.root / "streams" / f"{_safe(fit_id)}.parquet"

    def wellness_path(self, source: str, key: str) -> Path:
        return self.root / "wellness" / f"source={_safe(source)}" / f"{_safe(key)}.parquet"

    def write_stream(self, stream: Stream) -> Path:
        columns = {"t": pa.array(stream.columns["t"], type=pa.timestamp("ms", tz="UTC"))}
        for name, values in sorted(stream.columns.items()):
            if name != "t":
                columns[name] = pa.array(values, type=pa.float64())
        path = self.stream_path(stream.fit_id)
        _write(path, pa.table(columns))
        return path

    def write_wellness(self, source: str, samples: WellnessSamples) -> Path:
        table = pa.table(
            {
                "ts": pa.array(samples.ts, type=pa.timestamp("s", tz="UTC")),
                "metric": pa.array(samples.metric, type=pa.dictionary(pa.int16(), pa.string())),
                "value": pa.array(samples.value, type=pa.float64()),
            }
        )
        path = self.wellness_path(source, samples.key)
        _write(path, table)
        return path

    def read_stream(self, fit_id: str) -> pa.Table:
        return pq.read_table(self.stream_path(fit_id))

    def wellness_files(self, start: datetime, end: datetime) -> list[Path]:
        """The wellness files with samples in [start, end)."""
        root = self.root / "wellness"
        with self._index_lock:
            self._refresh_index(root)
            lo, hi = int(start.timestamp()), int(end.timestamp())
            # Sorted, like a scan of the folder: when two copies of a sample differ, the same
            # one comes first.
            return sorted(root / rel for rel, e in self._index.items() if e[2] < hi and e[3] >= lo)

    def _refresh_index(self, root: Path) -> None:
        if not root.is_dir():
            self._index, self._index_stamp = {}, None
            return
        folders = sorted(p for p in root.iterdir() if p.is_dir())
        stamp = tuple((p.name, p.stat().st_mtime_ns) for p in folders)
        if stamp == self._index_stamp:
            return  # no file added, replaced or removed since the last look
        if not self._index:
            try:
                self._index = json.loads((root / INDEX).read_text())
            except (OSError, ValueError):
                self._index = {}
        fresh: dict[str, list[int]] = {}
        for folder in folders:
            for entry in os.scandir(folder):
                if not entry.name.endswith(".parquet"):
                    continue
                rel = f"{folder.name}/{entry.name}"
                st = entry.stat()
                known = self._index.get(rel)
                if known and known[:2] == [st.st_mtime_ns, st.st_size]:
                    fresh[rel] = known
                elif (span := _ts_span(Path(entry.path))) is not None:
                    fresh[rel] = [st.st_mtime_ns, st.st_size, *span]
        changed = fresh != self._index
        # Folder times are coarse (a few ms): a file written in the same tick as this look
        # wouldn't change them, so trust them only once they are a moment old.
        settled = time.time_ns() - max(m for _, m in stamp) > 2_000_000_000 if stamp else True
        self._index, self._index_stamp = fresh, stamp if settled else None
        if changed:
            tmp = root / f"{INDEX}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(fresh, separators=(",", ":")))
            os.replace(tmp, root / INDEX)


def _ts_span(path: Path) -> tuple[int, int] | None:
    """First and last sample time (epoch seconds) from the file's footer; None when empty."""
    try:
        meta = pq.read_metadata(path)
    except (OSError, pa.ArrowException):
        return None
    column = meta.schema.names.index("ts") if "ts" in meta.schema.names else None
    lows, highs = [], []
    for i in range(meta.num_row_groups):
        group = meta.row_group(i)
        if column is None or group.num_rows == 0:
            continue
        stats = group.column(column).statistics
        if stats is None or not stats.has_min_max:
            return (0, 2**62)  # no statistics: always read it
        lows.append(int(stats.min.timestamp()))
        highs.append(int(stats.max.timestamp()))
    return (min(lows), max(highs)) if lows else None


def _write(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)
