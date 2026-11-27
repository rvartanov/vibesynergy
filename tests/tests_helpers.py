"""Small helpers shared by tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def add_public_url(config: Path, url: str = "https://server.example.ts.net") -> None:
    config.write_text(config.read_text() + f'\n[server]\npublic_url = "{url}"\n')


def free_port() -> int:
    """An unused local TCP port, so browser tests on parallel CI runners don't collide."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def midday_timezone(now=None) -> str:
    """A fixed-offset IANA zone where it is about noon right now.

    Browser tests seed data for "today" and then read pages for minutes. Run near local
    midnight, the day flips under them. In a zone where it is about 12:00 there is
    half a day of margin either way. ``Etc/GMT-5`` means UTC+5 (the sign is inverted).
    """
    from datetime import UTC, datetime

    hour = (now or datetime.now(UTC)).astimezone(UTC).hour
    offset = 12 - hour  # -11 … +12, all valid Etc/GMT zones
    if offset == 0:
        return "Etc/GMT"
    return f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def adopt_icu_plan(engine, prefix: str | None = None) -> str | None:
    """Copy the intervals.icu events a test wrote into VibeSynergy's plan, once (the explicit
    ``vibesynergy plan import``): since #111 the readers never take the plan from
    intervals.icu. A WORKOUT without an external id gets ``<prefix>:icu:<id>`` first, so
    the import takes it too. Called again, it replaces the earlier copy. Returns the plan
    id (None when there are no workouts)."""
    import re
    from collections import Counter

    from sqlalchemy import select, update

    from vibesynergy.query import plan_model
    from vibesynergy.store.schema import icu_event

    with engine.begin() as conn:
        rows = conn.execute(
            select(icu_event.c.id, icu_event.c.external_id, icu_event.c.category)
        ).all()
        if not any(r.category == "WORKOUT" for r in rows) and not any(
            r.category in ("NOTE", "TARGET") and r.external_id for r in rows
        ):
            return None
        heads = Counter(
            (r.external_id or "").split(":")[0]
            for r in rows
            if r.external_id
            and ":" in r.external_id
            and not re.match(r"^\d{4}-\d{2}-\d{2}$", r.external_id.split(":")[0])
        )
        prefix = prefix or (heads.most_common(1)[0][0] if heads else "test")
        for r in rows:
            ext = r.external_id or ""
            if r.category == "WORKOUT" and not ext.startswith(f"{prefix}:"):
                conn.execute(
                    update(icu_event)
                    .where(icu_event.c.id == r.id)
                    .values(external_id=f"{prefix}:icu:{r.id}")
                )
        # A test that writes more events adopts again: the copy replaces the earlier one.
        from sqlalchemy import delete

        from vibesynergy.store.schema import (
            plan,
            plan_link,
            plan_session,
            plan_target,
            plan_version,
            plan_week,
        )

        conn.execute(delete(plan_link).where(plan_link.c.session_id.like(f"{prefix}:%")))
        for table in (plan_session, plan_target, plan_week, plan_version):
            conn.execute(delete(table).where(table.c.plan_id == prefix))
        conn.execute(delete(plan).where(plan.c.id == prefix))
    plan_model.import_from_intervals(engine, prefix=prefix)
    # The plan changed when the events did (their ``updated``), not when the test ran.
    from sqlalchemy import func

    with engine.begin() as conn:
        last = conn.execute(select(func.max(icu_event.c.updated))).scalar()
        if last is not None:
            conn.execute(
                update(plan_version).where(plan_version.c.plan_id == prefix).values(created_at=last)
            )
    return prefix


def add_race(engine, name: str, day, priority: str | None = None, **values) -> str:
    """A race in VibeSynergy's own race list (#111)."""
    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.query import races
    from vibesynergy.store.schema import race

    ident = values.pop("id", None) or races.slug(day, name)
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(
            race.insert().values(
                id=ident,
                subject_id=1,
                name=name,
                date=day,
                priority=priority,
                links=values.pop("links", {}),
                origin="vibesynergy",
                created_at=now,
                updated_at=now,
                **values,
            )
        )
    return ident


def no_live_intervals_icu(monkeypatch: MonkeyPatch) -> None:
    """Pages and tools that read intervals.icu sport settings never reach the network."""
    from vibesynergy.query import thresholds
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuError

    def offline():
        raise IntervalsIcuError("intervals.icu is not set up: tests")

    monkeypatch.setattr(thresholds, "icu_settings_live", offline)


def contact_window_off(monkeypatch: MonkeyPatch) -> None:
    """The contact window (#119) has its own tests, which save one. The rest must not
    depend on the hour they run at, so an unsaved window is off here."""
    from vibesynergy.coach_runner import contact

    monkeypatch.setattr(contact, "DEFAULT", {**contact.DEFAULT_WINDOW, "on": False})


def portal_login_off(monkeypatch: MonkeyPatch) -> None:
    """The login gate has its own tests (marked ``auth``); the rest test the portal itself."""
    from vibesynergy.portal import auth_gate

    monkeypatch.setattr(auth_gate, "auth_required", lambda settings: False)


def isolate(monkeypatch: MonkeyPatch) -> None:
    """All three of the above, for a module-scoped fixture that sets up before them."""
    no_live_intervals_icu(monkeypatch)
    contact_window_off(monkeypatch)
    portal_login_off(monkeypatch)
