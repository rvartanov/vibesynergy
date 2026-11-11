# API coach runner

The coach can run inside VibeSynergy on your own API key instead
of as Claude Code cloud routines. It does the same four jobs as the routines, with the same
prompts and the same VibeSynergy tools, and adds a mailed report with a reply box in the
portal. It is **off by default**; nothing calls the API until it is switched on in
Coach → Settings with an API key.

Code: `src/vibesynergy/coach_runner/` (runner, schedule, tools, prompts, settings, pricing,
report), `query/coach_runner.py` and `query/coach_replies.py` for the portal,
`portal/coach_runner_api.py`. Migration `0038` adds `coach_run` and `coach_reply`.

## How a run works

| Job | When (Europe/Amsterdam) | Prompt | Writes | Report mail |
| --- | --- | --- | --- | --- |
| `morning` | daily 09:04 | `docs/coach-prompts/morning.md` | entry `morning`, key `morning-<date>` | "Coach ochtend – zo 1 nov" |
| `evening` | daily 21:45 | `evening.md` | entry `evening`, key `evening-<date>` | "Coach avond – …" |
| `weekly` | Sunday 19:44 | `weekly.md` | entry `note`, key `weekly-<date>`, summary starts "Weekreview <ISO-week>" | "Coach weekreview – …" |
| `insight` | on `activity.published` | `insight.md` | `activity_assessment` with `public_insight` | none |

1. **Trigger.** The scheduler asks `coach_runner.schedule.due` every tick; a due job is
   queued as a `coach_run` job (dedupe key `coach_run:<job>:<date>`). A job still starts up
   to 3 hours late (after a restart, say), never on the next day. The insight run is queued
   by an *internal subscriber* of the agent events (`schedule.on_agent_event`, see
   [EVENTS.md](EVENTS.md)) in the same transaction that stores the `activity.published`
   event, only for activities still missing their public insight. No HTTP is involved.
2. **Lane.** `coach_run` jobs run in their own worker lane (`coach`), so a run of a few
   minutes never holds up syncs or Strava webhooks.
3. **Prompt.** System = the MCP guide (`GUIDE` in `mcp/server.py`) + the job prompt from
   `docs/coach-prompts/<job>.md` with its leading HTML comment stripped, routine-only
   instructions rewritten (SendUserMessage → the entry's summary; ToolSearch and the Garmin
   connector → the VibeSynergy tools and intervals.icu) and the runner rules appended. Edit the
   markdown files to change the coach; the next run uses them. `[coach_runner] prompts_dir`
   points elsewhere if wanted. The first user message holds the date, ISO week, the
   idempotency key to use and **the athlete's unread replies** (or, for an insight, the fire
   payload `event=… event_id=… activity_ids=…`).
4. **Tools.** The same MCP server as `/vibesynergy/mcp`, called in-process
   (`MCPServer.call_tool`), so every call goes through `vibesynergy.query`, checks its scope and
   is audit-logged with client `coach-runner` (author `client:coach-runner`, label
   "AI coach (API)"). Scopes are those of the Claude coach app: `read`, `coach.write`
   (entries, knowledge base, public insight) and `plan.write` (intervals.icu events). Never
   offered: `sql`, `publish`, `sync` (`vibesynergy_refresh`), `diary.write`, `plan.propose`.
   The insight run only gets the activity, plan and knowledge tools and `vibesynergy_coach_write`
   (no diary, health or earlier entries), may only write `activity_assessment` for the
   event's activities, and a `public_insight` naming health, diary or injury details is
   refused back to the model (INS-2).
5. **Model loop.** Messages API with tool use, model `claude-opus-5-5` for every run
   (the athlete's decision), adaptive thinking, effort `high` (`[coach_runner] effort`). Prompt
   caching: a breakpoint at the end of the system prompt covers the tool definitions and the
   system (tools render first and are sorted by name, so the prefix is byte-identical
   between runs), and top-level automatic caching moves a second breakpoint to the end of
   the conversation every turn. No refusal fallback to another model: a refused run fails.
6. **Report.** After the loop the runner finds the entry by its idempotency key. Its text is
   mailed through `alerts.deliver` (the `[mail]` settings and the alert address of
   Settings → Alerts), with a link to `#/coach/report/<id>` in the portal. Unread replies
   that were in the start message are marked read with the run id. No entry means the run
   failed (nothing is mailed).

## Why the Messages API and not the Agent SDK

The Claude Agent SDK is Claude Code as a library: it starts the Claude Code CLI as a
subprocess and brings its own file, shell and web tools, sessions and permission system.
VibeSynergy needs none of that. It needs exactly its own tools, called in-process with the
right scopes and audit, a hard per-turn budget check, usage per response for the monthly
cap, and something that runs inside the existing worker without Node or a CLI. A short
manual tool loop over the Messages API (`anthropic` SDK) gives all of that in one module,
and the model client is one small protocol, so tests run on a scripted fake with no network.

## Budget and safety

- **Per run:** `max_turns` (default 40) and `max_output_tokens` for all turns together
  (default 60 000; at most 16 000 per response). Set in `[coach_runner]` in config.toml.
- **Per month:** the cap (default **$95**, `[coach_runner] monthly_cap_usd`, the portal
  value wins). Cost is computed from every response's `usage` with the published Opus 5.5
  prices: $4/MTok input, $20/MTok output, $5/MTok 5-minute cache write, $0.20/MTok cache
  read. Usage is stored after every turn, so a crashed run still counts. The cap is checked
  before each turn. At **80 %** and **100 %** VibeSynergy warns once per month by mail and in
  the log; at 100 % runs stop (recorded as `budget`) until the next month or a higher cap.
  The Console's own spend limit on the workspace is the hard backstop.
- **Idempotency:** `coach_run` is unique per (job, local date), or per event for insights.
  A finished run never repeats; a crash or a transient API error (429, 5xx, network) leaves
  it retryable once (two attempts in all); a permanent error (bad key, 400) is not retried.
- **Logging:** INFO only says which job ran, how it ended, turns and cost. Prompts, replies,
  tool arguments and results never go to the log. Each run is one audit row
  (`coach_runner.run`, actor `coach_runner`: job, status, turns, stop reason, cost, entry);
  every tool call has its own `mcp` audit row as usual.
- **API key:** stored encrypted in the `coach_runner` secret store, write-only in the page,
  never in a response, log line or audit row (only "set" and when it changed).

## Replies and approvals

Each coach entry on the Coach page has a reply box; the mail links to the report page with
the same box. A reply saves at once and can be changed until the coach has read it. The next
run (morning, evening or weekly) gets every unread reply in its start message and marks
them read afterwards. An explicit "akkoord" or "ja" on a concrete proposal counts as
The athlete's approval for that change (ICU-4): the coach carries out only that proposal and
quotes the reply in its entry. When the reply is ambiguous it does nothing and asks.

## Garmin multisport (brick) workouts

Not included. VibeSynergy no longer writes workouts to Garmin: the plan reaches Garmin
through intervals.icu. A brick is planned as its parts in intervals.icu, as today. A
dedicated Garmin multisport upload would need a new write path through the Garmin source
with ownership checks and read-back; that is a separate issue if wanted.

## Switching from the routines to the API

1. claude.ai → Settings → Billing → API credits: link the Console organisation once the
   option shows.
2. In the Console: create a workspace "VibeSynergy coach" with a monthly spend limit of $95,
   and an API key in that workspace.
3. VibeSynergy → Coach → Settings → Coach via API: paste the key (it is never shown again),
   check the monthly limit, switch it on.
4. Turn off the cloud routines that do the same jobs (claude.ai/code/routines: "Coach
   ochtend", "Coach avond", "Coach weekreview", "Coach na training"), and switch off the
   "Coach AI after a workout" webhook link in Settings → Sync between sources. Otherwise both
   run: the entries stay single (same idempotency keys) but you pay twice and get two
   messages.
5. Watch the first day: Coach → Settings shows the last run per job and this month's usage.

Switching back: switch it off in VibeSynergy and turn the routines on again.

## Costs

What a run costs depends on how many tools the coach calls and how long it thinks. A
morning or evening run is roughly 10–20 turns: the cached prefix (tools + prompt, about
20 000 tokens) is written once per run and read on every turn, each turn adds a few
thousand tokens of tool results and one to two thousand tokens of output. That comes to
roughly $0.40–1.00 per daily run, a little more for the weekly review and $0.10–0.30 per
insight: around $40–80 a month at two daily runs, a weekly review and one insight a day.
Coach → Settings shows the real figures for the month. Lowering `[coach_runner] effort` to
`medium` is the first lever if it runs close to the cap.

## Deploy

Code merge, then on the server: `uv sync --frozen`, `uv run vibesynergy db upgrade` (migration
0038 adds two tables; safe while the old code runs), then restart. It stays off until
switched on in the portal.
