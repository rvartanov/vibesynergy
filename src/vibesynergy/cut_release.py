"""Making a release when ``vibesynergy update`` installs merged pull requests (#140).

Before the upgrade, in a fresh shallow clone of ``main``:

1. ``main`` already carries a tag ``v<version>`` (an earlier update made the release and then
   failed, say): no new number; that version is installed.
2. Otherwise the next number (``releases.next_version``) with today's date and every item no
   release has yet goes on top of ``RELEASES`` in ``releases.py``, ``## Unreleased`` in
   CHANGELOG.md gets the version and date as its heading, and that is one commit on ``main``
   with the tag ``v<version>``, pushed together. ``uv tool upgrade`` then installs that commit.

At most one release a day (#205): when the newest release already has today's date, the
update installs ``main`` without a new number, and what it brings waits for the next day's
release (``urgent`` makes one anyway, for an urgent fix). Several pull requests merged since
the last release therefore make one release. Making it
needs the right to push to the repository (the server's git credentials); an install
without it updates as before and keeps the number it has.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

from vibesynergy import releases as running

RELEASES_PY = "src/vibesynergy/releases.py"
CHANGELOG = "CHANGELOG.md"
AUTHOR = ("-c", "user.name=VibeSynergy", "-c", "user.email=info@vibesynergy.nl")

Git = Callable[[list[str], Path | None], subprocess.CompletedProcess[str]]


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def load(path: Path) -> ModuleType:
    """The releases module of a checkout, without importing the rest of VibeSynergy."""
    spec = importlib.util.spec_from_file_location("_vibesynergy_releases_main", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot read {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _tag_at(url: str, head: str, git: Git) -> str | None:
    """The version tagged on ``head``, if any."""
    out = git(["ls-remote", "--tags", url, "v*"], None)
    if out.returncode != 0:
        raise RuntimeError(f"git ls-remote --tags failed: {out.stderr.strip()[-300:]}")
    for line in out.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if sha == head and ref.startswith("refs/tags/v"):
            return ref.removeprefix("refs/tags/v").removesuffix("^{}")
    return None


def _attempt(url: str, today: date, git: Git, work: Path, urgent: bool) -> dict[str, Any]:
    repo = work / "main"
    out = git(["clone", "--quiet", "--depth", "1", "--branch", "main", url, str(repo)], None)
    if out.returncode != 0:
        raise RuntimeError(f"git clone failed: {out.stderr.strip()[-300:]}")
    head = git(["rev-parse", "HEAD"], repo).stdout.strip()
    module = load(repo / RELEASES_PY)
    tagged = _tag_at(url, head, git)
    if tagged:
        release = module.RELEASES[0]
        return {"version": tagged, "new": False, "items": _texts(module, release)}
    if module.RELEASES and module.RELEASES[0].day == today and not urgent:
        raise _AlreadyToday(module.RELEASES[0].version)
    version = module.next_version(module.VERSION)
    ids = [item.id for item in module.unreleased()]
    source = repo / RELEASES_PY
    source.write_text(module.stamp_source(source.read_text(), version, today, ids))
    changelog = repo / CHANGELOG
    changelog.write_text(module.stamp_changelog(changelog.read_text(), version, today))
    for name, args in (
        ("add", ["add", RELEASES_PY, CHANGELOG]),
        ("commit", [*AUTHOR, "commit", "--quiet", "-m", f"Release {version}"]),
        ("tag", [*AUTHOR, "tag", "-a", f"v{version}", "-m", f"VibeSynergy {version}"]),
    ):
        out = git(args, repo)
        if out.returncode != 0:
            raise RuntimeError(f"git {name} failed: {out.stderr.strip()[-300:]}")
    out = git(["push", "--atomic", "origin", "HEAD:refs/heads/main", f"refs/tags/v{version}"], repo)
    if out.returncode != 0:
        detail = out.stderr.strip()[-300:]
        if "rejected" in out.stderr or "fetch first" in out.stderr:
            raise _Rejected(detail)
        raise RuntimeError(f"git push failed: {detail}")
    by_id = {item.id: item for item in module.ITEMS}
    return {"version": version, "new": True, "items": [by_id[i].text for i in ids]}


class _AlreadyToday(RuntimeError):
    """Today has its release already: this update waits for the next one."""


class _Rejected(RuntimeError):
    """``main`` moved while the release was made: try once more on the new ``main``."""


def _texts(module: ModuleType, release: Any) -> list[str]:
    by_id = {item.id: item for item in module.ITEMS}
    return [by_id[i].text for i in release.items if i in by_id]


def cut(
    url: str,
    *,
    today: date,
    say: Callable[[str], None] = print,
    git: Git = _git,
    urgent: bool = False,
) -> dict[str, Any] | None:
    """Make the release for what ``main`` has now. Returns ``{"from", "to", "new", "items"}``,
    or None when there is none (today has its release, no push right, no network): the update
    then goes on without one."""
    for attempt in (1, 2):
        work = Path(tempfile.mkdtemp(prefix="vibesynergy-release-"))
        try:
            made = _attempt(url, today, git, work, urgent)
        except _AlreadyToday as exc:
            say(
                f"No new version number: {exc} is today's release. These changes go into the "
                "next one (vibesynergy update --release makes one now, for an urgent fix)."
            )
            return None
        except _Rejected as exc:
            if attempt == 2:
                say(f"No new version number: main kept moving ({exc})")
                return None
            continue
        except (RuntimeError, OSError, ValueError) as exc:
            say(f"No new version number: {exc}")
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return {"from": running.VERSION, "to": made["version"], **made}
    return None
