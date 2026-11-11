"""Per-app author: new rows are client:<id>, never a fresh legacy label."""

from __future__ import annotations

import anyio
from mcp import Client

from vibesynergy.mcp.identity import (
    AI_COACH_AUTHOR,
    author_for_client,
    note_source,
    read_author,
    reset_connection,
    set_connection,
)
from vibesynergy.mcp.server import build_server
from vibesynergy.store.schema import note


def test_author_for_client_names_the_app() -> None:
    assert author_for_client("oa-claude") == "client:oa-claude"
    assert author_for_client("pat:abc") == "client:pat:abc"
    assert author_for_client("coach") == "client:coach"
    assert author_for_client("shared") == "client:shared"
    assert author_for_client("user") == "user"
    assert author_for_client("stdio") == "user"
    assert "legacy" not in {
        author_for_client("shared"),
        author_for_client("pat:1"),
        author_for_client("oa-claude"),
    }


def test_legacy_coach_author_reads_as_the_ai_coach() -> None:
    assert read_author("sherpa") == AI_COACH_AUTHOR == "ai_coach"
    assert read_author("client:oa-1") == "client:oa-1"
    assert read_author("user") == "user"
    assert read_author(None) is None


def test_note_source_is_claude_only_for_that_name() -> None:
    tokens = set_connection("oa-1", label="Claude")
    try:
        assert note_source() == "claude"
    finally:
        reset_connection(tokens)
    tokens = set_connection("oa-2", label="Hermes")
    try:
        assert note_source() == "client:oa-2"
    finally:
        reset_connection(tokens)


def test_add_note_stores_the_app_not_claude(runtime) -> None:
    async def go():
        tokens = set_connection("oa-hermes", label="Hermes")
        try:
            async with Client(build_server(runtime)) as client:
                return await client.call_tool(
                    "vibesynergy_add_note", {"text": "Easy day, legs heavy"}
                )
        finally:
            reset_connection(tokens)

    result = anyio.run(go)
    assert not result.is_error
    with runtime.engine.connect() as conn:
        stored = conn.execute(note.select()).mappings().all()
    assert stored[-1]["source"] == "client:oa-hermes"
    assert stored[-1]["source"] != "claude"
