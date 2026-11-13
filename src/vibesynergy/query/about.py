"""About this version: what is running, since when, and the last deploy.

Read-only. The commit comes from the git checkout the package runs from (server runs an
editable install of the service checkout). The last deploy comes from ``deploy.json`` in
the data directory when the deploy script writes one; without it that part is empty.
Only ``updates`` reaches the network, at most hourly and read-only: a ``git fetch`` of ``main``
in a checkout, or for an installed copy ``vibesynergy.updater.check`` (``git ls-remote``).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import UTC, datetime
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

from vibesynergy.core.timeutil import utc_now

REPO_URL = "https://github.com/rvartanov/vibesynergy"
STARTED_AT = utc_now()  # the moment this process imported the module, close to service start
_ROOT = Path(__file__).resolve().parents[3]  # src/vibesynergy/query → repository root


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


@lru_cache(maxsize=1)
def build() -> dict[str, Any]:
    """Package version and the commit this process runs. Cached: it cannot change while
    the process runs (a deploy restarts the service)."""
    try:
        version = metadata.version("vibesynergy")
    except metadata.PackageNotFoundError:
        version = None
    commit = _git("rev-parse", "HEAD")
    if commit is None:
        from vibesynergy import updater

        commit = updater.installed()["commit"]  # an installed copy: the commit it was built from
    committed = _git("log", "-1", "--format=%cI", "HEAD")
    subject = _git("log", "-1", "--format=%s", "HEAD")
    if subject and subject.startswith("Merge pull request"):
        # GitHub's merge commit: the PR title is the first line of the body.
        body = _git("log", "-1", "--format=%b", "HEAD")
        number = subject.split()[3] if len(subject.split()) > 3 else ""
        if body:
            subject = f"{body.splitlines()[0]} ({number})" if number else body.splitlines()[0]
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "version": version,
        "commit": commit,
        "commit_short": commit[:7] if commit else None,
        "commit_date": committed,
        "commit_subject": subject,
        "commit_url": f"{REPO_URL}/commit/{commit}" if commit else None,
        "local_changes": bool(dirty),
    }


_UPDATES: dict[str, Any] = {"at": 0.0, "value": None}
_UPDATES_LOCK = threading.Lock()
UPDATES_SECONDS = 3600
FRESH_SECONDS = 20  # "Check for updates" asks GitHub again, but not more often than this


def updates(now: float | None = None, *, fresh: bool = False) -> dict[str, Any] | None:
    """How far ``main`` on GitHub is ahead of what runs: count and the PR titles. Cached for
    an hour; ``fresh`` (the Check for updates button) asks again. When GitHub can't be
    reached on a fresh check, the last known answer stays."""
    from vibesynergy import updater

    now = time.monotonic() if now is None else now
    with _UPDATES_LOCK:
        keep = FRESH_SECONDS if fresh else UPDATES_SECONDS
        if _UPDATES["value"] is not None and now - _UPDATES["at"] < keep:
            return _UPDATES["value"]
        if updater.installed()["kind"] != "checkout":
            value = updater.check()
            if value is not None:
                _UPDATES.update(at=now, value=value)
            return value if value is not None else _UPDATES["value"]
        if (
            _git("fetch", "-q", "origin", "main") is None
            and _git("rev-parse", "origin/main") is None
        ):
            return _UPDATES["value"]
        log = _git("log", "--merges", "--format=%b", "HEAD..origin/main") or ""
        titles = [line for line in log.splitlines() if line.strip()]
        commits = _git("rev-list", "--count", "HEAD..origin/main")
        value = {
            "behind": int(commits) if commits and commits.isdigit() else 0,
            "titles": titles[:20],
            "checked_at": utc_now().isoformat(),
        }
        _UPDATES.update(at=now, value=value)
        return value


def last_deploy(data_dir: Path) -> dict[str, Any] | None:
    """The deploy script's record, if it left one: ``{"at", "commit", "result", ...}``."""
    path = data_dir / "deploy.json"
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    keep = ("at", "commit", "result", "migrated", "backup", "health")
    return {key: record.get(key) for key in keep if key in record}


# "Update now" on a server run from a git checkout starts this user unit (the one the
# switched-off autodeploy timer starts); it runs vibesynergy-deploy outside this process, so
# it survives the restart. A ``uv tool`` install uses ``updater.UNIT`` instead.
UPDATE_UNIT = "vibesynergy-autodeploy.service"


def _systemctl(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def update_unit(data_dir: Path | None = None) -> dict[str, Any] | None:
    """The update unit's state, or None when this server has none."""
    from vibesynergy import updater

    if updater.installed()["kind"] == "tool":
        state = updater.unit_state()
        if state is None:
            return None
        last = last_deploy(data_dir) if data_dir else None
        result = None if not last else "success" if last.get("result") == "ok" else "failed"
        return {**state, "result": None if state["running"] else result, "finished": None}
    out = _systemctl(
        "show", UPDATE_UNIT, "-p", "LoadState,ActiveState,Result,ExecMainExitTimestamp"
    )
    if not out:
        return None
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if props.get("LoadState") != "loaded":
        return None
    return {
        "unit": UPDATE_UNIT,
        "running": props.get("ActiveState") in ("activating", "active", "reloading"),
        "result": props.get("Result") or None,
        "finished": props.get("ExecMainExitTimestamp") or None,
    }


def last_update_log(data_dir: Path, lines: int = 12) -> dict[str, Any] | None:
    """The newest autodeploy log: when, and its last lines (the backup and the health)."""
    reports = data_dir / "reports"
    logs = sorted(
        [*reports.glob("autodeploy-*.log"), *reports.glob("update-*.log")],
        key=lambda p: p.stat().st_mtime,
    )
    if not logs:
        return None
    try:
        text = logs[-1].read_text(errors="replace")
    except OSError:
        return None
    return {"file": logs[-1].name, "tail": text.splitlines()[-lines:]}


def start_update(rt) -> dict[str, Any]:
    """Start the update unit. Refused without one, while it runs, while a job runs, or when
    there is nothing new."""
    from vibesynergy import updater

    kind = updater.installed()["kind"]
    unit = update_unit(rt.settings.data_dir)
    if unit is None:
        raise ValueError(updater.how(kind) or "This server can't update itself from here.")
    if unit["running"]:
        raise ValueError("An update is already running.")
    running = rt.jobs.counts().get("running", 0)
    if running:
        raise ValueError("A job is running; try again when it has finished.")
    news = updates()
    if news is not None and not news["behind"]:
        raise ValueError("Already up to date.")
    if kind == "tool":
        updater.start()
        return {"started": updater.UNIT, "behind": news["behind"] if news else None}
    if _systemctl("start", "--no-block", UPDATE_UNIT) is None:
        raise ValueError("The update could not be started.")
    return {"started": UPDATE_UNIT, "behind": news["behind"] if news else None}


def about(rt, *, fresh: bool = False) -> dict[str, Any]:
    """Everything the footer and Settings → System → About this version show. ``fresh``
    checks GitHub for updates now instead of using the hourly answer."""
    from vibesynergy import updater
    from vibesynergy.portal.auth_gate import auth_required
    from vibesynergy.store import db

    started = STARTED_AT.astimezone(UTC)
    news = updates(fresh=fresh)
    unit = update_unit(rt.settings.data_dir)
    return {
        **build(),
        "started_at": started.isoformat(),
        "uptime_s": int((datetime.now(UTC) - started).total_seconds()),
        "schema": db.current_revision(rt.engine),
        "schema_head": db.head_revision(),
        "deploy": last_deploy(rt.settings.data_dir),
        "repo_url": REPO_URL,
        "updates": news,
        "update": unit,
        # With the portal login on, the session is the check; without it, the passphrase.
        "confirm": "none" if auth_required(rt.settings) else "passphrase",
        "update_hint": updater.how(updater.installed()["kind"])
        if news and news.get("behind") and unit is None
        else None,
        "update_log": last_update_log(rt.settings.data_dir),
    }
