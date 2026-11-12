"""VibeSynergy's own MCP tools, called in-process for the model.

The tool box builds the same server as ``/vibesynergy/mcp`` (``mcp/server.py``) and calls its
tools directly, so every call runs the same handler, goes through ``vibesynergy.query``, checks
the same scope and lands in the audit log (``actor="mcp"``, client ``coach-runner``) like a
call over HTTP, without leaving the process. The model only sees the tools its scopes allow:
the same as the Claude coach app (read, coach.write with the knowledge base and the public
insight, plan.write for intervals.icu). The insight run gets a smaller set without diary,
health or earlier coach entries, so private data never reaches a public line (INS-2).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from typing import Any

import anyio

from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.query import publication_insight

log = logging.getLogger(__name__)

CLIENT_ID = "coach-runner"
LABEL = "AI coach (API)"
COACH_SCOPES = frozenset({"read", "coach.write", "plan.write"})
# ``publish`` because the insight run writes the one public line; its tools are limited to
# INSIGHT_TOOLS, so it cannot publish anything else.
INSIGHT_SCOPES = frozenset({"read", "coach.write", "publish"})
# The insight run reads the activity, the plan and the rules; nothing private.
INSIGHT_TOOLS = frozenset(
    {
        "vibesynergy_kb_get",
        "vibesynergy_kb_search",
        "vibesynergy_activity_insights",
        "vibesynergy_activity",
        "vibesynergy_intervals",
        "vibesynergy_icu_events",
        "vibesynergy_week",
        "vibesynergy_plan",
        "vibesynergy_thresholds",
        "vibesynergy_coach_write",
    }
)
# Never offered: fetching from the sources and writes outside the coach's role.
NEVER = frozenset({"vibesynergy_refresh", "vibesynergy_sql"})
MAX_RESULT_CHARS = 100_000
Guard = Callable[[str, dict[str, Any]], str | None]


def _group(engine, activity_id: str) -> str:
    from vibesynergy.query import publication_insight

    try:
        return publication_insight.resolve_link(engine, activity_id)
    except (LookupError, ValueError):
        return activity_id


def insight_guard(engine, activity_ids: Iterable[str] | None = None) -> Guard:
    """Only activity assessments, only the event's activities, nothing private in public."""
    allowed = {_group(engine, str(a)) for a in activity_ids or ()}

    def check(name: str, arguments: dict[str, Any]) -> str | None:
        if name != "vibesynergy_coach_write":
            return None
        if arguments.get("entry_type") != "activity_assessment":
            return "In this run only entry_type activity_assessment may be written."
        if allowed and _group(engine, str(arguments.get("activity_id") or "")) not in allowed:
            return f"Only these activities belong to this run: {', '.join(sorted(allowed))}."
        line = str(arguments.get("public_insight") or "")
        if publication_insight.PRIVATE.search(line):
            return (
                "public_insight mentions private health, diary or injury details (INS-2). "
                "Rewrite it about the session only, or leave the insight out."
            )
        return None

    return check


def kb_guard() -> Guard:
    """Knowledge-base care (task 18): changes only as a proposal the athlete approves."""

    def check(name: str, arguments: dict[str, Any]) -> str | None:
        if name == "vibesynergy_kb_upsert":
            return (
                "In this run the knowledge base is not written directly. Propose the change "
                "with vibesynergy_kb_propose_change; the athlete approves it."
            )
        return None

    return check


def _compact(text: str) -> str:
    """Tool envelopes are pretty-printed JSON; the model reads compact JSON just as well."""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        return text


class ToolBox:
    def __init__(
        self,
        rt,
        scopes: frozenset[str],
        *,
        only: frozenset[str] | None = None,
        guard: Guard | None = None,
    ) -> None:
        from vibesynergy.mcp.server import build_server

        self._server = build_server(rt)
        self._scopes = scopes
        self._guard = guard
        by_scope: dict[str, str] = self._server.vibesynergy_tool_scopes
        self._allowed = {
            name
            for name, scope in by_scope.items()
            if scope in scopes and name not in NEVER and (only is None or name in only)
        }
        self.calls: list[str] = []
        self.crashed: list[str] = []  # tools that crashed, for the failure warning

    @property
    def names(self) -> list[str]:
        return sorted(self._allowed)

    def definitions(self) -> list[dict[str, Any]]:
        """Tool definitions for the Messages API, sorted by name so the prefix (and the
        prompt cache) stays byte-identical between runs."""
        tools = anyio.run(self._server.list_tools)
        out = []
        for item in sorted(tools, key=lambda t: t.name):
            if item.name not in self._allowed:
                continue
            out.append(
                {
                    "name": item.name,
                    "description": (item.description or item.name).strip(),
                    "input_schema": item.input_schema,
                }
            )
        return out

    def call(self, name: str, arguments: Any) -> tuple[str, bool]:
        """Run one tool. Returns (text for the model, is_error). Never raises."""
        from mcp.server.mcpserver.exceptions import ToolError

        self.calls.append(name)
        if name not in self._allowed:
            return f"Unknown tool {name!r}.", True
        if not isinstance(arguments, dict):
            return "Tool input must be a JSON object.", True
        if self._guard and (problem := self._guard(name, arguments)):
            return problem, True
        tokens = set_connection(CLIENT_ID, scopes=self._scopes, label=LABEL)
        try:
            result = anyio.run(self._server.call_tool, name, arguments)
        except ToolError as exc:
            return str(exc), True
        except Exception as exc:  # a crash stays in the log; the model gets a short note
            log.exception("coach runner tool %s crashed", name)
            if name not in self.crashed:
                self.crashed.append(name)
            return f"The tool {name} failed ({type(exc).__name__}).", True
        finally:
            reset_connection(tokens)
        parts = [
            _compact(block.text) for block in result.content if getattr(block, "type", "") == "text"
        ]
        text = "\n".join(parts) or "{}"
        if len(text) > MAX_RESULT_CHARS:
            text = (
                text[:MAX_RESULT_CHARS]
                + "\n[cut: the result was longer; ask for a shorter period or fewer points]"
            )
        return text, bool(getattr(result, "is_error", False))
