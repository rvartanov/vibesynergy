# AI harness

The **AI harness** is VibeSynergy's AI module. It runs fixed tasks on a schedule and on
triggers, with any provider or model that meets the task's requirements. Each task gets a
fixed base context plus its own chain of context blocks.

## Principles

1. **Tasks and context chains:** the registry is defined in code, with tests. The choices and
   texts are editable in the portal: on/off, trigger, model, prompt, context chain and budget.
2. **Model requirement:** a model must be able to use tools. A model without tool use cannot
   be chosen for a task.
3. **Approval:** worked out per task and per part. The table below is a first proposal.
4. **The harness is the AI module.** The Claude Code cloud routines go away once the harness
   covers them. Access over MCP stays possible, but still needs to be designed.
5. **Every task can be switched on and off and configured in the harness**, including all
   routines and reports.

## The four guarantees (#88)

Every model call goes through the harness: `coach_runner.runner._loop` is the only place
that calls a model (`model.create`), and it runs only tasks from the registry. For each run:

1. **One registry** (`coach_runner/registry.py`): a job that is not a live model task there
   does not run.
2. **A versioned prompt and rules**: the task's prompt is a knowledge-base article with a
   version; the harness rules have `RULES_VERSION` (a test pins the text to the number).
3. **A fixed context chain**: `BASE_CONTEXT` (athlete, communication rule, safety, coach
   memory), then the task's own blocks, each with a version.
4. **An output contract that is validated and fails closed** (`coach_runner/contract.py`):
   checked when the entry is written, for the runner and for MCP clients alike, and again
   when the run ends.

The stamp `h<harness>.r<rules>.p<prompt>.c<context>` is stored on the run and on every
entry. Details and the audit of 17-11-2026: [COACH_RUNNER.md](COACH_RUNNER.md#the-harness-contract).

## Daily advice: one source (#89)

What every view shows as today's advice is `query/daily_advice.resolve`: the newest AI advice
that passes the contract decides colour, safety level and reasons; otherwise the fixed rules,
with a reason from a fixed list. The setting "Daily advice" is AI (default), rules only or
off. The runner-only hard-rule floor (illness, red-flag note, pain 7+, injury that changes
movement) is the one fixed exception. Details: [COACH_RUNNER.md](COACH_RUNNER.md#daily-advice-ai-first-the-rules-as-the-fallback-89).

## Layers

1. **Triggers**: when a task runs.
   - schedule: time and days;
   - event: activity in or published, check-in, sync done, injury logged, plan changed, run
     missed, reply to a report;
   - manual: test now, make again, update advice on the dashboard (#89).
2. **Task registry**: per task its id, goal, triggers, **output contract**, allowed tools and
   write rights, model requirements (tool use, context length), budget, prompt (KB article),
   provider and model, and on/off.
3. **Context**:
   - a **fixed base** for every task: athlete profile, communication rule, core KB rules,
     injuries and safety, coach memory;
   - a **context chain** per task: ordered context blocks, each with a token budget and a
     version, loaded before the first model turn so the model does not have to fetch
     everything itself.
4. **Execution**:
   - a provider adapter (Anthropic, the OpenAI format, local);
   - a capability check before the run;
   - the tool loop, budget and cost, retries, idempotency.
5. **Actions and guardrails**:
   - what a task may write;
   - what needs the athlete's approval (a proposal, then "akkoord", then execute);
   - the output checked against the contract.
6. **Delivery**: mail, push, portal, Strava and Garmin, following the communication rule.
7. **Observation**: run log (cost, versions), failure alerts, contract checks, prediction
   scores, later evals.
8. **Configuration (portal)**: one screen per task.

## Consistency

Gaps that appear when one thing changes and what depends on it does not are caught in three
layers (#60, #61, #62):

1. **Plan checks** (`query/plan_lint.py`, deterministic, no model):
   - **What they check:**
     - name against length;
     - strength warm-up (STR-4);
     - references to sessions that are gone;
     - neighbours of a changed session (`review_context`).
   - **When they run:**
     - every plan write returns them;
     - the start page and `vibesynergy_today` (`plan_checks`) list the next two weeks.
   - **Knowledge base:** a KB proposal that would replace a rule with unrelated text needs
     `replace=true`.
2. **AI auditor** (#61): an independent harness task that reviews the plan, the notes, the
   rules and recent change reasons, and proposes fixes. Planned.
3. **Weekly harness audit** (#62): tasks, contracts, pending approvals, missing rules. Each
   accepted finding becomes a layer-1 check or a rule. Planned.

## Task list

Every task is switchable and configurable. "Writes" is what the task may do on its own;
"approval" is what waits for the athlete's yes. This is a first proposal, to be worked out per
task.

| # | Task | Trigger | Output | Writes on its own | Needs approval | Now |
|---|---|---|---|---|---|---|
| 1 | Morning advice | schedule, daily | report (mail/push/portal) | the report; fill day 9 of the horizon | changing today or tomorrow | API runner |
| 2 | Evening review | schedule, daily | report | the report; predictions | moving sessions | API runner |
| 3 | Week review | schedule, Sunday | report + next-week proposal | the report | next week's changes | API runner |
| 4 | Insight under a public activity | event: activity published | line in the Strava/Garmin text | the insight | – | API runner |
| 5 | Reply to a report | event: the athlete replies | answer, or execute "akkoord" | the answer | the approved action itself | inside 1–3 |
| 6 | Horizon fill | schedule, daily | sessions for day 9 | sessions within the week note | changes beyond the note | inside 1 |
| 7 | Injury check | event: pain ≥4/10 or affecting movement, logged for today (once a day) | advice | the advice | plan changes (proposal + akkoord) | API runner (since 10-11) |
| 8 | Check-in reaction | event: check-in with 2+ low scores after the morning report, before 20:00 (once a day) | short advice for today | the advice | swapping today's session (proposal + akkoord) | API runner (since 10-11) |
| 9 | Weather swap | schedule, evening | outdoor ↔ indoor proposal | – | the swap | inside 2 |
| 10 | Test evaluation | event: planned test (run test, FTP, CSS) done and synced | evaluation + threshold proposal | the evaluation, the proposal | new thresholds (approved in VibeSynergy) | API runner (since 10-11) |
| 11 | Threshold check | schedule: Monday 09:34, only on a new signal (max HR above, resting HR moved) | evaluation + threshold proposal | the report, the proposal | the change (approved in VibeSynergy) | API runner (since 10-11) |
| 12 | Block review | schedule: the Sunday a plan phase ends, 20:14 | report | the report | the next block (proposal + akkoord) | API runner (since 10-11) |
| 13 | Race preparation | schedule: race −7 and −1 days, 18:04 | race brief (pacing, nutrition, kit) | the brief | taper changes (proposal + akkoord) | API runner (since 10-11) |
| 14 | Monthly report | schedule, monthly (day 1–28) | report: trend, fitness, goals | the report | – | API runner (since 10-11) |
| 15 | Prediction scoring | after every sync | scored predictions | scores | – | code (`predictions.score_due`); not switchable: bookkeeping, no model, no cost |
| 16 | Missed-run watch | event: coach.run_missed | alert; today's report caught up once | the alert, the catch-up | – | code, in the registry (since 10-11) |
| 17 | Plan fallback | on demand | Excel and PDF of the plan (Planning → Download) | – | – | portal download (since 10-11; the Google Sheet routine is retired) |
| 18 | Knowledge-base care | schedule: 15th of the month, 10:34 | five oldest articles checked, change proposals | the report, the proposals | KB changes (approved in VibeSynergy; direct writes refused) | API runner (since 10-11) |
| 19 | Plan draft | manual | draft plan | – | applying it | new |
