"""Coach decision predictions: storage, scoring from daily values, and read-back."""

from __future__ import annotations

import inspect
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from mcp import Client
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from vibesynergy.cli import main as cli_main
from vibesynergy.core.timeutil import Window
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.query import coach_workouts, predictions
from vibesynergy.store import db
from vibesynergy.store.schema import coach_decision_log, coach_prediction, daily_unified, metadata
from vibesynergy.sync.engine import SyncEngine

ENTRY_DAY = date(2026, 10, 5)
TARGET = ENTRY_DAY + timedelta(days=7)  # 2026-10-12
NEXT_DAY = ENTRY_DAY + timedelta(days=8)


def _week(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _call(runtime, scopes: frozenset[str] | None, tool: str, arguments: dict):
    async def go():
        tokens = set_connection("reader", scopes=scopes, label="Reader")
        try:
            async with Client(build_server(runtime)) as client:
                return await client.call_tool(tool, arguments)
        finally:
            reset_connection(tokens)

    return anyio.run(go)


def _write(runtime, **kwargs):
    payload = {
        "entry_type": "morning",
        "summary": "Synthetic plan for the test.",
        "idempotency_key": "pred-1",
        "for_date": ENTRY_DAY,
        "author": "client:synthetic",
        "meta": {"model_id": "synthetic-model"},
    }
    payload.update(kwargs)
    return coach_workouts.coach_write(runtime.engine, **payload)


def _prediction(**kwargs):
    row = {
        "metric": "hrv.night_avg",
        "horizon_days": 7,
        "predicted": 1.0,
        "ci_low": 0.5,
        "ci_high": 1.5,
        "model_id": "synthetic-model",
    }
    row.update(kwargs)
    return row


def _daily(engine, day: date, metric: str, value: float, *, subject_id: int = 1) -> None:
    with engine.begin() as conn:
        conn.execute(
            daily_unified.insert().values(
                subject_id=subject_id,
                date=day,
                metric=metric,
                value=value,
                unit="synthetic",
                source="synthetic",
            )
        )


def test_migration_0029_adds_coach_prediction(tmp_path):
    engine = db.create_db_engine(tmp_path / "pred.db")
    try:
        db.upgrade(engine, "0028")
        assert "coach_prediction" not in sa_inspect(engine).get_table_names()
        db.upgrade(engine)
        assert db.current_revision(engine) == db.head_revision()
        inspected = sa_inspect(engine)
        assert "coach_prediction" in inspected.get_table_names()
        columns = {col["name"] for col in inspected.get_columns("coach_prediction")}
        assert columns == {
            "id",
            "entry_id",
            "for_date",
            "horizon_days",
            "metric",
            "predicted",
            "ci_low",
            "ci_high",
            "model_id",
            "actual",
            "error",
            "scored_at",
        }
        fks = inspected.get_foreign_keys("coach_prediction")
        assert fks[0]["referred_table"] == "coach_decision_log"
        assert fks[0]["referred_columns"] == ["id"]
        with engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn), metadata) == []
    finally:
        engine.dispose()


def test_coach_write_stores_predictions_and_rules_and_stays_compatible(runtime):
    plain = coach_workouts.coach_write(
        runtime.engine,
        entry_type="note",
        summary="No forecast.",
        idempotency_key="plain-1",
        for_date=ENTRY_DAY,
        author="client:synthetic",
    )
    assert plain["predictions"] == []
    assert "kb_rules" not in (plain["meta"] or {})

    saved = _write(
        runtime,
        predictions=[
            _prediction(),
            _prediction(horizon_days=8, predicted=5.0, ci_low=4.0, ci_high=6.0, model_id=None),
        ],
        kb_rules=["HRV-2", "LOAD-1"],
    )
    assert saved["meta"]["kb_rules"] == ["HRV-2", "LOAD-1"]
    assert saved["meta"]["model_id"] == "synthetic-model"
    by_horizon = {row["horizon_days"]: row for row in saved["predictions"]}
    assert by_horizon[7]["for_date"] == TARGET.isoformat()
    assert by_horizon[7]["predicted"] == 1.0
    assert by_horizon[7]["actual"] is None
    assert by_horizon[7]["model_id"] == "synthetic-model"
    assert by_horizon[8]["model_id"] == "synthetic-model"
    assert by_horizon[8]["for_date"] == NEXT_DAY.isoformat()

    again = _write(
        runtime,
        summary="Different text that must not replace the first write.",
        predictions=[_prediction(predicted=9.0, ci_low=8.0, ci_high=10.0)],
        kb_rules=["OTHER-1"],
    )
    assert again["id"] == saved["id"]
    assert again["text"].startswith("Synthetic plan")
    assert again["meta"]["kb_rules"] == ["HRV-2", "LOAD-1"]
    assert [row["predicted"] for row in again["predictions"]] == [1.0, 5.0]
    with runtime.engine.connect() as conn:
        count = conn.execute(select(coach_prediction.c.id)).all()
    assert len(count) == 2


@pytest.mark.parametrize(
    ("prediction", "match"),
    [
        (_prediction(metric="activity.load"), "not stored"),
        (_prediction(metric="checkin.mood"), "not stored"),
        (_prediction(metric="value"), "unknown metric"),
        (_prediction(metric="not.a.metric"), "unknown metric"),
        (_prediction(horizon_days=-1), "horizon_days"),
        (_prediction(horizon_days=400), "horizon_days"),
        (_prediction(ci_low=2.0, ci_high=1.0), "ci_low"),
        (_prediction(ci_low=0.5, ci_high=None), "together"),
        (_prediction(predicted=9.0), "between"),
    ],
)
def test_prediction_input_is_rejected(runtime, prediction, match):
    with pytest.raises(ValueError, match=match):
        _write(runtime, idempotency_key=f"bad-{match}-{prediction}", predictions=[prediction])
    with runtime.engine.connect() as conn:
        assert conn.execute(select(coach_prediction.c.id)).all() == []


def test_predictions_require_a_date_and_rule_ids_are_checked(runtime):
    with pytest.raises(ValueError, match="for_date"):
        coach_workouts.coach_write(
            runtime.engine,
            entry_type="note",
            summary="Missing day.",
            predictions=[_prediction()],
            author="client:synthetic",
        )
    with pytest.raises(ValueError, match="rule id"):
        _write(runtime, idempotency_key="bad-rule", kb_rules=[" "])
    with pytest.raises(ValueError, match="duplicate"):
        _write(runtime, idempotency_key="dup-rule", kb_rules=["HRV-2", "HRV-2"])


def test_score_due_fills_actual_once_from_daily_unified(runtime):
    saved = _write(runtime, predictions=[_prediction()])
    assert predictions.score_due(runtime.engine) == 0
    assert saved["predictions"][0]["scored_at"] is None

    _daily(runtime.engine, TARGET, "hrv.night_avg", 3.0, subject_id=2)
    assert predictions.score_due(runtime.engine) == 0

    _daily(runtime.engine, TARGET, "hrv.night_avg", 3.0)
    when = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    assert predictions.score_due(runtime.engine, now=when) == 1
    assert predictions.score_due(runtime.engine, now=when) == 0

    with runtime.engine.begin() as conn:
        conn.execute(
            daily_unified.update()
            .where(daily_unified.c.subject_id == 1, daily_unified.c.date == TARGET)
            .values(value=9.0)
        )
    assert predictions.score_due(runtime.engine) == 0

    row = predictions.for_entry(runtime.engine, saved["id"])[0]
    assert row["actual"] == 3.0
    assert row["error"] == 2.0
    assert row["scored_at"] == when.isoformat()


def test_later_daily_value_scores_on_the_next_run(runtime):
    first = _write(
        runtime,
        idempotency_key="open-1",
        predictions=[_prediction(horizon_days=7)],
    )
    second = _write(
        runtime,
        idempotency_key="open-2",
        for_date=ENTRY_DAY,
        predictions=[_prediction(horizon_days=8, predicted=5.0, ci_low=4.0, ci_high=6.0)],
    )
    _daily(runtime.engine, TARGET, "hrv.night_avg", 3.0)
    assert predictions.score_due(runtime.engine) == 1
    open_row = predictions.for_entry(runtime.engine, second["id"])[0]
    assert open_row["actual"] is None and open_row["scored_at"] is None
    _daily(runtime.engine, NEXT_DAY, "hrv.night_avg", 1.0)
    assert predictions.score_due(runtime.engine) == 1
    scored = predictions.for_entry(runtime.engine, second["id"])[0]
    assert scored["actual"] == 1.0 and scored["error"] == -4.0
    assert predictions.for_entry(runtime.engine, first["id"])[0]["error"] == 2.0


def test_read_entries_and_weekly_scores(runtime):
    # An older workflow row (the removed proposal pipeline wrote these) is not a coach entry.
    with runtime.engine.begin() as conn:
        conn.execute(
            coach_decision_log.insert().values(
                subject_id=1,
                at=datetime.now(UTC),
                kind="note",
                author="user",
                entry_type="workflow",
                text="Workflow row, not a coach entry.",
                meta={},
            )
        )
    _write(
        runtime,
        idempotency_key="score-a",
        entry_type="morning",
        predictions=[
            _prediction(horizon_days=7, predicted=1.0),
            _prediction(horizon_days=8, predicted=5.0, ci_low=4.0, ci_high=6.0),
        ],
        kb_rules=["HRV-2"],
    )
    _write(
        runtime,
        idempotency_key="score-b",
        entry_type="note",
        summary="A note with another metric.",
        predictions=[
            _prediction(
                metric="sleep.score",
                horizon_days=14,
                predicted=2.0,
                ci_low=1.0,
                ci_high=3.0,
            )
        ],
    )
    _daily(runtime.engine, TARGET, "hrv.night_avg", 3.0)
    _daily(runtime.engine, NEXT_DAY, "hrv.night_avg", 1.0)
    later = ENTRY_DAY + timedelta(days=14)
    _daily(runtime.engine, later, "sleep.score", 2.0)
    assert predictions.score_due(runtime.engine) == 3

    span = Window(ENTRY_DAY, later)
    mornings = predictions.coach_entries(runtime.engine, span, "morning")
    assert [row["entry_type"] for row in mornings] == ["morning"]
    assert mornings[0]["kb_rules"] == ["HRV-2"]
    assert len(mornings[0]["predictions"]) == 2
    assert "Workflow row" not in [
        row["text"] for row in predictions.coach_entries(runtime.engine, span)
    ]

    with pytest.raises(ValueError, match="entry_type"):
        predictions.coach_entries(runtime.engine, span, "workflow")

    hrv = predictions.prediction_scores(runtime.engine, span, "hrv.night_avg")
    assert hrv["metric"] == "hrv.night_avg"
    assert len(hrv["weeks"]) == 1
    week = hrv["weeks"][0]
    assert week["week"] == _week(TARGET)
    assert week["n"] == 2
    assert week["bias"] == pytest.approx(-1.0)
    assert week["rmse"] == pytest.approx(math.sqrt(10))

    both = predictions.prediction_scores(runtime.engine, span, None)
    keys = {(row["week"], row["metric"]) for row in both["weeks"]}
    assert (_week(TARGET), "hrv.night_avg") in keys
    assert (_week(later), "sleep.score") in keys
    sleep = next(row for row in both["weeks"] if row["metric"] == "sleep.score")
    assert sleep["bias"] == 0 and sleep["rmse"] == 0 and sleep["n"] == 1

    with pytest.raises(ValueError, match="unknown metric"):
        predictions.prediction_scores(runtime.engine, span, "not.a.metric")


def test_mcp_write_and_read_tools(runtime):
    written = _call(
        runtime,
        None,
        "vibesynergy_coach_write",
        {
            "entry_type": "evening",
            "summary": "Synthetic evening note.",
            "idempotency_key": "mcp-pred-1",
            "for_date": ENTRY_DAY.isoformat(),
            "model_id": "synthetic-model",
            "kb_rules": ["HRV-2"],
            "predictions": [_prediction()],
        },
    )
    assert not written.is_error
    data = written.structured_content["data"]
    assert data["meta"]["kb_rules"] == ["HRV-2"]
    assert data["predictions"][0]["metric"] == "hrv.night_avg"
    assert data["predictions"][0]["for_date"] == TARGET.isoformat()

    rejected = _call(
        runtime,
        None,
        "vibesynergy_coach_write",
        {
            "entry_type": "note",
            "summary": "Bad metric.",
            "idempotency_key": "mcp-pred-bad",
            "for_date": ENTRY_DAY.isoformat(),
            "predictions": [_prediction(metric="activity.load")],
        },
    )
    assert rejected.is_error
    assert "not stored" in rejected.content[0].text

    _daily(runtime.engine, TARGET, "hrv.night_avg", 3.0)
    predictions.score_due(runtime.engine)
    entries = _call(
        runtime,
        frozenset({"read"}),
        "vibesynergy_coach_entries",
        {"period": f"{ENTRY_DAY.isoformat()}..{TARGET.isoformat()}", "entry_type": "evening"},
    )
    assert not entries.is_error
    body = entries.structured_content["data"]["entries"]
    assert len(body) == 1
    assert body[0]["predictions"][0]["actual"] == 3.0
    scores = _call(
        runtime,
        frozenset({"read"}),
        "vibesynergy_prediction_scores",
        {"period": "2026-10", "metric": "hrv.night_avg"},
    )
    assert not scores.is_error
    weeks = scores.structured_content["data"]["weeks"]
    assert weeks[0]["n"] == 1
    assert weeks[0]["bias"] == pytest.approx(2.0)
    assert weeks[0]["rmse"] == pytest.approx(2.0)


def test_scopes_split_writes_and_reads(runtime):
    denied_write = _call(
        runtime,
        frozenset({"read"}),
        "vibesynergy_coach_write",
        {
            "entry_type": "note",
            "summary": "No.",
            "idempotency_key": "scope-write",
            "for_date": ENTRY_DAY.isoformat(),
            "predictions": [_prediction()],
        },
    )
    assert denied_write.is_error
    assert "isn't allowed" in denied_write.content[0].text

    denied_read = _call(
        runtime,
        frozenset({"coach.write"}),
        "vibesynergy_coach_entries",
        {"period": "2026-10"},
    )
    assert denied_read.is_error
    assert "isn't allowed" in denied_read.content[0].text
    denied_scores = _call(
        runtime,
        frozenset({"coach.write"}),
        "vibesynergy_prediction_scores",
        {"period": "2026-10"},
    )
    assert denied_scores.is_error
    assert "isn't allowed" in denied_scores.content[0].text

    allowed = _call(
        runtime,
        frozenset({"coach.write"}),
        "vibesynergy_coach_write",
        {
            "entry_type": "note",
            "summary": "Allowed.",
            "idempotency_key": "scope-ok",
            "for_date": ENTRY_DAY.isoformat(),
        },
    )
    assert not allowed.is_error


def test_startup_and_sync_score_predictions(runtime):
    assert runtime.sync.on_score == runtime.score_predictions
    saved = _write(runtime, predictions=[_prediction()])
    _daily(runtime.engine, TARGET, "hrv.night_avg", 3.0)
    cli_main._score_open_predictions(runtime)
    assert predictions.for_entry(runtime.engine, saved["id"])[0]["actual"] == 3.0

    calls: list[str] = []
    runtime.sync.on_score = lambda: calls.append("scored")
    runtime.sync.sync("demo", ["profile"])
    assert calls == ["scored"]

    for fn in (cli_main.serve, cli_main.mcp, cli_main.worker):
        assert "_score_open_predictions" in inspect.getsource(fn)
    assert "on_score" in inspect.getsource(SyncEngine.sync)


def test_portal_activity_labels_name_the_new_reads():
    text = Path("src/vibesynergy/portal/static/js/settings_access.js").read_text()
    assert "vibesynergy_coach_entries:" in text
    assert 'N("read coach notes")' in text
    assert "vibesynergy_prediction_scores:" in text
    assert 'N("checked predictions")' in text
