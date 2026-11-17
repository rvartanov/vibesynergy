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
3. **Prompt.** System = the MCP guide (`GUIDE` in `mcp/server.py`) + the task prompt (the
   knowledge-base article `coach-prompt-<job>`, else `docs/coach-prompts/<job>.md`) with its
   leading HTML comment stripped and routine-only instructions rewritten (SendUserMessage →
   the entry's summary; ToolSearch and the Garmin connector → the VibeSynergy tools and
   intervals.icu), then **the harness rules** (`coach_runner/prompts.py`, versioned; see
   [The harness contract](#the-harness-contract)). Edit the article to change the coach; the
   next run uses it. `[coach_runner] prompts_dir` points elsewhere if wanted. The first user
   message holds the date, ISO week, the idempotency key to use, **the athlete's unread
   replies** (or, for an insight, the fire payload `event=… event_id=… activity_ids=…`) and
   **the preloaded context**: the fixed base (athlete, communication rule, safety, coach
   memory) and the task's own chain.
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
5. **Model loop.** Messages API with tool use, the model chosen per task in Coach →
   Settings (default `claude-opus-5-5`; #88 changed none of them), adaptive thinking, effort `high` (`[coach_runner] effort`). Prompt
   caching: a breakpoint at the end of the system prompt covers the tool definitions and the
   system (tools render first and are sorted by name, so the prefix is byte-identical
   between runs), and top-level automatic caching moves a second breakpoint to the end of
   the conversation every turn. No refusal fallback to another model: a refused run fails.
6. **Report.** After the loop the runner finds the entry by its idempotency key and checks
   it against the task's contract once more. Its text is mailed through `alerts.deliver`
   (the `[mail]` settings and the alert address of Settings → Alerts), with a link to
   `#/coach/report/<id>` in the portal. Unread replies that were in the start message are
   marked read with the run id. No entry, or an entry outside the contract, means the run
   failed: nothing is mailed and the athlete gets the failure message.

## The harness contract

Decided on 17-11-2026 (#88): every AI call goes through the harness, with fixed rules, a
fixed prompt and a fixed output contract, so the coach works the same way every time and
with every model.

### Audit: every model call (17-11-2026)

There is one place in the code that calls a model: `runner._loop` (`model.create`). Nothing
else imports a model client. `runner.run` is reached from the scheduler (`schedule.handle`),
from events (`schedule.on_agent_event`), and from Test now / Make again in the portal and
`vibesynergy_harness_run` over MCP, which all queue the same `coach_run` job.

| Call | Registry | Versioned prompt + rules | Fixed context chain | Contract, fail closed |
| --- | --- | --- | --- | --- |
| `morning`, `evening` | yes | yes | yes | yes, with `safety` and the target day |
| `injury`, `checkin` | yes | yes | yes | yes, with `safety` and the target day |
| `weekly`, `monthly`, `block`, `race` | yes | yes | yes | yes |
| `test`, `thresholds`, `kb` | yes | yes | yes | yes; proposals also by their own tools |
| `insight` | yes | yes | yes (no health in it) | at the write: INS-2 guard and the public-line rules; no entry is allowed |
| `reply` | planned task, no model call of its own: replies are read by the next run | – | – | – |
| `catch_up` | code, no model | – | – | – |
| advice run (#89, #90) | not built yet; it must be a registry task | – | – | the `safety` contract is ready for it |
| Daily review narrative (`query/daily_coach.py`) | no model: `narrative_enabled` is false and the text is fixed | – | – | – |
| Cloud routine webhooks (`query/agent_events.py`) | outside VibeSynergy: they fire a Claude Code routine, which comes back over MCP | by `vibesynergy_coach_prompt` | by `include_context=true` | yes, at `vibesynergy_coach_write` |

Gaps found and closed in #88:

1. **A job outside the registry could run** without a context chain or a contract
   (`_preloaded` returned nothing, `contract_issues` returned no issues). Now `runner.run`
   refuses any job that is not a live model task, before a model client exists.
2. **The contract was reported, not enforced**: issues went to the log and the entry was
   mailed anyway. Required fields were declared and never checked. Now see below.
3. **The rules had no version.** `RUNNER_RULES` was a constant; a change left no trace on
   the run. Now `RULES_VERSION`, pinned to the text by a test, and a stamp on every run and
   entry.
4. **An MCP coach had a different harness**: `vibesynergy_coach_prompt` served only the
   article, not the rules the runner appends, no context and no contract, and
   `vibesynergy_coach_write` accepted anything with a summary. Now one source for all.
5. **The safety block held only the injury log.**

### Rules any model can follow

The harness rules are written for every model that can use tools, not only the strongest:

- **numbered steps** in a fixed order (read the context, decide the safety level, weigh,
  write, repair a refused write, what to do when it cannot be decided);
- **decision rules** instead of prose: the four levels and their colour, start from the
  rules' level, stricter is always allowed, milder only with the reason, never milder for
  illness or real pain;
- **the data preloaded** in the context, so nothing depends on the model choosing the right
  tools first;
- **the output as a schema with a worked example**, and the mistakes that are refused;
- only what applies: the safety steps are in the rules of the daily advice tasks only, and
  each task's own word limit is filled in.

Room for judgement is named, not left open. Step 3 lists what the rules cannot weigh and the
model must: the check-in against the measurements, measurement gaps, what was already done
today and how, and complaints or pain without an injury log. The model has to say in
`reasons` what tipped it, and in `override_reason` why it went milder than the rules; only
repeating the rule outcome is not accepted as a reason by the rules, and an empty `reasons`
is refused by the contract.

### The output contract

`coach_runner/contract.py`, called by `vibesynergy_coach_write` before anything is stored,
for the runner and for an MCP client alike. A write that misses it is **refused** with a
message that names every problem, so one more try can fix them all; the model repairs it
within the same run. Nothing invalid is stored, mailed or shown.

| Check | Rule |
| --- | --- |
| Target day | `for_date` is today for `morning`, `injury`, `checkin`; today or tomorrow for `evening`. In a run, "today" is the run's day. |
| Length | the summary is within the task's word limit × 1.5 (70 for the daily advice, 100 for test and kb, 150 for the reviews). |
| Safety (daily advice) | `safety = {level, colour, reasons, override_reason?}`: a known level, the colour of that level, 1-5 reasons. |
| Against the rules | a level milder than the rules' floor needs `override_reason`. The runner can never soften `sick`, `red_flag_note`, `pain_7` or `affects_movement`. |
| Task | `task` must be a registry task and write that task's entry type. |

**Validity, not approval.** The contract checks form and validity: the schema, a consistent
safety outcome, the target day, and the idempotency key that keeps a retry from writing
twice. It asks for no approval. The two paths stay distinct:

- **The runner** acts on its own (scheduled and event tasks). The registry's `approval`
  applies to it: what a task may not do without the athlete's yes becomes a proposal, and
  it can never soften illness or real pain.
- **An MCP client** acts for the athlete: the request is his own, through his AI app, and
  is already approved. Its writes get the same validity checks and nothing more.

`meta.harness.via` (`runner` or `mcp`) and the author (`client:coach-runner` or the app's
own client id) say which path wrote an entry.

After the loop the runner checks what only the run knows (`runner.contract_issues`): the
entry is of the task's type, for the run's day, within the length, written under this run's
harness, with its safety outcome. Any issue fails the run: the entry is marked
`meta.harness.valid = false`, it is not mailed, the run is `failed` with
`output contract: …`, and the athlete gets the failure message.

Every entry stores `meta.safety` (the validated outcome: level, colour, reasons, override)
and `meta.harness`: `version`, `via` (runner or mcp), `task`, `stamp`
(`h<harness>.r<rules>.p<prompt>.c<context>`), `current`, `follows_current` and `rules_floor`
(the rules' level and rules at that moment). #89 can read the AI's colour and safety level
from `meta.safety`, treat an entry without it or with `valid = false` as "no valid advice",
and show where the AI and the rules differed from `rules_floor`.

### Safety in the context

The base block `safety` (version 2) is in every task's context and carries:

- `floor`: the rules' level, colour, the rules that fired, each one's reason, and notes;
- `sleep_check` (`suspect`, `incomplete`, `updated`) and `sleep_incomplete`;
- `measurement`: whether the night was updated after its first reading and which readiness
  reading counts;
- `checkin`, `complaints`, `pain_today`;
- `injuries`: the injury log of the last 14 days.

Each part is built on its own: an empty injury log does not hide a floor that fired, and a
part that fails says so and leaves the rest. The insight task writes a public line, so its
safety block is the injury log only, as before.

### The same harness over MCP

An AI that coaches over MCP (a Claude Code routine, another agent) is held to the same
harness:

1. **Same text.** `vibesynergy_coach_prompt(job)` returns the task's prompt and `rules_md`,
   the harness rules from the same constants the runner uses, with a short note on how an
   MCP coach reaches VibeSynergy instead of the runner's.
2. **Same context.** `include_context=true` returns the runner's preloaded chain for that
   task and day.
3. **Same contract**, enforced on the server at `vibesynergy_coach_write`.
4. **The version on the entry.** The client passes `harness=<stamp>`; the entry records it
   and whether it is the current one. A write under an old or missing stamp is stored with a
   warning, so the Coach page and `vibesynergy_coach_entries` show which entries followed
   the current harness.

Still open (follow-up): the harness stamp on `vibesynergy_coach_threshold_propose`,
`vibesynergy_coach_memory_write`, `vibesynergy_model_run_write` and the plan writes. Those
tools validate their own input and refuse what is invalid, and each call is audit-logged
with its client; they do not record the harness version yet.

### The fallback rules

The fixed rules (`query/safety_floor.py`, `query/dashboard.py`) are what the page shows when
there is no valid AI advice ([Daily advice](#daily-advice-ai-first-the-rules-as-the-fallback-89)), so they were reviewed against the last 90 days (19-8 to
17-11-2026, on a read-only copy of the live data):

- **Deviation from the plan** (`plan.deviation`) looks at intensity and RPE, not at
  duration alone. *Harder*: heart-rate time above the planned zone ceiling (10 minutes and
  15% of the session, or more); the ceiling is the highest "Z<n>" in the session's name,
  zone 1 for a training you give, zone 2 for a session planned easy. *Longer*: over 25%
  longer and not at a low effort (RPE above 4, or without an RPE, heart rate not nearly all
  in zones 1-2). Longer at a low effort is the session as planned. *Shorter*: over 25%
  shorter, named as such. The advice says which it was.
- **Measurement gaps** (`query/measurement.py`, from the raw history VibeSynergy keeps): a
  night that grew by an hour or more after its first reading is reported; a readiness from
  before the night was complete does not count; when the day's readiness is a later
  recomputation (after training), the floor judges the reading after waking up. The advice
  says so when the two readings fall on different sides of the rule.
- **Advice branches**: an amber day with a session still to come now says what amber means
  for it; a session that could not be matched no longer leads the advice on a red day.

What the review found, with the day it came from, is in the tests
(`tests/test_harness_safety.py`).

## Daily advice: AI first, the rules as the fallback (#89)

One function decides the advice for a day: `query/daily_advice.resolve` (or `current`,
which computes the rules first). The dashboard (`advice`), the Coach page (`GET
/api/advice`), `vibesynergy_today` (`verdict` and `advice`), the red-day alert and the
report mail all read it, so they give the same text, colour and level
(`tests/test_daily_advice.py`).

**The setting** "Daily advice" (Settings → System; `daily_advice` in `/api/prefs`):

| Value | What decides |
| --- | --- |
| `ai` (default) | The newest **valid** AI advice for the day: colour, safety level, reasons and text. Without one, the rules, with the reason shown. |
| `rules` | The fixed rules only. The dashboard leaves the coach's messages out of the advice card. |
| `off` | No daily advice; the card says so and links to the setting. |

**Valid** means: a coach entry for that day (`morning`, `evening`, or a `note` from the
`injury` or `checkin` task) whose `meta.safety` passes `contract.check_safety` again on
read, and that the runner did not mark `meta.harness.valid = false`. An AI output that
misses the contract never counts.

**One fixed exception** to "everything from the AI": the autonomous runner can never
soften the hard rules (illness, a red-flag note, pain 7/10 or more, an injury that changes
how you move; `safety_floor.HARD_RULES`). That is checked where the runner writes; a write
over MCP is the athlete's own request and is exempt. It is not a setting; the setting's help
text says so.

**The fallback reasons** (`daily_advice.REASONS`, a fixed list, each shown on the page):

| Reason | When |
| --- | --- |
| `no_run` | No valid advice and no run for the day yet (or one still queued or running). |
| `run_failed` | The day's newest advice run failed, before any valid advice or after it. |
| `contract` | The AI output is outside the contract (colour, safety or reasons missing, or marked invalid). |
| `ai_off` | The setting is AI but the coach runner is off and no AI app wrote valid advice. |
| `unavailable` | The monthly budget is used up, or the provider could not be reached or refused the key. |
| `new_signal` | A hard rule fired after the AI wrote (it was not in `meta.harness.rules_floor`), and the new run failed or could not run. |

**A hard new signal** (sickness, a red-flag note, pain 7+, an injury that changes movement)
after the advice queues one new `morning` run at once (`schedule.enqueue_advice`, run key
`signal-<day>-<rules>`, at most once per day and set of signals). The check runs where the
advice is read (the dashboard, the alert check every few minutes), so it does not depend on
which page logged the signal. While that run is queued or running the AI advice stays,
marked "updating"; the rules take over only when it fails, cannot run (runner off, budget,
another day), or ends without a valid advice that saw the signal.

**Updating**: an AI advice whose inputs changed since it was written (`needs_revision`)
stays on the page with "New data came in since this advice was written"; a run in progress
for the day shows "updating". Neither is a fallback.

**Shadow**: the rules keep computing (`verdict` and `safety` on the dashboard stay the
rules' own outcome, which the contract checks a coach against). Where the AI's colour or
level differs from the rules', the page shows it under "Why this advice" and it is logged once per
day, entry and rules outcome: `audit_log` action `advice.shadow`, `outcome` `lenient`
(the AI is milder), `stricter` or `other`, with both outcomes in `detail`.

**Manual refresh** ("Update advice" on the dashboard; `POST /api/advice/refresh`, then
`GET /api/advice/refresh?key=…&jobs=…` for progress): queues a sync of every enabled source
for today, then one advice run (`morning`, run key `refresh-<time>`, trigger `manual`) that
waits for those syncs (deferred without using an attempt, at most 10 minutes). Without AI
(setting, runner off, budget) only the syncs run. A run that fails gives the fallback with
its reason.

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

## Garmin multisport workouts (brick, duathlon, triathlon)

A combined workout is planned as its parts, one session per sport on the same day, linked by
the slot `ms<n>-<sport>` in order: `ms1-swim`, `ms2-ride`, `ms3-run` (#74). A letter after
`ms` (`msb1-run`) starts a second one that day. With Garmin delivery on for every sport in
it, VibeSynergy sends it as one Garmin multisport workout: a segment per sport with the
transitions, the swim as open water. The parts go separately as well. A brick without slots
still works: a run with "brick" in its name and that day's ride.

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
