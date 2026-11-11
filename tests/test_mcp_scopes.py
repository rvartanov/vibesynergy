"""Tool calls honour the token's scopes."""

from __future__ import annotations

import anyio
from mcp import Client

from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server


def _call(runtime, scopes: frozenset[str], tool: str, arguments: dict):
    async def go():
        tokens = set_connection("reader", scopes=scopes, label="Reader")
        try:
            async with Client(build_server(runtime)) as client:
                return await client.call_tool(tool, arguments)
        finally:
            reset_connection(tokens)

    return anyio.run(go)


def test_read_only_token_cannot_add_a_note(runtime) -> None:
    result = _call(runtime, frozenset({"read"}), "vibesynergy_add_note", {"text": "no"})
    assert result.is_error
    text = result.content[0].text
    assert "isn't allowed" in text


def test_diary_scope_can_add_a_note(runtime) -> None:
    result = _call(runtime, frozenset({"diary.write"}), "vibesynergy_add_note", {"text": "ok"})
    assert not result.is_error


def test_annotations_mark_publish_and_edit(runtime) -> None:
    async def go():
        async with Client(build_server(runtime)) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in anyio.run(go)}
    assert tools["vibesynergy_refresh"].annotations.open_world_hint is True
    assert tools["vibesynergy_activity_publish"].annotations.open_world_hint is True
    assert tools["vibesynergy_activity_unpublish"].annotations.open_world_hint is True
    assert tools["vibesynergy_edit_note"].annotations.destructive_hint is True
    assert tools["vibesynergy_day"].annotations.open_world_hint is False
    assert tools["vibesynergy_sql"].annotations.read_only_hint is True
