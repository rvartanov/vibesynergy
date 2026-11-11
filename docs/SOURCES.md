# Sources

> Issue and PR numbers in text written before 30-10-2026 refer to the old tracker (they are marked "old #N" where they were links); the GitHub tracker started fresh then.

Deployment (service, Funnel path, logins, webhooks): see deploy/README.md.

How each source collects, what it stores and what is mapped into the canonical tables.
Everything a source returns is kept in the raw archive, mapped or not. Adding a mapper later
and running `vibesynergy normalise <source>` fills the canonical tables from history.

## Garmin (`garmin`, `garmin_export`)

### Setup

Connect Garmin in Settings → Sources. Enter the email and password there and, if Garmin
asks, enter the sign-in code in the same page. The password is used once and is not saved.
The session is encrypted in the server-side secret store. Settings shows when description
writing is available; a live activity write is still verified separately.

```bash
vibesynergy sync garmin              # incremental: last 7 days re-fetched (Garmin revises them)
vibesynergy backfill garmin --start 2015-01-01          # history, month by month, resumable
vibesynergy import garmin-export ~/Downloads/<export>.zip
vibesynergy import files ~/old-fit-files/               # FIT files, zips of FIT files
```

The tokens are encrypted server-side. A failed login is not retried for 15 minutes, so one sync cannot hammer
Garmin's login.

### Datasets

`vibesynergy sources` lists all of them. By kind:

| Kind | Datasets | Raw item key |
|---|---|---|
| entity | `activities` (summary + splits, typed splits, HR/power time in zone, weather, gear, exercise sets), `activity_files` (original download zip, fetched once) | activityId |
| daily | `wellness_files` (monitoring FIT zip; days ≥ 3 days old are fetched once; today skipped), `user_summary`, `heart_rate`, `rhr`, `sleep`, `hrv`, `stress`, `body_battery_events`, `respiration`, `spo2`, `steps`, `floors`, `intensity_minutes`, `all_day_events`, `lifestyle_logging`, `hydration`, `training_readiness`, `morning_training_readiness`, `training_status`, `max_metrics`, `fitness_age`; off by default: `nutrition_food_log`, `nutrition_meals`, `menstrual` | date |
| range (per month) | `body_composition`, `weigh_ins`, `blood_pressure`, `body_battery`, `endurance_score`, `hill_score`, `race_predictions`, `running_tolerance`, `lactate_threshold`, `scheduled_workouts`, `sleep_summary`, `hrv_range`, `max_metrics_range`, `weekly_steps`, `weekly_stress`, `weekly_intensity_minutes`, `device_solar` | `YYYY-MM` |
| snapshot | `profile`, `heart_rate_zones`\*, `power_zones`\*, `cycling_ftp`\*, `critical_swim_speed`\*, `lactate_threshold_latest`\*, `devices`, `gear`, `personal_records`, `badges`, `challenges` (including available and non-completed badge challenges), `goals`, `workouts`, `training_plans`, `activity_types`; off by default: `pregnancy` | `current` |

Garmin's monthly `scheduled_workouts` response can temporarily omit every
`fbtAdaptiveWorkout` for a still active training plan. The raw response is archived, but
calendar reconciliation retains that plan's previously mapped workouts in the current
month when the response contains no adaptive workouts and the mapped `training_plans`
snapshot still identifies the plan as active over those dates. Other calendar items still
reconcile normally. A response with adaptive workouts (including replacements), or a plan
that has ended or disappeared from the current plan snapshot, removes absent workouts as
usual. Past dates before the fetch remain historical. The plan snapshot is fetched
separately, so an ended or replaced plan may need the next calendar refresh before its
old workouts are removed. Assessment plan inputs use stable item fields in date, kind,
source and source-ID order; raw receipt metadata is excluded.

\* History datasets: every version stays mapped, so zones and thresholds are known as they
were on each date (`zone_set.valid_from`).

In a detail bundle, one failing call (for example, no weather for an indoor ride) is recorded
under `errors` in the payload and the activity is still stored. Authentication and
rate-limit errors stop the dataset.

### Mapped so far

| Dataset | Canonical data |
|---|---|
| `user_summary` | `daily.steps`, `daily.floors`, `daily.distance`, `daily.active_calories`, `daily.total_calories`, `daily.intensity_minutes.*`, `hr.resting/min/max`, `recovery.stress.avg/rest_minutes/high_minutes`, `recovery.body_battery.max/min/charged/drained`, `spo2.avg/min`, `resp.waking_avg` |
| `sleep` | `sleep_session`, `sleep_stage`; `sleep.score/duration/deep/light/rem/awake/spo2_avg/avg_respiration/avg_stress/avg_hr/avg_hrv/restless_moments/resting_hr/skin_temp_deviation/body_battery_change` |
| `hrv` | `hrv.night_avg/weekly_avg/night_5min_high/baseline_low/baseline_high` |
| `training_readiness` | `recovery.readiness` (latest reading of the day) |
| `max_metrics`, `fitness_age` | `fitness.vo2max.run/ride`, `fitness.fitness_age` |
| `hydration` | `nutrition.hydration` |
| `body_composition` | `body_measurement` per weigh-in (time from `timestampGMT`); last of the day as `body.weight/bmi/fat_pct/water_pct/bone_mass/muscle_mass/visceral_fat` |
| `blood_pressure` | `body_measurement`; `bp.systolic/diastolic/pulse` |
| `race_predictions` | `fitness.race_pred.5k/10k/half/marathon` (seconds) |
| `heart_rate_zones`, `power_zones` | `zone_set` (boundaries are zone floors; HR adds max HR) |
| `critical_swim_speed` | `zone_set` kind `css`, sport `swimming`: threshold in mm/s, valid from the measurement day |
| `activities` | `activity`, `activity_lap`, `activity_zone_time` |
| `activity_files` | `fit_file`; stream Parquet; `zone_set` from the file (what the device used) |
| `wellness_files` | `fit_file`; wellness Parquet (`hr.sample`, `stress.sample`, `resp.sample`, `steps.cumulative.<activity>`) |
| `heart_rate` | wellness Parquet `hr.sample` (every 2 min, from the API; complements the FIT files, which arrive days later) |
| `stress` | wellness Parquet `stress.sample` (every 3 min; "not measured" values dropped) and `bodybattery.sample` |

Archived but not mapped yet: training status and load,
respiration and SpO2 intraday, endurance/hill score, running tolerance, lactate threshold,
weigh-in sources, devices, gear, badges. These will be mapped when phases 3 to 5 need
them, and their history is already in the archive. Also raw only: `rhr`, `sleep_summary`,
`hrv_range`, `max_metrics_range`, `weekly_steps`, `weekly_stress`,
`weekly_intensity_minutes` and `device_solar`. A plain sync leaves those range datasets
for an hour. `challenges` also stores available and non-completed badge challenges.
Solar and challenges use the same `errors` field as an activity detail bundle.

Checked against real payloads (via the Garmin connector, 2026-09-25): daily summary, sleep,
body composition and HR zones have the fields and units assumed here. HRV, readiness and
activity lists were only seen in the connector's simplified form; their raw shapes are
confirmed at the first real sync.

### FIT files

`vibesynergy.fit` decodes FIT files with fitdecode:

- **Streams** go to `parquet/streams/<fit_id>.parquet`, one row per record, with UTC `t`
  plus `lat`, `lon`, `alt`, `dist`, `speed`, `hr`, `cadence`, `power`, `temp` and any other
  numeric field under its FIT name. Developer fields (Stryd, HRM running dynamics) are named
  `dev_<field name>`.
- **Wellness** samples go to `parquet/wellness/source=<source>/<fit_id>.parquet` as
  `(ts, metric, value)`.
- **Identity:** a file's `fit_id` is `manufacturer-serial-time_created`, so the same file
  from the API, the export and a folder shares one stream file. When the same monitoring
  file arrives from both `garmin` and `garmin_export`, two wellness files exist; the query
  layer (phase 3) deduplicates on `(ts, metric)`.
- **Activities from files:** files imported with `vibesynergy import files` get an `activity`
  row keyed by `fit_id`, because they have no API summary. Linking these to the same
  activity from other sources is phase 2 (dedup).

### Account export (`garmin_export`)

Upload it in Settings → Sources → Garmin → Garmin history (or Settings → Data → History).
The upload is resumable in 8 MB chunks, so a multi-GB zip survives a dropped connection; the
import then runs as a background job with progress (a share of the zip, because most files
sit in nested zips). The Garmin card refuses a zip that is not the Garmin export. Importing
the same export again adds nothing.

The export zip is imported file by file: FIT files (including those in the nested
`UploadedFiles_*.zip`) by content hash, everything else by its path. The export is a separate
source so export rows never overwrite API rows. Phase 2 links the two copies of an activity
(same activityId).

Mapped: `*_summarizedActivities.json` (activities), `UDSFile_*.json` (daily summaries),
`*_sleepData.json` (sleep), `MetricsMaxMetData*.json` (VO2max). All other files are archived.

**To verify on the first real import.** The export format is undocumented and these layouts
come from public descriptions, not from the athlete's export:

1. The units in `summarizedActivities`: epoch-ms times, ms durations, cm distance and
   elevation. Compare a few activities with Garmin Connect.
2. Whether the FIT file names carry the activityId (it is read from `_<digits>.fit`).
3. Which other files hold data worth mapping (weight, HRV, training status, zones history).

If a mapper is wrong, fix it and run `vibesynergy normalise garmin_export`. The raw files are
kept, so nothing needs to be imported again.

## Withings (`withings`)

Own thin client for the official API (`sources/withings/client.py`); no third-party
Withings library.

### Setup (on the server)

```toml
[server]
public_url = "https://server.your-tailnet.ts.net"   # the Funnel address

[sources.withings]
enabled = true
```

```bash
vibesynergy auth withings        # Client ID + Secret once, then approve in a browser
vibesynergy backfill withings --start 2012-01-01
```

The Withings developer app must list `<public_url>/vibesynergy/withings/callback` as its
callback URL. The login works without the server running: approve in any browser, then
paste the address it lands on (`…/callback?code=…`) back into the terminal. App credentials
and tokens are in `~/.config/vibesynergy/secrets/withings/` (encrypted, mode 0600). Withings replaces the
refresh token on every refresh, and the new one is saved immediately.

### Datasets and mapping

| Dataset | Kind | Canonical data |
|---|---|---|
| `measures` | month | `body_measurement` for every measure type (unknown types as `withings.meastype_<n>`); last of the day as daily metrics: `body.weight/fat_pct/fat_mass/lean_mass/muscle_mass/bone_mass/water_mass/visceral_fat/pwv/vascular_age/nerve_health/…`, `bp.systolic/diastolic/pulse`, `spo2.spot`, `body.temperature` |
| `activity` | month | `daily.steps/distance/elevation/active_calories/total_calories/intensity_minutes.*`, `hr.avg/min/max` |
| `intraday_activity` | day | wellness Parquet: `hr.sample`, `steps.sample`, `spo2.sample`, … |
| `workouts` | month | `activity` (source `withings`) |
| `sleep_summary` | month | `sleep_session`; `sleep.score/duration/deep/light/rem/awake/efficiency/latency/avg_hr/avg_respiration/snoring/apnea_hypopnea_index/wakeup_count` |
| `sleep_series` | day (noon to noon) | `sleep_stage`; wellness Parquet `sleep.hr_sample/rr_sample/snoring_sample/rmssd_sample` |
| `heart` | month | ECG list with every raw signal archived; `heart.afib_ecg`, BPM readings as `bp.*` |
| `devices`, `goals` | snapshot | raw only |

Pulse (meastype 11) is `bp.pulse` when it comes with a blood-pressure reading and `hr.spot`
otherwise (for example, standing on a scale). Where Withings and Garmin both report a
metric, the best-source rules (phase 2) choose which one is shown; both are stored.

### Account data export: not imported (31-10-2026)

Withings' "Download my data" zip holds CSV files (`weight.csv`, `bp.csv`, `sleep.csv`,
`activities.csv`, `raw_hr_hr.csv`, `raw_tracker_*.csv`, `raw_bed_*.csv`, …). For the athlete's
devices (Body+ scale, BP Monitor V2, plus iPhone steps via HealthKit) it adds nothing the
API sync lacks: the API already returned every measure group back to the first weigh-in
(2014, including manual entries and the `comment` field), and activity and intraday
from 2012. The raw `raw_bed_*` and `sleep.csv` files would matter only with a Withings sleep
device or watch, which this account does not have. Revisit if one is added. The Withings
card in Settings says no export is needed.

## Strava (`strava`)

Own thin client for API v3 (`sources/strava/client.py`), which archives Strava's raw JSON.

### Setup

Connect Strava in Settings → Sources. Enter the app ID and secret there if they are not
already saved, then approve the requested `activity:write` scope in the opened Strava tab.
Settings reports the grant returned by Strava. The app secret and rotating tokens are
encrypted server-side; a read-only grant is insufficient for publication.

The API is for **new** activities only (webhook first, then a sync from the last one with a
two-day overlap, never further back than 30 days); `vibesynergy backfill strava` and the History
runs refuse older periods. The past comes from the account archive below, which you upload
under Data → Sources → Strava. Slowly changing datasets (profile, gear, routes, clubs) are
fetched at most once a day (profile every 6 hours) and an activity is only fetched again when
its summary changed, so the daily budget stays for new activities. When Strava asks us to
wait, Sources says so in plain words.

The Strava API app's "Authorization Callback Domain" must be the host of `public_url`
(`server.your-tailnet.ts.net`).

### Datasets and mapping

| Dataset | Kind | Canonical data |
|---|---|---|
| `activities` | per activity | `activity` (source `strava`; description, device, gear, trainer/commute flags, external_id in `extra`), `activity_lap`, `activity_zone_time` |
| `activity_streams` | per activity, fetched once | stream Parquet `parquet/streams/strava-<id>.parquet` (`lat`, `lon`, `dist`, `alt`, `speed`, `hr`, `cadence`, `power`, `temp`, `moving`, `grade`) |
| `profile`, `gear`, `routes`, `starred_segments`, `clubs` | snapshot | raw only |

**Rate limits.** When the 15-minute budget is nearly used, scheduled syncs wait for the
next window. When the daily budget is used, the sync stops with a clear error and continues
on the next run. The last few calls of each budget stay available for a webhook fetch of
one new activity. A hard stop (Strava returns 429, or the budget is actually used up)
pauses those fetches too, until the reset. With
about 3 calls per activity for details plus 1 for streams, a history of 7,000 activities
takes about two weeks of daily runs at the default 2,000 requests a day. Set
`activity_details = false` for a faster first pass that fetches summaries and streams only.

### Account archive (`strava_export`): the history, without the API

strava.com → Settings → My Account → Download or delete your account → Request your archive.
The zip arrives by email; import it as is:

```bash
vibesynergy import strava-export ~/Downloads/export_<id>.zip
```

or upload it on Data → Sources → Strava: resumable (it is ~2 GB), with "Importing: 1,240 of
7,300" while the server works. Activities already here (same Strava id, or the same start
time and distance as a Garmin recording) are recognised; importing twice is harmless.

| Dataset | Canonical data |
|---|---|
| `activities` (`activities.csv`) | `activity` (source `strava_export`, keyed by the Strava id: linked to the API's copy by id and to Garmin's by time; description, private note, gear and file name in `extra`) |
| `files` (originals: FIT, GPX, TCX, gzipped or not) | stream `strava-export-<id>` for that activity, preferred over the API's streams; a FIT file already archived through Garmin or an import is skipped by content hash |

Descriptions and private notes become diary notes (one per Strava activity, updated in place
when the API or a later archive has the same activity). The CSV isn't documented: repeated
columns keep their last (precise) value, older archives with one `Distance` column in km are
converted, and `Activity Date` is read as UTC in English or Dutch month names. Anything not
recognised stays in the raw archive; after a fix, `vibesynergy normalise strava_export` re-maps.

## Instant updates (webhooks)

`vibesynergy serve` receives notifications under `<public_url>/vibesynergy/`:

| Provider | Endpoint | What happens |
|---|---|---|
| Withings | `/vibesynergy/withings/notify/<random token>` | The notified category (weight, BP, activity, sleep, ECG) and dates are re-synced immediately. |
| Strava | `/vibesynergy/strava/webhook` | Activity created: the activity and its streams are fetched. Updated: the activity is fetched again. Deleted: the activity is removed from the canonical tables, and its raw history is kept. |

Handlers only queue a job (priority above scheduled syncs) and reply at once. Strava
activity fetches run on their own worker lane, so a sync that has already started does
not hold a new upload. Every
notification is written to `audit_log`. Withings does not sign notifications, so the URL
carries a random token. Strava events must match the stored subscription id and athlete.
Scheduled syncs (`[schedule]`) remain the safety net if a notification is missed.

## Combining sources (dedup and best source)

Every source keeps its own rows. `vibesynergy.derive.unify` builds two combined tables after
every sync, fetch or import that changed data (or on demand with `vibesynergy derive`):

- **`activity_unified`**: one row per real-world activity. `activity_link` records which
  source rows belong together and why:

  | Rule | Meaning |
  |---|---|
  | `same_id` | Garmin API and Garmin export share the activityId |
  | `fit_file` | an imported FIT file is the file of a Garmin activity |
  | `external_id` | Strava's `external_id` holds the Garmin activityId (`garmin_ping_<id>`) |
  | `filename` | Strava or the archive upload name carries the Garmin activityId (`<id>.fit`, `_<id>.fit`, or a bare id) |
  | `time` | different sources, start within 2 min, duration within 10%, distance within 5%; merged only if every member of both groups agrees and no source appears twice |

  The primary row follows the priority garmin > garmin_export > files > strava > withings.
  Its gaps are filled from the other copies. The title comes from Strava when it differs
  there, because that means it was edited. Streams come from the original FIT file,
  otherwise from Strava.
- **`daily_unified`**: one value per day and metric, taken from the best source that has it.
  Defaults: `body.*` and `bp.*` Withings, then Garmin; `sleep.*` Garmin, then Withings;
  everything else Garmin, then Withings, Strava, files. Settings → System → Best source per
  measurement shows, per metric, the sources that deliver it, the order in use and the source
  the value comes from now, and saves an order for one metric at once (`best-source.json` in
  the data folder, applied at the next derive). That beats the older per-prefix rules in
  the config, which still work and apply after a restart:

  ```toml
  [best_source]
  "sleep." = ["withings", "garmin"]     # e.g. if a Sleep Analyzer is more accurate
  ```

## iCal / CalDAV calendars (`caldav`, opt-in, read-only)

`calendar` uses an uncapped CalDAV VEVENT REPORT against **only explicitly selected HTTPS
collection URLs**. All selected collections must return complete, successful multistatus
responses with ETags and valid iCalendar data before one aggregate snapshot is archived.
There are no PUT/DELETE methods in this source. Personal appointments remain user-owned.
It works with any provider (iCloud, Google, Fastmail, Nextcloud, custom CalDAV servers).
It was called `icloud` before. That source is retired: an old `[sources.icloud]` or
`[schedule] icloud` entry is ignored with one warning, and its leftover sync state and queued
jobs are not reported as problems.

### Setup (portal only)

Setup never happens in the shell or through environment variables: `vibesynergy auth caldav`
only says where to go and exits non-zero. In the portal, **Settings → Calendar**:

1. **Sign in.** Pick the provider (iCloud `https://caldav.icloud.com/`, Fastmail
   `https://caldav.fastmail.com/`, Nextcloud/other with your own HTTPS CalDAV address, or
   Google), enter the username and an **app-specific password**, **Connect**. The server
   discovers the account over HTTPS: the given URL and `/.well-known/caldav`, PROPFIND
   `current-user-principal` → `calendar-home-set` → a Depth-1 listing of collections that are
   calendars and support VEVENT (tasks lists are left out). At most five redirects are
   followed, and credentials are only re-sent to the same host or inside the provider's
   trusted domain (`*.icloud.com` for iCloud's `pNN-caldav.icloud.com`); any other redirect
   is refused. Failures are reported as wrong password, not a CalDAV server, or network error.
2. **Choose calendars.** Tick one or more of the discovered calendars and **Save selection**.
   The server accepts only URLs it discovered for this account (never names) and writes
   `[sources.caldav] enabled = true` and `calendars = [...]` to `config.toml` (non-secret;
   `fetch()` reads that table from the file at every sync, so a changed selection applies
   at the next sync). The running scheduler picks up the selection without a restart.
3. **Connected** shows provider, username, calendars and last sync, with **Change
   calendars**, **Sign in again** and **Disconnect** (deletes the stored credentials and the
   selection; already archived items remain).

**Garmin target calendar** is a separate selector in Settings → Calendar and Data → Sources.
It accepts one of the selected calendar URLs or None and saves immediately as
`[sources.caldav] garmin_target_calendar`. The Garmin `scheduled_workouts` sync reads this
target on each fetch and records it with the fetch request. Changing the selected calendars
or disconnecting clears a target that is no longer selected, and turns copying off with it.
**Copy Garmin workouts onto this calendar** is a separate switch, saved immediately as
`[sources.caldav] mirror_enabled` (default off). While it is off, nothing is written.
While it is on, one VibeSynergy job copies Garmin workouts, events and coach sessions that have
a native id onto that calendar and removes only future copies it owns when Garmin no longer
lists them. Personal appointments are never created, changed or deleted. Google sign-in stays
read-only, so the switch cannot be turned on for Google.

**Google** signs in with OAuth 2.0 (authorization code, PKCE, `state`): enter your own Google
OAuth *Web application* client ID and secret once, register the redirect URI the page shows
(`<portal address>/api/caldav/google/callback`), then **Sign in with Google**. Tokens are
refreshed automatically; CalDAV requests use `Authorization: Bearer` against
`https://apidata.googleusercontent.com/caldav/v2/`. The scope is
`https://www.googleapis.com/auth/calendar.readonly`, the narrowest read-only calendar scope;
Google's CalDAV guide does not list scopes, so if Google rejects it for an account, the broader
`calendar` scope is the fallback (it also permits writes, which VibeSynergy never makes).

**Credentials** live in the secret store (`<config dir>/secrets/caldav/*.enc`), encrypted with
AES-256-GCM using a per-installation key in `<config dir>/keys/secrets.key` (0600, directory
0700, apart from the documents). The page never receives them back, only `connected` /
`password_set` / `client_set` flags. This protects copies and backups of the secrets folder
without the key; anyone who can read the service user's files can read both. Every other
source's credentials use the same store and key.

The mapper expands recurring events and exceptions over the requested range, including
moved/cancelled instances, all-day end-exclusive spans and DST. Floating times use the
configured timezone. A malformed series or more than 10,000 occurrences per resource fails
processing rather than silently truncating it. Old context remains available with an error
in Data & sync. The snapshot preserves resource URLs, UID/recurrence identity, ETags and
original calendar content in the private raw archive; do not paste these into GitHub.

Intervals default to the recent 30 days plus `[calendar] lookahead_months` ahead
(12 months, one year, when that value is unset). Past snapshots remain archived.
Reconciliation removes missing context only within the complete range, including commitments
spanning its start. Future refreshes use the existing worker/scheduler (15 minutes when the
source is enabled). After a complete read, each refresh lists all VEVENT ETags and fetches
only new or changed objects with calendar-multiget. Missing objects are reconciled only
from a complete inventory. Partial multiget responses, failed properties and changing ETags
fail the refresh without replacing the previous archive/context. The existing raw archive
is the restart checkpoint; a new sign-in/discovery or a daily reconciliation forces a full
read. No separate calendar store or scheduler is introduced.

Settings → Calendar also labels each selected collection as appointments, coaching,
planned personal exercise or unclassified. Selection uses the verified collection URL;
edits save directly and update the shared plan context. The label persists across syncs
and selection changes. Verified Garmin mirrors keep their own identity; no classification
creates write authority or converts a planned appointment into measured exercise/load.

Configuration written by the portal (synthetic example; you don't edit it by hand):

```toml
[sources.caldav]
enabled = true
calendars = [{ name = "Workouts", url = "https://calendar.example.test/account/workouts/" }]
```

Remap existing Garmin `scheduled_workouts` archives when adopting pre-existing rows
that lack `native_calendar_key`; matching never falls back to a title/date guess.

Portal, API and MCP use the shared plan query. Personal commitments and unmatched mirrors do
not enter completed-training or adherence totals. Verified legacy mirrors retain their
`@hermes-garmin-v2` UIDs and deterministic resource identity; an exact unique Garmin native key
combines them for presentation. This is read-time identification, **not an adopted write ledger**.
Malformed ownership signals stop processing. Personal context is excluded from existing public
shared Today/Dashboard/Calendar pages. The Calendar page adds a dated agenda with source and
receipt information; the original metric heatmap remains available.

Read-only ingestion plus the guarded mirror (ledger, conditional writes, read-back) are the implementation. Copying stays off until the portal switch is on, and a failed Garmin or
calendar read never deletes. Garmin Coach
workouts are only copied onto the calendar; this source never writes them back to Garmin.

Migration 0019 adds the nullable `pending_write` checkpoint to the existing mirror ledger.
Before each provider mutation the operation, intended fingerprint, date and sequence are
committed while preserving the previous owned fingerprint. A process interruption, timeout,
changed ETag or failed read-back leaves the checkpoint available for recovery. The next complete calendar read settles it without
guessing: the object shows the intended content (verified), still shows the recorded content
(the conditional write or delete is tried again against its current ETag), or is gone
(absent). Any other content stays a conflict and is not overwritten. A delete also needs the
Garmin schedule read to have reached the copy's date, and a ledger row recorded for another
account authorizes nothing until the copy is adopted again under the current account.
Recovery compares the saved intent even when Garmin has a newer revision, then handles
that revision on the next pass. Third-party content remains a conflict.

Garmin write coverage comes only from the latest complete, validated, fully processed sync
receipt, including the actual date range and current raw IDs. Pending mapping, malformed
months, interruptions, stale receipts and historical-only backfills cannot authorize writes;
the cumulative historical `synced_until` is not proof. Garmin collection and mirror writes
share a cross-process lock. Creates/updates are bounded by the receipt; deletion also stays
inside the 183-day planning window. A pre-upgrade database stays non-writable until a new
successful schedule sync records a receipt. Preview projects the post-adoption/recovery
writes in memory and does not mutate the ledger or provider.

For implementation, synthetic validation and remaining live acceptance, see the
[review handoff](archive/CALENDAR_REVIEW_HANDOFF.md).

References: [CalDAV RFC 4791](https://www.rfc-editor.org/rfc/rfc4791),
[icalendar](https://icalendar.readthedocs.io/en/latest/how-to/usage.html),
[recurrence handling](https://recurring-ical-events.readthedocs.io/en/latest/user-guide/examples.html).

## intervals.icu (`intervals_icu`)

intervals.icu is where VibeSynergy puts the active plan, so its workouts reach Zwift and the
other services connected there. Read: calendar events (cached in `icu_event`, migration 0032),
the workout library, and activities for the RPE and Feel recorded there (raw archive, one
payload per month). Fitness, fatigue, form, load and time in zones are VibeSynergy's own
(`query/fitness_model.py`, `query/own_load.py`); intervals.icu's are not read, and its
`wellness` dataset is no longer synced (since 11-11-2026).
Writes (plan events, check-in wellness, RPE/Feel, descriptions) are described under
`query/icu_plan.py`, `query/icu_wellness.py` and `query/activity_effort.py`.

The API key and athlete id are entered on the intervals.icu card under Settings → Sources
and stored encrypted (`secrets/intervals_icu/api.enc`). The page only sees whether a
key is set and the athlete id. Older installs passed `INTERVALS_ICU_API_KEY` and
`INTERVALS_ICU_ATHLETE_ID` through the service environment; `vibesynergy serve` copies them
into the store once at start (and logs that the environment entries can go), and until
then a read still falls back to them with a deprecation warning. The stored key wins.

Turn the source on under Settings → Sources (`[sources.intervals_icu] enabled = true`). A
scheduled sync runs every 60 minutes. The calendar window is 14 days back through 300 days
ahead. Basic auth uses username `API_KEY`; the key is not logged.

The whole calendar is one raw item (`events`, item key `calendar`), and each new version
replaces the previous one's `icu_event` rows. So every events fetch asks for at least that
full span, even when the sync was given fewer days (Refresh today, `vibesynergy_refresh` with a
period); a wider window is kept. A narrow fetch would otherwise leave only those days in the
cache and drop races, later weeks and the last days' workouts until the next full sync
(1 Nov 2026). The raw item stays a complete copy of the plan, so `rebuild` gives the same cache.

Each plan workout's `external_id` is `<date>:<sport>`. Brick parts add `:fiets` (bike) or
`:loop` (run) and share that calendar date as `session_key`. Events with no `external_id`
are cached too, with `source` `other`. Plan events (they have an `external_id`) use
`source` `plan`. The cache key is the intervals.icu event id. `external_id` is indexed.

The AI coach can change that plan through MCP (`plan.write`): create, update, move, delete
and revert one event. A reason is required. The cached version has to match, and the
live event has to match that cache, or the write is refused. Updates use the event id.
`external_id` only finds that id. There is no bulk upsert. Each write stores the before
and after snapshot on the audit log and a line on the coach feed. Revert puts the
before snapshot back through the API. Writes wait about a second between calls.

A Garmin workout with the same name and date as a plan workout is the copy intervals.icu
pushed. It is left out of the plan so it is not counted as Garmin Coach. A Garmin Coach
workout on the same date and sport stays, linked as one pair flagged `dubbel`.
`vibesynergy_icu_resolve_duplicate` deletes the plan event when `keep` is `garmin`, or marks
the Coach session for the athlete when `keep` is `plan`. Garmin Coach is not deleted from here.

A saved morning check-in is also written to intervals.icu wellness for that date
(`PUT /athlete/{id}/wellness/{date}`), unless
`[sources.intervals_icu] checkin_wellness = false`. Only mood, soreness, fatigue,
stress, motivation and sleepQuality are sent, on intervals.icu's 1-4 scale, where
1 is good and 4 is bad for every one of those fields. Soreness and stress compress
directly. Mood, motivation and sleep quality are reversed, because a high VibeSynergy
score is a good day. An empty field is cleared with -1. hrv, restingHR, sleepSecs
and sleepScore are never sent. The
check-in is saved even when intervals.icu does not accept the write; the next sync
tries again. The audit log keeps the previous and new values, and
`vibesynergy_icu_revert` puts the previous values back. VibeSynergy's check-in has no
fatigue score, so fatigue is left out.

**RPE and Feel.** Activities' `icu_rpe` (1-10) and `feel` (1 Strong … 5 Weak) are read
from the raw `activities` archive and shown with their source. A value entered in VibeSynergy
(only when Garmin and intervals.icu have none) is written with `PUT /activity/{id}`
(`icu_rpe`, `feel` only) after reading the activity; a different value there is kept.
Garmin's `directWorkoutRpe` (10-100) and `directWorkoutFeel` (0-100, 100 very strong) are
converted to the same scales. Garmin is not written: `garminconnect` has no call for them.

**Private notes (opt-in, off by default).** intervals.icu has no private activity note:
the description and activity chat follow the activity's visibility. Wellness `comments` can
be private: the athlete setting "Private wellness data" (`private_wellness_keys`) hides the
listed fields from everyone but the athlete and their coaches. With Settings → Sources →
intervals.icu → "Copy my diary notes to intervals.icu" on, the athlete's own notes for a day
(not the Garmin/Strava imports) go to that day's `comments`, and only while `comments` is in
`private_wellness_keys` (read before every write). An existing comment is only replaced
when it is still what VibeSynergy sent last (audit `icu.wellness_comments`). Garmin has no
private note field (the description follows the activity's privacy), so notes stay in
VibeSynergy there. On 31-10-2026 the athlete's intervals.icu profile was private but
`private_wellness_keys` did not include `comments`, so nothing is copied until the athlete adds
Comments there.

## Strava request budget

Strava's known request-budget deadline is checkpointed locally beside its source credentials
(`request_budget.json` contains only a reset timestamp and fixed status text). New clients and
service restarts respect the same deadline; a shorter concurrent deadline cannot shorten an
already known longer wait. Both near-limit response headers and explicit HTTP 429 responses
set it. The stored deadline expires naturally, after which requests resume without an OAuth
reset. A checkpoint write failure preserves the successful provider reply and in-process
backoff, while logging that cross-process persistence failed. This coordinates VibeSynergy's
known deferral; it does not reserve quota against other applications or guarantee freshness
while Strava rejects requests.
