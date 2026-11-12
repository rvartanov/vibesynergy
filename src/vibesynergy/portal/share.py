"""Share links: a secret address that shows chosen portal pages to someone without tailnet
access, served by the public app under ``/vibesynergy/share/<code>/``.

Each link has a title (who it is for), the pages it shows, whether it may change anything
(e.g. re-sync on Settings → Sources → Status), an optional expiry, and a use counter. Only a
SHA-256 of the code is stored, so neither the database nor a backup can hand out a working
link; the link is shown once, when it is made. Settings is never shareable: it controls access
itself.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import share_link

# Portal page -> the API routes it reads, and the ones it writes (only with can_write).
PAGES: dict[str, dict[str, tuple[str, ...]]] = {
    "dashboard": {"read": ("/dashboard", "/upcoming", "/best-runs", "/bests")},
    "calendar": {"read": ("/calendar",)},
    # One page since Today and Day merged; older links name either.
    "today": {"read": ("/today", "/day", "/body-summary")},
    "day": {"read": ("/today", "/day", "/body-summary")},
    "diary": {
        "read": ("/diary",),
        "write": ("/diary/note", "/diary/checkin", "/diary/injury"),
    },
    "activities": {"read": ("/activities", "/activity", "/activity-weather")},
    "metrics": {"read": ("/series", "/catalog")},
    # Planning & progress took over Training and Training plan; older links name either
    # and open the merged page.
    "planning": {
        "read": (
            "/planning",
            "/training",
            "/power_curve",
            "/pace_curve",
            "/plan/export.csv",
            "/plan/export.xlsx",
            "/plan/export.pdf",
            "/plan/booklet",
        )
    },
    "training": {"read": ("/planning", "/training", "/power_curve", "/pace_curve")},
    "plan": {"read": ("/planning", "/training-plan")},
    "sleep": {"read": ("/sleep",)},
    "recovery": {"read": ("/recovery",)},
    "injuries": {"read": ("/injuries",), "write": ("/diary/injury",)},
    "body": {"read": ("/body", "/body-summary")},
    "correlations": {"read": ("/correlate", "/catalog")},
    "sql": {"read": ("/sql/schema", "/sql")},  # POST /sql only reads (read-only runner)
    "knowledge": {
        "read": ("/knowledge", "/knowledge/article"),
        "write": ("/knowledge/proposal/approve", "/knowledge/proposal/reject"),
    },
    # Filling history (starting, pausing, uploading an account export) stays with the owner.
    "data": {
        "read": ("/data", "/coverage", "/files", "/history"),
        "write": ("/resync", "/files/upload"),
    },
    # Same API access as data; split in the menu since Data moved under Settings.
    "history": {
        "read": ("/data", "/coverage", "/files", "/history"),
        "write": ("/resync", "/files/upload"),
    },
    "schedule": {"read": ("/schedule", "/schedule/weather", "/weather")},
    "zones": {
        "read": ("/thresholds", "/bests", "/best-runs", "/form", "/race-predictions")
    },  # Fitness
    # The coach's advice, messages, tasks, knowledge and change log. Not its memory or
    # settings: those are the owner's own context.
    "coach": {
        "read": (
            "/coach/feed",
            "/coach/report/",
            "/coach/tasks",
            "/daily-coaching",
            "/knowledge",
            "/knowledge/article",
            "/knowledge/methods",
            "/coach-log",
            "/coach-log/diff",
        ),
        "write": ("/knowledge/proposal/approve", "/knowledge/proposal/reject"),
    },
    "gear": {"read": ("/gear",)},
}
# "Everything": stored as this, so pages added later are shared too.
ALL = "*"
# What the share dialog offers (the current menu); older names stay valid for old links.
CHOICES = (
    "dashboard",
    "today",
    "calendar",
    "diary",
    "activities",
    "planning",
    "schedule",
    "zones",
    "coach",
    "knowledge",
    "gear",
    "recovery",
    "injuries",
    "sleep",
    "body",
    "metrics",
    "correlations",
    "sql",
    "data",
)
# Never shared, said in the dialog: settings and access, the coach's memory and settings,
# secrets and tokens.


def expand(pages: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """The pages a stored list means: ``*`` is every page there is now."""
    return tuple(PAGES) if ALL in pages else tuple(pages)


ALWAYS = ("/meta",)  # every page needs it
MAX_USES_WRITE_INTERVAL = timedelta(minutes=1)  # last_used_at is refreshed at most this often
CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,64}")


@dataclass(frozen=True)
class Share:
    id: str
    title: str
    pages: tuple[str, ...]
    can_write: bool

    def allows(self, method: str, path: str) -> bool:
        """Whether this link may call ``method path`` on the portal API (path without /api)."""
        if path in ALWAYS and method == "GET":
            return True
        for page in expand(self.pages):
            routes = PAGES.get(page, {})
            if _match(path, routes.get("read", ())) and (method == "GET" or path == "/sql"):
                return True
            if self.can_write and method == "POST" and _match(path, routes.get("write", ())):
                return True
        return False

    def public(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "pages": list(expand(self.pages)),
            "all": ALL in self.pages,
            "can_write": self.can_write,
        }


def _match(path: str, routes: tuple[str, ...]) -> bool:
    """Exact routes, and ``/prefix/`` for routes with an id after it (``/coach/report/12``)."""
    return path in routes or any(
        r.endswith("/") and path.startswith(r) and "/" not in path[len(r) :] for r in routes
    )


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create(
    engine: Engine,
    title: str,
    pages: list[str],
    *,
    can_write: bool = False,
    expires_in_days: float | None = 7,
) -> tuple[str, dict[str, Any]]:
    """Make a link; returns (code, row). The code is not stored and can't be shown again."""
    title = title.strip()
    if not title:
        raise ValueError("give the link a title, so you know who has it")
    pages = _pages(pages)
    if expires_in_days is not None and not 0 < expires_in_days <= 3660:
        raise ValueError("expiry must be between a day and ten years, or none")
    code = secrets.token_urlsafe(32)  # 43 characters, 256 bits
    now = utc_now()
    row = {
        "id": secrets.token_hex(4),
        "token_hash": _hash(code),
        "title": title[:80],
        "pages": pages,
        "can_write": bool(can_write),
        "created_at": now,
        "expires_at": now + timedelta(days=expires_in_days) if expires_in_days else None,
        "revoked_at": None,
        "last_used_at": None,
        "uses": 0,
    }
    with engine.begin() as conn:
        conn.execute(share_link.insert().values(**row))
    return code, _public_row(row)


def _pages(pages: list[str]) -> list[str]:
    if ALL in pages:
        return [ALL]
    unknown = [p for p in pages if p not in PAGES]
    if unknown or not pages:
        raise ValueError(f"choose pages from {', '.join(PAGES)}")
    return [p for p in PAGES if p in pages]


def edit(
    engine: Engine,
    link_id: str,
    *,
    title: str | None = None,
    pages: list[str] | None = None,
    can_write: bool | None = None,
) -> dict[str, Any]:
    """Change an open link in place; the address stays the same."""
    values: dict[str, Any] = {}
    if title is not None:
        if not title.strip():
            raise ValueError("give the link a title, so you know who has it")
        values["title"] = title.strip()[:80]
    if pages is not None:
        values["pages"] = _pages(pages)
    if can_write is not None:
        values["can_write"] = bool(can_write)
    with engine.begin() as conn:
        if values:
            conn.execute(
                update(share_link)
                .where(share_link.c.id == link_id, share_link.c.revoked_at.is_(None))
                .values(**values)
            )
        row = conn.execute(select(share_link).where(share_link.c.id == link_id)).first()
    if row is None or row.revoked_at is not None:
        raise LookupError("no open link with that id")
    return _public_row(dict(row._mapping))


def resolve(engine: Engine, code: str, *, now: datetime | None = None) -> Share | None:
    """The live link for ``code`` (not revoked, not expired), counting the visit."""
    if not CODE_PATTERN.fullmatch(code):
        return None
    now = now or utc_now()
    with engine.connect() as conn:
        row = conn.execute(select(share_link).where(share_link.c.token_hash == _hash(code))).first()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= now:
        return None
    if row.last_used_at is None or now - row.last_used_at >= MAX_USES_WRITE_INTERVAL:
        with engine.begin() as conn:  # one "open" per minute, not one per request
            conn.execute(
                update(share_link)
                .where(share_link.c.id == row.id)
                .values(last_used_at=now, uses=share_link.c.uses + 1)
            )
    return Share(row.id, row.title, tuple(row.pages), bool(row.can_write))


def list_links(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(share_link).order_by(share_link.c.created_at.desc())).all()
    return [_public_row(dict(r._mapping)) for r in rows]


def revoke(engine: Engine, link_id: str) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            update(share_link)
            .where(share_link.c.id == link_id, share_link.c.revoked_at.is_(None))
            .values(revoked_at=utc_now())
        )
    return result.rowcount == 1


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k != "token_hash"}
    for key in ("created_at", "expires_at", "revoked_at", "last_used_at"):
        out[key] = out[key].isoformat() if out[key] else None
    now = utc_now()
    expired = row["expires_at"] is not None and row["expires_at"] <= now
    out["state"] = "revoked" if row["revoked_at"] else "expired" if expired else "active"
    return out
