# API coach runner

The AI coach can run inside VibeSynergy on your own API key, with the provider and model
you choose ([AI_HARNESS.md](AI_HARNESS.md#any-provider)), instead of as routines in an AI
app. It does the same four jobs as the routines, with the same
prompts and the same VibeSynergy tools, and adds a mailed report with a reply box in the
portal. It is **off by default**; nothing calls the API until it is switched on in
Coach → Settings with an API key.

Code: `src/vibesynergy/coach_runner/` (runner, schedule, tools, prompts, settings, pricing,
report), `query/coach_runner.py` and `query/coach_replies.py` for the portal,
`portal/coach_runner_api.py`. Migration `0038` adds `coach_run` and `coach_reply`.

## How a run works

| Job | When (Europe/Amsterdam) | Prompt | Writes | Report mail |
| --- | --- | --- | --- | --- |
| `morning` | daily 07:00 (the fixed advice run, #90), and on new data for today | `docs/coach-prompts/morning.md` | entry `morning`, key `morning-<date>` | "Advies voor vandaag – zo 1 nov"; a later version "Advies voor vandaag bijgewerkt – zo 1 nov", only on a real change (#147) |
| `evening` | daily 21:45, and on new data for tomorrow; its advice is for tomorrow | `evening.md` | entry `evening`, key `evening-<date>` | "Advies voor morgen – ma 2 nov": named after the day the advice is for (#138) |
| `injury`, `checkin` | on pain or an injury (at once), on the check-in (after the wait) | `injury.md`, `checkin.md` | entry `note`, key `<task>-<date>-event-…` | "Advies voor vandaag bijgewerkt – …", only on a real change |
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
   [The harness contract](#the-harness-contract)). Edit the article to change what the AI coach does; the
   next run uses it. `[coach_runner] prompts_dir` points elsewhere if wanted. The first user
   message holds the date, ISO week, the idempotency key to use, **the athlete's unread
   replies** (or, for an insight, the fire payload `event=… event_id=… activity_ids=…`) and
   **the preloaded context**: the fixed base (athlete, communication rule, safety, coach
   memory) and the task's own chain.
4. **Tools.** The same MCP server as `/vibesynergy/mcp`, called in-process
   (`MCPServer.call_tool`), so every call goes through `vibesynergy.query`, checks its scope and
   is audit-logged with client `coach-runner` (author `client:coach-runner`, label
   "AI coach (API)"). Scopes are those of an AI app that coaches: `read`, `coach.write`
   (entries, knowledge base, public insight) and `plan.write` (VibeSynergy's plan). Never
   offered: `sql`, `publish`, `sync` (`vibesynergy_refresh`), `diary.write`, `plan.propose`.
   The insight run only gets the activity, plan and knowledge tools and `vibesynergy_coach_write`
   (no diary, health or earlier entries), may only write `activity_assessment` for the
   event's activities, and a `public_insight` naming health, diary or injury details is
   refused back to the model (INS-2).
5. **Model loop.** One request per turn with tool use, through the adapter of the task's
   provider (the Messages format or the OpenAI format; `coach_runner/model.py`), with the
   provider and model chosen per task in Coach → Settings (the defaults are in
   `coach_runner/__init__.py`; #88 and #96 changed none of them). Where the provider has
   them: adaptive thinking with effort `high` (`[coach_runner] effort`) and prompt caching;
   elsewhere they are left out and the run is the same otherwise. Prompt
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
`vibesynergy_harness_run` over MCP, which all queue the same `coach_run` job. A redo of one
activity's insight (`vibesynergy coach redo insight <activity_id>`, or
`vibesynergy_harness_run(task="insight", activity_id=...)`, #158) queues it too, with run key
`redo-<activity>-<time>` and entry key `insight-<activity>-<run key>`.

| Call | Registry | Versioned prompt + rules | Fixed context chain | Contract, fail closed |
| --- | --- | --- | --- | --- |
| `morning`, `evening` | yes | yes | yes | yes, with `safety` and the target day |
| `injury`, `checkin` | yes | yes | yes | yes, with `safety` and the target day |
| `weekly`, `monthly`, `block`, `race` | yes | yes | yes | yes |
| `test`, `thresholds`, `kb` | yes | yes | yes | yes; proposals also by their own tools |
| `insight` | yes | yes | yes (no health in it) | at the write: INS-2 guard and the public-line rules; no entry is allowed |
| `reply` | planned task, no model call of its own: a reply makes a new version of the current advice (#90), which reads it | – | – | – |
| `catch_up` | code, no model | – | – | – |
| advice run (#89, #90) | the `morning` task at 07:00 and, for new data, `morning`, `injury`, `checkin` or `evening` | yes | yes | yes, with `safety` and the target day |
| Daily review narrative (`query/daily_coach.py`) | no model: `narrative_enabled` is false and the text is fixed | – | – | – |
| Cloud routine webhooks (`query/agent_events.py`) | outside VibeSynergy: they fire a routine in an AI app, which comes back over MCP | by `vibesynergy_coach_prompt` | by `include_context=true` | yes, at `vibesynergy_coach_write` |

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
twice. Approval follows from the writer's **role** (#118, docs/MCP.md "Roles per
connection"), enforced by the server:

- **The runner** is the **primary AI coach** (client `coach-runner`) and acts on its own
  (scheduled and event tasks). Its daily entry is the daily advice. A plan write needs an
  approval reference (`approval_id`: `reply:<id>`, the athlete's 'akkoord' reply, whose id
  is in the start message, or `proposal:<id>`, a proposal he approved, listed in the
  context block `coach_team`); without one the write is stored as a proposal under Waiting
  for you. One standing approval: a new session in a slot that held none, in a plan week
  that has its note (filling the horizon), is written directly. It can never soften illness or real pain.
- **The athlete's own agent** over MCP acts on his request: its writes are approved.
- **An advisory AI coach** over MCP gives a second opinion: its daily entries are shown
  beside the advice, its plan changes are always proposals, and where it differs from the
  primary in safety level or a session decision the runner gets it in `coach_team` and
  says in its reasons whether it follows it. The primary has the last word unless the
  athlete chooses.

When the athlete gives the primary role to another connection, the runner becomes an
advisory AI coach and its entries second opinions.

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

The base block `safety` (version 3) is in every task's context and carries:

- `floor`: the rules' level, colour, the rules that fired, each one's reason, and notes;
- `sleep_check` (#100): the night's automatic sleep check with the athlete's choice on top:
  `state` (`ok`, `suspect`, `incomplete`, `bad`), `auto_state`, `override` (`incomplete`,
  `ok` or null), `metrics` (per metric its own state), `not_counted_metrics` and `reasons`
  (sentences), or null when there is nothing to say. Since #100 it replaces
  `sleep_incomplete`; a night updated later is one of its reasons;
- `measurement`: whether the night was updated after its first reading and which readiness
  reading counts;
- `checkin`, `complaints`, `pain_today`;
- `injuries`: the injury log of the last 14 days.

Each part is built on its own: an empty injury log does not hide a floor that fired, and a
part that fails says so and leaves the rest. The insight task writes a public line, so its
safety block is the injury log only, as before.

### The activity in the context (#158)

The `activity` block (version 2; the `insight` and `test` tasks) is a compact view per
activity, a brick's holder and parts up to five (`context.activity_view`): first
`own_words`, per source the title and the description without VibeSynergy's own block
(`publication_format.find_blocks`, then `journal.personal_text`) and the linked notes (text
only, a note that repeats a description is given once); then `summary` (sport, date,
duration, distance, pace or speed, heart rate, power, ascent, load), `effort` (RPE and Feel
with their labels), compact `laps` (index, duration, distance, heart rate, power, pace or
speed, intensity) and `zones` (seconds per HR and power zone). No provider `extra`, no
account, profile or owner fields. The block fits itself to its 8000 characters
(`fit_activities`) and is never cut afterwards: the laps give way first (a `laps_total`
stays), then the zones, the effort and the summary; the athlete's own words never.

### The plan in the context: VibeSynergy only (#111)

Every context block that carries the plan reads VibeSynergy's own plan model and race list,
never intervals.icu:

- `plan_horizon`, `plan_next_week` and `plan_match`: the sessions and week notes of
  `plan_model.view`; `plan_horizon.guard` is the Garmin check of those sessions only.
  `plan_match` (version 2) also has, per session, what was done (#116): `match` (done,
  changed, missed), `planned` and `done` as one line each ("Zwemmen duur 40' — 3x300
  Middel", "lap_swimming 2525 m, 68' — 5x50 + 3x100 · RPE 3"), `check` (`match`,
  `differs`, `not_checked`) and the `deviation`; `today` (version 2) has the same per
  session. A `structure` deviation means another workout than planned, from the laps;
- `today` and `week`: `vibesynergy_today` and `vibesynergy_week`, built on
  `vibesynergy_plan`, whose planned sessions, week notes, targets and races are
  VibeSynergy's own. A Garmin workout or event that is not VibeSynergy's (Garmin Coach's
  "Basis", "Drempel", one added by hand in Garmin) is not in any block, nor in
  `plan_horizon.guard` (`plan_horizon` and `week` version 2, #153). So the advice never
  mentions it;
- `races` (version 2): VibeSynergy's race list with its A/B/C priority; the primary race
  is the next A race (#120).

intervals.icu only holds a copy that it passes on to other services. The insight run no
longer gets `vibesynergy_icu_events`. A test (`tests/test_plan_source_vs_only.py`) changes an
intervals.icu event and checks that the advice, the AI coach's context and the dashboard stay
the same.

### The same harness over MCP

An AI coach that works over MCP (a routine in an AI app, another agent) is held to the same
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
  shorter, named as such. *Structure* (#116): the planned steps against the laps
  (`query/plan_match.py`): another main set (3x300 planned, 4x50 + 3x100 swum) or a tempo
  block in a steady session; a few bad swim laps do not count, and laps too thin to judge
  give "done, not checked" (`comparison.check` `not_checked`). The advice says which it was.
- **The sleep check** (#100, `query/sleep_quality.py`; tests: `tests/test_sleep_check.py`):
  every night is checked against fixed rules (heart rate during the sleep window, the window
  against the usual bedtime and wake time, the stages, HRV over the night, a night updated
  later, the check-in) and marked `ok`, `suspect`, `incomplete` or `bad`, per metric. A
  metric that is `bad` or `incomplete` does not count for `short_sleep`, `low_readiness`,
  `low_hrv` or `rhr_up`, and the floor adds a note that says so; a `suspect` one still counts,
  with a note. The athlete's "yes, incomplete" or "no, it's right" (Today, or
  `vibesynergy_sleep_check_set`) beats the check. The harness rules (version 8, step 3b): a
  metric of a bad or incomplete night is weighed less, the advice leans on the check-in,
  resting HR and the trend, and says in one sentence that the night is uncertain. A changed
  mark is a new signal (trigger kind `sleep_check`, with the wait).
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
| `run_failed` | The newest advice run **for that target day** failed, before any valid advice for the day or after it (a refresh, a pain or check-in reaction). A run only counts for its own target day: the evening run prepares tomorrow, so its failure never touches today's advice. |
| `contract` | The AI output is outside the contract (colour, safety or reasons missing, or marked invalid). |
| `ai_off` | The setting is AI but the coach runner is off and no AI app wrote valid advice. |
| `unavailable` | The monthly budget is used up, or the provider could not be reached or refused the key. |
| `new_signal` | A hard rule fired after the AI wrote (it was not in `meta.harness.rules_floor`), and the new run failed or could not run. |
| `limit` | New data came in after the AI's advice and the daily maximum of updates is reached (#90). |

**A hard new signal** (sickness, a red-flag note, pain 7+, an injury that changes movement)
after the advice queues one new `morning` run at once (`schedule.enqueue_advice`, run key
`signal-<day>-<rules>`, at most once per day and set of signals). The check runs where the
advice is read (the dashboard, the alert check every few minutes), so it does not depend on
which page logged the signal. While that run is queued or running the AI advice stays,
marked "updating"; the rules take over only when it fails, cannot run (runner off, budget,
another day), or ends without a valid advice that saw the signal.

**Updating**: new data that will make a new version (#90) shows "your AI coach updates this
advice in a few minutes"; a run in progress for the day shows "updating"; an AI advice whose
inputs changed with no update coming (`needs_revision`, or updates switched off) shows "New
data came in since this advice was written". None of these is a fallback.

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

## Continuous advice (#90)

Decided by the athlete on 17-11-2026: the advice is revised all the time, and every view
shows the newest state. Code: `coach_runner/triggers.py`; tests:
`tests/test_continuous_advice.py`.

**The fixed advice run is the morning task, moved to 07:00** (Coach → Settings → Tasks →
Morning advice, time and days adjustable). The issue left two choices: a separate advice run
next to the morning review, or the morning review moving to it. It moved: one run, one cost
and one mail at the start of the day, and a check-in or sleep that comes in later makes a new
version anyway. A schedule saved before #90 (`schedule_version` 1) gets the new morning time
once; its other choices stay, and a time chosen after that holds.

**One current advice per target day.** The morning advice and every update during the day
are for today; the evening review's advice is for tomorrow (`daily_advice.target_day`,
`meta.harness.target` on the entry). The evening entry keeps `for_date` = the day it
reviews (as before, and as `daily_coach.target_date` reads it); a runner's evening write for
another day is refused in the run, so the model repairs it. So last night's evening advice is
today's AI advice until the 07:00 run. Advice for a past day is archive. Every version stays
in the log; `daily_advice.resolve` gives the newest valid one, `versions` (newest first,
each with `changes`, `trigger` and what really changed against the version before), and
`entry.changes` / `entry.differences` for the current one. The dashboard says when the
advice was written and why ("Updated 10:32, after your check-in. Changed: colour amber →
green."); the Coach page lists the earlier versions under the advice card.

**One view per advice (#147).** Every place reads an advice through
`query/advice_view.view(entry)`: `label` ("Advies voor vandaag – za 21 nov"), `stamp`
("Bijgewerkt 13:48, na je training", or "Geschreven 21:46, avondvooruitblik" for a first
version), `why` (from `meta.harness.changes` and `trigger`: na je training, na je
check-in, nacht binnen, plan gewijzigd, na je notitie, na je pijnmelding, na je reactie,
op je verzoek, ochtendupdate, avondvooruitblik), `state`, `verdict`, `recovery`, `notice`
and `push`. The banner, the push, the mail, Meldingen, the start page, the Coach feed, the
report page and the MCP (`advice.view`, `advice_tomorrow.view`, `advice_view` in
`vibesynergy_coach_entries`) show those words; none builds its own. A portal notice stores
only which entry it is about; `alerts.for_portal` makes its text from that entry when it is
read, so old notices show the current wording. The colour follows the day: `state` is
`colour` ("Oranje – Doe het wat rustiger aan") until the day's planned sessions are done
and the version says so (it marks each done, skipped or missed and none is still to keep,
adjust or propose); then `done` ("Gedaan voor vandaag", with "Herstel: oranje – <the first
stored reason>"). The colour, its reasons and the decisions come from the stored entry; the
plan (`query.plan`) only says what was planned and what was done.

**When a version is sent (#147).** `advice_view.notable`: the first version of a day, or
another colour, a stricter safety level, or a session whose decision changes to keep,
adjust, skip or proposal (held against the newest earlier version that names it). A
session that is now done or missed, a milder level in the same colour, or wording is
quiet: no push and no mail; the day's notice points at the new version and keeps its
`version`, so a notice the athlete closed stays closed. The contact window and its urgent
exception (#119) are unchanged.

**The day thread (#115).** The versions are one conversation. Each item in `versions` has
`version` (1 is the oldest valid one), `of`, `status` (`current`, `replaced` with
`replaced_by`, or `invalid`) and `changed_because`. Every daily advice run gets them in
context block `day_thread` (oldest first, with the athlete's replies; the evening run also
the day it reviews), and the start message names the current version: "De huidige versie
voor die dag is advies #1018 … geef changed_because = {"previous": 1018, …}". The output
contract refuses a later version without `changed_because` citing that id
(`contract.check_changed_because`), and `contract_issues` fails a run whose entry ignores
the thread. "Changed because: …" is under "Why this advice"; the Coach page marks earlier
versions *Replaced*, with their time and why, linked to their own report; the report page
is called "Advies", with the label, the stamp and "Eerdere versies (n)", and a replaced
version says "Replaced by a newer version" with a link to the current one
(`GET /api/coach/report/<id>` → `view`, `versions`, `thread`).

**What is sent is what is stored (#115).** `report.send` reads the entry back by id and
sends only that stored text, linked to `#/coach/report/<id>` (mail, push and notice; the
push opens that page). A daily advice version that is not the current one for its day is
never sent, also not by Send again. A version that only rewords is not sent, but the day's
notice is pointed at it (`quiet`). The continuity check (`query/coach_consistency.py`, once
a day from the alerts check, live on the Coach page and in `vibesynergy_today`) reports any
gap: a notice without its entry or with a text not in it, a notice on a replaced version, a
current advice without a notice, a later version without its reason, and (#147) a label
that contradicts the day (`label_day_state`: a colour to act on while every session of the
day is done and no new version is on its way, or "done" while a session is still open).

**Triggers.** Every daily-advice run stores a fingerprint of its inputs at its start
(`coach_run.harness.inputs`: one short hash per kind). On every scheduler tick
`triggers.due` compares the inputs now with the newest run for the target day:

| Kind | What is compared | Waits |
| --- | --- | --- |
| `sleep` | `sleep.*` daily values and the sleep sessions of the target day | yes |
| `hrv` | `hrv.*` daily values of the target day | yes |
| `sleep_check` | the night's sleep check (#100): its state, what does not count, the athlete's choice | yes |
| `checkin` | the target day's check-in | yes |
| `note` | notes covering the target day or the day before (illness is a note tagged sick) | no |
| `injury` | open injuries, and pain logged for the target day or the day before | no |
| `activity` | workouts of the target day and the day before (sport, duration, distance, purpose, load) | yes |
| `plan` | planned sessions on the target day and the day after | yes |
| `reply` | the newest reply to a report | yes |

Values, not `updated_at`, are compared: a sync that rewrites the same night or a rebuild of
the activity table starts nothing. Because the check reads the data, every write path counts
(syncs, the portal, the MCP, imports) without a hook in each.

**Wait**: a change waits until nothing changed for 5 minutes (`DEBOUNCE`), so three syncs in
a row give one run, but at most 30 minutes after the first change (`MAX_WAIT`). A note or an
injury runs at once. The scheduler checks every 30 seconds and queues the coach schedule at
most every 5 minutes, so "at once" is within a few minutes. Before the day's fixed run,
changes for today wait for it (it reads them), except a note or an injury.

**Which task**: for tomorrow (after the evening run) `evening`; for today `injury` when pain
or an injury came in, `checkin` when the check-in did, else `morning`. Each keeps its own
model from Coach → Settings. A task switched off falls back to `morning`. The run key is
`event-<target day>-<hash of the inputs>` (trigger `event`): one run per state of the data.
Nothing is queued while another advice run for that day is queued or running; it reads the
new data, and what comes in after its start is the next change. The start message tells the
model the day the advice is for and, for a new version, what came in (no model or provider
is named).

**Why the injury and check-in tasks never ran** (`last_run` null): their conditions could
not be met in the athlete's routine. The check-in reaction needed a check-in saved *after*
the morning report (09:04) with two or more low scores; the athlete checks in around 08:00
and had no low check-in, so it never qualified. The injury check needed pain of 4/10 or
more, and the last logged pains were 0. Now the fixed run is at 07:00, any check-in after it
starts the check-in task, and any pain or injury for today starts the injury task.

**Limit**: at most 4 automatic runs per local day by default (Coach → Settings → "Updates of
the advice per day", 0-24; 0 switches them off). Runs with trigger `event` and #89's
new-signal re-runs (`signal`) count; the scheduled runs and "Update advice" do not. The page
shows the average cost per advice run over 30 days and what the maximum costs a day and a
month. When the limit is reached and new data is waiting, the fixed rules give the advice
with the reason `limit`. With 0 the AI advice stays, marked "New data came in since this
advice was written".

**Mail only on a real change**: the first advice for a day is mailed; a new version is
mailed only when its colour, safety level or a session decision differs from the previous
valid version for that day (`daily_advice.real_change`). Otherwise `coach_run.harness.mail`
is `no_change`. So the 07:00 advice is mailed when it differs from last night's evening
advice for today, and the evening review is always mailed (the first advice for tomorrow).

## Why a plain model API and not an agent SDK

An agent SDK (a coding agent as a library) starts its own CLI as a
subprocess and brings its own file, shell and web tools, sessions and permission system,
and ties the runner to one provider.
VibeSynergy needs none of that. It needs exactly its own tools, called in-process with the
right scopes and audit, a hard per-turn budget check, usage per response for the monthly
cap, and something that runs inside the existing worker without Node or a CLI. A short
manual tool loop over a model API gives all of that in one module, and the model client is
one small protocol (`ModelClient.create`) with an adapter per request format, so another
provider is an adapter and tests run on a scripted fake with no network.

## Budget and safety

- **Per run:** `max_turns` (default 40) and `max_output_tokens` for all turns together
  (default 60 000; at most 16 000 per response). Set in `[coach_runner]` in config.toml.
- **Per month:** the cap (default **$95**, `[coach_runner] monthly_cap_usd`, the portal
  value wins). Cost is computed from every response's `usage` with the model's list
  price (`coach_runner/pricing.py`; a model without a listed price counts at the dearest
  one, to stop in time, and a local server costs nothing). Usage is stored after every turn, so a crashed run still counts. The cap is checked
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

## Contact window (#119)

Coach → Settings → **Contact window** (default 07:00–23:00, local time; stored as
`contact` in the runner's settings) says when any AI coach may reach the athlete. The
runner always runs at its scheduled time and writes its entry; outside the window
`report.send` keeps the report in the outbox (`coach_runner/contact.py`, store document
`contact_outbox`) instead of sending its notice, push and mail, and the run shows "The
report waits for your contact window" (`mail_error` `waiting for the contact window`). The
service job `coach_contact` sends the outbox once the window opens: per advice day only the
version that is current then, and it sets the run's `mailed_at`. A failed run's notice
waits the same way; sending again by hand does not. Today's red advice from a hard rule the
athlete's own report set off (illness, a red-flag note, pain 7/10 or more, an injury that
changes how he moves) is the one exception and goes out at once. The default start
matches the default 07:00 advice run; with a later start the run still runs at 07:00 and
only its delivery waits. Details and the decisions: [AI_HARNESS.md](AI_HARNESS.md#contact-window-119).

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

1. With your AI provider: make an API key for VibeSynergy, in its own workspace or project
   with a monthly spend limit when the provider offers one. A local server needs only its
   address.
2. VibeSynergy → Coach → Settings → Coach via API → AI providers: paste the key under its
   provider (it is never shown again; a key in another provider's form is refused), check
   the monthly limit, switch it on.
3. Per task, choose the provider and model when you want another than the default.
4. Turn off the routines in your AI app that do the same jobs ("Coach
   ochtend", "Coach avond", "Coach weekreview", "Coach na training"), and switch off the
   webhook link in Coach → Settings. Otherwise both
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
Updates of the advice during the day (#90) cost like a daily run each; Coach → Settings
shows what the daily maximum costs at most.
Coach → Settings shows the real figures for the month. Lowering `[coach_runner] effort` to
`medium` is the first lever if it runs close to the cap.

## Deploy

Code merge, then on the server: `uv sync --frozen`, `uv run vibesynergy db upgrade` (migration
0038 adds two tables; safe while the old code runs), then restart. It stays off until
switched on in the portal.
