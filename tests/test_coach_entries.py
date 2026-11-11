"""AI coach entries: author model, typed writes, session purpose, narrative gate."""

from __future__ import annotations

from datetime import date

import anyio
from sqlalchemy import func, select, text

from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.query import coach_workouts, session_purpose
from vibesynergy.store import db
from vibesynergy.store.schema import coach_decision_log


def test_migration_backfills_legacy_author(tmp_path):
    engine = db.create_db_engine(tmp_path / "migrate.db")
    try:
        db.upgrade(engine, "0020")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO coach_decision_log
                    (subject_id, at, kind, text, meta)
                    VALUES (1, '2026-10-01 08:00:00', 'note', 'Older note', '{}')
                    """
                )
            )
        db.upgrade(engine)
        with engine.connect() as conn:
            row = conn.execute(
                select(coach_decision_log.c.author).where(coach_decision_log.c.text == "Older note")
            ).scalar_one()
        assert row == "legacy"
    finally:
        engine.dispose()


def test_coach_write_idempotent(runtime):
    key = "coach-test-morning"
    first = coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary="Rustig aan vandaag.",
        idempotency_key=key,
        for_date=date(2026, 10, 11),
        author="client:coach",
    )
    second = coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary="Andere tekst.",
        idempotency_key=key,
        for_date=date(2026, 10, 11),
        author="client:coach",
    )
    assert first["id"] == second["id"]
    assert second["text"].startswith("Rustig aan")
    with runtime.engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(coach_decision_log)
            .where(coach_decision_log.c.idempotency_key == key)
        ).scalar()
    assert count == 1


def test_author_from_mcp_connection_ignores_argument(runtime):
    tokens = set_connection("coach")
    try:
        row = coach_workouts.coach_write(
            runtime.engine,
            entry_type="note",
            summary="Coach note",
            idempotency_key="author-test-1",
            for_date=date(2026, 10, 11),
        )
    finally:
        reset_connection(tokens)
    assert row["author"] == "client:coach"


def test_session_purpose_derivation_and_threshold_helper():
    assert session_purpose.derive("run", "Easy recovery jog") == "recovery"
    assert session_purpose.derive("run", "Park run") is None
    assert session_purpose.derive("swim", "fun swim") == "leisure"
    assert session_purpose.derive("run", "Marathon build") == "race"
    assert session_purpose.threshold_eligible("training") is True
    assert session_purpose.threshold_eligible("race") is True
    assert session_purpose.threshold_eligible("leisure") is False
    assert session_purpose.threshold_eligible(None) is False


def test_mcp_coach_write_tool(runtime):
    from mcp import Client

    async def call():
        tokens = set_connection("user", "user")
        try:
            async with Client(build_server(runtime)) as client:
                return await client.call_tool(
                    "vibesynergy_coach_write",
                    {
                        "entry_type": "evening",
                        "summary": "Goed gedraaid.",
                        "idempotency_key": "mcp-evening-1",
                        "for_date": "2026-10-11",
                        "model_id": "claude-sonnet-5-5",
                    },
                )
        finally:
            reset_connection(tokens)

    result = anyio.run(call)
    data = result.structured_content["data"]
    assert data["entry_type"] == "evening"
    assert data["author"] == "user"
    assert data["meta"]["model_id"] == "claude-sonnet-5-5"


def test_daily_coaching_reads_an_ai_coach_morning(runtime):
    """Rows by the AI coach read back with their author."""
    from vibesynergy.query import daily_coach

    coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary="Rustig duurloopje.",
        caveat="Geen tempo.",
        idempotency_key="daily-read-1",
        for_date=date(2026, 10, 11),
        author="ai_coach",
        meta={"model_id": "claude-sonnet-5-5", "context_version": "ctx-1"},
    )
    out = daily_coach.read(runtime, date(2026, 10, 11))
    morning = out["morning"]
    assert morning["source"] == "coach_decision_log"
    assert morning["author"] == "ai_coach"
    assert morning["advice"]["interpretation"]["summary"] == "Rustig duurloopje."
    assert morning["versions"]["model_id"] == "claude-sonnet-5-5"


def test_threshold_propose_and_confirm(runtime):
    from vibesynergy.store.schema import athlete_threshold as athlete_threshold_table

    row = coach_workouts.propose_threshold_change(
        runtime.engine,
        sport="run",
        metric="threshold_pace",
        value=4.5,
        unit="min/km",
        summary="Field test suggests faster pace.",
        idempotency_key="thr-1",
    )
    assert row["meta"]["status"] == "proposed"
    assert coach_workouts.workout_generation_thresholds(runtime.engine) == {}
    pending = coach_workouts.confirm_threshold_change(runtime.engine, row["id"])
    assert pending["pending_regeneration"]
    assert pending["pending_regeneration"][0]["sport"] == "run"
    current = coach_workouts.workout_generation_thresholds(runtime.engine)
    assert current["run"]["threshold_pace"]["value"] == 270.0  # 4:30 /km, stored in s/km
    again = coach_workouts.confirm_threshold_change(runtime.engine, row["id"])
    assert again["pending_regeneration"]
    with runtime.engine.connect() as conn:
        current_rows = conn.execute(
            select(athlete_threshold_table).where(
                athlete_threshold_table.c.status == "current",
                athlete_threshold_table.c.sport == "run",
                athlete_threshold_table.c.metric == "threshold_pace",
            )
        ).all()
    assert len(current_rows) == 1


def test_threshold_confirm_supersedes_previous(runtime):
    from vibesynergy.query import athlete_threshold
    from vibesynergy.store.schema import athlete_threshold as athlete_threshold_table

    athlete_threshold.propose_threshold(
        runtime.engine,
        sport="run",
        metric="lthr",
        value=170.0,
        unit="bpm",
        method="test",
    )
    first = athlete_threshold.confirm_threshold(runtime.engine, 1)
    proposed = coach_workouts.propose_threshold_change(
        runtime.engine,
        sport="run",
        metric="lthr",
        value=172.0,
        unit="bpm",
        summary="New field test.",
        idempotency_key="thr-lthr-2",
    )
    coach_workouts.confirm_threshold_change(runtime.engine, proposed["id"])
    with runtime.engine.connect() as conn:
        old = (
            conn.execute(select(athlete_threshold_table).where(athlete_threshold_table.c.id == 1))
            .mappings()
            .one()
        )
    assert old["status"] == "superseded"
    assert old["superseded_by"] == 2
    assert first["status"] == "current"


def test_leisure_swim_does_not_feed_threshold(runtime):
    from vibesynergy.query import athlete_threshold

    assert (
        athlete_threshold.record_activity_field_estimate(
            runtime.engine,
            sport="swim",
            metric="css",
            value=95.0,
            unit="s/100m",
            purpose="leisure",
        )
        is None
    )
    assert (
        athlete_threshold.record_activity_field_estimate(
            runtime.engine,
            sport="swim",
            metric="css",
            value=95.0,
            unit="s/100m",
            purpose=None,
        )
        is None
    )
    assert athlete_threshold.list_thresholds(runtime.engine) == []


def test_threshold_confirm_never_writes_to_garmin(runtime, monkeypatch):
    """Confirming a proposal only changes VibeSynergy; Garmin is an explicit choice."""
    from vibesynergy.store.schema import athlete_threshold as athlete_threshold_table

    calls: list[dict] = []
    monkeypatch.setattr(
        "vibesynergy.sources.garmin.heart_rate_zones.set_heart_rate_zones",
        lambda *a, **k: calls.append(k),
    )
    row = coach_workouts.propose_threshold_change(
        runtime.engine,
        sport="run",
        metric="lthr",
        value=173.0,
        unit="bpm",
        summary="LTHR update.",
        idempotency_key="thr-hr-no-garmin",
    )
    pending = coach_workouts.confirm_threshold_change(runtime.engine, row["id"])
    assert pending["pending_regeneration"]
    assert calls == []
    with runtime.engine.connect() as conn:
        feed_meta = conn.execute(
            select(coach_decision_log.c.meta).where(coach_decision_log.c.id == row["id"])
        ).scalar_one()
        current = (
            conn.execute(
                select(athlete_threshold_table).where(
                    athlete_threshold_table.c.id == feed_meta["athlete_threshold_id"]
                )
            )
            .mappings()
            .one()
        )
    assert feed_meta["status"] == "confirmed"
    assert current["status"] == "current"
    assert current["value"] == 173.0


def test_session_purpose_override_reason_and_eligibility(runtime):
    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.query import athlete_threshold
    from vibesynergy.store.schema import activity_purpose_override, activity_unified

    now = utc_now()
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.insert(),
            {
                "group_id": "test:purpose",
                "subject_id": 1,
                "start_utc": now,
                "local_date": date(2026, 10, 11),
                "sport": "swim",
                "name": "Pool swim",
                "elapsed_s": 1800,
                "moving_s": 1800,
                "primary_source": "garmin",
                "sources": ["garmin:swim1"],
                "updated_at": now,
            },
        )
    session_purpose.set_override(
        runtime.engine, "test:purpose", "training", reason="Coach: CSS session"
    )
    with runtime.engine.connect() as conn:
        row = (
            conn.execute(
                select(activity_purpose_override).where(
                    activity_purpose_override.c.source_ref == "garmin:swim1"
                )
            )
            .mappings()
            .one()
        )
    assert row["purpose"] == "training"
    assert row["reason"] == "Coach: CSS session"
    session_purpose.set_override(runtime.engine, "test:purpose", "leisure")
    with runtime.engine.connect() as conn:
        leisure_row = (
            conn.execute(
                select(activity_purpose_override).where(
                    activity_purpose_override.c.source_ref == "garmin:swim1"
                )
            )
            .mappings()
            .one()
        )
    assert leisure_row["purpose"] == "leisure"
    assert leisure_row["reason"] is None
    assert (
        athlete_threshold.record_activity_field_estimate(
            runtime.engine,
            sport="swim",
            metric="css",
            value=95.0,
            unit="s/100m",
            purpose="leisure",
        )
        is None
    )
    assert session_purpose.threshold_eligible("training") is True
    assert session_purpose.threshold_eligible("leisure") is False


def test_migration_0022_athlete_threshold_table(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from vibesynergy.store import db
    from vibesynergy.store.schema import metadata

    engine = db.create_db_engine(tmp_path / "thr.db")
    try:
        db.upgrade(engine)
        with engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn), metadata) == []
    finally:
        engine.dispose()


def test_migration_0024_equipment_tables(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect

    from vibesynergy.store import db
    from vibesynergy.store.schema import metadata

    engine = db.create_db_engine(tmp_path / "equip.db")
    try:
        db.upgrade(engine, "0023")
        db.upgrade(engine)
        tables = set(inspect(engine).get_table_names())
        assert "equipment_location" in tables
        assert "equipment_item" in tables
        with engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn), metadata) == []
    finally:
        engine.dispose()


def test_equipment_upsert_idempotent(runtime):
    from vibesynergy.query import equipment

    first = equipment.upsert_item(
        runtime.engine,
        "home",
        "resistance bands",
        "gym_kit",
        "confirmed",
    )
    second = equipment.upsert_item(
        runtime.engine,
        "home",
        "resistance bands",
        "gym_kit",
        "unknown",
    )
    assert first == second
    rows = equipment.list_items(runtime.engine, location_key="home")
    assert len([r for r in rows if r["item"] == "resistance bands"]) == 1
    assert rows[0]["status"] == "unknown"


def test_equipment_prescribable_only_confirmed(runtime):
    from vibesynergy.query import equipment

    equipment.upsert_item(runtime.engine, "pool", "kickboard", "pool_kit", "confirmed")
    equipment.upsert_item(runtime.engine, "pool", "snorkel", "pool_kit", "unknown")
    equipment.upsert_item(runtime.engine, "pool", "broken fins", "pool_kit", "unavailable")
    confirmed = equipment.confirmed_items(runtime.engine, "pool")
    prescribable = equipment.prescribable_items(runtime.engine, "pool")
    assert {row["item"] for row in confirmed} == {"kickboard"}
    assert prescribable == confirmed


def test_workout_prompt_excludes_non_confirmed_equipment(runtime):
    from vibesynergy.query import coach, equipment

    equipment.upsert_item(runtime.engine, "home", "dynabands", "gym_kit", "confirmed")
    equipment.upsert_item(runtime.engine, "home", "foam roller", "gym_kit", "unknown")
    prompt = coach.workout_prompt_context(runtime.engine, "home")
    names = {row["item"] for row in prompt["equipment_register"]["items"]}
    assert "dynabands" in names
    assert "foam roller" not in names
    assert coach.WORKOUT_EQUIPMENT_RULE in prompt["equipment_register"]["rule"]


def test_equipment_invalid_status(runtime):
    import pytest

    from vibesynergy.query import equipment

    with pytest.raises(ValueError, match="status must be"):
        equipment.upsert_item(runtime.engine, "home", "x", "gym_kit", "maybe")


def test_oauth_mcp_identity_not_clobbered():
    from vibesynergy.mcp.asgi import McpIdentityMiddleware
    from vibesynergy.mcp.identity import current_author, reset_connection, set_connection

    async def inner_app(scope, receive, send):
        assert current_author() == "client:coach"

    middleware = McpIdentityMiddleware(inner_app, "oauth")
    tokens = set_connection("coach", "client:coach")

    async def run():
        await middleware({"type": "http"}, lambda: None, lambda *a: None)

    import anyio

    anyio.run(run)
    reset_connection(tokens)


def test_portal_coach_feed_without_quick_reply(runtime):
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    coach_workouts.coach_write(
        runtime.engine,
        entry_type="morning",
        summary="Train zoals gepland.",
        idempotency_key="feed-1",
        for_date=date(2026, 10, 11),
        author="ai_coach",
    )
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    headers = {"x-vibesynergy": "portal"}
    listed = client.get("/api/coach/feed", headers=headers)
    assert listed.status_code == 200
    entry = listed.json()["data"]["entries"][0]
    assert entry["author"] == "ai_coach"
    assert entry["advice_text"].startswith("Train")
