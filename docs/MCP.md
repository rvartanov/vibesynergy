# VibeSynergy MCP: interface specification

> Issue and PR numbers in text written before 30-10-2026 refer to the old tracker (they are marked "old #N" where they were links); the GitHub tracker started fresh then.

Status: the design of 2026-09-25, with what is built marked. "Implementation status" below
and the "As built" lines say what the server offers now; where a tool section still gives
the design's parameters, the tool's own description and input schema (`tools/list`) decide.
This is the contract between VibeSynergy and any AI client (Claude,
ChatGPT, OpenClaw, Hermes, local LLM clients). The portal uses the same query layer, so
everything the MCP can answer, the portal can show and vice versa.

---

## Roles per connection

More than one AI coach can work under the same harness (#118). Every connection (an app
approved over OAuth, a personal token, the AI coach inside VibeSynergy) has a role, set in
Settings → Sharing → Coach apps and stored in `coach_role`. Rights hold per role, never per
provider or model, and the server enforces them for every write tool
(`vibesynergy/mcp/roles.py`, `WRITE_POLICY`); scopes still apply on top.

| Role | Coach entries | Plan tools (`vibesynergy_plan_*`) | intervals.icu writes (`vibesynergy_icu_*`) | KB, memory, publish, harness run, race priority |
| --- | --- | --- | --- | --- |
| `own`: the athlete's own agent | approved; a daily entry is the daily advice | written directly (`plan.write`) | written directly | allowed |
| `primary`: the primary AI coach (exactly one; by default `coach-runner`, the AI coach inside VibeSynergy) | its daily entry is the daily advice | with `approval_id`: written; without: stored as a proposal, except a horizon fill (below) | need `approval_id`, else refused | allowed |
| `advisory`: an advisory AI coach | notes and assessments; a daily entry is a **second opinion**; no `public_insight` | always stored as a proposal (`plan.propose` is enough) | refused | refused (use `vibesynergy_kb_propose_change`, a note) |

- **Defaults.** A local caller (stdio, the portal) is the athlete. Apps and tokens that
  were connected before roles existed keep `own`. A connection made later is `advisory`
  until the athlete chooses. Giving `primary` to a connection takes it from the one that
  had it (that one becomes advisory); taking it away gives it back to `coach-runner`.
- **Refusals.** A write outside the role is a tool error the model can act on:
  `Refused by this connection's role (advisory AI coach): vibesynergy_kb_upsert is not open
  to an advisory AI coach … Instead: suggest the change with vibesynergy_kb_propose_change`.
  Nothing is stored; the audit log has outcome `refused: …`.
- **Proposals.** A plan tool that is not approved answers `{"written": false, "proposal":
  {id, tool, arguments, reason, summary, status}, "note"}`. The athlete approves or rejects
  it under Waiting for you (`POST /api/plan_proposals/<id>/approve|reject`); approving
  carries the stored call out in the proposer's name and copies it to intervals.icu. A
  proposal that could never be carried out (unknown session, steps not understood) is
  refused when it is made. The same open proposal is not stored twice.
- **Standing approval: filling the horizon.** The primary AI coach's
  `vibesynergy_plan_session_set` without `session_id` is written directly, without
  `approval_id`, when the slot it fills (that day and `slot`, or that day and sport) never
  held a session (a skipped one counts as held) and the plan week of that day has a note
  (a week note is written by the athlete or with his approval). Everything else (changing,
  moving, skipping, week notes, targets) needs the reference. Not for an advisory AI coach.
- **Approval reference.** `approval_id` on the plan and intervals.icu write tools:
  `proposal:<id>` (approved by the athlete) or `reply:<id>` (his reply to a coach entry
  with an explicit yes such as 'akkoord' and no 'niet'/'no'), not older than 48 hours. A
  reference that does not hold is refused, never turned into a proposal.
- **Entries.** Each entry records `meta.harness.role`. Idempotency keys are per author
  (`morning-<date>-<author>`): a key another author used is stored as `<key>-<author>`; an
  advisory entry's key always carries its author. `vibesynergy_today` and the portal's
  advice carry `second_opinions` (each with `differences` and `disagrees`), and the
  primary's daily-advice tasks get them in the context block `coach_team`.
- `vibesynergy_coach_handbook` returns `connection`: `role`, `may`, `author`, `key_suffix`,
  `idempotency_key_example`, `writes_daily_advice`.

## Implementation status

**Done (phases 3a to 3c, and the diary of phase 6):** read and action tools, 8 prompts and 5 resources, in
`src/vibesynergy/mcp/server.py` over the query layer in `src/vibesynergy/query/`.

- Coach handbook (1-11-2026): `vibesynergy_coach_handbook(include_protocol=true)`
  (`read`) is the first call for any AI that coaches. It returns the provider-neutral coach
  protocol ([COACH_PROTOCOL.md](COACH_PROTOCOL.md): roles and what needs the athlete's
  akkoord, the morning/evening/weekly/insight runs and their tool order, idempotency keys,
  privacy rules for public text, replies, failure handling, onboarding) as `protocol_md`
  with a short `protocol_version` hash; `knowledge` with the active KB articles (`slug`,
  `title`, `topic`, `evidence_grade`, `version`, `updated_at`), `rule_ids` and `rules`
  (active rules of active articles, with their article); `start_here`; and `access`
  (the connection's `scopes`, `needed` = `read`, `coach.write`, `plan.propose`,
  `plan.write`, `optional`, `missing`, with a warning when something is missing).
  `include_protocol=false` leaves out the text for later runs. The same text is the
  resource `vibesynergy://coach/handbook`, and the server instructions start by pointing to
  it.
- Coach handbook, generated (#119): the wheel carries `docs/COACH_PROTOCOL.md` (as
  `vibesynergy/_docs/COACH_PROTOCOL.md`), so an installed app serves `protocol_md` and
  `protocol_version`; a test builds the wheel and reads the protocol from it. Everything
  else in the handbook is generated from the harness registry and this install's settings,
  never written by hand: `tasks` lists every harness task (14, registry order) with
  `title`, `status`, `runs`, `on`, `triggers` (each `kind` and `detail`; a schedule's also
  `when` and `time`), `trigger` (one line), `schedule` (`daily`, `weekly`, `days`,
  `monthly`, `event`, `per_activity`), `due` (the scheduled local time of Coach → Settings,
  `null` for an event task), `days`, `monthday`, `missing_after` (when a tracked run counts
  as missing: `due` plus three hours, at the latest 23:59), `entry_type`, `key_prefix`,
  `prompt`, `inputs`, `context` (the context chain), `contract` (`entry_type`, `required`,
  `max_words`, `safety_required`, `target_day`), `writes`, `approval`, `scopes`,
  `requires`, `notes`, `last_run` and `missing`. A test holds every `due` to the saved
  schedule, before and after a change. Also `timezone`; `approval` with `roles` (`name`,
  `may` per role), `default_role`, `approval_id` and `approval_hours`, `write_policy`
  (tools per policy: `open`, `entry`, `plan`, `approval`, `trusted`) with
  `write_policy_help`, and `tasks` (the approval steps per task); and `contact_window`
  (below). The protocol text itself states no time of its own (a test checks it).
- Contact window (#119): `vibesynergy_coach_state` and `vibesynergy_coach_handbook` return
  `contact_window`: `on`, `start`, `end` (local `HH:MM`, default 07:00–23:00), `timezone`,
  `open_now`, `opens_at` (ISO, `null` while open), `waiting` (deliveries VibeSynergy holds),
  `what_waits`, `rule` (one sentence to follow) and `urgent_exception`. An AI coach writes
  entries as usual outside the window but sends the athlete nothing until `opens_at`;
  VibeSynergy holds its own notice, push and mail about the entries the same way. The only
  exception is today's advice at safety level `refuse` from a hard rule the athlete's own
  report set off (illness, a red-flag note, pain 7/10 or more, an injury that changes how he
  moves).
- Coach predictions: `vibesynergy_coach_write` (`coach.write`) accepts optional
  `predictions` (`metric`, `horizon_days`, `predicted`, `ci_low`, `ci_high`, `model_id`)
  and `kb_rules` (rule ids stored on the entry). `metric` must be a daily value stored
  in `daily_unified`. The target day is the entry date plus `horizon_days`. A startup
  and each sync fill `actual`, `error` (actual minus predicted) and `scored_at` once
  that day's value is present; a later change to the daily value does not rescore.
  `vibesynergy_coach_entries` and `vibesynergy_prediction_scores` (`read`) read the entries
  back and return bias and RMSE per ISO week.
- Public activity insight: `vibesynergy_coach_write` with `entry_type="activity_assessment"`
  takes `activity_id` (group id or any `garmin:`/`strava:` id; stored as the group id in
  `meta.activity_id`) and `public_insight` (`meta.public_insight`). That one or two sentence
  text, and nothing else from the entry, becomes the `-Persoonlijke AI inzicht:` line of the
  activity's VibeSynergy block on Strava and/or Garmin (where the athlete publishes to). The coach
  decides what may go public: no health, diary, injury, sleep or other private details. At
  most 300 characters, one paragraph, no `[`, `]`, `/*` or `*/`; anything else is refused. Per activity the newest
  entry (by `at`) that sets `public_insight` wins; `""` withdraws the line. Entries without
  `public_insight` never reach a public text. Setting `public_insight` needs the `publish`
  permission, because the line becomes public; it is refused when it names health, diary or
  injury details or holds a link. Accepted earlier convention: `details` starting
  with `public_insight` and every activity in `evidence_refs` being that one activity; then
  the summary (first line) is the insight. With automatic publication on, an activity already
  published in the last two days gets the new line within about five minutes through the
  guarded publish path (only the VibeSynergy block changes; a hand-edited block is left for
  review); audit rows `publication.insight` and `publication.update`.
  `vibesynergy_activity_insights(days=2)` (`read`, 1–14 days) lists recent activities with a
  Strava copy, their Strava publication state, `strava_public` (`false` while the athlete
  keeps it private, `null` when unknown) and `insight` (`present`/`missing`); `missing`
  holds the ids still to fill, public or not, so the evening routine also covers activities
  that were never made public. `vibesynergy_activity_publication_preview` returns the same
  `insight` state for one activity.
- Events: `vibesynergy_events(since=None, types=None, limit=50)` (`read`) returns VibeSynergy's
  agent events oldest first (`{type, version, id, occurred_at, data}`; default the last 24
  hours; `next_since` continues). `activity.ready` names new merged activities with
  `public_insight_missing`; `activity.published` names activities the athlete just made
  public on Strava (title, description and gear final): the moment to write the public
  insight. A polling coach knows what to fill without a webhook. Schema:
  [EVENTS.md](EVENTS.md).
- Workout proposals removed (1-11-2026): `vibesynergy_coach_propose`,
  `vibesynergy_plan_status` and `vibesynergy_plan_withdraw` are gone, with VibeSynergy's own
  propose → approve → Garmin push route. They were never called. The plan is written to
  intervals.icu (`vibesynergy_icu_*`), which brings it to Garmin, or, when VibeSynergy leads
  the plan, to its own plan (`vibesynergy_plan_session_set` and the other plan tools in §3.5). Coach entries
  (`vibesynergy_coach_write`, `vibesynergy_coach_log`, `vibesynergy_coach_memory_write` for the coach's own memory items) and `vibesynergy_coach_threshold_propose`
  stay.
- Zones and thresholds (1-11-2026): `vibesynergy_thresholds` (`read`) returns per sport
  (`bike`, `run`, `swim`) the confirmed values the plan must use (`sports.<sport>.values`:
  `ftp`, `threshold_power`, `lthr`, `max_hr`, `threshold_pace` in s/km, `css` in s/100m,
  each with `method`, `date_set`, `activity_id`), the derived zones (Friel HR from LTHR up to
  max HR, Coggan power from FTP, Friel run-pace and CSS swim-pace zones), what Garmin (latest
  synced zone settings) and intervals.icu (live sport settings) hold now, `mismatches`
  (platform value vs plan value; also `hr_zones`), measured references (60-day resting HR,
  Garmin HRV band, VO2max) and open `proposals`. `vibesynergy_athlete` `thresholds.by_sport`
  lists the same confirmed values. Workout generation reads confirmed rows only
  (`workout_generation_thresholds`); proposed values never count until the athlete takes
  them over. After a test or race the coach calls `vibesynergy_coach_threshold_propose` with
  `method` (`test`, `race`, `field`, `device_auto`, `estimate`), `activity_id` (the test or
  race) and `date_set`; values may be given in m/s or min/km and are converted. The portal
  (Training → Zones & thresholds) shows the proposal with Take over / Ignore. Confirming
  (there or via the Coach page) never writes to Garmin or intervals.icu any more; the
  athlete ticks "also in Garmin" / "also in intervals.icu" for that, and each push is read
  back and logged in `audit_log` (`threshold.push.garmin`, `threshold.push.intervals`).
  Garmin takes per-sport LTHR, max HR, HR zone floors, cycling FTP (power zones) and CSS
  (saved as a manual critical swim speed in mm/s, 333-2000 = 0:50-5:00 /100 m; synced back
  as Garmin's `css` for swim); not run threshold pace, which Garmin detects itself and has
  no setting for in the Connect API. intervals.icu takes FTP, LTHR, max HR, HR zones and threshold
  pace. Zwift has no API: the page shows a reminder.
- API coach runner (1 Nov 2026, off by default): VibeSynergy can run the AI coach itself on a
  model API with these tools in-process; see [COACH_RUNNER.md](COACH_RUNNER.md) and §6.
  Its entries are ordinary `vibesynergy_coach_write` rows (author `client:coach-runner`).
- Knowledge base: `vibesynergy_kb_search` and `vibesynergy_kb_get` (`read`) search and read
  articles, rules and sources. A slug an article had before keeps working in both (#96:
  `claude-coach-loop` is now `ai-coach-loop`; `knowledge.SLUG_ALIASES`), and in proposals
  and writes, which land on the article under its current slug. A proposal for a routine
  instruction (topic `coach-prompts`) or a COACH rule that names a model or provider comes
  back with a warning in `meta.warnings`; the handbook lists such texts in use. The same lint
  (#120, `knowledge.lint`) also warns on any article or rule in use that has a retired name
  (Vitality, the `plan source` command, an old slug), a rule range that does not match the
  rules in use (`ICU-1..4` while ICU-6 exists), a rule that does not exist, or a tool the MCP
  server does not register (`SendUserMessage`; checked against the real tool list,
  `mcp.server.tool_names`), and on an instruction, working-method or plan article (or one of
  its rules, or a COACH rule) that writes a threshold as a number (`300 W`, `LTHR 170`,
  `5:00/km`) instead of reading it with `vibesynergy_thresholds`. `vibesynergy_kb_upsert` (`coach.write`) writes a new version
  and keeps every older one; `revert_to` copies an older article version forward.
  `vibesynergy_kb_propose_change` (`plan.propose`) stores a suggestion. The athlete approves
  or rejects it on the portal (Coach → Knowledge, and Coach → Methods & rules for the texts
  there), where it shows as a diff: the changed lines with two lines of context
  (`diff`, from `query/text_diff.py`), the full text one click away. Nothing in the current
  text changes until then, except for an **editorial fix** (#137): when every changed part
  of the text is a mechanical fix of a lint warning (`query/kb_fix.py`: old slug → current
  slug, Vitality → VibeSynergy, `vitality_*` → its `vibesynergy_*` tool, a rule range from 1
  → the real range, and on a plain instruction line a fixed threshold → "read live with
  vibesynergy_thresholds"), no other field changes, the result adds no warning, and the
  author is the athlete's own agent, the primary AI coach or VibeSynergy's own daily fix job
  (`kb_editorial`), the server applies it at once: status `applied`, a new version by
  `lint/editorial`, and `editorial` (`applied`, `reason`, `fixes`) on the proposal. The check
  works per hunk: one change that is not such a fix, a proposal from an advisory AI coach, a
  source, a new article or rule, a fix the athlete undid before, or another proposal waiting
  for the same text sends it to the athlete as usual. The optional `editorial=true` is a
  request only; when it is not applied, `meta.warnings` says why. Coach → Methods & rules
  lists the fixes of the last 30 days, each with an undo (the change log's revert), and
  Waiting for you → Notices has one line for those of the last week; nothing is pushed or
  mailed. Proposals made before #137 keep their rows (`editorial` null). A new proposal for the same target replaces the same author's proposal that
  is still waiting (status `superseded`), so the athlete only decides on the latest
  wording. The athlete can also edit articles and rules directly (Coach → Methods &
  rules); those versions have author `user` and show in Coach → Change log with a diff and
  a revert. Articles with status `retired` are hidden from the portal views but stay
  readable and searchable here.
- AI harness ([AI_HARNESS.md](AI_HARNESS.md)): `vibesynergy_harness_tasks` (`read`) lists
  every coach task with its trigger, context chain, approval, switch, schedule, provider,
  model and last run; `vibesynergy_harness_runs(task?, limit?)` (`read`) is the run log, with
  what was preloaded and the output-contract result; `vibesynergy_harness_run(task, activity_id?)`
  (`coach.write`) starts one run of a report task now, like Test now (it costs like any run);
  `task="insight"` with `activity_id` writes that activity's public insight again (#158), with
  its own idempotency key: the newest wins, the earlier entry stays in the coach log. The
  same redo runs by itself when the workout's texts change after its insight (#114).
- Task instructions: `vibesynergy_coach_prompt(job, include_context?, date?, activity_ids?)`
  (`read`; `job` is one of the harness's model tasks, e.g. `morning`, `evening`, `weekly` or
  `insight`) returns what the coach inside VibeSynergy gets for that task, from the same
  source (#88):
  - `body_md`: the knowledge-base article `coach-prompt-<job>` (topic `coach-prompts`), or
    the seed file in `docs/coach-prompts/` while the article does not exist (`source` says
    which); `version` changes with each edit.
  - `rules_md`: the harness rules (`coach_runner/prompts.py`, versioned), which go before
    the prompt where they differ.
  - `contract`: entry type, required fields, word limit, target day, whether `safety` is
    required and its schema.
  - `harness`: `version`, `rules_version`, `context_version`, the task's context `chain`
    and `stamp` (`h<harness>.r<rules>.p<prompt>.c<context>`).
  - `context` (with `include_context=true`): the preloaded blocks the runner starts with,
    as `{version, text, blocks}`, for `date` (default today).

  Read it at the start of every run.
- Output contract on `vibesynergy_coach_write` (#88): every write, from the runner or from
  an MCP client, is checked before it is stored and **refused** (a tool error, nothing
  stored) when it misses the contract. New optional arguments: `safety` (`{level, colour,
  reasons, override_reason?}`; required for `morning` and `evening` entries and for
  `task` `injury` or `checkin`), `task` (the harness task whose contract applies) and
  `harness` (the stamp from `vibesynergy_coach_prompt`). Checked: `for_date` is the target
  day (morning: today; evening: today or tomorrow), the summary is within the word limit
  (×1.5), and the safety outcome is complete and consistent (level, the colour of that
  level, at least one reason; a level milder than the rules' needs `override_reason`).
  It is a check of form and validity, never of approval: what needs approval follows from
  the connection's role (see "Roles per connection"). The rule that `sick`,
  `red_flag_note`, `pain_7` and `affects_movement` can never be softened applies to every
  AI coach that acts on its own (the runner, the primary and an advisory AI coach), not
  to the athlete's own agent. The
  stored entry has `meta.safety` and `meta.harness` (`version`, `via` runner or mcp, `task`,
  `stamp`, `current`, `follows_current`, `rules_floor`); a write under an old or missing
  stamp is stored with a warning in `meta.warnings`. `vibesynergy_coach_handbook` has the
  same under `harness`. **This changes behaviour**: a `morning` or `evening` write without
  `safety`, or for another day than today, used to be stored and is now refused.
  `vibesynergy_coach_threshold_propose`, `vibesynergy_coach_memory_write` and
  `vibesynergy_kb_propose_change` keep their own typed validation (they were already
  refused when invalid); they do not carry the harness stamp yet.
- The day thread (#115): a daily advice for a day that already has a valid version (from
  any AI coach that writes the daily advice, also last night's evening review) is the next
  version of it. `vibesynergy_coach_write` then needs `changed_because`
  (`{"previous": <id of the current version>, "reason": "what changes against it and
  why"}`) and **refuses** the write without it, with an older `previous`, or with a reason
  of a word; the error names the id to use. A first version and an advisory AI coach's
  second opinion need none. The thread is in `vibesynergy_coach_prompt(job,
  include_context=true)` as context block `day_thread` (oldest first, with the athlete's
  replies), in `vibesynergy_today` as `advice.versions` (newest first, each with `version`,
  `of`, `status` `current`/`replaced`/`invalid`, `replaced_by`, `changed_because`) and in
  `vibesynergy_coach_entries` as `advice_version` per entry. An MCP coach's daily advice
  gets the same per-day notice and push as the built-in AI coach's, linked to its entry
  (`#/coach/report/<id>`). `vibesynergy_today` also has `continuity`: the gaps the
  continuity check finds between the thread and what was sent (empty when in line).
  **This changes behaviour**: a second daily advice for the same day without
  `changed_because` used to be stored and is now refused.
- `vibesynergy_today` (#88): `safety` now also has `colour`, `reasons` (`[{rule, text}]`)
  and `notes`; `measurement` says whether the night was updated after its first reading
  and which readiness reading counts; `sleep_check` could be `updated` (since #100 a
  reason of the sleep check, see above). A planned session
  that departed from the plan carries `deviation` (`harder`, `longer`, `shorter`,
  `distance`, `structure`, `intensity`) in `vibesynergy_plan` and `vibesynergy_week`.
  Every planned session also has `comparison` (#116): `planned` (the main set from the
  steps), `done` (the main sets from the laps), `check` (`match`, `differs`,
  `not_checked`, or null when there is nothing to compare) and `rpe`; `vibesynergy_today`
  and `vibesynergy_week` give `planned` and `done` as one line each per session.
- Races and events (#91): VibeSynergy is the only source of races and of the other events
  that matter to the plan (kind `group_training`, `camp`, `holiday`, `unavailable`, `other`).
  - `vibesynergy_races(start?, end?, kind?)` (`read`): every race and event from today on (or
    the window) with all fields, its `id`, the Garmin and intervals.icu `copies` it is
    linked to, and `changes`: the latest writes with their `audit_id`.
  - `vibesynergy_race_add(reason, name, date, sport, distance_m?, priority?, start_time?,
    location?, url?, goal_time?, notes?, end_date?)` adds a race; `vibesynergy_event_add(reason,
    kind, name, date, end_date?, sport?, start_time?, location?, url?, notes?)` adds an
    event. One tool with a `kind` for every non-race event, so a new kind is no new tool.
  - `vibesynergy_race_update(reason, race_id, …)` changes a race or an event: only the fields
    passed change, `""` clears a text field or the priority. `vibesynergy_race_delete(reason,
    race_id)` deletes one (the row stays). `vibesynergy_race_revert(reason, audit_id)` undoes
    one write: an add is deleted, a delete comes back, a change gets its earlier values; it is
    refused when the race changed after it.
  - All five writes are `plan.write` plan tools (`plan.propose` makes a proposal): the
    athlete's own agent writes at once (his request is approved); the primary AI coach needs
    `approval_id`, else its change is stored as a proposal (`summary.action` `race_add`,
    `race_change`, `race_delete`, `event_add`, …) that the athlete approves under Waiting
    for you; an advisory AI coach always proposes. A reason is required on every write.
  - One validation for the MCP and the portal (`query/race_events.validate`): dates as
    `2027-04-11`, `start_time` `09:30`, `goal_time` `H:MM:SS` (races only), `url` `https://`,
    `distance_m` more than 0, `priority` A/B/C (races only), a sport for every race.
  - Dedupe: a race or event already in VibeSynergy with the same date, name and sport is
    refused (`already in VibeSynergy (id …)`); a Garmin event or intervals.icu `RACE_A/B/C`
    or `NOTE` event with the same date, name and sport becomes its linked copy (`copies`),
    never a second race. The calendar folds a linked Garmin event into the race.
  - Copy to intervals.icu (`query/race_mirror`, with the plan mirror, soon after every write
    and after each sync): a race as `RACE_A/B/C` (no priority: `NOTE`), an event as a
    `NOTE` over its days, external id `vs-race:<id>`, only while it is not over. The mirror
    only changes or removes its own copies: a race linked to an intervals.icu event it did
    not make gets no second copy, and that event is left alone. A change made in
    intervals.icu to a copy is put back and never read. Garmin is never written.
  - Every write stores a before and after snapshot on `audit_log` (`actor` `race`) and a
    "Plan change" on the coach feed with its reason. `vibesynergy_coach_state` has `races`
    and `events`.
- Race priority: `vibesynergy_race_set_priority` (`plan.propose`) stores A, B or C on one of
  VibeSynergy's races (#111). Null clears it. `vibesynergy_coach_state` races include
  `source` (`vibesynergy`), `source_id` (the race id) and `priority` (A, B, C, or null). A
  Garmin or intervals.icu id of a race's copy is accepted as `source_id` too. No sync
  changes a race or its priority. The portal calendar page edits it. `vibesynergy_plan`
  `races`, `next_event`, `supporting` and `next_race` include `source`, `source_id`, the
  priority and `priority_origin` (`saved` or `unset`). `next_race.is_primary` marks the
  primary race: the next A race (#120).
- Coach preferences: `vibesynergy_coach_state` is read-only. Since (31-10-2026) it returns
  only what the athlete sets on Coach → Settings: `documents.policy.effective` has
  `language` (`auto`/`en`/`nl`), `detail` (`concise`/`standard`/`detailed`), `tone`
  (`direct`/`supportive`), `instructions` (the athlete's notes for the coach),
  `policy_version` (`"3"`) and `boundaries`; `documents.context.effective.constraints` are
  the things to remember (availability, fixed moments, preferences, commitments, equipment,
  with optional weekdays and origin; retired notes are omitted). Plus `version` (combined
  fingerprint), `active_profile`, plan-calendar `races` and a `note` saying where the rest
  lives (the workout planner status went with the proposal pipeline). The method, goal priority, athlete profile, phase,
  sport order and per-sport methods, weekly time, usage budget, Garmin goal/event/Garmin
  Coach sources, manual races, source priorities and the internal coach's assessment
  schedule are gone, and so are `garmin_context` and `coaching_events`: the plan and its
  rules are in the knowledge base and VibeSynergy's plan, races and their A/B/C priority in
  VibeSynergy's own race list (#111). Older stored documents still
  load; the removed keys are ignored. Drafts are private settings; no planning or
  external-write capability is granted.
- Orientation: `vibesynergy_overview`, `vibesynergy_catalog`, `vibesynergy_schema`,
  `vibesynergy_explain`
- Days and periods: `vibesynergy_day`, `vibesynergy_period`, `vibesynergy_calendar`
- Series and statistics: `vibesynergy_series`, `vibesynergy_stats`, `vibesynergy_trend`,
  `vibesynergy_compare_periods`, `vibesynergy_correlate`, `vibesynergy_distribution`,
  `vibesynergy_baselines`, `vibesynergy_anomalies`
- Activity detail includes recorded-input versions and source aliases. A version is not an
  assessment.
- Activities: `vibesynergy_activities`, `vibesynergy_activity`, `vibesynergy_activity_streams`,
  `vibesynergy_bests` (power curve)
- RPE and Feel: `vibesynergy_activity` has `effort` with `rpe` (1-10) and `feel` (1-5,
  1 = Strong … 5 = Weak), each with `value`, `label`, `source` (`vibesynergy`, which wins,
  or `garmin`), `editable` (always true) and `others` (Garmin's value under your own). `vibesynergy_activities` and `vibesynergy_day` rows carry the short form
  (`rpe`, `rpe_source`, `feel`, `feel_source`). `vibesynergy_activity_set_effort`
  (`diary.write`) saves what the athlete said; it wins over Garmin's value. Garmin's 10-100
  effort and 0-100 feel are converted to these scales. Nothing is written to intervals.icu or
  Garmin.
- Workout texts (#114): every workout has its own name, public text and private text in
  VibeSynergy, each change stored as a version (when, source `vs`, `strava`, `garmin` or
  `ai`, old and new text; never overwritten). `vibesynergy_activity_texts` (`read`) gives the
  current texts and the history; until the first change they follow from the notes and the
  Strava/Garmin descriptions without VibeSynergy's block (a note that repeats the
  description is given once), and the first change stores those as `seed` versions.
  `vibesynergy_activity_set_texts` (`diary.write`) writes name, public and/or private
  (source `vs`); `vibesynergy_activity_restore_text` (`diary.write`) makes an old version
  current again as a new version. The private text is context only: it is never published,
  never in a share link and never in `vibesynergy_activity`. With Settings → Publishing and
  sync → *Keep name, texts and gear the same everywhere* on, a published workout's name and
  public text are kept the same on the places it is published to (two-way): edits made there come in as
  versions with source `strava` or `garmin` (author `sync`), and a version that lost to a
  later change is kept with kind `overwritten` (never the current text). Without it nothing
  is sent; nothing is ever sent to intervals.icu. The AI coach's activity context reads these
  texts when the workout has them.
- Insight and texts (#114, phase 4): the athlete's correction of the AI-coach insight (the
  workout page) is a version with field `correction` in the same history
  (`vibesynergy_activity_texts(field="correction")`); private like the private text. The
  insight run starts from VibeSynergy's texts too: after the Publish tick when the workout
  has no insight (run key `publish-<group>`), and as a rewrite of that one insight after its
  name, public or private text changed (five quiet minutes after the last edit) or after a
  correction (at once), run key `text-<group>-<version>`, at most
  `[coach_runner] insight_rewrites_per_day` (default 3, 0 = off) per workout per day. The
  run's context block `insight_rewrite` holds the current insight, the texts changed after
  it and the corrections; a `public_insight` that quotes the private text or a correction is
  refused. The new line reaches every published section on Strava and Garmin within
  about five minutes, whether or not new workouts are public.
- Training: `vibesynergy_training_load`, `vibesynergy_volume`, `vibesynergy_fitness`,
  `vibesynergy_intensity_distribution`
- Personal model runs: `vibesynergy_model_run_write` (`coach.write`) stores parameters
  and a markdown report for one `model_id`. The parameter diff against the previous
  run of that model is computed and stored. `vibesynergy_model_runs` (`read`) lists
  runs for a model and period, newest first.
- Wellness and body: `vibesynergy_sleep`, `vibesynergy_recovery`, `vibesynergy_intraday`,
  `vibesynergy_body`, `vibesynergy_blood_pressure`, `vibesynergy_profile`
- Sleep check (#100): every night is checked automatically, read-only on the raw history
  (`query/sleep_quality.py`) and marked `ok`, `suspect`, `incomplete` or `bad`, with the
  reasons and per metric (`sleep`, `hrv`, `readiness`, `resting_hr`) its own state. The
  rules: heart rate during the sleep window (a gap over 30 min or under 80% measured:
  incomplete), the window against the median bedtime and wake time of the 28 nights before
  (4 h or more off: suspect; under 6 h of sleep while the heart rate stays at a sleeping
  level for 90 min after the end: suspect), the stages (over 3 h of sleep without REM, an
  implausible share of one stage, or one stage for 4 h without a change: bad), HRV over
  under 70% of the window (incomplete for HRV), a night updated later and a readiness from
  the partial night (#88), and a good check-in against under 5 h measured (#53). It is
  stored as a derived result (`sleep_check` table, with `checked_at`) and checked again when
  Garmin delivers a new version of the night. The user decides last:
  `vibesynergy_sleep_check_set` (`diary.write`) with `choice` `incomplete`, `ok` ("it's
  right", beats the check) or `clear`. A metric that is `bad` or `incomplete` does not count
  for the safety floor (`short_sleep`, `low_readiness`, `low_hrv`, `rhr_up`) and is left out
  of `vibesynergy_baselines` (`left_out_nights`) and `vibesynergy_trend`
  (`left_out_nights`, the dates); a `suspect` one still counts and the advice names it.
  `vibesynergy_today` (`sleep_check`), `vibesynergy_sleep` (per night `check`) and
  `vibesynergy_explain` (`sleep_check`) show the mark. **This changes behaviour**: a night
  found badly recorded or incomplete is left out of the advice and the baselines without
  being asked; `sleep_check` in `vibesynergy_today` has a new shape (`state` is `ok`,
  `suspect`, `incomplete` or `bad`; `updated` is now one of its reasons).
- Data: `vibesynergy_sync_status`, `vibesynergy_latency`, `vibesynergy_coverage`, `vibesynergy_metric_status`,
  `vibesynergy_refresh`, `vibesynergy_sql`
- Shared coaching read: `vibesynergy_activity_assessment`, `vibesynergy_current_coaching_state`,
  `vibesynergy_daily_coaching`. All read what the coach wrote (the coach feed) and the fixed
  safety rules; none starts inference.
- Diary (phase 6): `vibesynergy_notes`, `vibesynergy_add_note`, `vibesynergy_edit_note`,
  `vibesynergy_checkin`, `vibesynergy_injuries`, `vibesynergy_log_injury` over
  `src/vibesynergy/query/journal.py`: notes for a day, a span or an activity with tags, a
  1-5 morning check-in, and an injury log (body part, side, status, worst pain per day)
  with fixed warning rules (pain above 3/10, worse 3 days running, pain that changes
  movement, a returning complaint, no update for a week). `vibesynergy_day` includes the
  day's notes, check-in and open injuries.  The diary tables are also available to `vibesynergy_sql`.
- The diary in analyses: `checkin.*` (1-5) and `pain.max` (worst pain of the day, 0-10)
  are metrics like any other (series, stats, correlate, …). Baselines and anomaly
  detection leave days tagged `sick` out of the personal norm by default (the day itself
  is still compared); `vibesynergy_stats`, `vibesynergy_trend`, `vibesynergy_compare_periods`,
  `vibesynergy_correlate` and `vibesynergy_distribution` take `exclude_tags`. Anomalies and
  `vibesynergy_baselines` carry the day's diary context (notes, check-in, pain).
- Prompts: all eight of section 5. Resources: `vibesynergy://guide`, `vibesynergy://metrics`,
  `vibesynergy://schema`, `vibesynergy://athlete/notes`, `vibesynergy://coach/handbook`.

Every tool call is written to `audit_log` (actor `mcp`, client id, author, scope, target,
outcome, duration). Write arguments are kept. Read arguments stay short, and a SQL query
is stored only as its length. Read rows older than 180 days are dropped; writes stay.

Coach entries carry their author: `client:<id>` for an app, `user` for the athlete. Rows from
the former external coach keep their stored author and read as `ai_coach` (shown as
"AI coach" / "AI-coach"); audit rows are never rewritten.

Transports are stdio (`vibesynergy mcp`) and streamable HTTP inside `vibesynergy serve` at
`<public_url>/vibesynergy/mcp`, protected by OAuth 2.1 (`src/vibesynergy/api/oauth.py`):
dynamic client registration, PKCE (S256), approval by passphrase on a VibeSynergy page,
access tokens for 1 hour, and refresh tokens for 90 days that are replaced on every use.
Tokens are stored as hashes. Every app can be revoked on its own (`vibesynergy mcp revoke`).
Request bodies on the public routes (registration, token, approval, webhooks) are limited to
64 KiB, MCP calls to 16 MiB; a bigger body, also one sent without `Content-Length`, gets
413. Registration free text (`client_name`, software id and version) is cut to 100
characters, and `oauth.register` and `oauth.denied` audit rows are kept for 90 days, at
most the newest 1,000 of each.
Discovery works under `/vibesynergy`: the 401 carries `resource_metadata`, and the issuer
`<public_url>/vibesynergy` serves RFC 8414 metadata at `/.well-known/oauth-authorization-server`
below it. The RFC 8414 path-inserted URL at the host root requires a Funnel route to this
endpoint. VibeSynergy does not issue ID tokens and `openid` is not a scope.

Path-prefix registration (issue): MCP clients with a path in the issuer try
`{issuer}/.well-known/openid-configuration` after the path-inserted well-known URLs.
For this issuer that is `<public_url>/vibesynergy/.well-known/openid-configuration`, which
the Funnel forwards. That document advertises the same absolute endpoints as the RFC
8414 metadata, including `registration_endpoint` `<public_url>/vibesynergy/register`.
The MCP TypeScript SDK parses this URL with its OpenID discovery schema, so the
document also includes `jwks_uri` (an empty key set at
`<public_url>/vibesynergy/.well-known/jwks.json`), `subject_types_supported` and
`id_token_signing_alg_values_supported`. Those fields do not mean an ID token is
issued. Dynamic registration against the advertised `registration_endpoint` is what
clients must use. `new URL("/register", authorizationServerUrl)` drops `/vibesynergy`
and requests `<public_url>/register`, which the Funnel does not forward to VibeSynergy.
Metadata does not point at that host-root URL.

After `mcp logout`, a client that rediscovers without the 401 `resource_metadata`
falls back to the bare host (`new URL("/", "<public_url>/vibesynergy/mcp")`).
Host-root well-known URLs are the Garmin connector, not VibeSynergy. A login that
starts from `<public_url>/vibesynergy/mcp` receives `resource_metadata` for
`<public_url>/vibesynergy/.well-known/oauth-protected-resource`, whose
`authorization_servers` value stays `<public_url>/vibesynergy`. That issuer is the
same on every metadata document VibeSynergy serves. The approval redirect includes
`iss` set to that issuer (RFC 9207) so the callback is bound to `/vibesynergy`, not
the host root. Retiring a host-root `/register` forwarder is a live check after
deploy, not part of this metadata change.

Protected-resource metadata (RFC 9728): the Funnel forwards only `/vibesynergy/*` to this
service; the host root belongs to the Garmin MCP connector. The strict path-inserted URL for
the resource `<public_url>/vibesynergy/mcp` is `<public_url>/.well-known/oauth-protected-resource/vibesynergy/mcp`,
which lives at that root and cannot reach VibeSynergy without a change outside this service
(Tailscale/root host). Clients therefore discover it through the `resource_metadata` URL in
the 401 `WWW-Authenticate` header, `<public_url>/vibesynergy/.well-known/oauth-protected-resource`,
whose `resource` is exactly `<public_url>/vibesynergy/mcp`. The service also answers the same
document at `/.well-known/oauth-protected-resource` and the RFC 9728 path-inserted route
`/.well-known/oauth-protected-resource/vibesynergy/mcp` when a proxy forwards those paths.
The old `/.well-known/oauth-protected-resource/mcp` alias is removed: its `/mcp` path
does not match the document's `/vibesynergy/mcp` resource. No client URL advertises that alias.
Clients that probe under the MCP address itself
(`<public_url>/vibesynergy/mcp/.well-known/oauth-protected-resource`,
`.../oauth-authorization-server` and `.../openid-configuration`, and the same three
paths after the Funnel strips `/vibesynergy`) receive those same documents. Those routes
are registered before the MCP endpoint so the endpoint does not take the request.
The public root remains assigned to the Garmin connector; this change does not alter the
Funnel mapping or make the strict path-inserted URL publicly reachable.

Redirect URIs at dynamic registration: `https`, `http` loopback (`127.0.0.1`, `[::1]`,
`localhost`, any port at authorization time) or a private-use app scheme (RFC 8252 §7.1, e.g.
`cursor://`, `zed://`, `com.example.app:/`). Dangerous schemes (`javascript`, `data`, `file`,
`vbscript`, `blob`, `about`, and other non-app schemes) are rejected.

The old secret-path address (`/vibesynergy/<secret>/mcp` and per-client paths of that kind) has
been removed (#9): every such path answers 404, and a leftover `[server] mcp_secret_url` key
is ignored with a warning at startup. Coaches use OAuth or a personal access token at
`/vibesynergy/mcp`.

Differences from the spec below, for now:
- Values are SI in every response (no unit conversion); the guide tells the AI to convert.
- `vibesynergy_sql` is SQLite over the documented tables (Power tools, below), read-only,
  at most 5,000 rows, 10 s, 1 MB per value and about 20 MB for the whole result (beyond it
  the rows stop and `truncated` is true). Streams are available through
  `vibesynergy_activity_streams`.
- Lists use `offset` instead of opaque cursors. Result sets are not built: `rs_…` handles,
  `input: rs_…`, `result_set`, `save_as_result_set`, `cursor` and `format` in the sections
  below are the design only.
- `vibesynergy_training_load` uses VibeSynergy's own load by default (`load="vibesynergy"`,
  the one the portal and the coach use; intervals.icu's fitness is not read). Per activity:
  power (TSS from normalized power and FTP) for rides with power, pace against CSS for swims
  (sTSS, from the first confirmed CSS on; before it heart rate), else the heart-rate stream
  (TRIMP scaled so an hour at LTHR is 100; the average without a stream), else run pace; with
  the thresholds valid on the activity's date (Zones & thresholds, then Garmin's history; the
  oldest known before that; LTHR 90 % of max heart rate until one is known). A session stored
  twice (Garmin and Strava not merged) counts once. Fitness and fatigue are 42/7-day EWMAs with
  weights `1 - e^(-1/n)`, form = same-day CTL - ATL, and `current.form_zone` is the zone on
  form % of fitness (`transition` > +20, `fresh` +5..+20, `grey` -10..+5, `optimal` -30..-10,
  `high_risk` < -30). `current.form` keeps the older absolute band. `load="trimp"` uses TRIMP (Banister, from each activity's duration and
  average heart rate), which covers every activity with heart rate; `load="garmin"` uses
  Garmin's per-activity load, which only activities from the Garmin API carry; `load_metric="tss"`
  uses TSS from power (normalized power when present, else average) where a power zone/FTP
  is set.
- The significance tests use normal approximations, and results say so.

**Scopes (implemented):** `read`, `read.expert`, `diary.write`, `coach.write`,
`plan.propose`, `plan.write`, `sync`, `publish`, `sql`. Each tool checks the token. `sql` is its own
scope (`vibesynergy_sql`); it is not folded into read or `read.expert` (`vibesynergy_schema` is
`read.expert`). The old single scope `vibesynergy` is retired: it is not advertised,
registration with it is refused, and an authorization request that names it gets
`invalid_scope`. Grants stored with it (personal tokens, app registrations, live tokens)
are rewritten once, when the service starts, to what it meant: every scope except
`publish` and `plan.write`, so including `sql`; an audit entry
`oauth.legacy_scope.expanded` records how many. A request or stored grant with no scope
at all still means that same set. The consent page lists each requested scope in plain
words (Dutch and English) and stores the checked set. A client that omits a scope at
registration may request any advertised scope; the consent page is the grant. A new personal
token starts as `read`, `coach.write`, `diary.write` and `plan.propose` (no `publish`, no
`sql`), expires in 30, 90 or 365 days, and is created in Settings → Sharing → Coach apps with the
coach passphrase: the tailnet alone cannot mint one. Wrong passphrases there and on
the approve page are limited to 5 per 15 minutes, counted per app (and once for the
portal), with a ceiling of 50 across all apps, so a stranger's registration cannot
lock the athlete out. The passphrase is set on that page and is never sent back to the
browser; changing it needs the current one, under the same limit. `publish` and
`unpublish` and `refresh` are annotated `openWorldHint`; `edit_note` is `destructiveHint`.

**Not built:** profiles (`core`/`analysis`/`full`; every client is offered every tool and the
scopes decide what it may call); the tools `vibesynergy_ecg`, `vibesynergy_nutrition`,
`vibesynergy_result` (result sets) and `vibesynergy_chart` (charts); the resources
`vibesynergy://overview`, `vibesynergy://day/{date}`, `vibesynergy://activity/{id}`,
`vibesynergy://week/{iso_week}` and `vibesynergy://profile` (the tools of the same name give
that data); resource templates; argument completion (`completion/complete`). A primary-coach picker and undoing a note or check-in from the activity list
are not part of this scope work.

## 1. Who calls this and for what

Two usage modes must both be first-class:

| Mode | Typical question | What the AI needs |
|---|---|---|
| **Viewing** | "How did I sleep?", "What did I do last week?", "Show my last ride." | One call, compact human-sized answer, sensible defaults, no parameters required |
| **Analysis** | "Does late training hurt my HRV?", "Has my aerobic efficiency improved since spring?", "Compare my threshold intervals this block vs last." | Precise metric definitions, aligned time series, server-side statistics, chaining large results without flooding context, raw access as an escape hatch |

Consequences for the design:
1. **Server does the maths.** LLMs are unreliable at arithmetic over hundreds of numbers.
   Aggregation, statistics, regression, correlation, curves and baselines are computed by
   the server and returned with the method stated.
2. **Everything is addressable by stable IDs.** Metrics, activities, days, result sets.
3. **Big results become handles** (design; not built). A tool can return a *result set* (`rs_…`) with a preview;
   other tools accept `rs_…` as input. The AI chains analysis without copying data through
   its context.
4. **Every answer says what it is.** Units, time zone, sources, method, freshness, and
   whether it is truncated.

---

## 2. Core concepts

### 2.1 Metric catalogue
Every quantity has a stable dotted ID. The catalogue (tool `vibesynergy_catalog`, resource
`vibesynergy://metrics`) lists for each metric:

| Field | Example |
|---|---|
| `id` | `sleep.score` |
| `label` | Sleep score |
| `unit` | `score_0_100` (canonical SI or named unit) |
| `display_units` | `{metric: "…", imperial: "…"}` |
| `domain` | `sleep` |
| `granularity` | `night` / `day` / `intraday` / `activity` / `event` |
| `daily_agg` | how several values in one day combine: `last`, `mean`, `sum`, `max`, `min` |
| `period_agg` | default aggregation over weeks/months: `mean`, `sum`, … |
| `sources` | `["garmin", "withings"]` with the best-source rule |
| `coverage` | first/last date with data, % of days present |
| `derived` | true/false + formula reference (e.g. `ctl`: 42-day EWMA of daily TSS) |
| `higher_is_better` | true / false / null, for baselines and anomaly wording |
| `description` | one sentence, including caveats |

Metric namespaces (non-exhaustive; the catalogue is generated from the data model, so new
sources add metrics automatically):

```
activity.*   count, duration, moving_time, distance, elevation_gain, calories, tss, trimp,
             intensity_factor, np, avg_power, avg_hr, max_hr, avg_pace, avg_speed, cadence,
             ef (efficiency factor), decoupling, training_effect.aerobic|anaerobic, …
             (also per sport: activity.run.distance, activity.ride.tss, …)
daily.*      steps, floors, active_calories, total_calories, intensity_minutes.moderate|vigorous,
             distance
hr.*         resting, min, max, avg (intraday: hr.sample)
hrv.*        night_avg, status, baseline_low|high, weekly_avg (intraday: hrv.reading)
sleep.*      score, duration, deep, light, rem, awake, efficiency, onset, wake, latency,
             regularity, avg_hr, avg_hrv, avg_respiration, spo2_avg, restless_moments
recovery.*   readiness, readiness.morning, readiness.sleep|sleep_history|recovery_time|hrv|
             acute_load|stress_history (Garmin's readiness factors, %), recovery_time,
             body_battery.max|min|charged|drained, stress.avg|rest|high_minutes
resp.*       waking_avg, sleep_avg, min, max          spo2.* avg, min
body.*       weight, fat_pct, fat_mass, muscle_mass, lean_mass, bone_mass, water_pct,
             visceral_fat, bmi, pwv, vascular_age, nerve_health
bp.*         systolic, diastolic, pulse
fitness.*    vo2max.run|ride, fitness_age, endurance_score, hill_score, hill_score.endurance|
             strength, lactate_threshold.hr|pace|power, cycling_ftp, acclimation.heat|altitude,
             race_pred.5k|10k|half|marathon
training.*   Garmin's own load model: load.acute|chronic|ratio,
             load_balance.aerobic_low|aerobic_high|anaerobic, running_tolerance,
             running_impact_load (weekly, dated to the week's first day)
load.*       ctl, atl, tsb, acwr, weekly_tss (VibeSynergy's own model)
```

Unknown metric IDs return an error listing the closest matches.

### 2.2 Time
- All tools take `start` / `end` **or** `period`. `period` accepts:
  `today`, `yesterday`, `last_7d`, `last_14d`, `last_28d`, `last_90d`, `last_365d`, `week`
  (Monday–Sunday of the current week), `this_week` (Monday through today), `last_week`,
  `last_<n>d`, `last_<n>m` (rolling calendar months, any *n* ≥ 1), `next_<n>d`, `next_<n>m`
  (forward from today), `this_month`, `last_month`, `this_year`, `ytd`, `all`, ISO week
  `2026-W38`,
  month `2026-09`, year `2026`.
- Dates are **local calendar dates** of the user (the zone they were in on that date; stored
  per day, so travel works). Timestamps are ISO 8601 with offset.
- A "day" for sleep is the night that **ends** on that date (Garmin convention); stated in
  the catalogue.
- Weeks are ISO weeks (Monday start) by default; configurable.

### 2.3 Granularity and aggregation
`granularity`: `raw` | `minute` | `hour` | `day` | `week` | `month` | `year` | `activity`.
`agg`: `mean`, `median`, `sum`, `min`, `max`, `count`, `first`, `last`, `std`, `p10`, `p25`,
`p75`, `p90`. Default per metric from the catalogue. Every aggregated value also returns
`n` (samples/days it is based on), so the AI can see thin data.

### 2.4 Identifiers
| ID | Format | Example |
|---|---|---|
| Activity | `act_<ulid>` (unified, cross-source) | `act_01J8…` |
| Source object | `garmin:123456789`, `strava:98765` | accepted anywhere an activity ID is |
| Day | `YYYY-MM-DD` | `2026-09-24` |
| Result set (not built) | `rs_<id>` (expires after 24 h, configurable) | `rs_7f3a…` |
| Gear | `gear_<id>` | |
| Note | `note_<id>` | |

### 2.5 Common response envelope
Every tool returns **structured content** (with a declared `outputSchema`) plus a short text
rendering for clients that ignore structure:

```json
{
  "data": { ... tool specific ... },
  "meta": {
    "units": {"body.weight": "kg", "sleep.duration": "s"},
    "timezone": "Europe/Amsterdam",
    "period": {"start": "2026-09-01", "end": "2026-09-24"},
    "sources": ["garmin", "withings"],
    "freshness": {"garmin": "2026-09-24T21:47Z", "withings": "2026-09-24T21:47Z"},
    "method": "daily mean of nightly values; weeks = ISO",
    "truncated": false,
    "next_cursor": null,
    "result_set": "rs_…",
    "portal_url": "https://app.example.org",
    "warnings": ["Withings has no data 2026-09-10..2026-09-12"]
  }
}
```

`meta.portal_url` is the portal's canonical address (#117): your own domain when one is set,
else the tailnet name. A `link` in the data, such as `#/coach/report/7`, opens at
`<portal_url>/#/coach/report/7`. It is never an alias, whatever address the MCP call came in on.

### 2.6 Output size control
- `detail`: `summary` (default) | `standard` | `full`.
- `limit` + `cursor` on every list.
- `max_points` on series (default 400); the server downsamples (LTTB for line data, proper
  aggregation for bucketed data) and says so in `meta.method`.
- `format`: `json` (default) | `table` (compact markdown/CSV-like text, good for viewing).
- Hard response cap (~25 k tokens). Above that: truncate, return `result_set`, and explain how
  to page or narrow.

### 2.7 Units
Canonical storage is SI. Output units follow the user profile (`metric` default) and can be
overridden per call with `units: "metric" | "imperial" | "si"`. Pace is shown as `mm:ss/km`
in `table` format and as seconds/km in JSON.

### 2.8 Provenance
Any value can be traced: `vibesynergy_explain` returns the source, the raw record it came from,
when it was fetched, which best-source rule chose it and which alternatives exist. This builds
trust when Garmin and Withings disagree about weight, for example.

---

## 3. Tool list

Tools are grouped by namespace. All are **read-only** (`readOnlyHint: true`) except those
marked with a scope. Profiles (§6) are not built: a client is offered every tool, and its
scopes decide which calls are allowed.

### 3.1 Orientation and discovery

#### `vibesynergy_overview`
The starting point. What is in the store and what happened recently.
- As built: `next_race` and `plan_phase` use the same planned-event projection as
  `vibesynergy_plan`; both are `null` when no upcoming race is recorded.
- Params: none (optional `period`, default `last_7d`).
- Returns: sources with connection state and freshness; data coverage per domain; the last 7
  days in one line each (sleep score, HRV, RHR, load, activities, weight); open sync problems;
  current training status and fitness numbers (CTL/ATL/TSB, VO2max, FTP, threshold pace).

#### `vibesynergy_catalog`
Search the metric catalogue.
- Params: `query` (text, optional), `domain` (optional), `include_coverage` (bool, default true).
- Returns: matching metric entries (§2.1).

#### `vibesynergy_schema`
Documentation of the SQL views for `vibesynergy_sql`: tables, columns, types, units, join keys,
example queries. Also available as resource `vibesynergy://schema`.

#### `vibesynergy_explain`
- Params: `metric` + `date`, or `activity_id` + `field`. `metric` `workout:<template id>` (or
  `workout` for every template; `date` is then ignored) explains the workout library's
  values instead (#142): the knowledge-base article `workout-method` and its `version`,
  `source` (`knowledge_base`, or `seed` when the article is missing or archived), the values
  in use per template, `from_seed` (the values the article lacks or holds in the wrong form,
  taken from the built-in seed) and `problems`.
- Returns: provenance (§2.8) and alternative values from other sources. A metric from the
  night (sleep, HRV, readiness, resting HR) adds `sleep_check` (#100): the night's mark
  (`state`, `auto_state`, `override`, `metrics`, `not_counted`, `reasons` with `rule`,
  `state` and `text`, `checked_at`), `this_metric` (the state of this metric) and `counts`
  (whether it counts for the advice and the baselines). The value itself stays as the
  source measured it. A night the athlete marked incomplete also adds `marked_incomplete`.

### 3.2 Viewing: days and periods

#### `vibesynergy_day`
Everything about one day, unified.
- As built: `next_race` (`name`, `date`, `days_to_go`, `distance_m`, `priority`) and
  `plan_phase` (`phase`, `days_left`) are projected as of the requested local date.
  `distance_m` is null when the race has no distance. `priority` is A, B, C, or null: the
  race's own letter in VibeSynergy. `priority_origin` is `saved` or `unset`. `is_primary`
  is true for the next A race. `source` is `vibesynergy` and `source_id` the race id
  (#111). A recorded race is in `taper`
  from 14 days before race day; otherwise the projected phase is `build` until then.
  Both fields are null when no future race is recorded.
- Params: `date` (default today), `detail`, `include` (subset of
  `sleep, recovery, activity, body, nutrition, intraday, notes, plan`).
- Returns: sleep (score, stages, times, HRV, RHR), readiness and its factors, body battery
  range, stress, steps/calories/intensity minutes, activities (compact list), planned vs done
  workouts, weigh-in and BP, notes. With `intraday`: downsampled HR, stress and body battery
  curves.
- Also compares each key value with the personal baseline (`+0.8 σ above your 60-day norm`).

#### `vibesynergy_period`
A digest of a period, for "how was my week/month".
- Params: `period` or `start`/`end`, `compare_to` (`previous` | `same_last_year` | `none`,
  default `previous`), `detail`.
- Returns: totals per sport (count, time, distance, elevation, load), intensity distribution,
  sleep and recovery averages, body trend, notable events (PRs, anomalies, illness notes),
  each with the comparison delta.

#### `vibesynergy_calendar`
Compact grid for scanning longer periods.
- Params: `period` (default `last_28d`), `metrics` (default: load, sleep score, HRV, RHR,
  activities).
- Returns: one row per day, one column per metric. Good for "show me the last month".

### 3.3 Time series (the backbone of analysis)

#### `vibesynergy_series`
Any metrics over time, aligned on one time axis.
- Params: `metrics[]` (1–12), `period` / `start` / `end`, `granularity` (default auto:
  day up to 180 days, week up to 3 years, month beyond), `agg` (per metric override
  allowed: `{"body.weight": "median"}`), `fill` (`none` | `null` | `ffill`, default `null`),
  `rolling` (e.g. `7d` rolling mean alongside raw), `filters` (e.g. `{"sport": "ride"}` for
  activity metrics), `max_points`, `format`.
- Returns: `columns`, `rows`, per-metric `n` and coverage; `result_set` for chaining.
- Intraday metrics (`hr.sample`, `stress.sample`, `body_battery.sample`, `hrv.reading`)
  require `granularity` ≤ `hour` and a range ≤ 14 days (server refuses bigger with advice).

#### `vibesynergy_stats`
Descriptive statistics.
- Params: `metrics[]`, period, `group_by` (`none` | `weekday` | `month` | `sport` | `year` |
  `tag:<note tag>`), or `input: rs_…`.
- Returns per metric (and group): n, mean, median, std, min, max, p10/p25/p75/p90, first and
  last value, change.

#### `vibesynergy_trend`
Is it changing, and how fast?
- Params: `metric`, period, `method` (`linear` | `theil_sen` (default, robust) | `loess`),
  `granularity`.
- Returns: slope per week/month with confidence interval, total change, R², change points
  (if any, with dates), plain-language summary ("weight −0.21 kg/week, 95% CI −0.28…−0.14").
  Nights the sleep check marked badly recorded or incomplete for the metric are left out
  and listed in `left_out_nights` (#100).

#### `vibesynergy_compare_periods`
- Params: `metrics[]`, `period_a`, `period_b` (or `period_a` + `baseline: "previous" |
  "same_last_year"`), `test` (`none` | `welch_t` | `mann_whitney`, default `mann_whitney`).
- Returns: per metric both summaries, absolute and relative difference, effect size, p-value
  when a test is requested, and a note about sample size.

#### `vibesynergy_correlate`
- Params: `x` (metric), `y` (metric), period, `lag_days` (int or range `0..3`),
  `method` (`spearman` default | `pearson`), `granularity` (`day` default), `filters`
  (e.g. only days with a ride), `partial_on` (optional control metric).
- Returns: coefficient per lag, n, p-value, a scatter preview (binned), outlier dates, and
  a caution line (correlation ≠ causation, sample size).

#### `vibesynergy_distribution`
- Params: `metric`, period, `bins` (auto), `group_by`.
- Returns: histogram, quantiles; for time-in-zone metrics the zone breakdown.

#### `vibesynergy_baselines`
Personal norms and deviations, the basis of "is this normal for me".
- Params: `metrics[]` (default: HRV, RHR, sleep score, readiness, weight), `window`
  (default `60d`), `date` (default today).
- Returns per metric: baseline mean/std or median/MAD, today's value, z-score, percentile,
  7-day trend direction. Nights the sleep check marked badly recorded or incomplete for the
  metric are left out of the baseline; `left_out_nights` counts them (#100).

#### `vibesynergy_anomalies`
- Params: `metrics[]`, period (default `last_28d`), `threshold` (z, default 2), `window`.
- Returns: dated list of unusual values with direction, size, and nearby context (e.g. a hard
  session the day before, a note "sick").

### 3.4 Activities

#### `vibesynergy_activities`
Search and list.
- Params (all optional): period, `sport` (list; run, trail_run, ride, virtual_ride, gravel,
  mtb, swim, strength, walk, hike, row, ski, other), `text` (title/description/location),
  `min_/max_distance`, `min_/max_duration`, `min_/max_tss`, `has` (`power`, `hr`, `gps`,
  `laps`), `gear`, `source`, `workout_type` (`race`, `long`, `interval`, `easy`, `commute`),
  `tag`, `sort` (`date` default desc, `distance`, `duration`, `tss`, `np`, `avg_hr`…),
  `fields` (choose columns), `limit` (default 20), `cursor`, `format`.
- Returns: rows with ID, date, sport, title, duration, distance, elevation, avg/NP power,
  avg HR, pace/speed, TSS, training effect, sources; totals for the full match set
  (not just the page).

#### `vibesynergy_activity`
One activity in depth.
- Params: `id`, `include` (subset of `summary, laps, splits, zones, intervals, weather,
  gear, device, sources, notes, curve, map, derived`; default `summary, laps, zones,
  derived`), `units`.
- Returns: summary; laps/splits; time in HR/power/pace zones (using the zones valid on that
  date); detected intervals; derived metrics (NP, IF, TSS, VI, EF, Pa:HR decoupling, TRIMP,
  aerobic/anaerobic effect, W′ balance minimum); weather; gear; sources and which one each
  value came from; `portal_url` to the map and charts. `map` returns an encoded polyline
  (simplified) plus start/end place names, not raw coordinates. `workout_steps`: for a
  Garmin structured workout, what was done per step (Garmin's step fields: intensity,
  duration, distance, heart rate, power, speed; a nested list becomes `<name>_count`), and
  each lap names its step in `extra.wktStepIndex`. `rounds`: the rounds of a HIIT, boxing or
  cardio class. Both are `[]` when Garmin has none.

#### `vibesynergy_activity_streams`
Per-second data, sized for context.
- Params: `id`, `channels` (time, distance, hr, power, cadence, speed, pace, altitude, grade,
  temperature, lat/lon, running dynamics (gct, vo, stride), developer fields), `range`
  (`{"from": "10:00", "to": "40:00"}` by elapsed time or `{"from_km": 5, "to_km": 10}`),
  `resample` (e.g. `10s`, `1min`, `100m`, or `max_points`), `format`.
- Returns: aligned columns; always a `result_set` so full resolution can be analysed
  server-side without transfer.

#### `vibesynergy_activity_analyze`
Analysis of a window within an activity (or the whole thing), server-side.
- Params: `id` or `input: rs_…`, `range`, `metrics` (default: avg/max/NP power, avg HR,
  HR drift, EF, decoupling, cadence, pace, grade-adjusted pace, time in zones).
- Returns: the metrics for the window, plus comparison with the rest of the activity.
  Use cases: "my HR drift in the last hour", "power on the final climb".

#### `vibesynergy_intervals`
Find and summarise work intervals.
- Params: `id`, `method` (`laps` | `auto` | `threshold`), `min_duration`, `target`
  (e.g. `power>=0.9*ftp` or `pace<=4:10/km`).
- Returns: interval table (start, duration, avg/max, HR response, recovery), consistency
  (fade from first to last), comparison with the planned workout if one was scheduled.

#### `vibesynergy_compare_activities`
- Params: `ids[]` (2–10), `align` (`time` | `distance` | `laps`), `metrics`.
- Returns: side-by-side summaries, per-lap/interval comparison, and differences; for the same
  route, split-by-split deltas.

#### `vibesynergy_similar_activities`
- Params: `id`, `by` (`route` (GPS overlap), `workout` (interval structure), `profile`
  (sport + duration + intensity)), `period`, `limit`.
- Returns: matches with similarity score and the key metric deltas; ideal for "am I getting
  faster on this loop".

#### `vibesynergy_activity_set_purpose` *(scope `diary.write`)*
- Params: `id`, `purpose` (`training`, `recovery`, `leisure`, `coached_by_me`, `race`, or
  null for automatic).
- Sets what the session was for. A training you give (`coached_by_me`) stays out of your
  own load. `vibesynergy_activity_set_coaching(id, is_coaching)` is the same switch for that
  one case: true = `coached_by_me`, false = `training`, null = automatic from the title.

#### `vibesynergy_bests`
Records and performance curves.
- Params: `sport`, `kind` (`power_curve` | `pace_curve` | `hr_curve` | `best_efforts` |
  `prs`), period (default `all`), `compare_period` (optional), `durations` / `distances`
  (optional custom points).
- Returns: best value per duration/distance with the activity it came from and date;
  with `compare_period`, the curve for both periods and the gap; modelled CP/W′ (critical
  power) and critical speed where enough data exists.

### 3.5 Training

#### `vibesynergy_training_load`
- Params: period (default `last_90d`), `sport` (default all), `model` (`banister` CTL/ATL/TSB
  default | `acwr`), `load_metric` (`tss` default, `trimp`, `garmin_load`).
- Returns: daily CTL/ATL/TSB (or ACWR), weekly load, ramp rate, current form band
  (fresh/optimal/overreaching), and Garmin's own training status/load balance for comparison.

#### `vibesynergy_intensity_distribution`
- Params: period, `sport`, `basis` (`hr` | `power` | `pace`), `model` (`3zone` | `5zone` |
  `garmin`).
- Returns: time per zone per week and total, polarisation index, share of low/moderate/high.

#### `vibesynergy_volume`
- Params: period, `granularity` (week default), `by` (`sport` default | `gear` | `workout_type`).
- Returns: time, distance, elevation, count, load per bucket, with totals and averages.

#### `vibesynergy_plan`
Planned vs completed training.
- Params: period (default `this_week` + next 7 days).
- Returns: scheduled workouts (Garmin calendar, training plan) with their
  structure, the matching completed activity if any, compliance (duration, intensity,
  target hit), and upcoming sessions.
- As built: default the last 7 days and the next 28; the active coaching plan(s); calendar
  entries (plan workouts, scheduled workouts, events) with status done / changed / missed /
  today / upcoming, matched to an activity of the same sport family that day. `changed`
  means that match differs by more than 25% in duration or distance, or the intensity
  differs. `this_week` and `week` on this tool are Monday to Sunday (other tools still treat
  `this_week` as Monday through today). Today's sessions; the next event and upcoming races
  with days to go; adherence (planned vs done, and a changed session is not counted as
  done). The shared `next_race` and `plan_phase` fields are also included. `races`,
  `next_event`, `supporting` and `next_race` each include `source` (`vibesynergy`),
  `source_id` and `race_id` (the race's id), `sport`, `priority` (A, B, C, or null) and
  `priority_origin` (`saved` when the race has a letter, else `unset`). Races are
  VibeSynergy's own (#111): a Garmin event or an intervals.icu `RACE_A/B/C` event is never
  read as a race. `next_race.is_primary` is true for the primary race: the next A race
  (#120). The soonest A race sets the phase; with no A race, the soonest race leads. The
  calendar is fetched `[calendar] lookahead_months` ahead (12 months when unset); Garmin
  drops past plan days from its replies, so every version is kept.
- The plan is VibeSynergy's own, and only that (#111): intervals.icu holds a one-way copy
  that it passes on to other services, and nothing in that copy (workouts, week notes,
  targets, races) is read as the plan. A change made in intervals.icu changes nothing in
  this tool, `vibesynergy_today`, `vibesynergy_week`, the dashboard or the AI coach's
  context. `plan_source` is `vibesynergy` when the window reaches `plan_start` (the first
  day with a planned session), else `garmin`. From `plan_start` on:
  - each planned session is an item with `source` `vibesynergy`, `source_id` and
    `session_id` the session id (for the plan writes below),
    `kind` `workout`, `ownership` `vibesynergy_plan`, `in_plan` true, a Garmin-style `sport`
    (`running`, `cycling`, `swimming`, `strength_training`, `walking`; the raw type is
    `extra.icu_type`) and `extra` with `description` (cue and steps), `duration_s` and
    `distance_m` when known, and, while the session has a copy in intervals.icu, `icu_id`
    (that event's id, for the activity pairing and Garmin delivery). A skipped session is
    not an item;
  - a Garmin scheduled workout that is a copy of a plan session is not a second item. It
    is listed on the session as `garmin_copies` (`source_id`, `title`, `sport`, `match`:
    `name` for the same day, sport family and name, `brick` for a part of a brick title,
    `plan_tag` for a title carrying the plan's id such as `(race27)` on a day with plan
    sessions, and its calendar `mirrors`). Garmin has no id that links the two;
  - any other Garmin workout (Garmin Coach's "Basis", "Drempel", one added by hand in
    Garmin) is not VibeSynergy's and is not shown (#153): it is in no list of this tool or
    of `vibesynergy_today`, `vibesynergy_week`, the horizon guard or the AI coach's context,
    and never counted. Its synced row stays in the database;
  - `weeks` come from the plan's own week notes and targets (`note.id` and a target's `id`
    are the plan's keys such as `race27:week:03`; a target has no `title`).
  Days before `plan_start`, and an install without a plan, keep the Garmin calendar as the
  plan, with `plan_source` `garmin` on each training item.
- `adherence` also has `changed` and `missed`. `planned` and `done` are unchanged in
  meaning.
- `weeks`: one row per Monday in the window that has a week note or weekly target in the
  plan: `start`, `end`, `note` (`id`, `title`, `text`) and `targets` (`sport`, `family`,
  `time_s`, `distance_m`, `load`, and `done_s` / `done_distance_m` from the activities that
  week), plus `time_s`, the sum of the time targets.
- Races: a VibeSynergy race is an item with `kind` `event`, `source` `vibesynergy` and
  `extra.ownership` `vibesynergy_plan`. A Garmin event on the day of a race (or linked to
  it) is its copy, named in `extra.garmin`; `intervals_id` is the id of its intervals.icu
  copy. Any other Garmin event is not shown (#153). Migration 0053 moved the races Garmin
  and intervals.icu held into VibeSynergy once; `vibesynergy plan import-races` repeats
  that copy for races VibeSynergy has not got yet, and never changes one.

#### `vibesynergy_plan_model`
The training plan as VibeSynergy's own model, the only plan. Read this before a plan write.
- Params: `start`, `end` (dates; default today to +13 days).
- Returns: `plan_id`, `source` (always `vibesynergy`), `version` (the plan's current
  version, null without a plan), `sessions` in the window (`session_id`, `date`, `name`,
  `sport`, `status`, `description`: cue and steps as intervals.icu text) and the `weeks`
  the window touches (`week`, `monday`, `phase`, `kind`, `hours`, `note`, and `targets`
  per `sport` with `time_s`, `distance_m`, `load`).
- Write with the five tools below. intervals.icu is no plan source: the old switch
  `vibesynergy plan source` is gone. `vibesynergy plan import` copies a plan that lives in
  intervals.icu into VibeSynergy once (it then leads); it never runs by itself.

The five plan writes share these rules (scope `plan.write`):
- `reason` is required and is stored with the new plan version, together with the author
  (the MCP connection).
- They are refused unless VibeSynergy leads the plan ("intervals.icu still leads the plan",
  with the `vibesynergy_icu_*` tool to use instead).
- Every write makes a new plan version and returns its number as `version`. The change
  reaches intervals.icu (and from there Zwift and Garmin) within minutes through the
  one-way mirror; a change made in intervals.icu itself is put back.
- Keep the week's note, targets and sessions in step: change them together.

#### `vibesynergy_plan_session_set` *(scope `plan.write`)*
Create or change one session.
- Params: `reason`, `date`, `name`, `description` (intervals.icu step text: the cue first,
  then the steps; unknown steps are refused), `sport` (optional), `session_id` (the session
  to change; omit to create one), `slot` (optional, for a new session's id; `ms1-swim`, `ms2-ride`, `ms3-run` link the
  sessions of one day into one multisport in that order, such as a triathlon).
- Or `template_id` and `template_params` (from `vibesynergy_workout_library`) instead of
  `description` (#142): VibeSynergy builds the text from the current workout method and
  records the template, its parameters and the method version in the session
  (`workout.template`); `name` and `sport` default to the template's. A session whose text
  is exactly a template's workout under the template's name is recognised and recorded too.
  Only such sessions are rebuilt by `vibesynergy_workout_rerender`.
- Returns: `session_id`, `version`.

#### `vibesynergy_workout_rerender` *(scope `plan.write`)*
Build planned sessions again with the current workout method (#142), after an approved
change to the knowledge-base article `workout-method`.
- Params: `reason`, `from_date`, `dry_run` (default true), `approval_id`.
- Only sessions still `planned`, dated after today (never today or earlier), that a template
  built (written with `template_id`, or recognised); a session written or changed by hand
  stays as it is, and done sessions never change. A name the template gave follows the
  method; a name the AI coach gave stays.
- `dry_run` true returns `changes` (per session `session_id`, `date`, `name`, `before`
  `{name, text}`, `description`, `template`, `built_with`: the method version it was built
  with), `unchanged`, `not_from_a_template` and the method `version`. `dry_run` false writes
  each change as a `vibesynergy_plan_session_set` with its `session_id`: the athlete's own
  agent writes, the primary AI coach needs `approval_id`, otherwise each change is a plan
  proposal the athlete approves. The mirror takes the changes to intervals.icu, Zwift and
  Garmin. The CLI equivalent is `vibesynergy plan rerender --from <date> [--apply]`.

#### `vibesynergy_plan_session_move` *(scope `plan.write`)*
Move one planned session to another day.
- Params: `reason`, `session_id`, `date`.
- Returns: `session_id`, `date`, `version`.

#### `vibesynergy_plan_session_skip` *(scope `plan.write`)*
Take one planned session out of the plan. Its intervals.icu copy is removed; the session
stays in the plan's history.
- Params: `reason`, `session_id`.
- Returns: `session_id`, `status` (`skipped`), `version`.

#### `vibesynergy_plan_week_set` *(scope `plan.write`)*
Change the note of one week (its intent and structure). A week that is not in the plan yet
is added with its first note.
- Params: `reason`, `week` (the plan's week number), `note`.
- Returns: `week`, `version`.

#### `vibesynergy_plan_target_set` *(scope `plan.write`)*
Set one weekly target for a sport.
- Params: `reason`, `week`, `sport` (`run`, `ride`, `swim`, …), and any of `time_s`
  (seconds), `distance_m` (metres), `load`. With none of the three the target is removed.
- Returns: `week`, `sport`, `version` and the values that were set.

#### `vibesynergy_today`
One call for "how should I train today?".
- Params: `date` (optional, default today).
- Returns: check-in or `"missing"`, open complaints and today's pain, recovery in words and
  numbers, load (form, ACWR, ramp), today's and tomorrow's items (id, owner, editable),
  the last three days done, the A race and any B race with the phase, the dashboard verdict
  and attention list, the safety floor (level, rules, text), this coach's last entries and
  freshness, and stale-data warnings. `planned_today` is the first session today.
  `sleep_check` (#100): the night's automatic sleep check with the user's choice on top,
  or null when there is nothing to say: `state` (`suspect`: ask, still counts;
  `incomplete` or `bad`: the metrics in `not_counted_metrics` are out of the safety floor
  and the baselines), `auto_state`, `override` (`incomplete`, `ok` or null), `headline`,
  `reasons` (sentences), `effect`, `metrics`, `rules`, `checked_at`. Relay it as
  uncertainty about the night, not as a short night.
  `date_label` is `d-m-yyyy`; other dates stay ISO. Items carry `source` and `in_plan`;
  `plan_source` says where the plan comes from (`vibesynergy`, or `garmin` before the plan
  starts or without a plan; never intervals.icu, #111). Garmin workouts that are not
  VibeSynergy's (Garmin Coach, added by hand) are not shown, in `today` nor in `horizon`
  (#153; see `vibesynergy_plan`).
  `advice` is the day's current advice, the same as the dashboard, the Coach page and the
  mail (#89): `source` `ai` (the newest valid AI advice decides `level` (colour),
  `safety_level`, `reasons` and `advice`) or `rules`; `mode` is the athlete's setting (`ai`,
  `rules`, `off`); `fallback` `{reason, text}` says why the rules give it (`no_run`,
  `run_failed`, `contract`, `ai_off`, `unavailable`, `new_signal`, `limit`: new data and the
  daily maximum of updates reached, #90); `updating` says a newer advice is on its way
  (`kind` `waiting` with `changes` for new data, `running`, `new_signal`, or `stale`);
  `rules` and `differences` are the rules' own outcome beside the AI. `entry` says when the
  AI advice was written, its `trigger`, what came in before it (`changes`: `sleep`, `hrv`,
  `checkin`, `note`, `injury`, `activity`, `plan`, `reply`) and what really changed against
  the version before (`differences`: colour, safety level, sessions). `versions` lists every
  version of the day's advice, newest first (#90): one current advice per target day; the
  evening review's advice is for tomorrow. It is the day thread (#115): each version has
  `version`, `of`, `status` (`current`, `replaced`, `invalid`), `replaced_by` and
  `changed_because`, the same list and order as the start page and the Coach page.
  `continuity` lists the continuity check's gaps (#115).
  `verdict` is its short form. `safety` stays the rules' outcome, which a coach's own
  `safety` is checked against in `vibesynergy_coach_write`.

#### `vibesynergy_week`
Monday to Sunday, planned against what was done.
- Params: `week` (default `this_week`, or an ISO week such as `2026-W42`).
- Returns: each session (title, sport, duration, distance, ascent, intensity, status
  done/changed/missed/today/upcoming, matched activity) and totals.
  Saturday and Sunday are included when today is Friday. Also `plan_source`, `note` (the
  plan's week note: `id`, `title`, `text`, or null), `targets` (weekly time targets
  per sport with `done_s`, as in `vibesynergy_plan` `weeks`). Garmin workouts that are not
  VibeSynergy's are not shown (#153).
- `progress` (the start page's week card, `query/week_progress.py`): `season`, `note`
  (`title`, `focus`, `text`), `sports` (per sport `target_s`, `planned_s`, `goal_s` with
  `basis` target/planned, `done_s`, `done_distance_m`, `done_load`, `remaining_s`,
  `remaining` workouts, status `counts`), `sessions` (each planned workout with `planned`
  and `actual` duration/distance/load (the planned load from the session's own steps),
  `match` paired/day_sport, `compliance` (time done against time planned), `rpe`/`feel`; activities outside the plan have status `extra`), `counts`
  and `totals`. A training you give (kind coaching) counts for a plan workout of that sport
  and day only when nothing of your own matches.

#### `vibesynergy_plan_vs_done`
Plan against done per Monday-to-Sunday week and per training phase, the numbers the
Planning & progress page shows (`query/planning.py`, built on `week_progress`).
- Params: `start`, `end` (dates; their Mondays count) or `weeks` (default 12, at most 52,
  ending with this week).
- Returns: `weeks` (per week `goal_s`, `done_s`, `done_pct`, `planned_load` (from the
  planned sessions' own steps) and `done_load` (VibeSynergy's own load of the activities),
  status `counts`, `sessions_done_pct` (done of the planned workouts
  whose day passed), `compliance_avg` (time done against time planned, of the planned
  workouts that were done),
  `number`/`phase`/`kind` from the weekly note, and `sports` per sport with `goal_s` and
  `basis` target/planned), `phases` (the same totals per phase) and `totals`.

#### `vibesynergy_race_predictions`
Predicted race times for 5 km, 10 km, 10 miles, half marathon, marathon and 50 km.
- Returns `predictions`: per distance `key`, `distance_m`, `seconds`, `pace_s_per_km`,
  `certainty` (`high` | `medium` | `low`: how far it extrapolates, how old its evidence is),
  `from` (`kind` `threshold`, one hour at the confirmed run threshold pace, or `effort`, the
  fastest time over a distance in the run streams of the last 12 weeks), `change_s` against
  six weeks ago (only between the same kind of evidence) and `garmin` (Garmin's own
  prediction, last 14 days).
- Method: Riegel, exponent 1.06 up to the half marathon and 1.08 beyond it; the fastest
  prediction from evidence within 4.5x of the distance counts.

#### `vibesynergy_projection`
A projection, not a measurement: fitness, fatigue and form per day from today to the last
upcoming race (or the plan's end; at most 300 days) if the plan is done as written, run
through VibeSynergy's model (`fitness_model.project`) from today's values.
- Returns: `rows` (`date`, `load`, `ctl`, `atl`, `tsb`, `form_pct`, `form_zone`), `races`
  (each upcoming race with its projected values on race day), `basis` (sessions counted
  from their steps and intensity (`planned_load`), sessions without steps counted at the
  athlete's own recent load per hour for that kind of session, weeks with only a weekly
  target spread over their days, the overall rate used),
  `assumptions` and `label: "projection"`. It follows the plan as soon as it changes.

#### `vibesynergy_model_run_write` *(scope `coach.write`)*
Store one personal-model fit.
- Params: `model_id`, `params` (JSON object), `report` (markdown), optional
  `started_at` and `finished_at` (ISO 8601; a time without a zone is read in the
  athlete's timezone). Omitted times are the moment of the call.
- The author is the MCP connection (`created_by`), not an argument.
- Returns the stored run. `diff` is null on the first run of that model. Later runs
  store `{previous_id, added, removed, changed}` against the latest run of the same
  model whose start is at or before this one. A backfilled run does not rewrite
  diffs already stored on later runs.

#### `vibesynergy_model_runs` *(scope `read`)*
- Params: `model_id`, `period` (default `all`), or `start`/`end`.
- Returns runs whose start falls on a local date in the period, newest first, each
  with the diff stored at write time.

#### `vibesynergy_fitness`
Fitness markers over time.
- Params: period (default `last_365d`), `markers` (default all: VO2max run/ride, FTP, LTHR,
  threshold pace, CP, endurance score, hill score, race predictions, fitness age).
- Returns: time series of each marker with the date and source of every change.

#### `vibesynergy_icu_events`
`start`, `end` (default today to +28 days), `category` (WORKOUT, NOTE, TARGET, RACE_A/B/C),
`refresh` (fetch intervals.icu first). intervals.icu's copy of the plan from VibeSynergy's
cache, for checking the copy; it is never the plan (#111) and is not on the AI coach's
insight tools. One row per
event with `event_id`, `external_id` and `version` (pass it as `expected_version` to
`vibesynergy_icu_update_event`, `vibesynergy_icu_move_event` and
`vibesynergy_icu_delete_event`), plus date, category, sport,
name, description, tags, `for_week`, `targets`, `moving_time_s`, `load` and `fetched_at`.
Scope `read`. Read this before every intervals.icu write.

The intervals.icu writes (scope `plan.write`, each with a `reason`) change intervals.icu
only: an event written there is not in VibeSynergy's plan and never becomes a planned
session or a race (plan with the `vibesynergy_plan_*` tools): `vibesynergy_icu_create_event` (`category`, `start_date`, `name`,
optional `sport`, `description`, `external_id`, `end_date`, `for_week`, `tags`, `targets`),
`vibesynergy_icu_update_event` (`expected_version`, `event_id` or `external_id`, and the
fields to change), `vibesynergy_icu_move_event` (`expected_version`, `start_date`),
`vibesynergy_icu_delete_event` (`expected_version`), `vibesynergy_icu_revert` (`audit_id`:
undoes one plan write from its before snapshot). A write is refused when the event has moved on since the
version that was read.

Strength and pilates sessions carry their Garmin steps in a `[garmin]` block in the
description (syntax in `query/garmin_strength.py` and the KB article
`intervals-icu-workout-authoring`). `vibesynergy_icu_create_event` and `vibesynergy_icu_update_event` refuse
a block with an exercise outside Garmin's library or a malformed line, before anything
reaches intervals.icu. VibeSynergy then delivers the session to Garmin itself (below).

#### `vibesynergy_workout_library`
`template_id` (omit for the catalogue), `lang` (`nl`/`en`), `params`. The workout library: run, ride, swim and strength templates (strength as a `[garmin]` block without weights; add `weight=` per exercise), relative targets only. The catalogue gives id,
sport, purpose, phases, load (easy/moderate/hard), params with defaults and the easier
alternative; a template gives `name`, `type`, `text` (intervals.icu syntax), planned
`seconds`, the `params` used and `method` (`slug`, `version`, `source`, `fallback`): the
version of the workout method that built it. Put it in the plan with
`vibesynergy_plan_session_set` and the same `template_id` and `params`. Scope `read`.

**Where the values live** (#142). The code holds only each template's structure (which
steps, repeats, which parameter sets a length, rendering for intervals.icu, Zwift and
Garmin). Every coaching value (cadence bands, cues, % FTP, % LTHR, HR and pace zones,
warm-up and cool-down minutes, distances, rests, exercises, names, purposes, phases, load,
the easier alternative, parameter defaults) is in the knowledge-base article
`workout-method`: one fenced `toml` block, one table per template, read each time a workout
is built. To change a value, an AI coach proposes the whole new article text with
`vibesynergy_kb_propose_change` (`kind` `article`, `target` `workout-method`); it is a
coaching fact, so it waits for the athlete, who sees it as a diff. `vibesynergy_kb_upsert`
refuses this article. Once approved, new workouts use it at once, without a code change or
deploy; planned ones change with `vibesynergy_workout_rerender`. A value the article lacks or
holds in the wrong form comes from the built-in seed (`docs/workout-method.md`, copied into
the knowledge base once by migration 0058); the lint warns `workout_value` and
`vibesynergy_explain` (`workout:<template>`) names it.

#### `vibesynergy_weather`
`days` (1–14, default 9). Daily forecast from Open-Meteo (no key; independent of
intervals.icu) at the start of the last outdoor session, rounded to about 1 km:
`temp_min_c`, `temp_max_c`, `wind_ms`, `gust_ms`, `rain_mm`, `snow_mm`, `summary` and `flags`
(`rain` ≥ 2 mm, `snow`, `gusts` ≥ 12 m/s, `cold` minimum ≤ 2 °C). Cached for an hour, not
stored; empty without an outdoor session. Scope `read`. The coach uses it to suggest outdoor
or Zwift. The weather of a done outdoor session is on the training calendar (Garmin's, else
Open-Meteo at its start and hour).

### 3.6 Wellness and recovery

#### `vibesynergy_sleep`
- Params: period (default `last_7d`), `detail` (`summary` | `stages` | `full`), `nights`
  (specific dates).
- Returns per night: bed/wake times, duration, stages, score and sub-scores, avg HR/HRV/RR,
  SpO2, restless moments, source; period summary (averages, regularity, consistency of bed
  time, sleep debt vs your need); `full` adds hypnogram segments. A night the sleep check
  (#100) has something to say about carries `check`: `state`, `auto_state`, `override`,
  `headline`, `reasons` (sentences), `not_counted` (metric names) and `effect`.

#### `vibesynergy_recovery`
- Params: period (default `last_14d`).
- Returns daily: HRV (with status and baseline band), RHR, readiness with factors, body
  battery charge/drain, stress, respiration, plus the day's load, so recovery can be read
  against training.

#### `vibesynergy_intraday`
- Params: `date` or a range ≤ 3 days, `channels` (hr, stress, body_battery, steps,
  respiration, spo2), `resample` (default 5 min).
- Returns aligned intraday series with activities and sleep marked as intervals.

### 3.7 Body and health

#### `vibesynergy_body`
- Params: period (default `last_90d`), `metrics` (default weight, fat %, muscle mass),
  `smoothing` (`none` | `ewma` default, shows trend weight next to raw).
- Returns: measurements (with time of day and device), smoothed trend, rate of change,
  min/max, measurement frequency.

#### `vibesynergy_blood_pressure`
- Params: period.
- Returns: readings, morning/evening split, averages, classification per reading
  (guideline category, informational only).

### 3.8 Gear, devices, profile, context

#### `vibesynergy_gear`
- Params: none.
- Returns: bikes and shoes (from Strava) with Strava's total distance, how often they were
  used and when last, and a warning for running shoes past 600 km.
  Each item has a source-namespaced `ref` (e.g. `strava:g1`); `other_sources` reports Garmin's
  own gear collection freshness only; it is not merged in yet. `references` cites the raw snapshots
  (`raw_item_id`, `sha256`). `activity_gear` lists Garmin gear ids used on activities, with
  no distance or model. Gear the athlete added in VibeSynergy (#114; swim gear such as a pull
  buoy, a wetsuit) has `source: "vs"`, a `vs:` ref, a `kind` (`swim`, `bike`, `shoes`,
  `other`) and no km total; `activities_seen` counts the workouts it was linked to.

#### `vibesynergy_exercises`
- Returns: strength exercises (`category` + specific `name`) your synced Garmin sessions
  used, each matched against Garmin's own workout-editor catalog (`known: true/false`) with
  a readable `label`, stable `ref`, session count and first/last date seen. `freshness` is
  the Garmin activities collection. `catalog.fingerprint` identifies the unique catalog rows.
  Workout validation looks up the same catalog with `query/workouts.exercise_references()`;
  no second catalog.

#### `vibesynergy_profile`
- Returns: current heart-rate and power zones and thresholds per sport, with their history
  (`valid_from` dates), so old activities can be judged against the zones of the time. The
  athlete's own details (time zone, units, sex, age) are in `vibesynergy_athlete`.

#### `vibesynergy_athlete` *(scope `read`)*
- Returns: profile (time zone, units, sex, max and resting heart rate, and `age` in whole
  years from the date of birth set under Zones & thresholds; never the date itself),
  threshold heart rate and pace, VibeSynergy's races (as in `vibesynergy_coach_state`), and the
  athlete's notes for a coach. The notes are read-only.
  The same text is `vibesynergy://athlete/notes`.

#### `vibesynergy_notes`
- Params: period, `tag`.
- Returns: your notes/annotations (illness, travel, injury, alcohol, stress, …).

#### `vibesynergy_sleep_check_set` *(scope `diary.write`)*
The user's own verdict on a night's sleep measurement (#100); it beats the automatic sleep
check.
- Params: `choice` (`incomplete`: yes, the measurement is incomplete; `ok`: no, it's right;
  `clear`: undo), `date` (the morning the night ends, default today).
- Returns: `date` and the night's `sleep_check` after the choice. Only on the user's own
  word. Garmin's record is never changed. Role policy: open (the user's diary).

#### `vibesynergy_add_note` *(scope `diary.write`)*
- Params: `text`, `date` (default today), `end` (for a span), `tags[]`, `activity_id` (a
  note on one activity).
- Notes feed back into analysis (`group_by: tag:sick`, anomaly context).

### 3.9 Power tools

#### `vibesynergy_sql` *(scope `sql`)*
Read-only SQL (SQLite dialect) over the tables that `vibesynergy_schema` documents, for
questions the other tools do not cover.
- Params: `query` (one `SELECT` statement), `limit` (default 200, at most 5 000 rows).
- Guardrails: a separate read-only connection; only `SELECT` on the documented tables (no
  writes, no `ATTACH`, no `PRAGMA`); one statement; 10 s time limit. Per-second streams are
  not reachable from here (use `vibesynergy_activity_streams`).
- Returns: `columns`, `rows`, `truncated`; when truncated, `meta.warnings` says to narrow
  the query or raise `limit`.

### 3.10 Data management

#### `vibesynergy_sync_status`
- Params: `source` (optional).
- Returns per dataset: last success, last attempt, error with reason, coverage, gaps, next
  scheduled run; account/auth state.

#### `vibesynergy_refresh` *(scope `sync`)*
- Params: `source` (optional), `dataset` (optional), `period` (default `today`),
  `wait` (bool, default true, up to 60 s).
- Returns: job IDs and, when waited, what changed.

#### `vibesynergy_coverage`
- Params: `domain` or `metrics[]`, period (default `all`).
- Returns: month-by-month coverage matrix, so an analysis can avoid gaps.

#### `vibesynergy_data_quality`
- Params: `limit` (default 200).
- Returns: a read-only audit of how Strava and Garmin activities are linked: near-duplicates
  that are not linked, and groups with two activities from the same source.

The registry has 101 tools (counted in `tests/test_mcp.py`). `tests/test_mcp_docs.py` checks
that each of them is named in this document and that this document names no
`vibesynergy_*` tool that does not exist, apart from the removed and not-built ones listed
under "Implementation status". Each description is written for an LLM: one line of purpose, when to use it,
when to use a different tool instead.

---

## 4. Resources

| URI | Content |
|---|---|
| `vibesynergy://metrics` | full metric catalogue |
| `vibesynergy://schema` | SQL views documentation |
| `vibesynergy://coach/handbook` | the coach protocol ([COACH_PROTOCOL.md](COACH_PROTOCOL.md)); `vibesynergy_coach_handbook` adds the live KB index |
| `vibesynergy://guide` | "how to use VibeSynergy well" for AI clients: tool choice, pitfalls, units |
| `vibesynergy://athlete/notes` | The athlete's notes for a coach (read-only; `vibesynergy_athlete` returns the same text) |

These five are the resources the server registers. Not built: the design's
`vibesynergy://overview`, `vibesynergy://day/{date}`, `vibesynergy://activity/{id}`,
`vibesynergy://week/{iso_week}` and `vibesynergy://profile` (the tools of the same name give
that data), resource templates, and argument completion (`completion/complete`) for metric
IDs, sports, gear, tags, dates and activity IDs.

---

## 5. Prompts

| Prompt | Args | What it does |
|---|---|---|
| `daily_check_in` | date | today's readiness, sleep, recovery vs baseline, planned session, advice to adjust |
| `weekly_review` | week | volume, intensity distribution, load and form, sleep/recovery, highlights, next week focus |
| `activity_review` | activity id | execution vs plan, intervals, pacing, HR drift, comparison with similar sessions |
| `trend_investigation` | metric, period | trend, change points, drivers (correlates), caveats |
| `race_readiness` | race date, sport, distance | form trajectory, taper check, predictions, bests |
| `body_composition_review` | period | trend weight, composition, energy balance |
| `sleep_review` | period | quality, regularity, relation with training and stress |
| `data_health` | none | sync state, gaps, what to fix |

Prompts only orchestrate the tools; they add no hidden logic.

---

## 6. Profiles, scopes and safety

- **Profiles** (not built; every client is offered every tool and the scopes decide what
  it may call). The design, config per client or via a URL parameter: `core` (viewing: overview, day,
  period, calendar, activities, activity, sleep, recovery, body, training_load, bests,
  sync_status; ~14 tools), `analysis` (core + series, stats, trend, compare, correlate,
  distribution, baselines, anomalies, streams, activity_analyze, intervals, sql, result),
  `full` (everything). Small or local models get `core`; frontier models get `full`.
- **Scopes** (OAuth, implemented): `read`, `read.expert`, `diary.write`, `coach.write`,
  `plan.propose`, `plan.write`, `sync`, `publish`, `sql`. See the implementation status above. The
  per-domain exclusions in this design (`-location`, `-ecg`, `-menstrual`) are not built.
- **Annotations**: `readOnlyHint` on read tools; `openWorldHint` on `vibesynergy_refresh`,
  `vibesynergy_activity_publish` and `vibesynergy_activity_unpublish`; `destructiveHint` on
  `vibesynergy_edit_note`.
- **Audit**: every call logged with client id, author, tool, scope, target, outcome and
  duration. Coach apps → Activity shows that log and can revoke the app.
- **In-process caller** (1 Nov 2026): the API coach runner ([COACH_RUNNER.md](COACH_RUNNER.md))
  builds this same server and calls its tools directly (`MCPServer.call_tool`), not over
  HTTP. It runs as client `coach-runner` (author `client:coach-runner`, label "AI coach
  (API)") with the scopes `read`, `coach.write` and `plan.write`, and offers the model only
  the tools those scopes allow (`build_server` records each tool's scope in
  `server.vibesynergy_tool_scopes`); `vibesynergy_refresh` and `vibesynergy_sql` are never offered.
  Scope checks and the audit log apply as for any app.
- **Medical framing**: the body and blood-pressure tools include a short "informational, not a diagnosis" note
  in `meta.warnings` when classification is returned.

---

## 7. Behaviour details that make it pleasant to use

- **Defaults answer the common question.** `vibesynergy_day()` with no args = today;
  `vibesynergy_sleep()` = last 7 nights; `vibesynergy_activities()` = last 20.
- **Helpful errors**: unknown metric → closest matches; empty result → why (no data, filter
  too narrow, sync failing) and what to try.
- **Consistency**: the same number is identical across tools (single query layer, the same
  best-source rules), so an AI never sees conflicting values between `day` and `series`.
- **Freshness awareness**: if the requested period touches data older than the source's
  expected sync interval, `meta.warnings` says so and suggests `vibesynergy_refresh`.
- **Portal deep links** in every response, so a human can switch to the visual view.
- **Deterministic** output ordering and rounding (per-unit precision table), so repeated
  analyses are comparable.

---

## 8. Implementation notes

- Built with the official MCP Python SDK (FastMCP), protocol ≥ 2025-06-18: structured
  output and tool annotations. Resource templates, completion and cursor pagination are not
  built.
- Transports: stdio (`vibesynergy mcp`) and streamable HTTP (`/vibesynergy/mcp`) with OAuth 2.1
  (authorization server metadata, dynamic client registration, PKCE).
- Tool handlers are thin: parse params → call `vibesynergy.query` → shape the envelope. All
  logic lives in the query layer and is unit-tested there; the portal's REST API calls the
  same functions.
- Contract tests: golden JSON snapshots per tool on a fixture dataset; an "LLM usability"
  eval set (natural-language questions → expected tool choices and answers) run against
  at least two models of different providers when tools or descriptions change.

`vibesynergy_activity_assessment(id)` reads the coach feed: `assessment` is the newest
`activity_assessment` entry written with `vibesynergy_coach_write` about this activity (its
`activity_id`, or an activity in `evidence_refs`, resolves to the same merged activity),
`decision_log` lists all of them newest first, and `state` is `ready` or `missing`
(`source: coach_feed`). Internal (model-run) assessments are retired, so
`coaching_state.reason` stays `retired`. The read does not call a model, queue work, or
store anything.

`vibesynergy_daily_coaching(date="today")` reads coach entries (the coach feed) and the fixed
safety rules for that local day. Morning, evening, weekly and block views come from those
entries. `weekly_previous` is last week's coach note when this week has none yet. Reading
never runs a model or queues work. The portal does not request an internal review.

Each kind includes `display` (`mode`, `target_date`, `archive`, `safety_text`) for how to
render the review (current vs archive, advice vs safety-only fixed copy). `advice.current`
only means the saved row matches today's input snapshot on the **review day**; it does not
replace `display.archive` (evening advice targets the next calendar day) or `display.mode`
(flag/refuse must show `safety_text`, not model fields).

`vibesynergy_activity_annotation_preview(id, language="en")` returns a read-only statistics
block (English or Dutch) from the activity's Garmin numbers. It does not need a saved
assessment, publish, modify descriptions, or call a provider. Without those numbers the
preview is blocked. Private coach, diary, recovery, calendar and goal text is excluded.
Quote and weather are off in this preview.

`vibesynergy_activity_publication_preview(id, destination, language="en")` reads the selected
provider description and returns the exact composed text with `preview_version`.
`vibesynergy_activity_publish(id, destination, preview_version, language="en")` writes once on
explicit request and returns `verified` only after provider read-back. Destinations are
`garmin` and `strava`. A changed preview, unknown marker or uncertain previous write blocks
publication. When the destination is one of the places under Settings → Publishing and sync
→ *Publish to* (`[publication] targets`, both by default), the same section then goes to the
other one too, through the same guarded path; the result's `others` says how that went
(`published`, `unchanged`, `waiting` or `failed`; a failure never undoes the first write).
Only VibeSynergy's section is written there: the name and the public text are the text
sync's (#175; the earlier Strava → Garmin copy with its 7-day watch is gone).
`vibesynergy_activity_unpublish(id, destination, preview_version, language="en")` removes
only the checked VibeSynergy section after an explicit call and verifies provider read-back,
and removes it from the other platform too where VibeSynergy put it (`others`).

VibeSynergy does not write activity descriptions to intervals.icu (#114, since the 22 Nov
2026 incident #160: intervals.icu passed the block on to the linked Strava activity and the
athlete's own Strava text was lost). intervals.icu reads activities from its own import; the
former "Also in intervals.icu" switch (`[publication] icu_description_enabled`) is ignored.
Blocks written there before stay as they are. An activity with only a Strava copy is
published automatically 30 minutes after it ended when no Garmin copy came; the same block
(same label) then carries what Strava has (Relative Effort, work in kJ, temperature, and
RPE/feel from VibeSynergy or Garmin when there are any).
