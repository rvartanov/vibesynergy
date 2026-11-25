# What VibeSynergy does

Every feature, by area. Kept current with each change: a pull request that adds, changes or
removes a feature updates this list and [CHANGELOG.md](../CHANGELOG.md) (see
[CONTRIBUTING.md](../CONTRIBUTING.md#versions-and-changelog)). Details per area are in the
linked documents.

Status: beta (version 0.1.0-beta.0.N, see Versions below). The portal is in Dutch and English.

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
- **Versions and "What changed"** (#140): the version (`0.1.0-beta.0.1`, `0.1.0-beta.0.2`, …;
  every update that brings new changes adds one to the last number) is at the bottom of every
  page with the commit, in `vibesynergy --version` and `vibesynergy version`, `/version`, the
  MCP server information and Settings → System → About this version. `vibesynergy update`
  makes the release when it installs merged pull requests: the next number with the date,
  the tag `v<version>`, and it prints "old → new" with what changed. After an update the
  first page you open shows a card "What changed in version …" once (kept per account);
  after that every version's changes are under Waiting for you → Notices. No push, no mail.
  AI coaches and AI apps read what changed for them in `vibesynergy_coach_handbook`
  (`what_changed`) and in the MCP server information.
- **Demo data** to look around without accounts: `vibesynergy init --demo`, then
  `vibesynergy sync demo`: three months of synthetic runs and rides with streams and a route,
  sleep, HRV, resting heart rate, steps and weight.
- **Get started**: a card on the start page with the three setup steps (a source, your zones,
  your AI coach), what is done and a link each; it can be put away.
- **Backups**: `vibesynergy db backup` (online, safe while running).
- **Reaching it from other devices**: behind HTTPS (a reverse proxy or Tailscale), with the
  name in `[portal] hosts`. The portal itself only listens on the computer it runs on.
- **One own address** (#117): the canonical address (`[server] canonical_url`, else the first
  `[portal] hosts` domain, else the tailnet name) is in every link VibeSynergy shows or sends:
  login and reset mails, alert mails, notices, push, the coach report links and MCP
  `meta.portal_url`. Other names (the tailnet name, more `[portal] hosts`, `[server] aliases`)
  are aliases: an explicit list, accepted as host and origin, never used for links; any other
  Host is refused. Settings → System shows the address and "Also reachable at".
  `[server] redirect_aliases = true` sends portal pages opened on an alias to the address.

## Sources

Details: [SOURCES.md](SOURCES.md).

- **Garmin Connect**: activities with streams and laps, the steps of a structured workout
  and the rounds of a HIIT class, daily health (sleep, HRV, resting heart rate, stress, Body
  Battery, readiness with its morning factors, steps, intraday heart rate), Garmin's own
  training model (acute and chronic load, load focus, running tolerance, endurance and hill
  score, heat and altitude acclimation, lactate threshold and cycling FTP history), zones
  and thresholds, the training calendar, gear. History from the API or from a Garmin
  account export.
- **Withings**: weight and body composition, blood pressure.
- **Strava**: activities, gear with mileage; history from the account archive, with the
  workouts' photos and videos.
- **intervals.icu**: where VibeSynergy puts a copy of the active plan, so its workouts reach
  Zwift and the other services connected there. A one-way mirror: its events are never read
  as the plan, the week notes, the targets or the races, and a change made there changes
  nothing in the advice, the reports or the AI coach (#111). Read: the workout library, which
  plan workout an activity was paired with, and its calendar to check the copy. Not read:
  fitness, load, zones, RPE/Feel, compliance. The copies are kept in the audit log, not on
  the Coach page: Settings → Connections → intervals.icu shows when the last copy changed
  something and how many changes it made, with what was copied under Details (#141).
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
  Every night gets an automatic sleep check (heart rate all night, the usual bedtime and
  wake time, plausible sleep stages, HRV over the night, a night updated later, the check-in
  against the measurement): a night that may be off, incomplete or badly recorded is shown
  with the reason, and you answer "Yes, incomplete" or "No, it's right" (undo possible; also
  over MCP). Sleep, HRV, readiness or resting heart rate of a badly recorded or incomplete
  night do not count for the advice.
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
  you closed them there. Under the notices: **What changed in VibeSynergy**, per version
  (#140).

## Training

- **Activities**: one row per session (sport icon, name, time, distance, pace or speed),
  filters in the address, totals for the selection.
- **Activity page**: summary, route map, aligned stream charts (a marker follows the route),
  laps, time in zones, the copy from each source, the training load and how it was counted,
  RPE and Feel, gear, purpose, notes, your name and texts with their history, the Publish
  tick, the published description, the workout's photos.
- **Photos of a workout** (#114): *Add photos* on the workout page (JPEG, PNG, WebP or GIF),
  and the photos on its Strava and Garmin copies come in by themselves. One photo is kept
  once, also when it comes from several places or in another size. Strava and Garmin take
  no photos from other apps, so a photo you add stays in VibeSynergy. Removing a photo
  only removes it here (it stays on Strava and Garmin, nothing is deleted there) and can be
  put back from the workout's photo history. Shown on the workout page and on share links
  with Activities, always without the camera's location data; a removed photo never.
- **Videos of a workout** (#114): MP4 and MOV (up to 500 MB), added on the workout page or
  taken in from Strava (also a streamed video) and the Strava archive, each video once (by
  its bytes and by Strava's id). Kept as they came; played from a copy without the file's
  metadata, with a poster frame, in the browser's own player (seeking works, also on a
  phone), on the workout page and on share links. They count toward the workout's 60 photos
  and videos. Needs ffmpeg (in the container); without it, photos are not affected.
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
  Garmin calendar workout or event that is not VibeSynergy's own (Garmin Coach's "Basis",
  "Drempel", one added by hand in Garmin) is not shown: not on the start page, the day view,
  the planning page, in the AI apps or to the AI coach, and it never counts as planned
  (#153). VibeSynergy's own copies on Garmin fold into their session; the synced Garmin
  calendar stays in the database and VibeSynergy does not change it.
- **Races**: VibeSynergy's own race list with an A/B/C priority per race (Calendar page,
  `vibesynergy_race_set_priority`); the next A race is the primary race and sets the taper.
  Garmin and intervals.icu events are no races; the races they held moved into VibeSynergy
  once, and `vibesynergy plan import-races` copies new ones on request.
- **Races and events, added, changed and deleted** (#91): Planning & progress → "Races and
  events" lists your races (countdown, priority, projected race-day form) and your other
  events (group training, training camp, holiday, not available, other, over one or more
  days), with an "Add race or event" form and Edit and Delete per row; a delete can be undone
  at once. AI apps use `vibesynergy_race_add`, `vibesynergy_event_add`,
  `vibesynergy_race_update`, `vibesynergy_race_delete`, `vibesynergy_race_revert` and
  `vibesynergy_races`, each write with a reason and undoable. One check for both (name, date,
  sport, distance, priority, start time, place, link, goal time, notes), and no doubles: one
  that is already there (same date, name and sport) is refused, and one that also comes from
  Garmin or intervals.icu becomes its linked copy and shows once. VibeSynergy keeps them;
  intervals.icu gets a copy (A/B/C race, or a note) that a change there never overwrites,
  and Garmin is only read. Your own request through your agent is carried out at once; the
  AI coach's own race change waits for your yes under Waiting for you. The events are on the
  calendar and in the AI coach's state.
- **Workout library from the method**: run, ride, swim and strength templates whose every
  coaching value (cadence band, cues, % FTP and zones, warm-up and cool-down minutes,
  distances, exercises, names) is the knowledge-base article `workout-method` (Coach →
  Methods & rules), not code. An AI coach proposes a change, you approve it as a diff, and
  the next workout uses it; sessions planned from a template are rebuilt on request
  (`vibesynergy_workout_rerender`, `vibesynergy plan rerender`), done sessions never change.
- **Strength and Pilates library** with Garmin exercise mapping.
- **Gear**: shoes, bikes and the rest with distance against lifetime, last use, your own
  status, limits and service reminders; default gear per sport. Gear of your own (#114):
  swim gear such as a pull buoy, paddles or fins, a wetsuit or a trainer, added on the Gear
  page. A one-time **mapping to Garmin's gear list** on the Gear page, with suggested
  matches by type and name (*Use the suggested matches*), each editable; a Strava bike or
  pair of shoes is its own Strava item.
- **Gear on the workout page** (#114): one tap links the gear VibeSynergy suggests, with the
  reason in words (the gear of your last workout of the same sport, else your default for
  the sport, else the bike or shoes you used last; retired gear never). A small picker
  takes other gear, no gear, or several extra items such as swim gear. Every change is kept
  in the workout's gear history (VibeSynergy, Strava or Garmin). The main item counts in the
  km totals at once.

## Health

- **Recovery**: HRV against your own normal band, resting heart rate, readiness, Body Battery,
  morning check-ins. Missing days are never drawn as zero.
- **Sleep**: stages and score per night, bed and wake times and their regularity, hypnograms,
  the other nightly measurements, and the nights the sleep check marked, with the reason.
  Badly recorded or incomplete nights stay out of the HRV, sleep and readiness baselines and
  trends.
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
- **Contact window** (Coach → Settings, default 07:00–23:00): when any AI coach may reach
  you. Outside it your AI coach still makes its advice and you see it in the portal, but
  its notice, push and mail wait until the window opens (for a day's advice only the
  newest version); AI coaches over MCP read the window and keep to it too. Only today's
  advice to rest because you reported illness or serious pain comes at once.
- **One handbook for every AI coach**: the handbook an AI app reads over MCP is generated
  from the same task list, schedule, contract and approval steps the AI coach inside
  VibeSynergy runs on, and an installed app serves the full protocol text.
- **Continuous advice**: your AI coach gives the day's advice at 07:00 (time in Coach →
  Settings) and makes a new version when new data comes in: sleep or HRV, your check-in, a
  note, pain or an injury (at once), a workout, a plan change for today or tomorrow, or your
  reply. It waits a few minutes so several syncs give one update, up to a daily maximum you
  set, with its cost shown; at the maximum the fixed rules give the advice. The evening
  review's advice is for tomorrow. There is one current advice per day: the start page says
  when it was written and why, the Coach page lists the earlier versions, and a push or mail
  comes only for a new day's advice or a real change (#147).
- **One view per advice** (#138, #147): every advice is named after the day it is for and
  says when and why a version came, in plain words: "Advies voor vandaag – za 21 nov",
  "Bijgewerkt 13:48, na je training" (or na je check-in, nacht binnen, plan gewijzigd,
  avondvooruitblik). The banner, the push, the mail, Meldingen, the start page, the Coach
  page, the report page ("Advies") and the AI apps show the same words, made from the stored
  advice when they are read, so an old notice never shows an old wording. The banner is one
  or two lines ("Advies voor vandaag bijgewerkt om 13:48, na je training: … · Bekijk"). Once
  the day's sessions are done and your AI coach's version says so, the label is "Gedaan
  voor vandaag" with the recovery colour in a small line, so the colour never asks for
  something the text says is over. A version that only reports (a session done) is quiet:
  no push, no mail; the notice points at it. From the evening run until midnight the start
  page shows tomorrow's advice in a small card under today's. Notice and push texts end on a
  whole sentence or word, with "…".
- **Workouts you can open** (#181): every workout an advice names, on the Coach page and
  the report page, opens to its steps from VibeSynergy's plan, and once done it links to the
  activity; one the plan does not have stays plain text. A plan change on the Coach page
  shows its session the same way, as do the workouts under Coming up on the start page; on
  the plan page a done workout links to its activity. The stored advice is never changed:
  the links are made when it is read.
- **One ongoing conversation per day** (#115): every run of your AI coach (07:00, an
  update, the evening, a check-in, pain) starts from the day so far: the earlier versions
  of the advice and your replies. A new version says what changed and why ("Changed
  because: your resting HR is normal again …"), or it is refused. Every notice, push and
  mail shows a stored report and opens exactly that report; an older version is marked
  *Replaced* (on the Coach page and on its report page, with a link to the current one) and
  is never sent. The start page, the Coach page, the notices, the mail and the AI apps over
  MCP show the same thread in the same order, also when an AI coach in an app wrote a
  version. A daily check reports any gap on the Coach page.
- **Your own words in every advice** (#170): your AI coach works as in one ongoing session
  that moves along with the days. Every advice (morning, evening, week, check-in, injury)
  reads what you wrote in the last 9 days (`[coach_runner] recent_words_days`): your
  check-in notes, complaints, diary notes and per workout its name, public and private text
  and its notes, newest day first. Today and yesterday go in word for word; when there is
  a lot, older days are shortened (complaints stay word for word), and no day is left out.
  The line under a public workout reads only the names and public texts of your other
  workouts.
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
  Methods & rules (and every AI coach, in the handbook) also gets a warning for a text with
  a retired name, a rule range that does not match the rules in use, a tool that does not
  exist, or a threshold written as a fixed number instead of read live. A fix of such a
  warning that needs no judgment (an old name, a rule range, a threshold on an instruction
  line now read live) goes through without your approval, from VibeSynergy's daily check or
  from your own agent or primary AI coach, never from an advisory AI coach; Methods & rules
  lists each fix with an undo, and Notices has one line for them. A change that still waits
  for you shows only the changed lines, with the full text one click away.
  Memory's "What to remember" is a compact list: one line per note (its kind, then the
  text); tap a line to edit it in place, with where it came from and Remove. A fixed day
  goes in the text; there is no weekday picker.
- **Any AI app over MCP** (Claude, ChatGPT, local models): 101 tools to read your data,
  write diary notes and coach entries, propose plan changes. Per app you choose the scopes
  (`read`, `read.expert`, `diary.write`, `coach.write`, `plan.propose`, `plan.write`, `sync`,
  `publish`, `sql`). OAuth with your passphrase, or personal access tokens that expire.
- **Events for AI coaches** ([EVENTS.md](EVENTS.md)): VibeSynergy notes when a new workout is
  in, when you make it public on Strava and when a coach run was missed. Your AI coach inside
  VibeSynergy writes its insight as soon as a workout goes public; AI apps read the same
  events over MCP (`vibesynergy_events`). VibeSynergy sends them nowhere: the outgoing coach
  links (webhooks to routines in an AI app, "AI coach after a workout" under Coach → Settings)
  were removed (#182); their stored secrets are deleted on the update, their history stays.
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

- **Activity descriptions** on Strava and Garmin: a block with statistics (in
  your temperature unit), the weather and an optional quote, added beside your own text and
  never replacing it. Automatic after an activity (new workouts *public*), or per activity. A ride recorded twice on
  Garmin (Zwift's upload and the watch) takes its figures from one copy. A copy deleted on
  Garmin is marked removed (kept on the workout page, "Garmin-kopie verwijderd op Garmin") and
  the description goes to the copy that is left. When nothing can be published, the workout
  page still shows your AI coach's insight and says why. Nothing is written to intervals.icu
  any more: it passed the text on to Strava and lost yours there (#160); intervals.icu reads
  your activities itself.
- **Workout texts** (#114): every workout has its own name, public text and private text in
  VibeSynergy, with every change kept (when, from VibeSynergy, Strava, Garmin or the AI
  coach) and older versions restorable. They start from your notes and your own Strava and
  Garmin text, without VibeSynergy's block and without doubles. The private text is never
  published or shared. Edited on the workout page (#114): the name, a **public part** (your
  AI coach's insight and the figures, read-only, then your own public text) and a **private
  part** (your AI coach's comments, read-only, then your own private text), each with its
  history and **Restore**; also through AI apps (MCP), and your AI coach reads them.
- **Publish tick** on the workout page (#114): ticking it publishes VibeSynergy's block (the
  AI coach's insight, the figures, the optional weather and quote) in the description on each
  place under **Publish to**, below the text that is already there; unticking removes the
  block everywhere again. The name and your texts are not sent by it. Settings → Publishing
  and sync → **New workouts**: *public* (published by themselves, the tick starts on) or *only
  me* (the default: nothing goes out until you tick it). Unticking a workout that would be
  published by itself keeps it to you.
- **Publish workouts** (#175, Settings → **Publishing and sync**): your workouts are
  VibeSynergy's, and publishing one writes it out. **Publish to**: Strava, Garmin or both (both
  by default; Garmin without Strava works too). Each place keeps its own text above
  VibeSynergy's block; *Keep name, texts and gear the same everywhere* makes them the same.
  One status line per place says whether it is connected, whether VibeSynergy may write
  there, when it last synced and which workouts need a look, with a link to the first. The
  earlier "Copy the Strava text to Garmin too" switch is now the Garmin tick (an older
  setting carries over), and its separate 7-day copy is gone.
- **Automatic workout names** (#114, #188, Settings → Publishing and sync → *Give workouts a
  name automatically*, on by default): when the AI coach writes the insight of a new workout,
  it writes a short name too, in the language of the published text, from what was actually
  done: the sport, indoor (Zwift, virtual, trainer) or outdoor, the structure or main set, the
  intensity, distance or duration, and the place of an outdoor workout. The plan only says
  what the session was meant to be; its title is not copied (a planned MTB ride done on Zwift
  is named as a Zwift ride). Without an insight the workout keeps its name from Garmin or
  Strava; VibeSynergy no longer makes names by a rule. The name is a version in the name's
  history (*AI coach · Named automatically*) and goes out with the text sync like a name you
  type. A new insight (*Write the insight again*, a changed text, a correction) may rename it.
  A name you change yourself (here, or on Strava or Garmin and pulled in by the sync), or a
  version you restore, locks it: the workout page shows *Locked*, and the AI coach never names
  that workout again. The name the workout came in with from Garmin or Strava does not lock
  it. Only workouts that start after the setting first took effect (or was switched on again)
  are named, also when the insight comes days later; switched off, names are left alone.
- **The insight follows your texts** (#114): the AI coach's insight reads the workout's name,
  public text and private text in VibeSynergy (the private text as context only). Publishing
  from VibeSynergy (the tick, or *new workouts: public*) has the first insight written when
  there is none. A change of the name or a text after the insight (in VibeSynergy, or taken
  in from Strava or Garmin) has it rewritten a few minutes after the last edit; **Correct
  the insight** on the workout page stores your comment privately and has it rewritten at
  once with it in mind. At most `[coach_runner] insight_rewrites_per_day` rewrites per
  workout per day (default 3; 0 switches them off). The page shows when the insight is being
  written and the earlier insights; a new insight reaches every published section on Strava
  and Garmin by itself.
- **Names and texts the same everywhere** (#114, Settings → Publishing and sync → *Keep
  name, texts and gear the same everywhere*, off by default): a published workout's name and
  public text are kept the same in VibeSynergy and on the places under *Publish to* (Strava,
  Garmin); since #175 this is the only thing that writes them to Garmin; the description is
  your public text, then VibeSynergy's block. A change in VibeSynergy, on Strava or on Garmin
  is taken in (with its source and time in the history) and passed on within a few minutes;
  when two places changed, the latest change wins and the other version stays in the history
  as "overwritten, from …". The first sync adds the paragraphs one platform has to the
  text, so nothing is lost or doubled. VibeSynergy never writes over a change it has not
  read, never touches a hand-edited block (that workout waits for review) and never sends the
  private text. Strava visibility is not changed: Strava's API can only make an activity more
  private, so the workout page says when only you can see it on Strava. A multisport is
  synced with its Garmin multisport activity only; its legs sync as workouts of their own.
- **Gear the same everywhere** (#114, on the same switch and the same check as the name and
  texts, so no extra setting and no extra Strava reads): a published workout's gear is kept the same in
  VibeSynergy, on Strava (its one gear slot: the bike or the shoes) and on Garmin (the
  items you matched; a Garmin item you did not match is never touched). A change on Strava
  or Garmin is taken in with its source in the history and passed to the other; when two
  places changed, the latest wins and the other stays in the history as "overwritten". Swim
  gear and other gear without a slot there stays in VibeSynergy and is not put in the public
  text. VibeSynergy never writes over a gear change it has not read; gear Strava refuses
  (for example a bike on a run) is not pushed again until you pick other gear; the workout
  page says so, and the texts keep syncing.
- **Share links** (Settings → Sharing): read-only views of chosen pages, optionally with diary
  writing.

## Alerts

- **In the portal, by mail and by browser push** (Settings → Alerts), per kind: injury
  warning flags, recovery and training warnings, a login that stopped working, a source that
  stopped syncing, a failed sync job, history runs and imports, coach messages, a new or
  changed daily advice from your AI coach (by default push and portal, no email), a coach
  token about to expire. A failed sync job leaves the portal header as soon as the same data
  syncs successfully again.
- **Another app overwriting your plan** (#110, portal, mail and push by default): when
  another app or device keeps changing VibeSynergy's copy of your plan in intervals.icu
  (sessions, races and events), Garmin (a delivered workout that keeps being removed) or your
  calendar, the alert says where, which sessions, since when and, where the mirror says so,
  which app wrote it. One change by hand is no alert: it takes the same session put back
  twice within 3 hours (or the same app behind two put-backs). After 3 put-backs VibeSynergy
  stops putting that mirror back and says so; under Waiting for you → Notices you confirm the
  other app is stopped, and your plan goes back. Your own changes keep going out meanwhile.
  The calendar is never put back, so its alert only names what changed. The numbers are
  settings: `[alerts] plan_conflict_repeats`, `plan_conflict_hours`,
  `plan_conflict_stop_after`.

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
