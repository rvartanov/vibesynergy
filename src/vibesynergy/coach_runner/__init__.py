"""API coach runner (docs/COACH_RUNNER.md).

Runs the coach on the Claude API from inside VibeSynergy instead of Claude Code cloud routines:
the morning, evening and weekly jobs on the scheduler, and the public insight when the
athlete makes an activity public (``activity.published``). The model gets VibeSynergy's own MCP
tools in-process, writes its coach entry with ``vibesynergy_coach_write``, and the entry is
mailed as the report. Off by default; switched on in Coach → Settings once an API key is set.

Modules: ``settings`` (switch, cap, key), ``pricing`` (cost from usage), ``model`` (the model
client; tests use a fake), ``tools`` (the in-process tool box), ``prompts`` (docs/coach-prompts),
``runner`` (one run: loop, budget, report), ``schedule`` (when jobs are due, the job queue).
"""

from __future__ import annotations

MODEL = "claude-opus-5-5"  # the athlete's decision (1 Nov 2026): Opus for every coaching run.
# the athlete wants a choice per kind of run in Coach → Settings; Opus stays the default.
MODELS = ("claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001")
MODEL_KINDS = ("coach", "insight")  # coach: morning, evening, week review; insight: the line
from vibesynergy.coach_runner import registry  # noqa: E402  (tasks live in the registry)

JOBS = registry.live()
REPORT_JOBS = registry.reports()
