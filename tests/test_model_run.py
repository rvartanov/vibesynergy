"""Personal model runs: stored parameters, markdown report, and a param diff."""

from __future__ import annotations

from datetime import date, datetime

import anyio
import pytest
from mcp import Client
from sqlalchemy import inspect

from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.query.model_run import list_runs, write_run
from vibesynergy.store.db import _alembic_config, create_db_engine, upgrade


def test_migration_0030_depends_on_0029_and_roundtrips(tmp_path) -> None:
    from alembic import command

    engine = create_db_engine(tmp_path / "model-run.db")
    upgrade(engine, "0029")
    assert "model_run" not in inspect(engine).get_table_names()
    upgrade(engine, "head")
    assert "model_run" in inspect(engine).get_table_names()
    columns = {col["name"] for col in inspect(engine).get_columns("model_run")}
    assert columns == {
        "id",
        "model_id",
        "started_at",
        "finished_at",
        "params",
        "report",
        "diff",
        "created_by",
    }
    cfg = _alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0029")
    assert "model_run" not in inspect(engine).get_table_names()
    engine.dispose()


def test_first_run_has_no_diff_and_second_run_diffs_params(runtime) -> None:
    first = write_run(
        runtime.engine,
        model_id="durability",
        params={"ftp": 250, "zones": {"z2": 140}},
        report="First fit.",
        started_at="2026-10-01T08:00:00+02:00",
        finished_at="2026-10-01T08:05:00+02:00",
        timezone="Europe/Amsterdam",
    )
    assert first["diff"] is None
    assert first["created_by"] == "user"
    assert first["model_id"] == "durability"
    assert first["report"] == "First fit."
    assert first["params"] == {"ftp": 250, "zones": {"z2": 140}}
    assert datetime.fromisoformat(first["started_at"]) == datetime.fromisoformat(
        "2026-10-01T06:00:00+00:00"
    )

    second = write_run(
        runtime.engine,
        model_id="durability",
        params={"ftp": 255, "zones": {"z2": 142, "z3": 160}, "cp": 260},
        report="## Update\n\nFTP moved up.",
        started_at="2026-10-20T08:00:00+02:00",
        finished_at="2026-10-20T08:10:00+02:00",
        timezone="Europe/Amsterdam",
    )
    assert second["diff"]["previous_id"] == first["id"]
    assert second["diff"]["added"] == {"cp": 260, "zones.z3": 160}
    assert second["diff"]["removed"] == {}
    assert second["diff"]["changed"] == {
        "ftp": {"from": 250, "to": 255},
        "zones.z2": {"from": 140, "to": 142},
    }


def test_diff_is_against_the_same_model_only(runtime) -> None:
    write_run(
        runtime.engine,
        model_id="durability",
        params={"ftp": 250},
        report="Run model.",
        started_at="2026-10-01T08:00:00Z",
        timezone="Europe/Amsterdam",
    )
    other = write_run(
        runtime.engine,
        model_id="pace",
        params={"threshold_s_per_km": 300},
        report="Pace model.",
        started_at="2026-10-02T08:00:00Z",
        timezone="Europe/Amsterdam",
    )
    assert other["diff"] is None


def test_backdated_run_diffs_against_the_chronological_predecessor(runtime) -> None:
    early = write_run(
        runtime.engine,
        model_id="durability",
        params={"a": 1},
        report="Early.",
        started_at="2026-10-01T08:00:00Z",
        timezone="UTC",
    )
    later = write_run(
        runtime.engine,
        model_id="durability",
        params={"a": 2},
        report="Later.",
        started_at="2026-10-20T08:00:00Z",
        timezone="UTC",
    )
    middle = write_run(
        runtime.engine,
        model_id="durability",
        params={"a": 3, "b": 1},
        report="Backfill.",
        started_at="2026-10-10T08:00:00Z",
        timezone="UTC",
    )
    assert middle["diff"]["previous_id"] == early["id"]
    assert middle["diff"]["changed"] == {"a": {"from": 1, "to": 3}}
    assert middle["diff"]["added"] == {"b": 1}
    listed = list_runs(
        runtime.engine,
        model_id="durability",
        start=date(2026, 10, 1),
        end=date(2026, 10, 31),
        timezone="UTC",
    )
    assert [row["id"] for row in listed] == [later["id"], middle["id"], early["id"]]
    stored_later = next(row for row in listed if row["id"] == later["id"])
    assert stored_later["diff"]["previous_id"] == early["id"]


def test_unchanged_params_still_record_the_previous_run(runtime) -> None:
    first = write_run(
        runtime.engine,
        model_id="durability",
        params={"ftp": 250, "notes": ["easy", "steady"]},
        report="Same.",
        started_at="2026-10-01T08:00:00Z",
        timezone="UTC",
    )
    second = write_run(
        runtime.engine,
        model_id="durability",
        params={"ftp": 250, "notes": ["easy", "steady"]},
        report="Still the same.",
        started_at="2026-10-02T08:00:00Z",
        timezone="UTC",
    )
    assert second["diff"] == {
        "previous_id": first["id"],
        "added": {},
        "removed": {},
        "changed": {},
    }


def test_list_uses_the_local_date_of_started_at(runtime) -> None:
    inside = write_run(
        runtime.engine,
        model_id="durability",
        params={"ftp": 250},
        report="October.",
        started_at="2026-10-31T22:30:00Z",
        timezone="Europe/Amsterdam",
    )
    outside = write_run(
        runtime.engine,
        model_id="durability",
        params={"ftp": 260},
        report="November locally.",
        started_at="2026-10-31T23:30:00Z",
        timezone="Europe/Amsterdam",
    )
    october = list_runs(
        runtime.engine,
        model_id="durability",
        start=date(2026, 10, 1),
        end=date(2026, 10, 31),
        timezone="Europe/Amsterdam",
    )
    assert [row["id"] for row in october] == [inside["id"]]
    november = list_runs(
        runtime.engine,
        model_id="durability",
        start=date(2026, 11, 1),
        end=date(2026, 11, 30),
        timezone="Europe/Amsterdam",
    )
    assert [row["id"] for row in november] == [outside["id"]]


def test_write_rejects_blank_model_bad_params_and_inverted_times(runtime) -> None:
    with pytest.raises(ValueError, match="model_id"):
        write_run(
            runtime.engine,
            model_id="  ",
            params={"ftp": 1},
            report="x",
            timezone="UTC",
        )
    with pytest.raises(ValueError, match="JSON object"):
        write_run(
            runtime.engine,
            model_id="durability",
            params=["not", "an", "object"],
            report="x",
            timezone="UTC",
        )
    with pytest.raises(ValueError, match="finished_at"):
        write_run(
            runtime.engine,
            model_id="durability",
            params={"ftp": 1},
            report="x",
            started_at="2026-10-02T08:00:00Z",
            finished_at="2026-10-01T08:00:00Z",
            timezone="UTC",
        )


def test_removed_keys_and_replaced_lists(runtime) -> None:
    write_run(
        runtime.engine,
        model_id="durability",
        params={"keep": 1, "drop": 2, "block": ["a", "b"]},
        report="Before.",
        started_at="2026-10-01T08:00:00Z",
        timezone="UTC",
    )
    second = write_run(
        runtime.engine,
        model_id="durability",
        params={"keep": 1, "block": ["a"]},
        report="After.",
        started_at="2026-10-02T08:00:00Z",
        timezone="UTC",
    )
    assert second["diff"]["removed"] == {"drop": 2}
    assert second["diff"]["changed"] == {"block": {"from": ["a", "b"], "to": ["a"]}}
    assert second["diff"]["added"] == {}


def _call(
    runtime,
    scopes: frozenset[str] | None,
    tool: str,
    arguments: dict,
    client: str = "coach",
):
    async def go():
        tokens = set_connection(client, scopes=scopes, label="AI coach")
        try:
            async with Client(build_server(runtime)) as mcp:
                return await mcp.call_tool(tool, arguments)
        finally:
            reset_connection(tokens)

    return anyio.run(go)


def test_mcp_write_stores_connection_author_and_read_lists_the_period(runtime) -> None:
    written = _call(
        runtime,
        frozenset({"coach.write"}),
        "vibesynergy_model_run_write",
        {
            "model_id": "durability",
            "params": {"ftp": 250},
            "report": "Goed hersteld, drempel blijft.",
            "started_at": "2026-10-11T09:00:00+02:00",
            "finished_at": "2026-10-11T09:02:00+02:00",
        },
    )
    assert not written.is_error
    data = written.structured_content["data"]
    assert data["created_by"] == "client:coach"
    assert data["diff"] is None
    assert data["params"] == {"ftp": 250}

    again = _call(
        runtime,
        frozenset({"coach.write"}),
        "vibesynergy_model_run_write",
        {
            "model_id": "durability",
            "params": {"ftp": 248},
            "report": "Iets lager.",
            "started_at": "2026-10-18T09:00:00+02:00",
        },
    )
    assert again.structured_content["data"]["diff"]["changed"] == {"ftp": {"from": 250, "to": 248}}

    listed = _call(
        runtime,
        frozenset({"read"}),
        "vibesynergy_model_runs",
        {"model_id": "durability", "period": "2026-10"},
    )
    assert not listed.is_error
    runs = listed.structured_content["data"]["runs"]
    assert [row["created_by"] for row in runs] == ["client:coach", "client:coach"]
    assert listed.structured_content["meta"]["period"] == {
        "start": "2026-10-01",
        "end": "2026-10-31",
    }
    assert runs[0]["report"] == "Iets lager."


def test_mcp_scopes_split_write_and_read(runtime) -> None:
    denied = _call(
        runtime,
        frozenset({"read"}),
        "vibesynergy_model_run_write",
        {"model_id": "durability", "params": {"ftp": 1}, "report": "no"},
    )
    assert denied.is_error
    assert "isn't allowed" in denied.content[0].text

    write_only = _call(
        runtime,
        frozenset({"coach.write"}),
        "vibesynergy_model_runs",
        {"model_id": "durability", "period": "all"},
    )
    assert write_only.is_error
    assert "isn't allowed" in write_only.content[0].text
