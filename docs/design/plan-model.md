# Plan in VibeSynergy: design

Status: built. Steps 1 to 4 of the change-over below are in the code: the pages and the coach read VibeSynergy's own plan since [#26](https://github.com/rvartanov/vibesynergy/pull/26), and `vibesynergy plan source` says who leads. This is the design it was built from (5-11-2026); commands and tool names below are the ones that exist now. Part of the grand plan, phase 1. Feeds (Garmin delivery) (horizon guard) (planning page) and (planner).

## Goal

The training plan lives in VibeSynergy as its own model: plan → phases → weeks (note, targets per sport, hard days) → sessions (workout with steps) plus tests and races. intervals.icu becomes a mirror for load, backup and Zwift. **The plan stays usable at every step of the change.**

Non-goals for this phase: the planner, delivery to Garmin for endurance sports, the own load model.

## Today

| What | Where | Note |
|---|---|---|
| Sessions, week notes, TARGETs | intervals.icu, cached in `icu_event` (raw JSON, `external_id` `lake27:<date>:<slot>`, `lake27:week:NN`, `lake27:target:NN:<sport>`) | The plan's source of truth. The coach writes it through `query/icu_plan.py` (version tokens, reason, audit). |
| Plan text, rules | KB `lakeside-2027-plan` (phases, week structure, strength, tests) | Prose. Not machine-readable. |
| Garmin calendar | `planned_item` (source garmin) | Copies of the intervals.icu pushes plus VibeSynergy's strength workouts (`garmin_strength_delivery`). |
| Readers | `plan.py` (`_merge_intervals`), `training_plan.py`, `week_progress`, `daily_coach`, `coach_brief` | All read `icu_event`. |

## Model

New tables (one migration; `store/schema.py` and the revision pin in `tests/test_cli.py` move with it):

- `plan` — id, subject_id, name ("Lakeside 2027"), method (e.g. `friel`), start, end, status (`draft`/`active`/`archived`), `current_version`.
- `plan_version` — id, plan_id, n, created_at, author, reason, summary. Every write creates a version row; rows below point at the version that last changed them.
- `plan_phase` — plan_id, name (Instap, Base 1…), first_week, last_week.
- `plan_week` — plan_id, n, monday, phase, kind (`load`/`recovery`/`race`/`taper`), hours_total, note (the week note text), hard_days (list of weekdays).
- `plan_target` — week_id, sport (`ride`/`run`/`swim`/`strength`), time_s, distance_m, load.
- `plan_session` — id, plan_id, date, slot (`ride`, `run`, `strength`, `brick-ride`…), sport, name, purpose (`easy`/`quality`/`long`/`test`/`brick`/`class`/`coached`), intensity, duration_s, distance_m, `workout` (JSON, below), description (the cue text for the athlete), external_key (`lake27:<date>:<slot>`), status (`planned`/`moved`/`skipped`/`done`), version.
- `plan_link` — session_id, target (`intervals`/`garmin`), remote_id, remote_version, fingerprint, synced_at, last_error. One place for the mirror and delivery state (`garmin_strength_delivery` moves here in).

### Workout JSON

```json
{"steps": [
  {"kind": "warmup", "duration_s": 900, "target": {"type": "pct_ftp", "low": 50, "high": 65}, "cadence": 90},
  {"kind": "repeat", "times": 4, "steps": [
    {"kind": "work", "duration_s": 360, "target": {"type": "pct_ftp", "low": 75, "high": 85}, "cadence": [50, 60]},
    {"kind": "rest", "duration_s": 180, "target": {"type": "pct_ftp", "low": 55, "high": 55}}
  ]},
  {"kind": "cooldown", "duration_s": 540, "target": {"type": "pct_ftp", "low": 50, "high": 60}}
]}
```

- `kind`: warmup, work, rest, recovery, cooldown, repeat, open (until lap), exercise.
- End: `duration_s`, `distance_m`, `reps` or `until_lap: true`.
- Target types: `pct_ftp`, `pct_lthr`, `hr_zone`, `pace_zone`, `pct_css`, `rpe` (Licht/Middel/Zwaar), `none`. Always **relative**: absolute watts, bpm and pace are computed at delivery time from the confirmed thresholds (ICU-6).
- `exercise` steps: Garmin category and name, sets, reps or time, side, rest, weight (the `[garmin]` block as data/#190).
- Writers: an exporter to intervals.icu text (`- 6m 75-85% 50-60rpm`, `intensity=rest`, `Press lap`) and later a Garmin builder. Readers: a parser from intervals.icu text for the import.

## Change-over (the plan stays usable)

1. **Import (shadow, read-only).** A command `vibesynergy plan import` (`--prefix <external_id prefix>` on the first import, `--name`, `--dry-run`) builds the model from `icu_event` (sessions with parsed workouts, notes, targets) and the KB plan (phases, week kinds). It writes nothing to intervals.icu. A diff report lists what could not be parsed. The athlete's plan keeps running from intervals.icu as today.
2. **Shadow compare (≥ 1 week).** After every intervals.icu sync, a job compares the model with `icu_event`. A difference goes to the coach and the horizon guard, not silently into the model. Exit criterion: 7 days without unexplained differences.
3. **Flip writes.** The coach writes to the model (MCP tools: `vibesynergy_plan_session_set`, `vibesynergy_plan_session_move`, `vibesynergy_plan_session_skip`, `vibesynergy_plan_week_set`, `vibesynergy_plan_target_set`, all with reason, version and COACH-5 coupling: note, targets and sessions together). A mirror job pushes each change to intervals.icu through the existing `icu_plan` writes, with `plan_link` as the idempotency record. The old `vibesynergy_icu_*` tools stay as a fallback and write through the model.
   - **Edits made in intervals.icu** (by the athlete) are detected by version and imported as a new plan version with author `intervals.icu`. The coach is told. No conflict UI in this phase: the latest edit wins and the previous version stays in history.
   - Switch: `vibesynergy plan source intervals|vibesynergy` (kept on the plan itself, `plan.status` `shadow`/`leading`; there is no config setting). Back to `intervals` = stop the mirror and read `icu_event` again.
4. **Flip readers.** `plan.py`, `training_plan.py`, `week_progress`, the coach brief and the planning page read the model. `icu_event` stays as the mirror's cache and for matching activities.

Each step is its own PR with tests. The coach routines and KB (`claude-coach-loop`, `intervals-icu-workout-authoring`, `docs/COACH_PROTOCOL.md`) change in the same PR as the step that changes their tools.

## Exports

PDF and Excel of the plan from the model ( acceptance; the Google Sheet fallback becomes a generated export): overview, weeks, sessions of the next 3 weeks, thresholds and zones, the "zonder coach" rules.

## Tests

- Round trip: intervals.icu text → workout JSON → intervals.icu text gives the same steps for every workout in the live plan (snapshot of today's 24 workouts and 35 notes as a fixture without personal data).
- Mirror: a model change becomes exactly one intervals.icu write; a repeat run writes nothing; an intervals.icu edit becomes a version with author `intervals.icu`.
- Switch back: `vibesynergy plan source intervals` restores today's behaviour (the current plan tests pass unchanged).

## Open for the athlete

1. When you edit a workout in intervals.icu yourself, should that win (proposal), or go to the coach as a proposal first?
2. Is the method name for the current plan `friel` (Training Bible + Fast After 50) right, or do you want a different label?
