# Changelog

What changed in VibeSynergy, newest first. Every change that a user or an AI app notices gets
a line here in the pull request that makes it ([CONTRIBUTING.md](CONTRIBUTING.md#features-and-changelog));
the complete list of what VibeSynergy does is [docs/FEATURES.md](docs/FEATURES.md).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`0.0.1a1` is 0.0.1 alpha 1). "Unreleased" is what
`main` has beyond the last version; `vibesynergy update` and **Update now** install `main`.

## Unreleased

### Added
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

### Changed
- Fitness, fatigue, form and the projection to the races are VibeSynergy's own calculation
  only: per session power, pace or the heart-rate stream with the thresholds of that day;
  planned load from each session's steps. intervals.icu is used only to deliver the plan
  ([#10](https://github.com/rvartanov/vibesynergy/pull/10)).
- RPE and Feel come from Garmin or your own entry, which is always possible and wins
  ([#14](https://github.com/rvartanov/vibesynergy/pull/14)).
- The bug button opens GitHub's bug form in your own account instead of sending from the
  server ([#6](https://github.com/rvartanov/vibesynergy/pull/6)).

### Fixed
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
