# VibeSynergy

Your own health and training archive. VibeSynergy collects your data from Garmin, Withings,
Strava, intervals.icu, CalDAV calendars and activity files into one local archive. You see
it in a web portal, and any AI app can read it through a standard MCP server. An optional
AI coach runs inside VibeSynergy on your own API key.

Everything runs on your own machine. Your data stays in your data folder and never leaves
it unless you connect a service yourself.

- Sources and what each one collects: [docs/SOURCES.md](docs/SOURCES.md)
- The MCP interface for AI apps: [docs/MCP.md](docs/MCP.md)
- The AI coach: [docs/AI_HARNESS.md](docs/AI_HARNESS.md), [docs/COACH_RUNNER.md](docs/COACH_RUNNER.md)
- Running it as a service: [deploy/README.md](deploy/README.md)
- Map of the code: [docs/OVERVIEW.md](docs/OVERVIEW.md)

Status: alpha. Expect changes between versions.

## Requirements

- Linux or macOS
- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) (recommended) or pipx

## Install

```bash
uv tool install git+https://github.com/rvartanov/vibesynergy
```

Or from a checkout:

```bash
git clone https://github.com/rvartanov/vibesynergy
cd vibesynergy
uv tool install .
```

## First run

```bash
vibesynergy init --email you@example.com   # config file, data folder, database and your login
```

`init` prints a one-time link (valid 24 hours) that logs you in for the first time; there you
choose your password and, if you like, a second step (a code by mail or an authenticator app).
Lost the link? `vibesynergy login-link` makes a new one.

`init` writes `~/.config/vibesynergy/config.toml` (or `$XDG_CONFIG_HOME/vibesynergy`). Data
goes to `~/.local/share/vibesynergy` (or `$XDG_DATA_HOME/vibesynergy`).

To look around without any account, enable the demo source (synthetic data) in the
config file:

```toml
[sources.demo]
enabled = true
```

Then:

```bash
vibesynergy sync      # collect data from the enabled sources
vibesynergy serve     # the service, with the portal on http://127.0.0.1:8447
```

Open the link from `init` (or http://127.0.0.1:8447) in your browser.

## Connect your accounts

Enable a source in the config file and log in once. VibeSynergy only stores tokens, and
stores them encrypted:

```toml
[sources.garmin]
enabled = true
```

```bash
vibesynergy auth garmin                           # once
vibesynergy backfill garmin --start 2015-01-01    # history, month by month
vibesynergy import garmin-export <export.zip>     # a Garmin account export
vibesynergy import strava-export <export.zip>     # a Strava account archive
vibesynergy import files <folder>                 # FIT, GPX or TCX files
```

Withings, Strava, intervals.icu and CalDAV are set up the same way or in the portal under
Settings. See [docs/SOURCES.md](docs/SOURCES.md) for each source.

## Connect an AI app

Any MCP client (Claude, ChatGPT, a local model, …) connects in the portal under
**Settings → Coach apps**. Set a passphrase, give the app the MCP address, and approve it on
the consent page. You choose per app what it may do: `read`, `read.expert`, `diary.write`,
`coach.write`, `plan.propose`, `plan.write`, `sync`, `publish` and `sql`. Writing to your
plan, publishing and free-form queries stay off unless you turn them on.

Personal access tokens are made on the same page and always expire (30, 90 or 365 days).

## The AI coach

The coach runs inside VibeSynergy on your own API key (Anthropic, OpenAI, xAI or a local
server) and is off until you switch it on in **Coach → Settings**. Each task (morning
advice, week review, race brief, and more) has its own switch, schedule and model. It
fills in sessions within the plan you set up; changing planned sessions, thresholds or the
knowledge base waits for your approval. See [docs/AI_HARNESS.md](docs/AI_HARNESS.md).

## Dates and times

Dates are shown day-month-year (`d-m-yyyy`), clock times on 24 hours, in the time zone from
`[user] timezone`. The portal is in Dutch or English.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run basedpyright
uv run pytest -n 4
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the conventions. Tests use synthetic data only;
never commit real exports, FIT files or tokens.

## Licence

MIT, see [LICENSE](LICENSE). Third-party code and assets: [THIRD_PARTY.md](THIRD_PARTY.md).
