"""Which help videos a change to the portal makes out of date.

A pull request that changes a page a help video shows re-records that video
(tools/help_videos/README.md), or carries the label no-help-video when nothing visible
changed. tools/help_videos/coverage.json says which files each video shows.

    uv run python tools/check_help_videos.py [--base origin/main]

Exits 1 and lists the videos to re-record. CI passes the pull request's labels in LABELS.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = "src/vibesynergy/portal/static/js/"
HELP = "src/vibesynergy/portal/static/help/"
COVERAGE = ROOT / "tools" / "help_videos" / "coverage.json"
LABEL = "no-help-video"


def coverage() -> dict[str, list[str]]:
    data = json.loads(COVERAGE.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def affected(changed: list[str], cover: dict[str, list[str]]) -> dict[str, list[str]]:
    """Video id -> the changed portal files it shows."""
    out: dict[str, list[str]] = {}
    for path in changed:
        if not path.startswith(JS):
            continue
        rel = path[len(JS) :]
        for video, patterns in cover.items():
            if any(fnmatch.fnmatchcase(rel, p) for p in patterns):
                out.setdefault(video, []).append(rel)
    return out


def stale(changed: list[str], cover: dict[str, list[str]]) -> dict[str, list[str]]:
    """The affected videos whose recording this change did not update."""
    videos = [p for p in changed if p.startswith(HELP) and p.endswith(".webm")]
    updated = {p[len(HELP) :].split(".")[0] for p in videos}
    return {v: files for v, files in affected(changed, cover).items() if v not in updated}


def changed_files(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="what the change is compared with")
    args = parser.parse_args()
    labels = {x.strip() for x in os.environ.get("LABELS", "").split(",") if x.strip()}
    todo = stale(changed_files(args.base), coverage())
    if not todo:
        print("Help videos: up to date for this change.")
        return 0
    lines = [f"  {video}: {', '.join(sorted(files))}" for video, files in sorted(todo.items())]
    message = "\n".join(
        [
            "This change touches pages that help videos show. Re-record them",
            "(tools/help_videos/README.md: run.py " + " ".join(sorted(todo)) + ", then build.sh):",
            *lines,
        ]
    )
    if LABEL in labels:
        print(message + f"\nLabelled {LABEL}: nothing visible changed, so the videos stay.")
        return 0
    print(message + f"\nNothing visible changed? Add the label {LABEL}.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
