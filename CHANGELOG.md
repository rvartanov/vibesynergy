# Changelog

What changed in VibeSynergy, newest first. Every change that a user or an AI app notices gets
a line here in the pull request that makes it ([CONTRIBUTING.md](CONTRIBUTING.md#features-and-changelog));
the complete list of what VibeSynergy does is [docs/FEATURES.md](docs/FEATURES.md).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`0.0.1a1` is 0.0.1 alpha 1). "Unreleased" is what
`main` has beyond the last version; `vibesynergy update` and **Update now** install `main`.

## Unreleased

### Added
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
- intervals.icu's fitness (`wellness`), activity load, zone times and RPE/Feel are no longer
  read; `[user] load_source` is ignored ([#10](https://github.com/rvartanov/vibesynergy/pull/10),
  [#14](https://github.com/rvartanov/vibesynergy/pull/14)).
- Morning check-ins are no longer written to intervals.icu
  ([#14](https://github.com/rvartanov/vibesynergy/pull/14)).

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
