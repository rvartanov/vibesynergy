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
  one with `vibesynergy login-link`.
- **Login**: password, or a one-time link by mail; "Forgot your password?" by mail.
  Optional second step: a code by mail, or an authenticator app (TOTP) with 10 backup codes.
  Brute-force brake, sessions per device (log out one or all), a mail on a new login.
- **Settings → Account**: change the password and the second step (asks the password again),
  see and end your sessions.
- **Updates**: Settings → System → About this version shows waiting updates with what they
  bring (checked hourly, or at once with **Check for updates**) and has **Update now** (asks your password). On the command line
  `vibesynergy update` (`--check`). Each update backs up the database first and logs what it
  did. A container updates by pulling the new image.
- **Demo data** to look around without accounts: `vibesynergy init --demo`, then
  `vibesynergy sync demo`: three months of synthetic runs and rides with streams and a route,
  sleep, HRV, resting heart rate, steps and weight.
- **Get started**: a card on the start page with the three setup steps (a source, your zones,
  your coach), what is done and a link each; it can be put away.
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
- **intervals.icu**: where VibeSynergy puts the active plan, so its workouts reach Zwift and
  the other services connected there. Read: plan events, the workout library, and which plan
  workout an activity was paired with. Not read: fitness, load, zones, RPE/Feel.
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

- **Today** (`#/dashboard`): advice for today, the plan and what was done, check-in and
  complaints, recovery and the week, what's coming up, key numbers, last night, body and
  measurements, the timeline, best efforts and the diary. Blocks can be reordered and hidden.
  A block or key number without data takes no space; one line at the bottom names what is
  left out. A block you tick under "Arrange the start page" stays even while it is empty.
- **Any day** (`#/dashboard/<date>`): past days with what happened; days ahead with what is
  planned and the weather forecast.
- **Quick add** and **Search** (`/`) over activities, notes, coach messages, the knowledge
  base and gear.
- **Waiting for you**: proposals only you decide on, and notices worth a look.

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
  zones, the history of each value, what Garmin and intervals.icu hold, the coach's
  proposals. A change saves at once; Garmin and intervals.icu only change when you tick them.
- **Calendar** (`#/schedule`): month or week grid, sessions in their sport's colour with
  their title (week: also time and distance; on a phone the week is a list of days), the day
  with recovery, weather (°C/°F, wind in km/h, m/s, knots or Beaufort, and where) and the
  plan.
- **Planning & progress**: the week against the plan per sport and per workout, plan against
  done over the weeks and phases, form, the next nine days; download the plan as Excel or PDF.
- **Training plan**: a plan model in VibeSynergy (weeks, phases, targets, sessions with steps),
  started in VibeSynergy (`vibesynergy plan create`) or taken over from intervals.icu, a plan
  builder (goal race, sport and distance, method, hours, rhythm, rest days, fixed
  sessions), delivery of the plan's workouts to Garmin (per sport) and to intervals.icu.
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

- **Coach inside VibeSynergy** on your own API key (Anthropic, OpenAI, xAI or a local server),
  off until you switch it on: morning advice, evening review, week review, race brief,
  check-in, injury and threshold tasks, each with its own switch, schedule and model, a
  monthly cost cap with warnings.
- **Coach page**: advice and the active profile, Knowledge, Methods & rules, Memory, Change log,
  Settings. Changes to planned sessions, thresholds and the knowledge base wait for your
  approval.
- **Any AI app over MCP** (Claude, ChatGPT, local models): 102 tools to read your data,
  write diary notes and coach entries, propose plan changes. Per app you choose the scopes
  (`read`, `read.expert`, `diary.write`, `coach.write`, `plan.propose`, `plan.write`, `sync`,
  `publish`, `sql`). OAuth with your passphrase, or personal access tokens that expire.
- **Coach apps** (Settings → Sharing): the connected apps, their scopes and activity, revoke.

## Publishing

- **Activity descriptions** on Strava, Garmin and intervals.icu: a block with statistics (in
  your temperature unit), the weather and an optional quote, added beside your own text and
  never replacing it. Automatic after an activity, or per activity.
- **Share links** (Settings → Sharing): read-only views of chosen pages, optionally with diary
  writing.

## Alerts

- **In the portal, by mail and by browser push** (Settings → Alerts), per kind: injury
  warning flags, recovery and training warnings, a login that stopped working, a source that
  stopped syncing, a failed sync job, history runs and imports, coach messages, a coach token
  about to expire.

## Settings

- **Sources**, **Sync**, **Alerts**, **Sharing**, **Account** and **System** (time zone, units,
  temperature scale, wind unit, weather place, appearance and colour palette, language, the
  menu, schedules, best source per measurement, drop folder, about this version).
  Edits save directly; settings the service reads at start say so with a restart banner.

## Help and feedback

- **Report a bug** in the footer of every page and under Settings → System: opens GitHub's
  bug form with the version, page, error and browser filled in; you send it from your own
  account.
- **Bug report and feature request forms** with the information that's needed:
  [REPORTING.md](REPORTING.md). Security problems privately via GitHub.
