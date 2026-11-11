"""The drop folder: activity files put there (or uploaded in the portal) are imported and
merged with the copies VibeSynergy already has. Checked every few minutes by the service.

``[dropfolder] dir`` (default ``<data dir>/inbox``). Each file is moved afterwards: to
``imported/`` or, when it can't be read, to ``failed/`` with a ``.error.txt`` saying why.
Files changed in the last seconds are left for the next check (still being copied).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from vibesynergy.core.timeutil import utc_now

log = logging.getLogger(__name__)

DROP_MINUTES = 5
SETTLE_S = 20  # a file must be unchanged this long before it is imported
MAX_BYTES = 200 * 1024 * 1024
HISTORY = ".history.json"
HISTORY_KEPT = 50


def folder(settings: Any) -> Path:
    configured = settings.dropfolder.get("dir")
    path = Path(configured).expanduser() if configured else settings.data_dir / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def delete_archive(settings: Any, path: Path) -> bool:
    """Unlink only a regular zip in an app upload folder, without following links."""
    base = folder(settings).absolute()
    if path.suffix.lower() != ".zip" or path.parent.absolute() not in (
        base,
        base / "imported",
        base / "failed",
    ):
        return False
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return False
            os.unlink(path.name, dir_fd=fd)
        except FileNotFoundError:
            return False
    finally:
        os.close(fd)
    return True


def scan(rt: Any, *, settle_s: float = SETTLE_S) -> list[dict[str, Any]]:
    """Import every settled file in the drop folder; returns what happened to each."""
    from vibesynergy import history
    from vibesynergy.imports import archive_kind, import_one  # runtime pieces; keep module light

    base = folder(rt.settings)
    results = []
    known = {f["file"] for f in history.fills(rt)}
    for path in sorted(base.iterdir()):
        if path.is_symlink() or not path.is_file() or path.name.startswith("."):
            continue
        if time.time() - path.stat().st_mtime < settle_s:
            continue
        entry: dict[str, Any] = {"file": path.name, "at": utc_now().isoformat()}
        if path.suffix.lower() == ".zip" and (kind := archive_kind(path)):
            # An account export: imported by the history filler, in the background.
            if str(path) not in known:
                history.start_archive(rt, path, kind)
                entry.update(status="archive import started", imported=0)
                results.append(entry)
            continue
        if path.suffix.lower() == ".zip" and not zipfile.is_zipfile(path):
            delete_archive(rt.settings, path)
            entry.update(status="failed", error="Unreadable archive removed. Upload it again.")
            results.append(entry)
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ValueError(f"larger than {MAX_BYTES // (1024 * 1024)} MB")
            entry.update(import_one(rt, path.name, path.read_bytes()))
            entry["status"] = "imported" if entry["imported"] else "nothing new"
            if path.suffix.lower() == ".zip":
                delete_archive(rt.settings, path)
            else:
                _move(path, base / "imported")
        except Exception as exc:
            if path.suffix.lower() == ".zip":
                log.error("archive import failed; uploaded file removed")
                entry.update(status="failed", error="Import failed; file removed. Upload it again.")
                delete_archive(rt.settings, path)
            else:
                log.warning("drop folder: %s failed: %s", path.name, exc)
                entry.update(status="failed", error=str(exc)[:300])
                move_failed(path, str(exc))
        results.append(entry)
    if results:
        _remember(base, results)
    return results


def history(settings: Any) -> list[dict[str, Any]]:
    path = folder(settings) / HISTORY
    return json.loads(path.read_text()) if path.exists() else []


def upload_path(settings: Any, name: str) -> Path:
    """Where an upload of this name goes in the drop folder: a safe name, never an existing
    file."""
    safe = "".join(c for c in Path(name).name if c.isalnum() or c in "._- ").strip() or "upload"
    target = folder(settings) / safe
    if target.exists():
        target = target.with_name(f"{target.stem}-{datetime.now():%Y%m%d%H%M%S}{target.suffix}")
    return target


def move_failed(path: Path, reason: str) -> Path:
    """Move a file to failed/, with a .error.txt next to it saying why."""
    target = _move(path, path.parent / "failed")
    target.with_name(target.name + ".error.txt").write_text(reason)
    return target


def _move(path: Path, directory: Path) -> Path:
    directory.mkdir(exist_ok=True)
    target = directory / path.name
    if target.exists():
        target = directory / f"{path.stem}-{datetime.now():%Y%m%d%H%M%S}{path.suffix}"
    path.rename(target)
    return target


def _remember(base: Path, results: list[dict[str, Any]]) -> None:
    path = base / HISTORY
    old = json.loads(path.read_text()) if path.exists() else []
    path.write_text(json.dumps((results[::-1] + old)[:HISTORY_KEPT]))
