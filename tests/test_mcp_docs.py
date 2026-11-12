"""docs/MCP.md names the tools the server really has: none missing, none invented."""

from __future__ import annotations

import re
from pathlib import Path

from vibesynergy.mcp.server import build_server

DOC = Path(__file__).parents[1] / "docs" / "MCP.md"
# A tool name in the text; not an attribute such as server.vibesynergy_tool_scopes.
NAME = re.compile(r"(?<![.\w])vibesynergy_[a-z0-9_]+")
# Named in the document on purpose, as removed or as not built ("Implementation status").
GONE = {
    "vibesynergy_coach_propose",
    "vibesynergy_plan_status",
    "vibesynergy_plan_withdraw",
    "vibesynergy_ecg",
    "vibesynergy_nutrition",
    "vibesynergy_result",
    "vibesynergy_chart",
}


def test_the_document_and_the_registry_name_the_same_tools(runtime) -> None:
    registered = set(build_server(runtime).vibesynergy_tool_scopes)  # type: ignore[attr-defined]
    named = set(NAME.findall(DOC.read_text()))
    # `vibesynergy_icu_*` stands for a family: the prefix must match a registered tool.
    families = {n for n in named if n.endswith("_")}
    unknown_family = sorted(f for f in families if not any(t.startswith(f) for t in registered))
    assert not registered & GONE, sorted(registered & GONE)
    missing = sorted(registered - named)
    invented = sorted(named - families - registered - GONE)
    assert not missing, f"registered, but not in docs/MCP.md: {missing}"
    assert not invented and not unknown_family, (
        f"docs/MCP.md names tools that are not registered: {invented + unknown_family}"
    )
