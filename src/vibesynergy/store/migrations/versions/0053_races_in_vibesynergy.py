"""Races and the plan live in VibeSynergy only (#111, #91, #120).

- A new table ``race``: the races VibeSynergy owns, with their A/B/C priority.
- The races Garmin events and intervals.icu ``RACE_A/B/C`` events held move into it, once.
  A Garmin race and an intervals.icu race on the same day are one race (the Garmin name,
  place and distance). The priority is the one chosen for the Garmin event
  (``race_priority``), else the intervals.icu letter, else A for Garmin's primary race. The
  copies are kept as ``links``; nothing reads them as races any more.
- A plan that still followed intervals.icu (``shadow``) now leads: VibeSynergy keeps what it
  imported last and from now on only copies it to intervals.icu.

The steps are self-contained (the code that reads races may change later) and do nothing
for races that are in ``race`` already, so a second run adds nothing.

Revision ID: 0053
Revises: 0052
"""

import json
import re
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

ICU_PRIORITY = {"RACE_A": "A", "RACE_B": "B", "RACE_C": "C"}
_WORDS = (
    ("triathlon", r"triat|ironman"),
    ("duathlon", r"duat"),
    ("trail", r"trail|ultra"),
    ("swim", r"zwem|swim"),
    ("ride", r"fiets|ride|cycl|wielren|gravel|mtb"),
    ("run", r"marathon|\bloop\b|\brun\b|\bkm\b|\d+k\b"),
)
_ICU_SPORT = {"run": "run", "ride": "ride", "virtualride": "ride", "swim": "swim"}


def _load(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _sport(title, icu_type=None, text=None) -> str:
    lowered = f"{title or ''} {text or ''}".casefold()
    if (
        re.search(r"\blop|\bloop|\brun", lowered)
        and re.search(r"zwem|swim", lowered)
        and re.search(r"fiets|bike|ride|cycl", lowered)
    ):
        return "triathlon"
    for sport, pattern in _WORDS:
        if re.search(pattern, title or "", re.IGNORECASE):
            return sport
    return _ICU_SPORT.get((icu_type or "").casefold(), "other")


def _distance(extra: dict):
    target = extra.get("completionTarget") or {}
    if isinstance(target, dict) and target.get("unitType") == "distance":
        value = target.get("value")
        if isinstance(value, (int, float)) and value > 0:
            unit = str(target.get("unit") or "kilometer").casefold()
            return round(float(value) * (1 if unit.startswith("meter") else 1000), 1)
    value = extra.get("distance")
    return float(value) if isinstance(value, (int, float)) and value >= 100 else None


def _candidates(conn) -> list[dict]:
    garmin = conn.execute(
        sa.text(
            'SELECT source_id, date, title, sport, "primary", extra FROM planned_item '
            "WHERE source = 'garmin' AND kind = 'event' AND (is_race OR \"primary\") "
            "ORDER BY date"
        )
    ).all()
    icu = conn.execute(
        sa.text(
            "SELECT id, date, category, name, type, description, raw FROM icu_event "
            "WHERE category IN ('RACE_A', 'RACE_B', 'RACE_C') ORDER BY date"
        )
    ).all()
    saved = dict(
        ((source, source_id), priority)
        for source, source_id, priority in conn.execute(
            sa.text("SELECT source, source_id, priority FROM race_priority")
        )
    )
    out: list[dict] = []
    for source_id, day, title, sport, primary, extra in garmin:
        extra = _load(extra)
        letter = saved.get(("garmin", source_id))
        when = (extra.get("eventTimeLocal") or {}).get("startTimeHhMm")
        out.append(
            {
                "name": title or "Race",
                "date": _day(day),
                "sport": _sport(title, sport),
                "distance_m": _distance(extra),
                "priority": letter if letter in ("A", "B", "C") else None,
                "start_time": when if isinstance(when, str) else None,
                "location": extra.get("location"),
                "links": {"garmin": source_id},
                "primary": bool(primary),
            }
        )
    for event_id, day, category, name, icu_type, text, raw in icu:
        day = _day(day)
        raw = _load(raw)
        same = next((c for c in out if c["date"] == day and "intervals" not in c["links"]), None)
        if same is not None:
            same["links"]["intervals"] = str(event_id)
            same["priority"] = same["priority"] or ICU_PRIORITY[category]
            if same["sport"] == "other":
                same["sport"] = _sport(name, icu_type, text)
            same["distance_m"] = same["distance_m"] or _distance(raw)
            continue
        out.append(
            {
                "name": name or "Race",
                "date": day,
                "sport": _sport(name, icu_type, text),
                "distance_m": _distance(raw),
                "priority": ICU_PRIORITY[category],
                "start_time": None,
                "location": None,
                "links": {"intervals": str(event_id)},
                "primary": False,
            }
        )
    for c in out:
        if c["priority"] is None and c["primary"]:
            c["priority"] = "A"  # Garmin's primary race was read as A
    return out


def _slug(day: date, name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "race").casefold())
    return f"{day.isoformat()}-{'-'.join(words)[:48].strip('-') or 'race'}"


def _move_races(conn) -> None:
    known = conn.execute(sa.text("SELECT id, links FROM race")).all()
    ids = {ident for ident, _links in known}
    linked = {(t, str(ref)) for _ident, links in known for t, ref in _load(links).items()}
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    for c in _candidates(conn):
        if any((t, str(ref)) in linked for t, ref in c["links"].items()):
            continue
        ident = base = _slug(c["date"], c["name"])
        n = 2
        while ident in ids:
            ident, n = f"{base}-{n}", n + 1
        ids.add(ident)
        conn.execute(
            sa.text(
                "INSERT INTO race (id, subject_id, name, date, sport, distance_m, priority, "
                "start_time, location, links, origin, created_at, updated_at) VALUES "
                "(:id, 1, :name, :date, :sport, :distance_m, :priority, :start_time, "
                ":location, :links, 'migrated', :now, :now)"
            ),
            {
                **{k: v for k, v in c.items() if k not in ("links", "primary", "date")},
                "id": ident,
                "date": c["date"].isoformat(),
                "links": json.dumps(c["links"]),
                "now": now,
            },
        )


def upgrade() -> None:
    op.create_table(
        "race",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("sport", sa.String(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("priority", sa.String(), nullable=True),
        sa.Column("start_time", sa.String(), nullable=True),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("goal_time", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("links", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("deleted_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_race")),
    )
    op.create_index(op.f("ix_race_date"), "race", ["date"], unique=False)
    conn = op.get_bind()
    _move_races(conn)
    conn.execute(
        sa.text("UPDATE plan SET status = 'leading', updated_at = :now WHERE status = 'shadow'"),
        {"now": datetime.now(UTC).isoformat(timespec="microseconds")},
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_race_date"), table_name="race")
    op.drop_table("race")
