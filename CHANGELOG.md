# Changelog

What changed in VibeSynergy, newest first. Every change that a user or an AI app notices gets
a line here in the pull request that makes it ([CONTRIBUTING.md](CONTRIBUTING.md#features-and-changelog));
the complete list of what VibeSynergy does is [docs/FEATURES.md](docs/FEATURES.md).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`0.0.1a1` is 0.0.1 alpha 1). "Unreleased" is what
`main` has beyond the last version; `vibesynergy update` and **Update now** install `main`.

## Unreleased

### Added
- **Check of the knowledge-base texts** (#120): Coach → Methods & rules now shows a warning,
  with a link to the article, when a text your AI coach follows uses a retired name (the old
  app name, the removed plan-source command, an old article name), refers to a range of
  rules that does not match the rules in use ("ICU-1..4" while ICU-6 exists), names a rule
  or a tool that does not exist ("SendUserMessage"), or writes a threshold as a fixed number
  ("FTP 265 W", "5:10/km") in a routine instruction, the working method or the plan, where it
  goes stale after the next test. Every AI coach gets the same warnings in the handbook, and
  a proposed change gets them before you approve it. A warning only: you decide about the
  text.
- **Sleep check for every night** (#100): Garmin's sleep tracking is not always right, so
  VibeSynergy now checks each night itself and marks it as fine, possibly off, incomplete
  or badly recorded, with the reason ("Sleep may be badly recorded: no REM sleep recorded
  in a night of 10 h 55 min; HRV measured only from 20:22 to 00:07"). It looks at whether
  the watch measured your heart rate all night, whether the night fits your usual bedtime
  and wake time, whether the sleep stages are plausible, whether HRV covered the night, and
  whether Garmin updated the night later. Today shows the mark with "Yes, incomplete" and
  "No, it's right"; your choice wins and can be undone, also through your AI app
  (`vibesynergy_sleep_check_set`). Sleep, HRV, readiness or resting heart rate from a
  badly recorded or incomplete night no longer count for the day's advice, your AI coach
  weighs them less and says the night is uncertain, and they stay out of your baselines and
  trends. The sleep page lists the marked nights. A changed mark updates the advice. **This
  changes behaviour**: a night found badly recorded or incomplete leaves the advice without
  being asked first; say "No, it's right" to count it again.
- **Contact window for every AI coach** (#119): Coach → Settings → Contact window says
  when an AI coach may reach you, by default 07:00–23:00 in your time zone. Outside it your
  AI coach still runs and writes its advice, and you see it in the portal, but the notice,
  the push and the mail wait until the window opens; for a day's advice only the version
  that is current then goes out, once. Report mails, "Messages from your AI coach" and the
  notice that a run failed wait too. The one exception: today's advice to rest because you
  reported illness, a red-flag complaint, pain 7/10 or more or an injury that changes how
  you move goes out at once. AI coaches over MCP get the window in
  `vibesynergy_coach_state` and the handbook (`contact_window`) and keep to it. **This
  changes behaviour**: an advice update made at night (new sleep data at 05:00, say) no
  longer pushes at once but at 07:00.
- **One ongoing conversation with your AI coach per day** (#115): every run (07:00, an
  event, an update, the evening, a check-in, pain) gets the day thread in its context:
  the earlier versions of the advice, what changed and why, and your replies. A new
  version must say what changes against the current one and why (`changed_because`,
  naming its id); without it the output contract refuses the write, for the AI coach
  inside VibeSynergy and for AI coaches over MCP alike. The start page ends with "Changed
  because: …"; the Coach page numbers the earlier versions, marks them *Replaced* and links
  each to its own report; a report page says whether it is the current version.
- **Every notice traceable** (#115): every notice, push and mail about your AI coach is
  read back from a stored report and opens exactly that report (`#/coach/report/<id>`).
  A text that is not stored, or a replaced version, is not sent; a version that only
  rewords points the day's notice at itself without a new push. An MCP coach's daily
  advice gets the same per-day notice as the built-in one. A daily continuity check
  (Coach page, `vibesynergy_today` `continuity`, audit log) reports gaps: a notice without
  its report, a notice on an older version, an advice without a notice, a change without
  a reason. **This changes behaviour**: a second daily advice for the same day without
  `changed_because` is refused, and the advice notice links to the report instead of the
  start page.
- **More than one AI coach, each with a role** (#118): in Settings → Sharing → Coach apps
  every app and token gets a role. *My own agent* works on your request and what it writes
  counts as approved. The *primary AI coach* (exactly one; the AI coach inside VibeSynergy
  unless you choose another) gives the daily advice. An *advisory AI coach* gives a second
  opinion and proposals only. VibeSynergy enforces the role for every write tool and
  refuses a write outside it with an error that says what to do instead. Apps and tokens
  that were connected already keep working as your own agent; a new connection starts as
  advisory until you choose.
- **Plan changes wait for your approval** (#118): a plan change by the primary AI coach
  needs an approval reference (`approval_id`: a proposal you approved or your 'akkoord'
  reply, at most 48 hours old). Without one, and always for an advisory AI coach, the
  change is stored as a proposal under Waiting for you, with Approve and Reject; approving
  carries it out. One standing approval: the primary AI coach may add a new session in a
  slot that held none, in a plan week that has its note (filling the horizon, "day 9"),
  without a separate approval. **This changes behaviour**: the AI coach inside VibeSynergy
  no longer changes, moves or skips a session, or writes a week note or target, by itself;
  those arrive as proposals.
- **Second opinion** (#118): an advisory AI coach's morning or evening entry is never the
  daily advice. It shows under "Second opinion" with the day's advice on the start page
  and the Coach page, and in the Coach page's messages with its own chip. Where it differs
  from your AI coach in the safety level or a session decision it is flagged, Waiting for
  you has a notice, and your AI coach weighs it in its next advice. `vibesynergy_today`
  carries the same `second_opinions`.
- **Entry keys per author** (#118): two AI coaches can no longer collide on
  `morning-<date>` or get each other's entry: a key belongs to its author
  (`morning-<date>-<author>`), and `vibesynergy_coach_handbook` tells each connection its
  role and key suffix (`connection`).
- **Races are VibeSynergy's own** (#111): one race list with an A/B/C priority per race. The
  races Garmin and intervals.icu held moved into it once, by themselves, on update; a change
  in Garmin or intervals.icu changes nothing. `vibesynergy plan import-races` copies races
  added there later, on request only. The primary race is now always the next A race
  (#120), so the taper and the AI coach follow the race that matters.
- **Reset the password from the terminal** (#105): `vibesynergy password-reset` mails a
  one-time link (15 minutes) to choose a new password without the old one, also with a second
  step on, and clears the login lockout. `--show` prints the link too; `--reset-mfa` turns the
  second step off when its phone and backup codes are lost. Command line only.
- **Planned against done, by content** (#116): a workout of the same sport but another
  content now counts as changed, not done. The planned steps are compared with the
  activity's laps: 3x300 Middel planned and drills, 100s and 200+50s swum is "changed"
  ("Another workout than planned"), and so is a tempo run in place of an easy run. A few bad
  swim laps (a length miscounted) do not change the verdict. When the laps are too thin to
  judge (one lap, auto laps for an interval plan), the workout says "done, not checked". On
  the start page, the day view and in This week each done or changed workout shows two
  lines, "Planned: Zwemmen duur 40' — 3x300 Middel" and "Done: Swimming 2525 m, 68' — 5x50 +
  3x100 + 4x(200+50) + 7x50 · RPE 3", with the reason on top when changed. The AI coach gets
  the same planned and done per session (`today`, `plan_match`), so a report does not call a
  different session done as prescribed.
- **"Changed" says why** on the start page: under a workout marked changed, one line
  such as "Harder than planned: 28 min above zone 1" or "Shorter than planned: 30 min
  instead of 50 min".
- **Open a planned workout on the start page**: click it under Planned and it folds open
  with its steps, the same as in This week.
- **Any AI provider works just as well** (#96): the harness names no model or provider in
  what a model reads (the harness rules, the routine instructions' seeds, the coach
  handbook, the tool descriptions) or in the portal's texts. Thinking settings, an effort
  level and prompt caching are used where the provider has them and left out elsewhere;
  the output contract is checked the same for every model. A key is checked against the
  form of the provider you save it under, so a key pasted under the wrong provider is
  refused there.
- **Knowledge-base warning for model names** (#96): a routine instruction or a COACH rule
  that names a model or provider gives a warning in the coach handbook and when an AI app
  proposes such a text, so the instructions stay usable by any model.
- **Continuous advice** (#90): your AI coach makes the day's advice at 07:00 (adjustable in
  Coach → Settings) and a new version when new data comes in: sleep or HRV, your check-in,
  a note, pain or an injury (at once), a workout, a plan change for today or tomorrow, or your
  reply to a report. It waits a few minutes so several syncs give one update. Set the most
  updates per day in Coach → Settings, which shows what they cost; at the maximum the fixed
  rules give the advice and say why.
- **One current advice per day, with its versions** (#90): the start page says when the
  advice was written and after what ("Updated at 10:32 after your check-in"), and the Coach
  page lists the earlier versions. The evening review's advice is for tomorrow, so it is
  today's advice until the morning advice. A mail comes only when the colour, the safety
  level or a session decision changes, not for new wording.
- **Today's advice comes from your AI coach** (#89): its colour, safety level, reasons and
  words, everywhere: the start page, the Coach page, `vibesynergy_today` and the mail show
  the same advice. When there is no valid advice from your AI coach, the fixed rules give it
  and the page says why. Choose AI (default), rules only or off under Settings → System →
  Daily advice.
- **Something new after the advice** (pain, illness or a complaint) starts a new AI run at
  once; the advice says it is updating, and the rules take over only if that run fails.
- **Update advice** on the start page: syncs Garmin, Strava, intervals.icu and Withings, then
  asks your AI coach for new advice, with its progress.
- **Where the AI and the rules differ** is under "Why this advice", and logged, so it
  shows over time whether the AI is too lenient.
- **One harness for every AI coach** (#88): every coach entry is checked against the
  task's output contract when it is written, by the coach inside VibeSynergy and by any AI
  app over MCP. An entry for the wrong day, a report that is far too long, or a daily advice
  without its safety outcome (level, colour, reasons) is refused with what to fix, and is
  never stored, mailed or shown. A run that ends outside the contract fails and you get the
  failure message. Each entry records the harness, rules, prompt and context version it was
  written under.
- **The coach starts from the safety outcome**: every task's context now holds what the
  fixed rules decided and why, the sleep check, measurement gaps, your check-in, complaints
  and pain, next to the injury log. Before, the coach saw only the injury log unless it
  looked the rest up itself.
- **AI apps get the coach's own rules and context**: `vibesynergy_coach_prompt` returns the
  harness rules, the output contract and, with `include_context=true`, the same preloaded
  context the built-in coach starts with; `vibesynergy_coach_write` takes `safety`, `task`
  and `harness`.
- **Measurement gaps are named**: when Garmin updates a night after its first reading
  (3 h 29 that became 10 h 55), or the day's readiness is the reading after training
  instead of the one after waking up, the advice says so and judges the morning reading. A
  readiness computed on a partial night does not count.
- **A night the watch cut short**: when your check-in says you slept well (4-5) but the watch
  measured under 5 hours, Today asks whether the measurement was incomplete instead of
  calling it a short night. **Yes, ignore this night** keeps that short sleep and the
  readiness from it out of today's advice; **Undo** puts it back. Garmin's own record is not
  changed. The coach sees the same in `vibesynergy_today`, and `vibesynergy_explain` shows
  the mark. (#53)
- **⚡ in front of every Garmin workout from VibeSynergy** (strength, run, ride, swim and
  brick), so on the watch and in Garmin Connect you see which workouts are VibeSynergy's next
  to Garmin Coach and your own. Upcoming workouts on Garmin are renamed once. (#64)
- **Choose that mark** in Settings → Sources → Garmin: any emoji or symbol; empty means ⚡.
  Changing it renames the upcoming workouts on Garmin within a few minutes. (#66)
- **Or a mark per workout type**: strength 🏋️, pilates 🤸, yoga 🧘, run 🏃, ride 🚴, swim 🏊
  and brick 🚴🏃 are filled in; change any of them, or keep one mark for everything. (#68)
- **Multisport marks from the sports**: every combined workout (a brick, duathlon or
  triathlon) gets the marks of its sports in order, such as 🚴🏃 for ride then run; fill in one
  multisport mark (🧱, say) to use that instead. (#70, #72)
- **No mark** is a choice too: Garmin then shows the plain names. (#72)
- **Triathlons and other multisport workouts on Garmin**: sessions on one day linked by their
  slot in order (`ms1-swim`, `ms2-ride`, `ms3-run`; a duathlon `ms1-run`, `ms2-ride`,
  `ms3-run`) go to Garmin as one multisport workout, a segment per sport with the
  transitions and the swim as open water, and show as one entry on the calendar and Today.
  Bricks planned the old way keep working. (#74)
- **Multisport names fit on Garmin**: a prefix the parts share is said once
  (`Frysman-simulatie: zwemmen openwater 1900 m + fietsen 2u30 + lopen 45'`), and a name
  still longer than Garmin's 80 characters shortens its longest parts, so every part shows.
  (#76)
- **Plan checks** (consistency, layer 1). Every plan write, move and skip now returns
  warnings for the sessions around it:
  - a name whose minutes don't match the steps (45' while it takes 50');
  - a strength session that starts heavy barbell work without a full warm-up (STR-4);
  - a text that refers to a session the day no longer has ("na de looptest");
  - after a move, skip or add, the sessions nearby that mention it, so they get a second look.

  The start page lists what the checks find for the next two weeks, and `vibesynergy_today`
  carries it as `plan_checks` for the coach. A knowledge-base proposal that would replace an
  existing rule with unrelated text is refused unless `replace` is set; the message names the
  next free id. (#60)
- **RunSynergy group trainings** become the workout for your "Training geven" sessions. Paste
  your RunSynergy calendar link under Settings → Sources → RunSynergy. Today then shows each
  group training you coach as a proposal: warm-up and cool-down until you press lap, the core
  on time so you can blow the whistle, and you in Z1. Adopt puts it in the plan and on your
  watch, and it counts as planned. A change in RunSynergy makes a new proposal; a cancelled
  training says so. The AI coach sees open proposals in `vibesynergy_today`. New help video
  "Coaching a group training with RunSynergy". (#55)
- **Help** in the footer of every page: 17 short video manuals (logging in, the day and
  check-in, quick logging, activities, calendar, planning, the plan builder, zones and form,
  the AI coach, health, complaints, insight, search, sources, sharing, account security and
  arranging the app), with Dutch and English captions. `#/help/<manual>` links to one.
- **Check for updates** on Settings → System → About this version asks GitHub now instead of
  waiting for the hourly check, and shows when it last looked
  ([#22](https://github.com/rvartanov/vibesynergy/pull/22)).
- Race predictions for 5 km, 10 km, 10 miles, half marathon, marathon and 50 km on
  Fitness → Form, from your threshold pace and recent runs, with how certain each is, the
  change against six weeks ago and Garmin's prediction next to it; also over MCP
  (`vibesynergy_race_predictions`) ([#17](https://github.com/rvartanov/vibesynergy/pull/17)).
- Time in heart-rate, power and pace zones counted by VibeSynergy from each activity's stream,
  with the zones valid on that day ([#15](https://github.com/rvartanov/vibesynergy/pull/15)).
- Days in the future open on the start page, with what is planned and the weather forecast
  ([#12](https://github.com/rvartanov/vibesynergy/pull/12)).
- Weather in °C or °F and wind in km/h, m/s, knots or Beaufort (Settings → System), and the
  place the weather is for ([#7](https://github.com/rvartanov/vibesynergy/pull/7)).
- Bug report and feature request forms that ask for what is needed, and
  [docs/REPORTING.md](docs/REPORTING.md) ([#6](https://github.com/rvartanov/vibesynergy/pull/6)).
- This changelog and [docs/FEATURES.md](docs/FEATURES.md), kept current with every change.
- `vibesynergy plan create` starts a training plan in VibeSynergy itself, without
  intervals.icu. Your coach can then add weeks, targets and sessions to it; a week that is
  not there yet is added with its first note or target, and a session keeps the planned time
  or distance of its steps ([#26](https://github.com/rvartanov/vibesynergy/pull/26)).

- **Get started** on the start page: three steps (connect a source, set your zones, set up
  your coach) with what is done and a link each; put it away when you no longer need it
  ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Demo data now has runs and rides with heart rate, pace, cadence, elevation and a route, and
  nights with sleep stages, HRV and weight, so Activities, the calendar, Fitness and Best
  efforts show something when you try VibeSynergy without an account ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- `vibesynergy import files` also takes GPX and TCX files (and zips of them), like the upload
  and the drop folder ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Calendar: the week view shows each session's title, time and distance; the month view
  shows a short title on a wide screen. On a phone the week is a list of days ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).

### Changed
- **Cadence band for Z2 rides**: new endurance rides and brick rides from the workout
  library now use 80–95 rpm in the warm-up and the main set (was 90 and 85–95), with the cue
  "cadans 80-95 (eigen keuze binnen die band)". Power and duration are unchanged.
- **Routine instructions without another app's tool** (#120): the seed prompts for the
  morning, evening and week review no longer ask for `SendUserMessage`; the message to the
  athlete is the summary of the AI coach's entry, which VibeSynergy sends. Changes go to
  the plan in VibeSynergy, not to intervals.icu. `vibesynergy_coach_state` no longer says the
  plan lives in intervals.icu: the plan is VibeSynergy's own, and the primary race is the
  next A race.
- **The coach handbook is generated from the harness** (#119): `vibesynergy_coach_handbook`
  lists all 14 harness tasks (was 4) with the times, days and switches of Coach → Settings
  (`due` was 11:00 and 23:30, now the scheduled time, e.g. 07:00 and 21:45), their
  triggers, context chain, contract and approval steps, plus `approval` (roles, the write
  policy per tool, `approval_id`) and `contact_window`, from the same registry and settings
  the AI coach inside VibeSynergy runs on. The missed-run check and Coach → Tasks read the
  same schedule: a run counts as missing three hours after its scheduled time.
- **The advice, the reports, the dashboard and the AI coach read only VibeSynergy's plan**
  (#111): planned sessions, week notes, weekly targets, races and the season. intervals.icu
  only gets a copy to pass on to Zwift and other services; a workout, note, target or race
  changed or added there is never planned and never reaches the advice or the AI coach. A
  plan that still followed intervals.icu now leads in VibeSynergy. Workouts on your Garmin
  calendar that are not in the plan stay "not in plan". Compliance and the planned load are
  VibeSynergy's own (time done against time planned; the load from the session's steps); the
  Planning page column is now "Time as planned".
- **"AI coach" everywhere** (#96; Dutch: "AI-coach"): the portal, the mails, the docs and
  everything a model reads use this one term for the AI that coaches, instead of "your
  coach", "the AI" or a model's name. "AI app" stays the word for the app that connects
  over MCP. Coach → Settings no longer speaks of one provider's credits or routines.
- **The article `claude-coach-loop` is now `ai-coach-loop`** (#96). The old slug keeps
  working everywhere a slug is read (`vibesynergy_kb_get`, `vibesynergy_kb_search`,
  proposals, the change log), and proposals that were waiting for it apply to the renamed
  article. Its text is unchanged.
- **Notes written by an AI app carry the app's id as their source** (`client:<id>`),
  whatever the app is called; older notes with the source `claude` now have `ai`. The
  harness rules are version 3 (the same rules, without a product name).
- **A new or changed daily advice is a push and a notice, no longer a mail.** The 07:00
  advice and its updates go where Settings → Alerts sends the new kind "Daily advice": by
  default a push on your phone and a notice at the top of the portal, only when the advice
  is new for that day or changed in colour, safety level or a session decision. Tick
  email there to get it by mail again. Weekly and other reports still come by mail.
- **A new knowledge-base proposal replaces the same AI's waiting one** for the same article,
  rule or source: the older one becomes "superseded", so Waiting for you only shows the
  latest wording.
- **"Even kijken" on Waiting for you is now "Meldingen"** (Notices): the section lists what
  went wrong or needs attention (a skipped coach run, a workout not on your Garmin, a sync
  problem), and the old title did not say so.
- **`vibesynergy_today`'s `verdict` is the current advice** (#89): the AI coach's when it is
  valid, else the rules' (`verdict.source`, `verdict.fallback`); the full advice is in
  `advice`. `safety` stays the rules' own outcome. The coach's message that is today's advice
  is no longer repeated under it on the start page, and the rules' safety line no longer
  sits beside an AI advice that decided its own safety.
- **"Differed from the plan" looks at how hard, not only how long** (#88): a longer session
  at a low effort (RPE 4 or lower, or heart rate nearly all in zones 1-2) counts as done. A
  session with time above its planned zone counts as harder than planned, also when its
  length was right, and the advice says how many minutes. A shorter session is called
  shorter.
- **Amber with a session still to come** now says what amber means for it (keep it easy, or
  do it controlled or swap it), where it used to say only how many activities were recorded.
- **The coach's instructions are numbered steps** with decision rules, the output as a
  schema with an example, and the points where the coach must weigh things itself and say
  why, so that every model follows them the same way. No model choice changed.
- **AI apps over MCP**: a `morning` or `evening` entry without `safety`, or for another day
  than today (evening: today or tomorrow), is now refused. See docs/MCP.md.
- **Workouts on the watch: one rest before a new station.** In a strength workout the last set
  of an exercise no longer counts down its rest and then waits for lap again. It goes straight
  to the Herstel step until lap, whose text says the minimum ("Herstel minstens 3 min, dan
  Lap"). Rests between sets of one station stay. The last set of the workout has no rest. Run,
  ride and swim workouts do the same: a repeat followed by a step until lap (or the end) skips
  its last recovery. This uses Garmin's own "skip last rest" option on the repeat, so the watch
  still shows 3 of 3 sets. Upcoming workouts on Garmin are rebuilt once. (#57)
- The bell (Waiting for you) also lists the warnings that appear at the top of the screen,
  so one you closed or missed can still be found ([#30](https://github.com/rvartanov/vibesynergy/pull/30)).
- Texts and the morning coach instructions no longer mention Garmin Coach; you don't use it
  any more ([#30](https://github.com/rvartanov/vibesynergy/pull/30)).
- **Update now** no longer asks your password: being logged in to the portal is enough.
  Without a portal login it still asks the coach passphrase
  ([#28](https://github.com/rvartanov/vibesynergy/pull/28)).
- Your training plan now lives in VibeSynergy. When VibeSynergy leads the plan, the start
  page, the calendar, Planning & progress, the week card, the Garmin check and the
  projection to the races read VibeSynergy's own plan; intervals.icu only holds a copy. A
  session shows and can be moved or skipped even when it has no copy in intervals.icu, and a
  skipped session is no longer planned. Nothing changes while intervals.icu leads the plan
  ([#26](https://github.com/rvartanov/vibesynergy/pull/26)).
- "Only on your Garmin calendar" says that those workouts are not in your plan in
  VibeSynergy, and to ask your coach to add one, when the plan lives in VibeSynergy
  ([#26](https://github.com/rvartanov/vibesynergy/pull/26)).
- For AI apps: with the plan in VibeSynergy, `vibesynergy_plan`, `vibesynergy_today` and
  `vibesynergy_week` give `plan_source` `vibesynergy`, and each planned session has `source`
  `vibesynergy` and a `session_id` for the `vibesynergy_plan_session_*` tools; `extra.icu_id`
  stays while the session has a copy in intervals.icu
  ([#26](https://github.com/rvartanov/vibesynergy/pull/26)).
- Start page and Planning: Garmin workouts that are not in your intervals.icu plan now stand
  under "Only on your Garmin calendar", with why they do not count in planned versus done and
  what to do about it ([#23](https://github.com/rvartanov/vibesynergy/pull/23)).
- Start page: blocks and key numbers without data take no space; they are named in one
  line at the bottom with a link to Sources. A block you tick under "Arrange the start page"
  stays even while it is empty. A new layout starts with advice, check-in, this week,
  recovery and coming up; your own arrangement is kept ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Header: one row on a phone. The theme (system, light, dark) is chosen under Settings →
  System, and "Report a bug" is in the footer and under Settings → System. On a phone
  Quick add sits in the header instead of floating over the page, where it covered values
  in a table ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Activities: one line per session with the sport as an icon before the name; pace or speed
  is shown for imported GPX and TCX files too (from distance and time) ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Zones & thresholds: the sport you train most comes first, and the cards line up ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- The main action of a card stands out: connecting a source, making a draft plan, adding a
  coach link ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Coach: without a coach the Advice tab says so once, with the way to set one up ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- An AI app needs the **publish** permission to set the public insight line of an activity;
  with only "write messages" it writes the assessment without that line. Allow it under
  Settings → Coach apps for the app that writes your insights. The line is also refused when
  it names health, diary or injury details or holds a link ([#19](https://github.com/rvartanov/vibesynergy/pull/19)).
- Asking for a login or reset link no longer cancels the link sent just before, and these
  mails are limited to a few per quarter of an hour ([#19](https://github.com/rvartanov/vibesynergy/pull/19)).
- Fitness, fatigue, form and the projection to the races are VibeSynergy's own calculation
  only: per session power, pace or the heart-rate stream with the thresholds of that day;
  planned load from each session's steps. intervals.icu is used only to deliver the plan
  ([#10](https://github.com/rvartanov/vibesynergy/pull/10)).
- RPE and Feel come from Garmin or your own entry, which is always possible and wins
  ([#14](https://github.com/rvartanov/vibesynergy/pull/14)).
- The bug button opens GitHub's bug form in your own account instead of sending from the
  server ([#6](https://github.com/rvartanov/vibesynergy/pull/6)).

### Fixed
- **The installed app serves the coach protocol** (#119): `protocol_md` and
  `protocol_version` in `vibesynergy_coach_handbook` were empty in an installed app,
  because `docs/COACH_PROTOCOL.md` was not in the package. The wheel now carries it.
- A Zwift ride of a plan workout ("Zwift - Intervals icu: Rustige rit Z2 1u") no longer counts
  as "changed: hard instead of easy": the "Intervals" in intervals.icu's name is no intensity.
- **One daily-advice notice per day** (#108): a new version of the day's advice replaces the
  earlier notice instead of adding a second one that contradicts it, comes back at the top
  also when you dismissed the earlier version, and starts with "Updated:". On the phone a
  push for a new version replaces the earlier one (one push tag per day).
- **The "Changed" line on the start page** reads as normal text: each changed session by its
  title as written, with what changed ("Easy ride Z2 1h: dropped").
- **An activity insight no longer uses up the day's advice updates.** The AI coach's
  insight after a workout (started by the same activity event) counted toward the daily
  maximum (Coach → Settings, default 4), so the maximum was reached too early.
- **A login link opened twice no longer ends on an error.** When a mail app opens the
  link a second time, or an older link is tapped after a newer one worked, the login page
  said "This link has expired or was used" although you were already logged in; it now
  takes you to the portal.
- **The injury check and the check-in reaction now run** (#90). They waited for a check-in
  after a 09:04 morning report with two low scores, or pain of 4/10 or more, which never
  happened; now any check-in after the morning advice and any pain or injury for today starts
  them.
- **A failed sync no longer stays in the header after the next sync works.** A one-off
  calendar error (HTTP 401) showed "Calendar sync problem" for 24 hours while every later
  sync succeeded; it now goes away once the same data syncs again. This applies to every
  source.
- Today's advice no longer adds "Review what you have already done before adding training"
  after the activities recorded today: it was unclear and repeated what Plan and done
  shows. The count and what is still on the plan stay. (#85)
- The safety notice no longer sends you to a physiotherapist or doctor about "what you
  reported" when only measurements set it off (low HRV, readiness, a short night, resting
  heart rate): it now says your measurements point to incomplete recovery and to check again
  tomorrow. A complaint, pain or illness you reported still gets the physio advice. (#54)
- The start page's recovery line agrees with today's advice: one sign off that doesn't
  change the advice (resting heart rate alone) now reads "keep an eye on it" instead of
  "take it a little easier" next to advice for a quality session
  ([#37](https://github.com/rvartanov/vibesynergy/issues/37)).
- With only the demo data in, the Get started guide says why connecting a source is still
  to do ([#38](https://github.com/rvartanov/vibesynergy/issues/38)).
- Settings → Account lists where you are logged in by browser and system ("Chrome on
  Linux") instead of the browser's raw identification string; hover for the full string
  ([#41](https://github.com/rvartanov/vibesynergy/issues/41)).
- Going to another page opens it at the top instead of where the previous page was
  scrolled ([#44](https://github.com/rvartanov/vibesynergy/issues/44)).
- The check-in uses the same words everywhere: **Muscle soreness** (fresh to heavy) on the
  start page, in the + dialog, the diary and recovery, and one **+ Pain or niggle** button
  that opens the same form. Changing an answer under **Edit check-in** keeps the check-in
  open until you choose **Done editing**
  ([#39](https://github.com/rvartanov/vibesynergy/issues/39)).
- A complaint without a title of its own shows its name in your language ("Kuit links"
  instead of "Left calf") ([#40](https://github.com/rvartanov/vibesynergy/issues/40)).
- Coach → Memory: clicking **Add a note** straight after typing your notes for the coach
  works the first time. Saving the notes showed a status line that pushed the button down
  under the pointer ([#35](https://github.com/rvartanov/vibesynergy/issues/35)).
- Settings → Sharing says up front, in your language, when share links can't be made yet
  because VibeSynergy has no public address, instead of failing after you fill in the form
  ([#36](https://github.com/rvartanov/vibesynergy/issues/36)).
- The share-link page list no longer shows "Calendar" twice: the start page's races and
  agenda now have their own name ([#43](https://github.com/rvartanov/vibesynergy/issues/43)).
- Email alerts with no address of their own go to your account's address, and Settings →
  Alerts says so; it no longer shows "needs an address" while your account has one. The
  alert check now also runs without that address, so the banner and push stay current
  ([#42](https://github.com/rvartanov/vibesynergy/issues/42)).
- The plan builder keeps each week within your hours: strength, test and interval sessions
  now count against the week, and the easy sessions share what is left (with 4 to 7 hours a
  week, base weeks came out at almost 6 hours). With too few hours strength and then test
  sessions are left out. Switching the sport now picks a sensible distance (marathon,
  olympic) instead of ultra ([#34](https://github.com/rvartanov/vibesynergy/issues/34)).
- On a phone a warning in the top bar gets its own row under the buttons; it covered the
  VibeSynergy name and could not be read ([#29](https://github.com/rvartanov/vibesynergy/pull/29)).
- Alert mails no longer cut the subject off with "…" halfway a sentence: the subject is the
  first whole part of the message ([#29](https://github.com/rvartanov/vibesynergy/pull/29)).
- Messages that send you to Settings name the page as it is called now: Settings → Sources
  (and its Calendar, Status and History parts), Settings → Sharing → Coach apps, Settings →
  System. They no longer point to "Settings → Calendar", "Data → Sources" or "Data & sync",
  which are gone ([#27](https://github.com/rvartanov/vibesynergy/pull/27)).
- Moving or skipping a planned session when there is no plan says so, instead of saying that
  intervals.icu leads the plan ([#27](https://github.com/rvartanov/vibesynergy/pull/27)).
- The MCP documentation ([docs/MCP.md](docs/MCP.md)) describes what the server offers: the
  plan tools (`vibesynergy_plan_model` and the five plan writes) are in it, `vibesynergy_sql`
  is described as it works (SQLite, `query` and `limit`), and tools, resources, profiles and
  argument completion that were never built are marked as not built
  ([#27](https://github.com/rvartanov/vibesynergy/pull/27)).
- Signing in to Google Calendar works again with a domain of your own in `[portal] hosts`:
  the address Google sends you back to is the tailnet address again, the one registered at
  Google, and no longer moves when a domain is added
  ([#25](https://github.com/rvartanov/vibesynergy/pull/25)).
- The button in mails (evening report, coach reports, alerts) and the portal address on
  Settings open your own domain from `[portal] hosts` when you have one, instead of always the
  tailnet address ([#24](https://github.com/rvartanov/vibesynergy/pull/24)).
- Settings → System on a phone: a long line in the update log wraps instead of making the
  whole page wider than the screen, and the weather choice shows each option on one row
  ([#22](https://github.com/rvartanov/vibesynergy/pull/22)).
- The name VibeSynergy is back next to the logo on phones; only screens narrower than 370px
  show the logo alone ([#21](https://github.com/rvartanov/vibesynergy/pull/21)).
- Planning & progress no longer fails to load without a plan; it says how to get one. "Are
  your workouts on your watch?" only shows with a plan and a connected Garmin ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Zones & thresholds no longer says intervals.icu could not be reached when it was never
  connected ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Calendar: a week whose load cannot be counted shows a dash instead of "Load 0" ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- Activity page: "Zones not configured" links to Fitness → Zones ([#20](https://github.com/rvartanov/vibesynergy/pull/20)).
- An update whose database migration fails puts the database and the previous version back
  by itself, and the running service is left as it was; in a container a failed migration
  restores the backup made just before ([#19](https://github.com/rvartanov/vibesynergy/pull/19)).
- When Garmin refuses with "too many requests" or the login has expired, the sync stops at
  once and waits an hour instead of asking on ([#19](https://github.com/rvartanov/vibesynergy/pull/19)).
- A sync for one day (a scale measurement, `--start today`) after days without syncing no
  longer hides the days in between ([#19](https://github.com/rvartanov/vibesynergy/pull/19)).
- Too many wrong passwords no longer stop the login link from being sent; strangers
  registering apps can no longer keep you from approving your own, and apps nobody approved
  are removed after a day ([#19](https://github.com/rvartanov/vibesynergy/pull/19)).
- `/health` also watches the portal and the scheduler, and says how long the oldest job has
  been running ([#19](https://github.com/rvartanov/vibesynergy/pull/19)).
- Security: the sign-in landing page no longer shows a name from the address as it came
  in; Strava webhook events are only accepted with this install's own subscription id; an
  AI app can no longer rewrite the coach's instructions by itself (it proposes, you approve);
  an import refuses archives built to fill memory or disk; the SQL tool bounds the size of a
  value; the login page only returns to pages on this site ([#18](https://github.com/rvartanov/vibesynergy/pull/18)).
- In a container, a sync that was running when the service stopped or was updated is picked
  up again at the next start instead of blocking that source for good ([#18](https://github.com/rvartanov/vibesynergy/pull/18)).
- The coach's schedule and "today" follow your own time zone (Settings → System) when the
  coach has none of its own ([#18](https://github.com/rvartanov/vibesynergy/pull/18)).
- A coach run that failed on an unexpected answer stayed "running" and was never retried or
  reported; a planned step written as "Z3-Z2" broke Fitness → Form; the morning coach entry
  could fail while VibeSynergy was still working out the load of a new activity ([#18](https://github.com/rvartanov/vibesynergy/pull/18)).
- Known vulnerabilities in two dependencies (certifi, opentelemetry-api) ([#18](https://github.com/rvartanov/vibesynergy/pull/18)).
- A ride recorded by a trainer app (MyWhoosh, Zwift) and the watch counted as two activities
  ([#13](https://github.com/rvartanov/vibesynergy/pull/13)).
- Strength, Pilates and walking added no training load; swims were scored on wrist heart rate
  ([#10](https://github.com/rvartanov/vibesynergy/pull/10)).

### Removed
- **`vibesynergy plan source`** (#111): intervals.icu can no longer be the plan source.
  `vibesynergy plan import` remains as an explicit one-time copy into VibeSynergy. The AI
  coach's insight run no longer gets `vibesynergy_icu_events`.
- intervals.icu's fitness (`wellness`), activity load, zone times and RPE/Feel are no longer
  read; `[user] load_source` is ignored ([#10](https://github.com/rvartanov/vibesynergy/pull/10),
  [#14](https://github.com/rvartanov/vibesynergy/pull/14)).
- Morning check-ins are no longer written to intervals.icu
  ([#14](https://github.com/rvartanov/vibesynergy/pull/14)).
- Garmin Coach leftovers: the coach rule about preserving Garmin Coach (coach policy
  version 3, written by migration 0050), the `dubbel` pair between a Garmin Coach session
  and a plan workout, and the MCP tool `vibesynergy_icu_resolve_duplicate`. A Garmin workout
  that is not in the plan is simply listed as not in the plan
  ([#31](https://github.com/rvartanov/vibesynergy/issues/31)).

## 0.0.1a1 — 2026-11-11

The first public alpha.

### Added
- The archive, portal, MCP server and AI coach as one installable app (`uv tool install`),
  with sources Garmin, Withings, Strava, intervals.icu, CalDAV and files.
- Login: password, link by mail, password reset and an optional second step (mail code or
  authenticator app) ([#1](https://github.com/rvartanov/vibesynergy/pull/1)).
- Update checker and updater: `vibesynergy update` and **Update now**
  ([#4](https://github.com/rvartanov/vibesynergy/pull/4)).
- Container for Windows, macOS and Linux, and [INSTALL.md](INSTALL.md) for people and AI
  assistants ([#5](https://github.com/rvartanov/vibesynergy/pull/5)).
- Bug button on every page ([#3](https://github.com/rvartanov/vibesynergy/pull/3)).

### Fixed
- The plan builder guessed the distance from a race's name instead of its distance
  ([#2](https://github.com/rvartanov/vibesynergy/pull/2)).
