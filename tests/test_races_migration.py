"""Migration 0053 moves the races Garmin and intervals.icu held into VibeSynergy, once, and
lets a plan that followed intervals.icu lead (#111, #120). Synthetic data only."""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import create_engine, text

from vibesynergy.query import races
from vibesynergy.store import db

NOW = "2026-11-19T08:00:00.000000+00:00"


def _before(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'before.sqlite'}")
    db.upgrade(engine, "0052")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_item (id, source, dataset, item_key, sha256, content_type, size, "
                "request, fetched_at, last_seen_at, is_current) VALUES ('r', 'garmin', 'x', 'k', "
                ":sha, 'application/json', 2, '{}', :now, :now, 1)"
            ),
            {"sha": "ab" * 32, "now": NOW},
        )
        garmin = [
            # The A race of the season: Garmin does not mark it primary.
            ("event:main", "2027-07-03", "Example Iron 2027", 1, 0, {"completionTarget": {"unitType": "distance", "unit": "kilometer", "value": 226.0}, "location": "Example town", "eventTimeLocal": {"startTimeHhMm": "07:00"}}),  # noqa: E501
            # A B race that Garmin marks primary (#120).
            ("event:b", "2027-05-30", "Cross triatlon XXL", 1, 1, {}),
            ("event:c", "2026-12-20", "Winter Trail 38km", 1, 0, {}),
            ("event:club", "2026-12-01", "Club night", 0, 0, {}),
        ]  # fmt: skip
        for source_id, day, title, is_race, primary, extra in garmin:
            conn.execute(
                text(
                    "INSERT INTO planned_item (source, source_id, subject_id, date, kind, title, "
                    'is_race, "primary", extra, raw_item_id, updated_at) VALUES '
                    "('garmin', :sid, 1, :day, 'event', :title, :race, :primary, :extra, 'r', :now)"
                ),
                {
                    "sid": source_id,
                    "day": day,
                    "title": title,
                    "race": is_race,
                    "primary": primary,
                    "extra": json.dumps(extra),
                    "now": NOW,
                },
            )
        icu = [
            ("1", "2027-07-03", "RACE_A", "EXAMPLE IRON", "3,8 km zwemmen / 180 km fietsen / 42 km lopen"),  # noqa: E501
            ("2", "2026-12-20", "RACE_C", "WINTER TRAIL", ""),
            ("3", "2027-04-11", "RACE_C", "Spring half marathon", ""),
        ]  # fmt: skip
        for event_id, day, category, name, description in icu:
            conn.execute(
                text(
                    "INSERT INTO icu_event (id, source, category, type, date, name, description, "
                    "tags, for_week, targets, raw, fetched_at, raw_item_id, updated_at) VALUES "
                    "(:id, 'plan', :cat, 'Other', :day, :name, :text, '[]', 0, '{}', '{}', :now, "
                    "'r', :now)"
                ),
                {
                    "id": event_id,
                    "cat": category,
                    "day": day,
                    "name": name,
                    "text": description,
                    "now": NOW,
                },
            )
        conn.execute(
            text(
                "INSERT INTO race_priority (source, source_id, priority, updated_at) VALUES "
                "('garmin', 'event:b', 'B', :now)"
            ),
            {"now": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO plan (id, subject_id, name, status, current_version, updated_at) "
                "VALUES ('old', 1, 'Old plan', 'shadow', 1, :now)"
            ),
            {"now": NOW},
        )
    return engine


def test_races_move_into_vibesynergy_once(tmp_path) -> None:
    engine = _before(tmp_path)
    db.upgrade(engine)
    found = {r["title"]: r for r in races.rows(engine, date(2026, 11, 19))}
    assert set(found) == {
        "Example Iron 2027",
        "Cross triatlon XXL",
        "Winter Trail 38km",
        "Spring half marathon",
    }  # the club night is no race
    iron = found["Example Iron 2027"]
    # Garmin's name, place and distance; the intervals.icu letter; both copies linked.
    assert iron["priority"] == "A" and iron["sport"] == "triathlon"
    assert iron["extra"]["links"] == {"garmin": "event:main", "intervals": "1"}
    assert iron["extra"]["distance"] == 226000.0 and iron["extra"]["location"] == "Example town"
    assert iron["extra"]["start_time"] == "07:00"
    # #120: primary follows the A race, not Garmin's flag on the B race.
    assert iron["primary"] is True
    assert (found["Cross triatlon XXL"]["priority"], found["Cross triatlon XXL"]["primary"]) == (
        "B",
        False,
    )
    assert found["Winter Trail 38km"]["priority"] == "C"
    assert found["Winter Trail 38km"]["extra"]["links"] == {"garmin": "event:c", "intervals": "2"}
    assert found["Spring half marathon"]["extra"]["links"] == {"intervals": "3"}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM plan")).scalar() == "leading"
        assert conn.execute(text("SELECT DISTINCT origin FROM race")).scalar() == "migrated"
    # The same races again (``plan import-races``) add nothing.
    assert races.adopt(engine)["added"] == []


def test_downgrade_drops_the_race_table(tmp_path) -> None:
    engine = _before(tmp_path)
    db.upgrade(engine)
    from alembic import command

    cfg = db._alembic_config()  # pyright: ignore[reportPrivateUsage]
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0052")
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master"))}
    assert "race" not in names
