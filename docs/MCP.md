# VibeSynergy MCP: interface specification

> Issue and PR numbers in text written before 30-10-2026 refer to the old tracker (they are marked "old #N" where they were links); the GitHub tracker started fresh then.

Status: design, 2026-09-25. This is the contract between VibeSynergy and any AI client (Claude,
ChatGPT, OpenClaw, Hermes, local LLM clients). The portal uses the same query layer, so
everything the MCP can answer, the portal can show and vice versa.

---

## Implementation status

**Done (phases 3a to 3c, and the diary of phase 6):** read and action tools, 8 prompts and 3 resources, in
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
  activity's VibeSynergy block on Strava, the Garmin mirror and intervals.icu. The coach
  decides what may go public: no health, diary, injury, sleep or other private details. At
  most 300 characters, one paragraph, no `[`, `]`, `/*` or `*/`; anything else is refused. Per activity the newest
  entry (by `at`) that sets `public_insight` wins; `""` withdraws the line. Entries without
  `public_insight` never reach a public text. Accepted earlier convention: `details` starting
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
  insight. A polling coach knows what to fill without a webhook. The
  same events go to webhook links set up in Settings → Sync between sources. Schema and
  delivery rules: [EVENTS.md](EVENTS.md).
- Workout proposals removed (1-11-2026): `vibesynergy_coach_propose`,
  `vibesynergy_plan_status` and `vibesynergy_plan_withdraw` are gone, with VibeSynergy's own
  propose → approve → Garmin push route. They were never called. The plan is written to
  intervals.icu (`vibesynergy_icu_*`), which brings it to Garmin. Coach entries
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
- API coach runner (1 Nov 2026, off by default): VibeSynergy can run the coach itself on the
  Claude API with these tools in-process; see [COACH_RUNNER.md](COACH_RUNNER.md) and §6.
  Its entries are ordinary `vibesynergy_coach_write` rows (author `client:coach-runner`).
- Knowledge base: `vibesynergy_kb_search` and `vibesynergy_kb_get` (`read`) search and read
  articles, rules and sources. `vibesynergy_kb_upsert` (`coach.write`) writes a new version
  and keeps every older one; `revert_to` copies an older article version forward.
  `vibesynergy_kb_propose_change` (`plan.propose`) stores a suggestion. The athlete approves
  or rejects it on the portal (Coach → Knowledge). Nothing in the current text changes
  until then. The athlete can also edit articles and rules directly (Coach → Methods &
  rules); those versions have author `user` and show in Coach → Change log with a diff and
  a revert. Articles with status `retired` are hidden from the portal views but stay
  readable and searchable here.
- AI harness ([AI_HARNESS.md](AI_HARNESS.md)): `vibesynergy_harness_tasks` (`read`) lists
  every coach task with its trigger, context chain, approval, switch, schedule, provider,
  model and last run; `vibesynergy_harness_runs(task?, limit?)` (`read`) is the run log, with
  what was preloaded and the output-contract result; `vibesynergy_harness_run(task)`
  (`coach.write`) starts one run of a report task now, like Test now (it costs like any run).
- Routine prompts: `vibesynergy_coach_prompt(job)` (`read`; `job` is one of the
  harness's model tasks, e.g. `morning`, `evening`, `weekly` or `insight`) returns the current instructions for that routine:
  the knowledge-base article `coach-prompt-<job>` (topic `coach-prompts`), or the seed file
  in `docs/coach-prompts/` while the article does not exist (`source` says which). Read it
  at the start of every run; `version` changes with each edit.
- Race priority: `vibesynergy_race_set_priority` (`plan.propose`) stores A, B or C for one
  plan-calendar race. Null clears it. `vibesynergy_coach_state` races include `source`,
  `source_id` and `priority` (A, B, C, or null). A Garmin primary race is A until a choice
  is stored. Calendar sync does not overwrite the choice. The portal calendar page edits it.
  `vibesynergy_plan` `races`, `next_event`, `supporting` and `next_race` include `source`,
  `source_id`, that same effective priority, and `priority_origin` (`saved`, `derived`, or
  `intervals` when the letter comes from the intervals.icu RACE_A/B/C category).
  `next_race.is_primary` is Garmin's primary-event flag.
- Coach preferences: `vibesynergy_coach_state` is read-only. Since (31-10-2026) it returns
  only what the athlete sets on Coach → Settings: `documents.policy.effective` has
  `language` (`auto`/`en`/`nl`), `detail` (`concise`/`standard`/`detailed`), `tone`
  (`direct`/`supportive`), `instructions` (the athlete's notes for the coach),
  `policy_version` (`"2"`) and `boundaries`; `documents.context.effective.constraints` are
  the things to remember (availability, fixed moments, preferences, commitments, equipment,
  with optional weekdays and origin; retired notes are omitted). Plus `version` (combined
  fingerprint), `active_profile`, plan-calendar `races` and a `note` saying where the rest
  lives (the workout planner status went with the proposal pipeline). The method, goal priority, athlete profile, phase,
  sport order and per-sport methods, weekly time, usage budget, Garmin goal/event/Garmin
  Coach sources, manual races, source priorities and the internal coach's assessment
  schedule are gone, and so are `garmin_context` and `coaching_events`: the plan and its
  rules are in the knowledge base and intervals.icu, races and their A/B/C priority in the
  plan calendar and the intervals.icu RACE_A/B/C category. Older stored documents still
  load; the removed keys are ignored. Drafts are private settings; no planning or
  external-write capability is granted. See [COACH.md §6.4](COACH.md#64-implemented-foundation-and-remaining-work).
- Orientation: `vibesynergy_overview`, `vibesynergy_catalog`, `vibesynergy_schema`,
  `vibesynergy_explain`
- Days and periods: `vibesynergy_day`, `vibesynergy_period`, `vibesynergy_calendar`
- Series and statistics: `vibesynergy_series`, `vibesynergy_stats`, `vibesynergy_trend`,
  `vibesynergy_compare_periods`, `vibesynergy_correlate`, `vibesynergy_distribution`,
  `vibesynergy_baselines`, `vibesynergy_anomalies`
- Activity detail includes recorded-input versions and source aliases; see
  [INTEGRATION_CONTRACTS.md](INTEGRATION_CONTRACTS.md). A version is not an assessment.
- Activities: `vibesynergy_activities`, `vibesynergy_activity`, `vibesynergy_activity_streams`,
  `vibesynergy_bests` (power curve)
- RPE and Feel: `vibesynergy_activity` has `effort` with `rpe` (1-10) and `feel` (1-5,
  1 = Strong … 5 = Weak), each with `value`, `label`, `source` (`garmin`, `intervals_icu`
  or `vibesynergy`), `editable` and `others` (a different value elsewhere), plus
  `intervals_icu.state` for a VibeSynergy entry (`pending`, `sent`, `waiting`, `conflict`,
  `failed`). `vibesynergy_activities` and `vibesynergy_day` rows carry the short form
  (`rpe`, `rpe_source`, `feel`, `feel_source`). `vibesynergy_activity_set_effort`
  (`diary.write`) saves what the athlete said; it is refused for a field a platform already
  has. Garmin's 10-100 effort and 0-100 feel are converted to these scales. A VibeSynergy
  entry is copied to intervals.icu (`PUT /activity/{id}`, `icu_rpe` and `feel` only) after
  reading the activity; a different value there is kept. Garmin is not written.
- Training: `vibesynergy_training_load`, `vibesynergy_volume`, `vibesynergy_fitness`,
  `vibesynergy_intensity_distribution`
- Personal model runs: `vibesynergy_model_run_write` (`coach.write`) stores parameters
  and a markdown report for one `model_id`. The parameter diff against the previous
  run of that model is computed and stored. `vibesynergy_model_runs` (`read`) lists
  runs for a model and period, newest first.
- Wellness and body: `vibesynergy_sleep`, `vibesynergy_recovery`, `vibesynergy_intraday`,
  `vibesynergy_body`, `vibesynergy_blood_pressure`, `vibesynergy_profile`
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
- `vibesynergy_sql` uses SQLite, not DuckDB, over the documented tables. Streams are available
  through `vibesynergy_activity_streams`.
- Lists use `offset` instead of opaque cursors; there are no result sets or charts yet.
- `vibesynergy_training_load` uses intervals.icu's own numbers by default (`load="intervals"`): its wellness fitness/fatigue/ramp rate per day and each activity's
  `icu_training_load`, so they match its app. Days it has no figures for are computed with
  its algorithm (42/7-day EWMA with weights `1 - e^(-1/n)`, form = same-day CTL - ATL),
  from its activity load when it has the day's activities, else TRIMP; each row carries
  `source` (`intervals` | `computed`) and `load_source` (`intervals` | `trimp`), and
  `current.form_zone` is its zone on form % of fitness (`transition` > +20, `fresh` +5..+20,
  `grey` -10..+5, `optimal` -30..-10, `high_risk` < -30). `current.form` keeps the older
  absolute band. `load="trimp"` uses TRIMP (Banister, from each activity's duration and
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
`sql`), expires in 30, 90 or 365 days, and is created in Settings → Coach apps with the
coach passphrase: the tailnet alone cannot mint one. Wrong passphrases there and on
the approve page are limited to 5 per 15 minutes, counted per app (and once for the
portal), with a ceiling of 50 across all apps, so a stranger's registration cannot
lock the athlete out. The passphrase is set on that page and is never sent back to the
browser; changing it needs the current one, under the same limit. `publish` and
`unpublish` and `refresh` are annotated `openWorldHint`; `edit_note` is `destructiveHint`.

**Not yet:** profiles; `vibesynergy_ecg`, `vibesynergy_nutrition`, result sets, charts; argument
completion. A primary-coach picker and undoing a note or check-in from the activity list
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
3. **Big results become handles.** A tool can return a *result set* (`rs_…`) with a preview;
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
recovery.*   readiness, body_battery.max|min|charged|drained, stress.avg|rest|high_minutes
resp.*       waking_avg, sleep_avg          spo2.* avg, min
body.*       weight, fat_pct, fat_mass, muscle_mass, lean_mass, bone_mass, water_pct,
             visceral_fat, bmi, pwv, vascular_age, nerve_health
bp.*         systolic, diastolic, pulse
fitness.*    vo2max.run|ride, fitness_age, endurance_score, hill_score, lthr, lt_pace, ftp,
             race_pred.5k|10k|half|marathon
load.*       ctl, atl, tsb, acwr, weekly_tss, training_status, load_balance.*
nutrition.*  calories_in, protein, carbs, fat, hydration
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
| Result set | `rs_<id>` (expires after 24 h, configurable) | `rs_7f3a…` |
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
    "portal_url": "https://server.your-tailnet.ts.net:8447/metrics?m=body.weight&p=…",
    "warnings": ["Withings has no data 2026-09-10..2026-09-12"]
  }
}
```

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
marked with a scope. Server profiles (§6) control which groups a client sees.

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
- Params: `metric` + `date`, or `activity_id` + `field`.
- Returns: provenance (§2.8) and alternative values from other sources.

### 3.2 Viewing: days and periods

#### `vibesynergy_day`
Everything about one day, unified.
- As built: `next_race` (`name`, `date`, `days_to_go`, `distance_m`, `priority`) and
  `plan_phase` (`phase`, `days_left`) are projected as of the requested local date.
  `distance_m` is null when the calendar lacks a usable distance. `priority` is A, B, C,
  or null: a stored race priority, or A when the race is Garmin's primary event and nothing
  is stored. `priority_origin` is `saved` or `derived`. `is_primary` is Garmin's primary-event
  flag. `source_id` is the Garmin event id or the calendar source id. A recorded race is in `taper`
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
  7-day trend direction.

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
  (simplified) plus start/end place names, not raw coordinates.

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
- Returns: scheduled workouts (Garmin calendar, training plan, Garmin Coach) with their
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
  `next_event`, `supporting` and `next_race` each include `source`, `source_id`, `priority`
  (A, B, C, or null) and `priority_origin` (`saved` when the letter is in `race_priority`,
  `derived` when it follows Garmin's primary flag). A stored choice wins; otherwise a Garmin
  primary race is A and every other race is unset. `next_race.is_primary` keeps that Garmin
  flag. The soonest A race sets the phase; with no A race, Garmin's primary event still
  leads. The calendar is
  fetched `[calendar] lookahead_months` ahead (12 months when unset); Garmin drops past plan
  days from its replies, so every version is kept.
- intervals.icu is the plan. `plan_start` is the first day of an intervals.icu
  plan workout (an event with an `external_id`); `plan_source` is `intervals_icu` when the
  window reaches it, else `garmin`. From `plan_start` on:
  - each intervals.icu WORKOUT is an item with `source` `intervals_icu`, `source_id` the
    event id, `kind` `workout`, `ownership` `intervals_plan`, `in_plan` true, a Garmin-style
    `sport` (`running`, `cycling`, `swimming`, `strength_training`, `walking`; the raw type
    is `extra.icu_type`) and `extra` with `icu_id`, `external_id`, `description`,
    `duration_s`, `distance_m`, `load` and `version` (for the `vibesynergy_icu_*` writes);
  - a Garmin scheduled workout that intervals.icu pushed is not a second item. It is listed
    on the plan item as `garmin_copies` (`source_id`, `title`, `sport`, `match`: `name` for
    the same day, sport family and name, `plan_tag` for a title carrying the plan's
    external-id prefix such as `(lake27)` on a day with plan workouts, and its calendar
    `mirrors`). Garmin has no id that links the two;
  - any other Garmin workout (including Garmin Coach) is in `not_in_plan` with
    `in_plan` false and `status` `not_in_plan`. It is not in `items`, `today`, `adherence`
    or the week. A Garmin Coach session on the same day and sport as exactly one plan
    workout keeps `flag` `dubbel` there for `vibesynergy_icu_resolve_duplicate`; its
    `duplicate` object has the `garmin` session and the `plan` workout.
    `vibesynergy_icu_resolve_duplicate(keep="garmin")` deletes the intervals.icu event,
    `keep="plan"` keeps it and marks the Garmin Coach session for the athlete. The old
    values `coach` (= `garmin`) and the former coach's name (= `plan`) are still
    accepted for one release.
  Days before `plan_start` keep the Garmin calendar as the plan, with `plan_source`
  `garmin` on each training item.
- `adherence` also has `changed`, `missed` and `not_in_plan` (Garmin workouts outside the
  plan up to today). `planned` and `done` are unchanged in meaning.
- `weeks`: one row per Monday in the window that has an intervals.icu weekly NOTE or
  TARGET: `start`, `end`, `note` (`id`, `title`, `text`) and `targets` (`sport`, `family`,
  `time_s`, `distance_m`, `load`, and `done_s` / `done_distance_m` from the activities that
  week), plus `time_s`, the sum of the time targets.
- Races: an intervals.icu RACE_A/B/C on the day of a Garmin race is that race; the row
  keeps the Garmin `source_id` and adds `intervals_id`. A race only in intervals.icu is
  listed with `source` `intervals_icu`. Its priority cannot be stored and follows the
  category; `vibesynergy_coach_state` races mark it `editable` false.

#### `vibesynergy_today`
One call for "how should I train today?".
- Params: `date` (optional, default today).
- Returns: check-in or `"missing"`, open complaints and today's pain, recovery in words and
  numbers, load (form, ACWR, ramp), today's and tomorrow's items (id, owner, editable),
  the last three days done, the A race and any B race with the phase, the dashboard verdict
  and attention list, the safety floor (level, rules, text), this coach's last entries and
  freshness, and stale-data warnings. `planned_today` is the first session today.
  `date_label` is `d-m-yyyy`; other dates stay ISO. Items carry `source` and `in_plan`;
  `plan_source` says where the plan comes from, and `not_in_plan` lists today's Garmin
  workouts that are not in the intervals.icu plan (see `vibesynergy_plan`).

#### `vibesynergy_week`
Monday to Sunday, planned against what was done.
- Params: `week` (default `this_week`, or an ISO week such as `2026-W42`).
- Returns: each session (title, sport, duration, distance, ascent, intensity, status
  done/changed/missed/today/upcoming, matched activity) and totals.
  Saturday and Sunday are included when today is Friday. Also `plan_source`, `note` (the
  intervals.icu weekly note: `id`, `title`, `text`, or null), `targets` (weekly time targets
  per sport with `done_s`, as in `vibesynergy_plan` `weeks`) and `not_in_plan` (Garmin workouts
  that week outside the plan, same shape as `items`).
- `progress` (the start page's week card, `query/week_progress.py`): `season`, `note`
  (`title`, `focus`, `text`), `sports` (per sport `target_s`, `planned_s`, `goal_s` with
  `basis` target/planned, `done_s`, `done_distance_m`, `done_load`, `remaining_s`,
  `remaining` workouts, status `counts`), `sessions` (each planned workout with `planned`
  and `actual` duration/distance/load, `match` paired/day_sport, intervals.icu `compliance`
  when paired, `rpe`/`feel`; activities outside the plan have status `extra`), `counts`
  and `totals`. A training you give (kind coaching) counts for a plan workout of that sport
  and day only when nothing of your own matches.

#### `vibesynergy_plan_vs_done`
Plan against done per Monday-to-Sunday week and per training phase, the numbers the
Planning & progress page shows (`query/planning.py`, built on `week_progress`).
- Params: `start`, `end` (dates; their Mondays count) or `weeks` (default 12, at most 52,
  ending with this week).
- Returns: `weeks` (per week `goal_s`, `done_s`, `done_pct`, `planned_load`/`done_load`
  from intervals.icu, status `counts`, `sessions_done_pct` (done of the planned workouts
  whose day passed), `compliance_avg` (intervals.icu's compliance of paired workouts),
  `number`/`phase`/`kind` from the weekly note, and `sports` per sport with `goal_s` and
  `basis` target/planned), `phases` (the same totals per phase) and `totals`.

#### `vibesynergy_projection`
A projection, not a measurement: fitness, fatigue and form per day from today to the last
upcoming race (or the plan's end; at most 300 days) if the plan is done as written, run
through intervals.icu's model (`icu_fitness.project`) from today's values.
- Returns: `rows` (`date`, `load`, `ctl`, `atl`, `tsb`, `form_pct`, `form_zone`), `races`
  (each upcoming race with its projected values on race day), `basis` (workouts with
  intervals.icu's own load, workouts estimated from planned time at the recent load per
  hour, weeks with only a weekly target spread over their days, the rate used),
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
`refresh` (fetch intervals.icu first). The intervals.icu plan from VibeSynergy's cache, one row per
event with `event_id`, `external_id` and `version` (pass it as `expected_version` to
`vibesynergy_icu_update_event`, `_move_event`, `_delete_event`), plus date, category, sport,
name, description, tags, `for_week`, `targets`, `moving_time_s`, `load` and `fetched_at`.
Scope `read`. Read this before every plan write.

Strength and pilates sessions carry their Garmin steps in a `[garmin]` block in the
description (syntax in `query/garmin_strength.py` and the KB article
`intervals-icu-workout-authoring`). `vibesynergy_icu_create_event` and `_update_event` refuse
a block with an exercise outside Garmin's library or a malformed line, before anything
reaches intervals.icu. VibeSynergy then delivers the session to Garmin itself (below).

#### `vibesynergy_workout_library`
`template_id` (omit for the catalogue), `lang` (`nl`/`en`), `params`. The workout library: run, ride, swim and strength templates (strength as a `[garmin]` block without weights; add `weight=` per exercise), relative targets only. The catalogue gives id,
sport, purpose, phases, load (easy/moderate/hard), params with defaults and the easier
alternative; a template gives `name`, `type`, `text` (intervals.icu syntax) and planned
`seconds`, ready for `vibesynergy_icu_create_event`. Scope `read`.

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
  time, sleep debt vs your need); `full` adds hypnogram segments.

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

#### `vibesynergy_ecg`
- Params: period for the list, or `id` for one recording, `max_points`.
- Returns: recordings with classification (e.g. Withings AFib result) and HR; for one
  recording a downsampled signal and `portal_url` for the full-resolution viewer.

### 3.8 Nutrition (if logged)

#### `vibesynergy_nutrition`
- Params: period (default `last_7d`), `detail`.
- Returns: daily intake (calories, macros, hydration) vs goals and vs expenditure (energy
  balance), top foods.

### 3.9 Gear, devices, profile, context

#### `vibesynergy_gear`
- Params: `id` (optional), `include_retired` (bool).
- Returns: gear with total distance/time, usage per period, last used, service reminders.
  Each item has a source-namespaced `ref` (e.g. `strava:g1`); `other_sources` reports Garmin's
  own gear collection freshness only — not yet merged in (V-C6; see
  [INTEGRATION_CONTRACTS.md](INTEGRATION_CONTRACTS.md)). `references` cites the raw snapshots
  (`raw_item_id`, `sha256`). `activity_gear` lists Garmin gear ids used on activities, with
  no distance or model.

#### `vibesynergy_exercises`
- Returns: strength exercises (`category` + specific `name`) your synced Garmin sessions
  used, each matched against Garmin's own workout-editor catalog (`known: true/false`) with
  a readable `label`, stable `ref`, session count and first/last date seen. `freshness` is
  the Garmin activities collection. `catalog.fingerprint` identifies the unique catalog rows.
  Workout validation looks up the same catalog with `query/workouts.exercise_references()`;
  no second catalog.

#### `vibesynergy_profile`
- Returns: profile (age, sex, height), current and historic zones (HR, power, pace) with
  validity dates, thresholds, devices with battery/firmware, unit preferences.

#### `vibesynergy_athlete` *(scope `read`)*
- Returns: profile (time zone, units, sex, max and resting heart rate, and `age` in whole
  years from the date of birth set under Zones & thresholds; never the date itself),
  threshold heart rate and pace, plan-calendar races (as in `vibesynergy_coach_state`), and the
  athlete's notes for a coach. The notes are read-only.
  The same text is `vibesynergy://athlete/notes`.

#### `vibesynergy_notes`
- Params: period, `tag`.
- Returns: your notes/annotations (illness, travel, injury, alcohol, stress, …).

#### `vibesynergy_add_note` *(scope `annotate`)*
- Params: `date` or `start`/`end`, `text`, `tags[]`.
- Notes feed back into analysis (`group_by: tag:sick`, anomaly context).

### 3.10 Power tools

#### `vibesynergy_sql`
Read-only SQL (DuckDB dialect) over the documented views.
- Params: `query`, `limit` (default 200, max 5 000), `format`, `save_as_result_set` (bool).
- Guardrails: views only, single statement, no file/network functions, 10 s timeout,
  memory cap. Errors echo the schema hint for the table involved.
- Returns: columns, rows, row count, `result_set`.

#### `vibesynergy_result`
Work with result sets.
- Params: `id`, `action` (`preview` | `page` | `describe` | `export`), `cursor`,
  `format` (`csv` | `parquet` | `json` for export).
- Returns: rows, stats, or a download link (tailnet portal URL, expiring).

#### `vibesynergy_chart` *(optional, profile `full`)*
- Params: `input` (`rs_…` or a `vibesynergy_series` spec), `type` (`line` | `bar` | `scatter` |
  `heatmap` | `pmc` | `curve` | `map`), `title`.
- Returns: a PNG (MCP image content) for clients that display images, plus the
  `portal_url` of the interactive version.

### 3.11 Data management

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

Total: 57 tools (this count drifts from the actual registry; the registry has 90 tools
since checked in `tests/test_mcp.py`). Each description is written for an LLM: one line of purpose, when to use it,
when to use a different tool instead.

---

## 4. Resources

| URI | Content |
|---|---|
| `vibesynergy://overview` | same as `vibesynergy_overview` |
| `vibesynergy://metrics` | full metric catalogue |
| `vibesynergy://schema` | SQL views documentation |
| `vibesynergy://day/{date}` | day summary (resource template, with completion on dates) |
| `vibesynergy://activity/{id}` | activity summary |
| `vibesynergy://week/{iso_week}` | week digest |
| `vibesynergy://profile` | profile, zones, thresholds |
| `vibesynergy://coach/handbook` | the coach protocol ([COACH_PROTOCOL.md](COACH_PROTOCOL.md)); `vibesynergy_coach_handbook` adds the live KB index |
| `vibesynergy://guide` | "how to use VibeSynergy well" for AI clients: tool choice, pitfalls, units |
| `vibesynergy://athlete/notes` | The athlete's notes for a coach (read-only; `vibesynergy_athlete` returns the same text) |

Argument completion (`completion/complete`) is provided for metric IDs, sports, gear, tags,
dates with data, and recent activity IDs.

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

- **Profiles** (config per client or via a URL parameter): `core` (viewing: overview, day,
  period, calendar, activities, activity, sleep, recovery, body, training_load, bests,
  sync_status; ~14 tools), `analysis` (core + series, stats, trend, compare, correlate,
  distribution, baselines, anomalies, streams, activity_analyze, intervals, sql, result),
  `full` (everything). Small or local models get `core`; frontier models get `full`.
- **Scopes** (OAuth, implemented): `read`, `read.expert`, `diary.write`, `coach.write`,
  `plan.propose`, `sync`, `publish`, `sql`. See the implementation status above. The
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
- **Medical framing**: body/BP/ECG tools include a short "informational, not a diagnosis" note
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
  output with `outputSchema`, resource templates, completion, tool annotations, pagination.
- Transports: stdio (`vibesynergy mcp --stdio`) and streamable HTTP (`/mcp`) with OAuth 2.1
  (authorization server metadata, dynamic client registration, PKCE).
- Tool handlers are thin: parse params → call `vibesynergy.query` → shape the envelope. All
  logic lives in the query layer and is unit-tested there; the portal's REST API calls the
  same functions.
- Contract tests: golden JSON snapshots per tool on a fixture dataset; an "LLM usability"
  eval set (natural-language questions → expected tool choices and answers) run against
  Claude and one other model when tools or descriptions change.

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
Quote and weather are off in this preview. See
[preview contract](INTEGRATION_CONTRACTS.md#read-only-annotation-block-preview).

`vibesynergy_activity_publication_preview(id, destination, language="en")` reads the selected
provider description and returns the exact composed text with `preview_version`.
`vibesynergy_activity_publish(id, destination, preview_version, language="en")` writes once on
explicit request and returns `verified` only after provider read-back. Destinations are
`garmin` and `strava`. A changed preview, unknown marker or uncertain previous write blocks
publication. See [publication contract](INTEGRATION_CONTRACTS.md#explicit-description-publication).
`vibesynergy_activity_unpublish(id, destination, preview_version, language="en")` removes
only the checked VibeSynergy section after an explicit call and verifies provider read-back.

Since 1 Nov 2026 a verified Strava block also goes to the matching intervals.icu activity's
description (`query/publication_icu.py`; Settings → Sync between sources → "Also in
intervals.icu", `[publication] icu_description_enabled`, on by default). Same owned-block
markers: the athlete's own text there stays, a later publication replaces only VibeSynergy's
block, a removal on Strava removes it there too. Each write is read back, kept in
`publication_ledger` (source `intervals_icu`) and audit-logged (`publication.icu`, no text);
intervals.icu activities taken from Strava are skipped (it may not change them). The
publish/unpublish results carry `icu`. An activity with only a Strava copy is
published automatically 30 minutes after it ended when no Garmin copy came; the same block
(same label) then carries what Strava has (Relative Effort, work in kJ, temperature, and
RPE/feel from intervals.icu or VibeSynergy when there are any).
