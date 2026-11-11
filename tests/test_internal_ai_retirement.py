"""Archive-then-drop of the internal AI tables, and an old [ai] config still loads."""

import json
import logging
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import command
from sqlalchemy import inspect

from vibesynergy.core.config import load_settings
from vibesynergy.store import db


def test_old_ai_config_loads_and_warns_once(tmp_path, monkeypatch, caplog):
    from vibesynergy.core import config as config_mod

    config_mod._AI_WARNED = False
    path = tmp_path / "config.toml"
    path.write_text(
        """
[user]
timezone = "Europe/Amsterdam"

[portal]
legacy_ai = true

[ai]
provider = "openrouter"
model = "anthropic/claude-sonnet"
"""
    )
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(path))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    with caplog.at_level(logging.WARNING):
        first = load_settings()
        second = load_settings()
    assert first.ai["provider"] == "openrouter"
    assert second.portal["legacy_ai"] is True
    assert sum("internal coach has been removed" in rec.message for rec in caplog.records) == 1


def _downgrade(engine, revision: str) -> None:
    cfg = db._alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def test_archive_then_drop_round_trips(runtime):
    names_at_head = set(inspect(runtime.engine).get_table_names())
    assert "assessment_revision" not in names_at_head
    assert "daily_review" not in names_at_head
    assert "publication_ledger" in names_at_head
    assert "coach_decision_log" in names_at_head

    _downgrade(runtime.engine, "0026")
    assert "assessment_identity" in inspect(runtime.engine).get_table_names()
    # ISO text, not date objects: sqlite3's default date adapters are deprecated (3.12+).
    when = datetime(2026, 10, 1, 8, 0, tzinfo=UTC).isoformat()
    with runtime.engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO assessment_identity (id, subject_id, created_at) "
                "VALUES ('ident-1', 1, :created)"
            ),
            {"created": when},
        )
        conn.execute(
            sa.text(
                "INSERT INTO assessment_revision "
                "(id, identity_id, input_version, inputs, public_facts, state, created_at) "
                "VALUES ('rev-1', 'ident-1', 'v1', :inputs, :facts, 'ready', :created)"
            ),
            {"inputs": '{"recorded": 1}', "facts": "{}", "created": when},
        )
        conn.execute(
            sa.text(
                "INSERT INTO daily_review "
                "(id, subject_id, local_date, kind, input_version, inputs, safety, state, "
                "created_at) VALUES "
                "('day-1', 1, :day, 'morning', 'v1', '{}', :safety, 'ready', :created)"
            ),
            {"day": date(2026, 10, 1).isoformat(), "safety": '{"level": "ok"}', "created": when},
        )
    reports = runtime.settings.data_dir / "reports"
    reports.mkdir()
    report = reports / "rep_synthetic.json"
    report.write_text('{"summary": "private"}')

    db.upgrade(runtime.engine, "0027")
    gone = inspect(runtime.engine).get_table_names()
    assert "assessment_revision" not in gone
    assert "daily_review" not in gone
    assert "publication_ledger" in gone
    assert not report.exists()
    archive = runtime.settings.db_path.parent / "llm-archive.json"
    payload = json.loads(archive.read_text())
    assert payload["tables"]["assessment_revision"][0]["id"] == "rev-1"
    assert payload["tables"]["daily_review"][0]["id"] == "day-1"
    assert payload["reports"][0]["name"] == "rep_synthetic.json"
    assert payload["reports"][0]["body"] == '{"summary": "private"}'

    _downgrade(runtime.engine, "0026")
    with runtime.engine.connect() as conn:
        revision = conn.execute(sa.text("SELECT id, state FROM assessment_revision")).one()
        review = conn.execute(sa.text("SELECT id, kind, local_date FROM daily_review")).one()
    assert revision == ("rev-1", "ready")
    assert review[0] == "day-1" and review[1] == "morning"
    assert review[2] == "2026-10-01" or str(review[2]).startswith("2026-10-01")
    assert report.read_text() == '{"summary": "private"}'

    db.upgrade(runtime.engine)
    assert set(inspect(runtime.engine).get_table_names()) == names_at_head
    assert not report.exists()
    again = json.loads(archive.read_text())
    assert again["tables"]["assessment_identity"][0]["id"] == "ident-1"
