"""MCP tools: the AI harness. Its tasks, its run log, and one run now.

The same as Coach → Settings: another AI (Claude Code, a routine) can see which coach tasks
exist, how they ran, and start one, without running the coach itself.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from vibesynergy.mcp.server import READ, WRITE, ToolKit
from vibesynergy.query import coach_runner


def register(kit: ToolKit) -> None:
    rt = kit.rt
    tool = kit.tool
    envelope = kit.envelope

    @tool(READ, "read")
    def vibesynergy_harness_tasks() -> dict[str, Any]:
        """The coach's tasks: trigger, context chain, approval, switch, schedule, provider,
        model and last run (with what was preloaded and the output-contract result)."""
        return envelope({"tasks": coach_runner.harness_tasks(rt)})

    @tool(READ, "read")
    def vibesynergy_harness_runs(task: str | None = None, limit: int = 10) -> dict[str, Any]:
        """The run log, newest first (at most 50): status, cost, turns, error, the entry it
        wrote, and ``harness`` (context blocks loaded, contract issues). ``task`` narrows it."""
        return envelope({"runs": coach_runner.harness_runs(rt, task, limit)})

    @tool(WRITE, "coach.write")
    def vibesynergy_harness_run(task: str) -> dict[str, Any]:
        """Start one run of a report task now, like Test now in Coach → Settings. It costs
        like any run and mails the report. The coach must be switched on (Coach via API)."""
        try:
            coach_runner.test_run(rt, task)
        except coach_runner.TestRunError as exc:
            raise ToolError(str(exc)) from exc
        return envelope({"queued": task})
