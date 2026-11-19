# What VibeSynergy does

Every feature, by area. Kept current with each change: a pull request that adds, changes or
removes a feature updates this list and [CHANGELOG.md](../CHANGELOG.md) (see
[CONTRIBUTING.md](../CONTRIBUTING.md#features-and-changelog)). Details per area are in the
linked documents.

Status: alpha (0.0.1). The portal is in Dutch and English.

## Installing, updating and access

- **Install** as a container (Windows, macOS, Linux; amd64 and arm64), with `uv tool install`
  (macOS, Linux) or from source. Step by step for people and for AI assistants:
  [INSTALL.md](../INSTALL.md).
- **First login** by a one-time link from `vibesynergy init --email …` (valid 24 hours); a new
  one with `vibesynergy login-link`. **Password reset from the terminal**:
  `vibesynergy password-reset` mails (or with `--show`, prints) a 15-minute one-time link to
  choose a new password without the old one, clears the login lockout and audits
  `auth.reset.cli`; `--reset-mfa` also turns the second step off. Command line only (shell
  access is the proof), not in the portal or the AI apps.
- **Login**: password, or a one-time link by mail; "Forgot your password?" by mail.
  Optional second step: a code by mail, or an authenticator app (TOTP) with 10 backup codes.
  Brute-force brake, sessions per device (log out one or all), a mail on a new login.
- **Settings → Account**: change the password and the second step (asks the password again),
  see and end your sessions.
- **Updates**: Settings → System → About this version shows waiting updates with what they
  bring (checked hourly, or at once with **Check for updates**) and has **Update now** (one click when you are logged in; without a portal login it asks the coach passphrase). On the command line
  `vibesynergy update` (`--check`). Each update backs up the database first and logs what it
  did. A container updates by pulling the new image.
- **Demo data** to look around without accounts: `vibesynergy init --demo`, then
  `vibesynergy sync demo`: three months of synthetic runs and rides with streams and a route,
  sleep, HRV, resting heart rate, steps and weight.
- **Get started**: a card on the start page with the three setup steps (a source, your zones,
  your AI coach), what is done and a link each; it can be put away.
- **Backups**: `vibesynergy db backup` (online, safe while running).
- **Reaching it from other devices**: behind HTTPS (a reverse proxy or Tailscale), with the
  name in `[portal] hosts`. The portal itself only listens on the computer it runs on.

## Sources

Details: [SOURCES.md](SOURCES.md).

- **Garmin Connect**: activities with streams and laps, daily health (sleep, HRV, resting
  heart rate, stress, Body Battery, readiness, steps, intraday heart rate), zones and
  thresholds, the training calendar, gear. History from the API or from a Garmin account
  export.
- **Withings**: weight and body composition, blood pressure.
- **Strava**: activities, gear with mileage; history from the account archive.
- **intervals.icu**: where VibeSynergy puts a copy of the active plan, so its workouts reach
  Zwift and the other services connected there. A one-way mirror: its events are never read
  as the plan, the week notes, the targets or the races, and a change made there changes
  nothing in the advice, the reports or the AI coach (#111). Read: the workout library, which
  plan workout an activity was paired with, and its calendar to check the copy. Not read:
  fitness, load, zones, RPE/Feel, compliance.
- **RunSynergy** (the running-group app): the group trainings you give, read from your personal
  calendar link (Settings → Sources → RunSynergy). Each becomes a workout proposal for the
  plan's "Training geven" session that day: warm-up and cool-down until lap, the core on time
  so you can blow the whistle, Z1 for yourself. Adopt it on Today and it reaches the watch and
  counts as planned. Read only; the link is kept encrypted.
- **CalDAV calendars** (e.g. iCloud): events on the calendar; optionally a copy of Garmin
  workouts in a chosen calendar.
- **Files**: FIT, GPX and TCX, by upload, by a drop folder or by import
  (`vibesynergy import files <folder>`).
- **One activity per session** across all sources (Garmin, Strava, files, trainer apps such as
  MyWhoosh and Zwift), with the best source per measurement (`Settings → System → Best source
  per measurement`).
- **Sync**: on a schedule per source, webhooks where a source offers them, a job queue, re-sync
  of any period, problems with a retry, a coverage map per month (Settings → Sources → Status).
- **History**: per source an estimate and two ways to fill the past (API month by month, or
  the account export), resumable.

## The day and the start page

- **Planned workouts open on the start page**: click one under Planned to fold open its
  steps, as in This week. A workout marked "changed" says why underneath (harder, longer,
  shorter, another distance or intensity, or another workout than planned).
- **Planned against done**: under each done or changed workout on the start page, the day
  view and in This week, two lines: "Planned:" with the planned main set (3x300 Middel,
  25' Z2) and "Done:" with what was done (sport, distance, time, the main sets from the laps,
  RPE). The content counts, not only the size: the planned steps are compared with the laps,
  so other sets or intervals in place of a steady session make it "changed". Too few laps to
  judge: "done, not checked".
- **Today** (`#/dashboard`): advice for today, the plan and what was done, check-in and
  complaints, recovery and the week, what's coming up, key numbers, last night, body and
  measurements, the timeline, best efforts and the diary. Blocks can be reordered and hidden.
  A block or key number without data takes no space; one line at the bottom names what is
  left out. A block you tick under "Arrange the start page" stays even while it is empty.
  A short measured night that your check-in contradicts is asked about, and can be marked as
  an incomplete measurement so it leaves the advice (undo possible).
  A night Garmin updated after its first reading, and a readiness that is the reading after
  training instead of the morning one, are named in the advice; the morning reading counts.
  A finished session is judged on how hard it was, not only how long: longer at a low effort
  is done, time above the planned zone is harder than planned, and the advice says which.
  Under a safety restriction the text follows its cause: a reported complaint, pain or
  illness points to a physio or doctor; measurements alone are about recovery; load alone
  about load.
- **Today's advice from your AI coach** (#89): with the setting "Daily advice" on AI (the
  default), the AI coach's advice decides the colour, the safety level and the reasons. When
  there is no valid advice from it, the fixed rules give it and the page says why (no advice
  yet, the run failed, the advice was incomplete, the AI is off, budget or service
  unavailable, or something new came in after it). Pain, illness or a complaint after the
  advice starts a new AI run at once. Under "Why this advice": where the AI and the rules differ.
  **Update advice** syncs every source, then asks the AI coach for new advice, with its
  progress. The start page, the Coach page, `vibesynergy_today` and the mail show the same
  advice. On its own, the AI coach can never make illness, a red-flag complaint, pain of 7
  or more, or an injury that changes how you move milder than the rules.
- **Any day** (`#/dashboard/<date>`): past days with what happened; days ahead with what is
  planned and the weather forecast.
- **Quick add** and **Search** (`/`) over activities, notes, coach messages, the knowledge
  base and gear.
- **Waiting for you** (the bell): proposals only you decide on (**Your decision**), and **Notices** about what went wrong or needs attention,
  including the warnings that appeared at the top of the screen in the last day, also after
  you closed them there.

## Training

- **Activities**: one row per session (sport icon, name, time, distance, pace or speed),
  filters in the address, totals for the selection.
- **Activity page**: summary, route map, aligned stream charts (a marker follows the route),
  laps, time in zones, the copy from each source, the training load and how it was counted,
  RPE and Feel, gear, purpose, notes, the published description.
- **RPE and Feel**: from Garmin, or your own entry, which is always possible and wins.
- **Training load per session** (VibeSynergy's own): power (TSS) on the bike, pace against CSS
  in the water, otherwise the heart-rate stream (an hour at threshold is 100), with the
  thresholds valid on that day. A session stored twice counts once.
- **Fitness, fatigue and form** (Fitness → Form): 42- and 7-day weighted averages, form zones
  (transition, fresh, grey, optimal, high risk), ramp rate.
- **Race predictions** (Fitness → Form): 5 km, 10 km, 10 miles, half marathon, marathon and
  50 km, from one hour at your threshold pace and your fastest runs of the last 12 weeks
  (Riegel; stricter beyond the half marathon), with how certain each is, the change against
  six weeks ago and Garmin's prediction next to it.
- **Projection to the races**: fitness and form per day if the plan is done as written, from
  each planned session's steps and intensity; the projected form on each race day.
- **Training analysis**: the fitness chart, weekly volume per sport, time in heart-rate, power
  and pace zones (counted from the streams with the zones of that day), the
  easy/moderate/hard split and polarisation index, fitness markers (VO2max, Garmin's
  race predictions), the power curve. Heart-rate TRIMP, TSS and Garmin's load remain
  available to compare.
- **Zones & thresholds**: FTP, LTHR, max heart rate, threshold pace and CSS per sport, their
  zones, the history of each value, what Garmin and intervals.icu hold, the AI coach's
  proposals. A change saves at once; Garmin and intervals.icu only change when you tick them.
- **Calendar** (`#/schedule`): month or week grid, sessions in their sport's colour with
  their title (week: also time and distance; on a phone the week is a list of days), the day
  with recovery, weather (°C/°F, wind in km/h, m/s, knots or Beaufort, and where) and the
  plan.
- **Planning & progress**: the week against the plan per sport and per workout, plan against
  done over the weeks and phases, form, the next nine days; download the plan as Excel or PDF.
- **Training plan**: the plan lives in VibeSynergy (weeks, phases, targets, sessions with
  steps) and every page, the advice and the AI coach read it there and only there; started in
  VibeSynergy (`vibesynergy plan create`) or copied once from intervals.icu
  (`vibesynergy plan import`), a plan builder (goal race, sport and distance, method,
  hours, rhythm, rest days, fixed sessions), delivery of the plan's workouts to Garmin (per
  sport, with a mark in front of the name: one for all, ⚡ by default, one per workout type with multisport joining its sports' marks, or none) and to intervals.icu;
  a triathlon, duathlon or brick is one session per sport linked by slot (`ms1-swim`,
  `ms2-ride`, `ms3-run`), one entry in the calendar and one Garmin multisport workout. A
  Garmin calendar workout that is not in the plan (Garmin Coach's "Basis", "Drempel" and so
  on) is listed apart as not in the plan and never counts as planned.
- **Races**: VibeSynergy's own race list with an A/B/C priority per race (Calendar page,
  `vibesynergy_race_set_priority`); the next A race is the primary race and sets the taper.
  Garmin and intervals.icu events are no races; the races they held moved into VibeSynergy
  once, and `vibesynergy plan import-races` copies new ones on request.
- **Strength and Pilates library** with Garmin exercise mapping.
- **Gear**: shoes, bikes and the rest with distance against lifetime, last use, your own
  status, limits and service reminders; default gear per sport.

## Health

- **Recovery**: HRV against your own normal band, resting heart rate, readiness, Body Battery,
  morning check-ins. Missing days are never drawn as zero.
- **Sleep**: stages and score per night, bed and wake times and their regularity, hypnograms,
  the other nightly measurements.
- **Body**: weigh-ins with a smoothed trend, body composition, blood pressure.
- **Diary**: morning check-in (stays in VibeSynergy), notes with tags (e.g. sick, travel),
  linked to activities; optional private copy of notes to intervals.icu.
- **Complaints & injuries**: pain per day on a body map, how it went, fixed warning rules (not a
  diagnosis), what healed.

## Insight

- **Metrics**: any metrics over any period, shared time axis or indexed to the average,
  smoothing, a comparison period, statistics and a table.
- **Correlations**: two daily metrics, same day or with a lag of up to 7 days.
- **SQL**: one read-only SELECT over the documented tables, as a table or a chart.
- **Anomalies and baselines** (also over MCP), leaving out sick days.

## The AI coach and AI apps

Details: [AI_HARNESS.md](AI_HARNESS.md), [COACH_RUNNER.md](COACH_RUNNER.md),
[COACH_PROTOCOL.md](COACH_PROTOCOL.md), [MCP.md](MCP.md).

- **AI coach inside VibeSynergy** on your own API key (Anthropic, OpenAI, xAI or a local
  server; any of them works just as well: nothing a model reads names a model or provider,
  and what a provider lacks, such as thinking settings or prompt caching, is left out),
  off until you switch it on: morning advice, evening review, week review, race brief,
  check-in, injury and threshold tasks, each with its own switch, schedule and model, a
  monthly cost cap with warnings.
- **Continuous advice**: your AI coach gives the day's advice at 07:00 (time in Coach →
  Settings) and makes a new version when new data comes in: sleep or HRV, your check-in, a
  note, pain or an injury (at once), a workout, a plan change for today or tomorrow, or your
  reply. It waits a few minutes so several syncs give one update, up to a daily maximum you
  set, with its cost shown; at the maximum the fixed rules give the advice. The evening
  review's advice is for tomorrow. There is one current advice per day: the start page says
  when it was written and after what, the Coach page lists the earlier versions, and a mail
  comes only when the colour, the safety level or a session decision changes.
- **One harness for every AI coach**: each task has a versioned prompt and rules, a fixed
  context that starts with the safety outcome (the rules' level and reasons, sleep check,
  measurement gaps, check-in, complaints, injuries), and an output contract. An entry that
  misses the contract (wrong day, far too long, a daily advice without level, colour and
  reasons, or milder than the rules without saying why) is refused, for the built-in AI coach
  and for AI apps over MCP alike. It checks validity, not approval: what you ask through
  your own agent is already approved, and approval steps are for an AI coach acting on its
  own (the primary and any advisory AI coach), which also can never soften illness or real
  pain. Every entry records the harness version it was written under, how it came in and
  the role that wrote it.
- **Plan checks**: every plan change is checked for gaps around it (a name that doesn't
  match the length, a strength session without a full warm-up, a reference to a session that
  is gone, nearby sessions that mention the changed one). The start page and the AI coach see
  what the checks find for the next two weeks. A rule proposal can't overwrite an unrelated
  rule by mistake.
- **Coach page**: advice and the active profile, Knowledge, Methods & rules, Memory, Change log,
  Settings. Changes to planned sessions, thresholds and the knowledge base wait for your
  approval; a newer knowledge-base proposal from the same AI coach replaces its waiting one. A
  routine instruction or COACH rule that names a model or provider gives a warning, and
  an article's earlier slug keeps working (`claude-coach-loop` is now `ai-coach-loop`).
- **Any AI app over MCP** (Claude, ChatGPT, local models): 101 tools to read your data,
  write diary notes and coach entries, propose plan changes. Per app you choose the scopes
  (`read`, `read.expert`, `diary.write`, `coach.write`, `plan.propose`, `plan.write`, `sync`,
  `publish`, `sql`). OAuth with your passphrase, or personal access tokens that expire.
- **Coach apps** (Settings → Sharing): the connected apps, their scopes and activity, revoke.
- **More than one AI coach, with roles** (Settings → Sharing → Coach apps): each app and
  token is your own agent (writes count as approved), the primary AI coach (exactly one;
  gives the daily advice; a plan change needs your approval) or an advisory AI coach (a
  second opinion and proposals only). VibeSynergy enforces it for every write, whatever
  the app or model. Plan changes an AI coach may not make by itself wait under Waiting for
  you with Approve and Reject; approving carries them out. The primary AI coach may still
  fill the horizon by itself: a new session in a free slot of a week that has its note. An advisory AI coach's advice
  shows as "Second opinion" with the day's advice on the start page and the Coach page,
  flagged where it differs from your AI coach in safety level or a session decision.

## Publishing

- **Activity descriptions** on Strava, Garmin and intervals.icu: a block with statistics (in
  your temperature unit), the weather and an optional quote, added beside your own text and
  never replacing it. Automatic after an activity, or per activity.
- **Share links** (Settings → Sharing): read-only views of chosen pages, optionally with diary
  writing.

## Alerts

- **In the portal, by mail and by browser push** (Settings → Alerts), per kind: injury
  warning flags, recovery and training warnings, a login that stopped working, a source that
  stopped syncing, a failed sync job, history runs and imports, coach messages, a new or
  changed daily advice from your AI coach (by default push and portal, no email), a coach
  token about to expire. A failed sync job leaves the portal header as soon as the same data
  syncs successfully again.

## Settings

- **Sources**, **Sync**, **Alerts**, **Sharing**, **Account** and **System** (time zone, units,
  daily advice (AI, rules only or off), temperature scale, wind unit, weather place, appearance and colour palette, language, the
  menu, schedules, best source per measurement, drop folder, about this version).
  Edits save directly; settings the service reads at start say so with a restart banner.

## Help and feedback

- **Help** in the footer of every page: short video manuals for each thing you do, with
  Dutch and English captions; `#/help/<manual>` opens one. They are recorded from the demo
  data with [tools/help_videos](../tools/help_videos/README.md).
- **Report a bug** in the footer of every page and under Settings → System: opens GitHub's
  bug form with the version, page, error and browser filled in; you send it from your own
  account.
- **Bug report and feature request forms** with the information that's needed:
  [REPORTING.md](REPORTING.md). Security problems privately via GitHub.
