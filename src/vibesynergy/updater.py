"""Updates: is a newer VibeSynergy on GitHub, and installing it.

How VibeSynergy was installed decides both:

- ``tool``: ``uv tool install git+https://github.com/…`` (the README's way). The installed
  commit is in the package's ``direct_url.json``; the newest one comes from ``git ls-remote``,
  which uses the same git access as the install (so a private repository works too).
  ``vibesynergy update`` backs up the database, runs ``uv tool upgrade vibesynergy``, applies
  the migrations with the new code and restarts the user service.
- ``checkout``: a git checkout (a developer's, or a server run from one): ``query.about``
  fetches ``main`` there; updating is ``git pull`` plus ``uv sync`` by hand.
- ``container``: the image says which commit it holds (``VIBESYNERGY_COMMIT``); updating is
  pulling the new image.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

from vibesynergy.core.timeutil import utc_now

BRANCH = "main"
DEFAULT_URL = "https://github.com/rvartanov/vibesynergy"
SERVICE = "vibesynergy.service"
UNIT = "vibesynergy-update"  # the transient unit "Update now" runs in (survives the restart)
KEEP_BACKUPS = 3


def _run(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _direct_url() -> dict[str, Any]:
    try:
        text = metadata.distribution("vibesynergy").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return {}
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _uv_tool() -> bool:
    """Running from a ``uv tool`` environment (it leaves a receipt next to the venv)."""
    return (Path(sys.prefix) / "uv-receipt.toml").exists()


def installed(root: Path | None = None) -> dict[str, Any]:
    """How this copy was installed: kind, the git URL it came from, and its commit."""
    if os.environ.get("VIBESYNERGY_CONTAINER") == "1":
        return {
            "kind": "container",
            "url": os.environ.get("VIBESYNERGY_SOURCE") or DEFAULT_URL,
            "commit": os.environ.get("VIBESYNERGY_COMMIT") or None,
        }
    root = root or Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        return {"kind": "checkout", "url": DEFAULT_URL, "commit": None}
    direct = _direct_url()
    vcs = direct.get("vcs_info") or {}
    if vcs.get("vcs") == "git" and direct.get("url"):
        return {
            "kind": "tool" if _uv_tool() else "other",
            "url": str(direct["url"]),
            "commit": vcs.get("commit_id"),
        }
    return {"kind": "other", "url": DEFAULT_URL, "commit": None}


def latest(url: str) -> str | None:
    """The newest commit on ``main`` of the repository VibeSynergy was installed from."""
    out = _run(["git", "ls-remote", url, f"refs/heads/{BRANCH}"], timeout=20)
    if out is None or out.returncode != 0 or not out.stdout.strip():
        return None
    sha = out.stdout.split()[0]
    return sha if len(sha) == 40 else None


def _titles(url: str, base: str, head: str) -> list[str] | None:
    """What changed, from GitHub (merge commits carry the PR title). None when GitHub won't
    say (a private repository without a token, or no network): the update still shows."""
    if not url.startswith("https://github.com/"):
        return None
    repo = url.removeprefix("https://github.com/").removesuffix(".git").strip("/")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/compare/{base}...{head}",
        headers={"accept": "application/vnd.github+json", "user-agent": "vibesynergy"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            data = json.loads(reply.read() or b"{}")
    except (OSError, ValueError):
        return None
    titles = []
    for commit in data.get("commits") or []:
        message = str((commit.get("commit") or {}).get("message") or "")
        lines = [line for line in message.splitlines() if line.strip()]
        if lines and lines[0].startswith("Merge pull request") and len(lines) > 1:
            titles.append(lines[1])
    return titles


def check(*, get_latest: Callable[[str], str | None] = latest) -> dict[str, Any] | None:
    """For a tool install or a container: is a newer commit on GitHub. None for a checkout
    (``query.about`` checks that itself) or when GitHub can't be reached."""
    info = installed()
    if info["kind"] == "checkout":
        return None
    head = get_latest(info["url"])
    if head is None or not info["commit"]:
        return None
    available = head != info["commit"]
    titles = (_titles(info["url"], info["commit"], head) or []) if available else []
    return {
        "behind": (len(titles) or 1) if available else 0,
        "count_known": bool(titles) or not available,
        "titles": titles[:20],
        "latest": head,
        "kind": info["kind"],
        "checked_at": utc_now().isoformat(),
    }


def how(kind: str) -> str | None:
    """How to update when this server can't do it itself."""
    return {
        "container": "Pull the newest image and start the container again.",
        "checkout": "In the checkout: git pull, uv sync, vibesynergy db upgrade, then restart.",
        "other": "Install it again from GitHub: uv tool install git+https://github.com/rvartanov/vibesynergy",
    }.get(kind)


# --- updating a tool install -----------------------------------------------------------------


def _uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"):
        if candidate.exists():
            return str(candidate)
    return None


def _backup(db_path: Path, folder: Path) -> Path:
    """An online copy of the database (safe while the service writes), and only the newest
    few of these kept."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"before-update-{utc_now().strftime('%Y%m%d-%H%M%S')}.sqlite"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        with dest:
            source.backup(dest)
        dest.close()
    finally:
        source.close()
    for old in sorted(folder.glob("before-update-*.sqlite"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def _service_active() -> bool:
    out = _run(["systemctl", "--user", "is-active", SERVICE], timeout=10)
    return bool(out and out.stdout.strip() in ("active", "activating", "reloading"))


def run(settings: Any, *, restart: bool, say: Callable[[str], None] = print) -> dict[str, Any]:
    """Update a tool install: backup, ``uv tool upgrade``, migrations, restart. Writes the
    result to ``deploy.json`` and a log to ``reports/update-*.log``; returns the record."""
    info = installed()
    if info["kind"] != "tool":
        raise ValueError(how(info["kind"]) or "This install can't update itself.")
    uv = _uv()
    if uv is None:
        raise ValueError("uv is not found; update with: uv tool upgrade vibesynergy")
    lines: list[str] = []

    def note(text: str) -> None:
        lines.append(f"{utc_now().isoformat(timespec='seconds')} {text}")
        say(text)

    record: dict[str, Any] = {"at": utc_now().isoformat(), "result": "failed", "migrated": False}
    try:
        backup = _backup(settings.db_path, settings.data_dir / "backups")
        record["backup"] = str(backup)
        note(f"Database backed up to {backup}")
        upgrade = _run([uv, "tool", "upgrade", "vibesynergy"], timeout=900)
        if upgrade is None or upgrade.returncode != 0:
            raise RuntimeError(
                f"uv tool upgrade failed: {(upgrade.stderr if upgrade else '')[-500:]}"
            )
        note((upgrade.stderr or upgrade.stdout or "upgraded").strip().splitlines()[-1])
        command = Path(sys.prefix) / "bin" / "vibesynergy"
        migrate = _run([str(command), "db", "upgrade"], timeout=900)
        if migrate is None or migrate.returncode != 0:
            output = (migrate.stdout if migrate else "")[-500:]
            raise RuntimeError(f"db upgrade failed (the backup is {backup}): {output}")
        record["migrated"] = True
        note("Database up to date")
        after = _run([str(command), "version"], timeout=60)
        record["commit"] = _new_commit()
        if after is not None:
            note(after.stdout.strip() or "new version installed")
        if restart and _service_active():
            note("Restarting the service")
            _run(["systemctl", "--user", "restart", SERVICE], timeout=120)
            time.sleep(5)
            if not _service_active():
                raise RuntimeError("the service did not come back after the restart")
            note("The service runs again")
        elif restart:
            note("No running user service found: restart VibeSynergy yourself.")
        record["result"] = "ok"
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        note(f"Update failed: {exc}")
    finally:
        _write(settings, record, lines)
    return record


def _new_commit() -> str | None:
    """The commit the upgrade installed (read fresh: this process still runs the old code)."""
    receipt = list(Path(sys.prefix).glob("lib/python*/site-packages/vibesynergy-*.dist-info"))
    for dist in receipt:
        try:
            data = json.loads((dist / "direct_url.json").read_text())
        except (OSError, ValueError):
            continue
        commit = (data.get("vcs_info") or {}).get("commit_id")
        if commit:
            return commit
    return None


def _write(settings: Any, record: dict[str, Any], lines: list[str]) -> None:
    reports = settings.data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    (reports / f"update-{stamp}.log").write_text("\n".join(lines) + "\n")
    target = settings.data_dir / "deploy.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.replace(tmp, target)


# --- "Update now" from the portal ------------------------------------------------------------


def unit_state() -> dict[str, Any] | None:
    """The running update, if any (the transient unit is gone once it has finished)."""
    out = _run(["systemctl", "--user", "show", f"{UNIT}.service", "-p", "ActiveState"], 10)
    if out is None or out.returncode != 0:
        return None
    state = out.stdout.strip().partition("=")[2]
    return {"unit": UNIT, "running": state in ("activating", "active", "reloading")}


def start() -> None:
    """Run ``vibesynergy update --yes --restart`` in its own transient user unit, so the
    restart of the service does not end it."""
    command = Path(sys.prefix) / "bin" / "vibesynergy"
    out = _run(
        [
            "systemd-run",
            "--user",
            f"--unit={UNIT}",
            "--collect",
            "--quiet",
            str(command),
            "update",
            "--yes",
            "--restart",
        ],
        timeout=20,
    )
    if out is None or out.returncode != 0:
        detail = (out.stderr if out else "systemd-run is not available").strip()
        raise ValueError(f"The update could not be started: {detail[-200:]}")
