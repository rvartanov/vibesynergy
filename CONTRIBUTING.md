# Contributing

## Checks before a pull request

```bash
uv run ruff check . && uv run ruff format --check .
uv run basedpyright          # query/ and mcp/, against .basedpyright/baseline.json
uv run pytest -n 4
```

Portal changes also run the browser tests (they skip silently without Playwright):

```bash
uv run --with playwright pytest -q tests/test_portal_a11y.py tests/test_portal_polish.py
```

and the JavaScript checks: `node --check` on every module, `node --test tests/*.test.mjs`,
`node tools/check_*.mjs` and `uv run python docs/design/check_design.py`. CI runs all of
these. Docs-only changes: check links and `git diff --check`.

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
