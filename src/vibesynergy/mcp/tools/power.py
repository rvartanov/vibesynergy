"""MCP tools: power tools (split from mcp/server.py)."""

from __future__ import annotations

from typing import Any

from vibesynergy.mcp.server import (
    READ,
    ToolKit,
)
from vibesynergy.query import (
    sql,
)


def register(kit: ToolKit) -> None:
    settings = kit.settings
    tool = kit.tool
    envelope = kit.envelope

    # Power tools ------------------------------------------------------------------------

    @tool(READ, "sql")
    def vibesynergy_sql(query: str, limit: int = 200) -> dict[str, Any]:
        """Read-only SQL (SQLite dialect) over the tables in vibesynergy_schema, for questions
        the other tools do not cover. One SELECT; max 5000 rows; 10 s limit."""
        data = sql.run(settings.db_path, query, limit=min(limit, 5000))
        warnings = (
            ["result truncated; narrow the query or raise limit"] if data["truncated"] else []
        )
        return envelope(data, warnings=warnings)
