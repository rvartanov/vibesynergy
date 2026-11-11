"""Resumable uploads of account exports (several GB, possibly from a phone over the tailnet).

The browser sends the file in chunks, each at the offset the server already has, so an
interrupted upload (a dropped connection, a closed laptop, a reload) continues where it
stopped instead of starting again. The parts live in ``<drop folder>/.uploads`` (the drop
folder skips dot folders) and are removed when finished or after a week untouched.

    start(name, size)            -> id
    status(id)                   -> bytes received so far
    append(id, offset, chunks)   -> bytes received (refused when offset isn't that)
    finish(id)                   -> the complete file, moved into the drop folder
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from vibesynergy import dropfolder

MAX_BYTES = 200 * 1024**3  # an account export can be many GB; free disk space is the real limit
# Keep this much free after the upload, so an export can't fill the disk (the import itself
# reads the zip in place, plus room for the files it adds).
SPARE_BYTES = 5 * 1024**3


class NotEnoughSpace(ValueError):
    """The disk can't take the file and still keep ``SPARE_BYTES`` free."""

    def __init__(self, short: int) -> None:
        super().__init__(f"not enough free space: free up {short} bytes")
        self.short = short


def check_space(path: Path, size: int) -> None:
    """Refuse an upload of ``size`` bytes into ``path``'s disk when it doesn't fit."""
    free = shutil.disk_usage(path).free
    if size + SPARE_BYTES > free:
        raise NotEnoughSpace(size + SPARE_BYTES - free)


KEPT_S = 7 * 24 * 3600
_ID = re.compile(r"[0-9a-f]{16}")
_BUSY: set[str] = set()  # uploads receiving a chunk right now (one event loop: no lock)


class OffsetMismatch(ValueError):
    """The chunk doesn't start where the upload is; ``received`` says where it is."""

    def __init__(self, received: int) -> None:
        super().__init__(f"the upload is at byte {received}")
        self.received = received


def start(settings: Any, name: str, size: int) -> dict[str, Any]:
    if not name.lower().endswith(".zip"):
        raise ValueError("choose the export's .zip file")
    if not 0 < size <= MAX_BYTES:
        raise ValueError(f"the file must be between 1 byte and {MAX_BYTES // 1024**3} GB")
    base = _dir(settings)
    check_space(base, size)
    upload_id = secrets.token_hex(8)
    (base / f"{upload_id}.json").write_text(json.dumps({"name": name, "size": size}))
    (base / f"{upload_id}.part").touch()
    return {"id": upload_id, "received": 0, "size": size}


def status(settings: Any, upload_id: str) -> dict[str, Any]:
    meta, part = _open(settings, upload_id)
    return {"id": upload_id, "received": part.stat().st_size, "size": meta["size"]}


async def append(
    settings: Any, upload_id: str, offset: int, chunks: AsyncIterator[bytes]
) -> dict[str, Any]:
    meta, part = _open(settings, upload_id)
    received = part.stat().st_size
    # One chunk at a time: a retry can arrive while the server still receives the chunk the
    # browser gave up on; it's told where the upload is and tries again.
    if offset != received or upload_id in _BUSY:
        raise OffsetMismatch(received)
    _BUSY.add(upload_id)
    try:
        with part.open("ab") as fh:
            async for chunk in chunks:
                if received + len(chunk) > meta["size"]:
                    fh.truncate(offset)  # the whole chunk or nothing
                    raise ValueError("more bytes than the file has")
                fh.write(chunk)
                received += len(chunk)
    finally:
        _BUSY.discard(upload_id)
    return {"id": upload_id, "received": received, "size": meta["size"]}


def finish(settings: Any, upload_id: str) -> Path:
    meta, part = _open(settings, upload_id)
    if part.stat().st_size != meta["size"]:
        raise ValueError(f"the upload has {part.stat().st_size} of {meta['size']} bytes")
    target = dropfolder.upload_path(settings, meta["name"])
    part.rename(target)
    (part.with_suffix(".json")).unlink(missing_ok=True)
    return target


def cancel(settings: Any, upload_id: str) -> None:
    _, part = _open(settings, upload_id)
    part.unlink(missing_ok=True)
    part.with_suffix(".json").unlink(missing_ok=True)


def clean(settings: Any, now: float | None = None) -> int:
    """Remove uploads nobody touched for a week; returns how many."""
    now = now or time.time()
    removed = 0
    for meta in _dir(settings).glob("*.json"):
        part = meta.with_suffix(".part")
        touched = max(p.stat().st_mtime for p in (meta, part) if p.exists())
        if now - touched > KEPT_S:
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            removed += 1
    return removed


def _dir(settings: Any) -> Path:
    path = dropfolder.folder(settings) / ".uploads"
    path.mkdir(exist_ok=True)
    return path


def _open(settings: Any, upload_id: str) -> tuple[dict[str, Any], Path]:
    if not _ID.fullmatch(upload_id or ""):
        raise ValueError("unknown upload")
    base = _dir(settings)
    meta, part = base / f"{upload_id}.json", base / f"{upload_id}.part"
    if not meta.exists() or not part.exists():
        raise ValueError("unknown upload (finished, cancelled, or older than a week)")
    return json.loads(meta.read_text()), part
