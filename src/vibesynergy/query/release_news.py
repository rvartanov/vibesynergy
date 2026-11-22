"""What changed, for the portal and for AI apps (#140; the releases: ``vibesynergy.releases``).

- ``news``: the "What changed in version …" card, once after a release. It returns the
  releases with items for you that this account has not seen, and from then on counts the
  current version as seen (``release_seen``, per account).
- ``history``: every release with items for you, newest first, for Waiting for you →
  Notices.
- ``for_ai``: the newest releases with items for AI apps, for the MCP server information and
  ``vibesynergy_coach_handbook``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy import releases
from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import auth_account, release_seen

log = logging.getLogger(__name__)
NO_ACCOUNT = 0  # an install without a portal account (login off)
AI_RELEASES = 5  # how many releases with items for AI apps the handbook and MCP info carry


def news(engine: Engine) -> dict[str, Any]:
    """The card after a release: the unseen releases with items for you, newest first. The
    first call after a release returns them; every later call returns none."""
    try:
        with engine.begin() as conn:
            account = conn.execute(select(auth_account.c.id, auth_account.c.created_at)).first()
            key = account.id if account else NO_ACCOUNT
            joined = account.created_at.date() if account and account.created_at else None
            seen = conn.execute(
                select(release_seen.c.version).where(release_seen.c.account_id == key)
            ).scalar()
            if seen == releases.VERSION:
                return {"version": releases.VERSION, "releases": []}
            found = releases.unseen(seen, joined)
            values = {"version": releases.VERSION, "seen_at": utc_now()}
            if seen is None:
                conn.execute(release_seen.insert().values(account_id=key, **values))
            else:
                conn.execute(
                    release_seen.update().where(release_seen.c.account_id == key).values(**values)
                )
    except Exception:  # the card must never break a page
        log.exception("What changed: could not read or store what was seen")
        return {"version": releases.VERSION, "releases": []}
    return {
        "version": releases.VERSION,
        "releases": [releases.as_dict(r, items) for r, items in found],
    }


def history() -> list[dict[str, Any]]:
    """Every release with items for you, newest first."""
    return [releases.as_dict(r, items) for r, items in releases.history("you")]


def for_ai(limit: int = AI_RELEASES) -> list[dict[str, Any]]:
    """The newest releases with items for AI apps."""
    return [releases.as_dict(r, items) for r, items in releases.history("ai")[:limit]]


def for_ai_text(limit: int = AI_RELEASES) -> str:
    """The same as plain text, for the MCP server's instructions."""
    lines = [f"Version {releases.VERSION}. What changed for AI apps (newest first):"]
    for release in for_ai(limit):
        for item in release["items"]:
            lines.append(f"- {release['version']} ({release['date']}): {item['text']}")
    return "\n".join(lines)
