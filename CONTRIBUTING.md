# Contributing

## Checks before a pull request

```bash
uv run ruff check . && uv run ruff format --check .
uv run basedpyright          # query/ and mcp/, against .basedpyright/baseline.json
uv run pytest -n 4
```

Portal changes also run the browser tests (they skip without Playwright or Chrome; set
`VIBESYNERGY_A11Y_REQUIRED=1` to make that a failure, as CI does). These are the browser
steps of `.github/workflows/ci.yml`, one command per step:

```bash
VIBESYNERGY_A11Y_REQUIRED=1 uv run --with playwright pytest tests/test_portal_a11y.py tests/test_calendar_settings_a11y.py tests/test_coach_runner_a11y.py tests/test_login_a11y.py tests/test_bug_report_a11y.py tests/test_future_day_a11y.py tests/test_workout_page_a11y.py tests/test_workout_gear_a11y.py tests/test_workout_media_a11y.py -q
VIBESYNERGY_A11Y_REQUIRED=1 uv run --with playwright pytest tests/test_language_appearance.py tests/test_portal_polish.py -q
VIBESYNERGY_A11Y_REQUIRED=1 uv run --with playwright pytest tests/test_dashboard_phone.py tests/test_alert_click_dismiss.py tests/test_chart_fit_browser.py tests/test_first_load_browser.py -q
VIBESYNERGY_A11Y_REQUIRED=1 uv run --with playwright pytest tests/test_settings_tabs.py tests/test_start_page_browser.py tests/test_portal_quicklog.py -q
VIBESYNERGY_A11Y_REQUIRED=1 uv run --with playwright --with radicale pytest tests/test_caldav_radicale_e2e.py -q
```

CI runs every browser test file, and `tests/test_browser_tests_in_ci.py` fails when one is
missing from `.github/workflows/ci.yml` or from the commands above. They share
`tests/browser_helpers.py`: Chrome, axe-core (cached under `~/.cache/vibesynergy-tests`) and
throwaway servers on ports picked while the test runs, so two test runs can go side by side.

Then the JavaScript checks: `node --check` on every module, `node --test tests/*.test.mjs`,
`node tools/check_*.mjs` and `uv run python docs/design/check_design.py`. CI runs all of
these (`check_zone_presentation.mjs` and `check_dashboard_refresh.mjs` inside `pytest`, the
other checks as their own steps). Docs-only
changes: check links and `git diff --check`.

Small pull requests, one theme each; say in the description which checks you ran.

## Conventions

- The portal and the MCP server go through `src/vibesynergy/query` only. Do not add another
  health store, coach or scheduler next to the existing ones.
- Every database change is an Alembic migration that also updates `store/schema.py` (a test
  compares them) and the revision pinned in `tests/test_cli.py`.
- Portal pages are plain ES modules without a build step; charts and maps are vendored
  (ECharts, MapLibre). The content security policy forbids `eval`.
- User-facing strings are Dutch and English through i18n (`portal/static/i18n`), never
  hard-coded. Dates are `d-m-yyyy`, times 24-hour, through `vibesynergy.core.user_datetime`.
- Setting up an integration (sources, calendars, mail, webhooks) goes through the portal, not
  the shell. Credentials are stored encrypted and never sent back to the page.
- External writes (intervals.icu, Garmin, calendars) need verified ownership, retries and a
  read-back, and never touch the athlete's own entries.
- Tests use synthetic data only. Never commit real exports, FIT files, tokens or personal
  data.

## Communication rule

Everything an athlete or user reads (coach advice, UI text, help, mail), in Dutch and
English alike:

1. **Advice or conclusion first:** what it means and what to do now.
2. **Plain coach language**, without technical terms or an explanation of how the system
   works.
3. **At most one short caveat**, and only if it changes the advice.
4. **Numbers turned into meaning** ("well recovered") instead of a list of values.
5. **Short.**

Evidence, versions and provenance stay internal (logs, MCP, API) or in a collapsed
"details" element.

## UI

Edits save directly; never ask for a reason for a change. UI work follows Nielsen Norman
Group's usability heuristics and WCAG 2.2 AA, with design tokens from
`portal/static/css/design-tokens.css` (see `docs/design/README.md`). Before merging, run an
axe check and a keyboard-only pass. A UI pull request lists the guidelines it applied and
any deviation with its reason.

## Bugs and ideas

Report them through the issue forms (bug report, feature request). What they need, and what
happens next: [docs/REPORTING.md](docs/REPORTING.md).

## Versions and changelog

[docs/FEATURES.md](docs/FEATURES.md) lists everything VibeSynergy does; [CHANGELOG.md](CHANGELOG.md)
says what changed, per version. Both are part of the change itself, in the same pull request.

**Versions** (#140, the model of RunSynergy): `0.1.0-beta.0.1`, `0.1.0-beta.0.2`, … Every
deploy that brings merged pull requests is a release, and every release adds one to the last
number. A next minor (`0.2.0-beta.0.1`) only when the owner says so; out of beta it becomes
`1.0.0`. The numbers live in `src/vibesynergy/releases.py`; `pyproject.toml` keeps a PEP 440
version for the line (`0.1.0b0`), because `0.1.0-beta.0.1` is not one. Never write a version
number or a release by hand.

In a pull request:

- **Changelog**: anything you or an AI app notice gets one line at the end of the list under
  `## Unreleased`, in plain language (what it does for you, not which file changed), with the
  pull request or issue number.
- **What changed** (only for a change that noticeably changes use): one or more `Item`s at the
  end of `ITEMS` in `src/vibesynergy/releases.py`: an id (`"<number>-<word>"`), the audience
  (`"you"`, the person using the portal, or `"ai"`, AI coaches and AI apps, for MCP and
  harness changes) and a short English text by the communication rule, with its Dutch in
  `portal/static/i18n/nl.json` (`en.json` only holds the few texts whose English is not the
  key itself). Items for `you` show once in the "What changed" card after the update and
  then under Waiting for you → Notices; items for `ai` go into `vibesynergy_coach_handbook`
  (`what_changed`) and the MCP server information. Small fixes get no items.
- **Features**: a feature that is added, changed or removed is updated in FEATURES.md, in its
  area.
- **Internal only** (refactor, tests, CI, docs for developers): add the label `no-changelog`.

**A release** is made by `vibesynergy update` (and **Update now**) when it installs new
commits: on a fresh clone of `main` it puts the next number with the day's date on top of
`RELEASES` (with every item no release has yet), renames `## Unreleased` in CHANGELOG.md to
`## <version> – <d-m-yyyy>` under a new empty `## Unreleased`, commits that as
`Release <version>`, tags it `v<version>` (the container workflow publishes it) and pushes
both, then installs that commit. It prints `0.1.0-beta.0.3 → 0.1.0-beta.0.4` and the items.
Several pull requests deployed at once make one release. If `main` already carries a version
tag (an update that failed after the release), it installs that version without a new number.
It needs the right to push to the repository; an install without it updates as before. An open
pull request may then get a conflict in CHANGELOG.md: keep your line under `## Unreleased`.

CI fails a pull request that changes `src/`, `Dockerfile` or `deploy/` without touching
CHANGELOG.md, unless it has the `no-changelog` label.
