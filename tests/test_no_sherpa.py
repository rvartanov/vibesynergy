"""The coach is the AI coach: the old external coach name ships nowhere.

Old database rows and audit entries keep the name; only the legacy-alias readers in
``ALLOWED`` know it, so they can read those rows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import anyio
from mcp import Client

from vibesynergy.mcp.server import GUIDE, build_server

SRC = Path(__file__).parents[1] / "src" / "vibesynergy"
STATIC = SRC / "portal" / "static"
OLD_NAME = re.compile("sherpa", re.IGNORECASE)
# Legacy-alias readers: old author rows and old ``keep`` values from earlier callers.
ALLOWED = {
    SRC / "mcp" / "identity.py",
    SRC / "query" / "icu_plan.py",
}
# Applied migrations are history and stay as they ran.
HISTORY = SRC / "store" / "migrations" / "versions"


def _hits(paths) -> list[str]:
    found = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if OLD_NAME.search(line):
                found.append(f"{path.relative_to(SRC)}:{number}: {line.strip()[:120]}")
    return found


def test_portal_static_files_do_not_name_the_old_coach() -> None:
    files = [path for path in STATIC.rglob("*") if path.is_file()]
    assert files
    assert not _hits(files)


def test_source_names_the_old_coach_only_in_legacy_readers() -> None:
    files = [
        path for path in SRC.rglob("*.py") if path not in ALLOWED and HISTORY not in path.parents
    ]
    assert not _hits(files)


def test_mcp_tools_and_instructions_do_not_name_the_old_coach(runtime) -> None:
    async def go():
        async with Client(build_server(runtime)) as client:
            return (await client.list_tools()).tools

    tools = anyio.run(go)
    assert tools
    assert not OLD_NAME.search(GUIDE)
    for tool in tools:
        assert not OLD_NAME.search(tool.name), tool.name
        assert not OLD_NAME.search(tool.description or ""), tool.name
        assert not OLD_NAME.search(json.dumps(tool.input_schema)), tool.name
