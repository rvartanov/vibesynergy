"""Migration 0037 drops the unused workout-proposal tables, and refuses if any holds data."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import inspect, text

from vibesynergy.store.db import _alembic_config, create_db_engine, upgrade

TABLES = {"coach_plan_version", "coach_session", "coach_workout_ledger"}


def _downgrade(engine, revision: str) -> None:
    cfg = _alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def test_empty_tables_are_dropped_and_come_back_on_downgrade(tmp_path) -> None:
    engine = create_db_engine(tmp_path / "drop.db")
    upgrade(engine, "0036")
    assert set(inspect(engine).get_table_names()) >= TABLES
    upgrade(engine, "0037")
    assert not TABLES & set(inspect(engine).get_table_names())
    assert "coach_decision_log" in inspect(engine).get_table_names()
    _downgrade(engine, "0036")
    assert set(inspect(engine).get_table_names()) >= TABLES
    engine.dispose()


def test_upgrade_refuses_when_a_table_has_rows(tmp_path) -> None:
    engine = create_db_engine(tmp_path / "refuse.db")
    upgrade(engine, "0036")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO coach_plan_version (id, subject_id, status, weeks_json, rationale,"
                " safety, policy_fingerprint, policy_version, framework, created_at)"
                " VALUES ('p1', 1, 'proposed', '[]', 'x', '{}', 'f', 1, '{}', :at)"
            ),
            {"at": datetime(2026, 10, 1, tzinfo=UTC).isoformat()},
        )
    with pytest.raises(RuntimeError, match=r"coach_plan_version \(1 rows\)"):
        upgrade(engine, "0037")
    assert set(inspect(engine).get_table_names()) >= TABLES
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM coach_plan_version")).scalar_one() == 1
    engine.dispose()
