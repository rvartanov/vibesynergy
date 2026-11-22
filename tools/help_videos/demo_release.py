"""Let the demo account see the "What changed" card again, or mark it seen (#140).

The demo account is new, and a new account gets no old news. ``seed`` makes the account a
month old and forgets which version it saw, so the next page shows "What changed in version
…" once. ``clear`` marks the current version as seen, so other videos don't show the card.

Only for the help-video demo home (``~/vs-help-home``), never for real data. Run with the
demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \\
        uv run python tools/help_videos/demo_release.py seed|clear
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, update

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import release_news
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import auth_account, release_seen

DEMO_HOME = Path.home() / "vs-help-home"


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


def seed(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        conn.execute(update(auth_account).values(created_at=utc_now() - timedelta(days=30)))
        conn.execute(delete(release_seen))


def clear(rt: Runtime) -> None:
    release_news.news(rt.engine)  # the current version counts as seen


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ("seed", "clear"):
        sys.exit("usage: demo_release.py seed|clear")
    rt = Runtime.open(load_settings(None))
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()


if __name__ == "__main__":
    main()
