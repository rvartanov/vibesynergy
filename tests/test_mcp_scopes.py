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


def test_a_public_insight_needs_the_publish_permission(runtime) -> None:
    arguments = {
        "entry_type": "activity_assessment",
        "summary": "Solid tempo run.",
        "idempotency_key": "insight-1",
        "for_date": "2026-09-01",
        "activity_id": "garmin:1",
        "public_insight": "Steady pace from start to finish.",
    }
    denied = _call(
        runtime, frozenset({"read", "coach.write"}), "vibesynergy_coach_write", arguments
    )
    assert denied.is_error and "isn't allowed to publish" in denied.content[0].text
    # Without the public line the same entry is the coach's to write.
    private = {k: v for k, v in arguments.items() if k not in ("public_insight", "activity_id")}
    private["entry_type"] = "morning"
    assert not _call(
        runtime, frozenset({"read", "coach.write"}), "vibesynergy_coach_write", private
    ).is_error


def test_a_public_insight_never_holds_private_terms_or_links() -> None:
    import pytest

    from vibesynergy.query import publication_insight as insight

    assert insight.validate(" Steady pace. ") == "Steady pace."
    for text in ("Good run despite poor sleep.", "Knee pain held back the pace.", "See www.x.io"):
        with pytest.raises(insight.InsightError):
            insight.validate(text)
        assert insight.normalise(text) is None
