# VibeSynergy at a glance

A short map of the code. Details: [MCP.md](MCP.md) (MCP contract), [EVENTS.md](EVENTS.md)
(agent events), [COACH_PROTOCOL.md](COACH_PROTOCOL.md) (how any AI coaches over MCP),
[AI_HARNESS.md](AI_HARNESS.md) and [COACH_RUNNER.md](COACH_RUNNER.md) (the built-in coach),
[SOURCES.md](SOURCES.md) (sources), [design/README.md](design/README.md) (portal design) and
[../deploy/README.md](../deploy/README.md) (running it).

## Repository

| Path | What is there |
| --- | --- |
| `src/vibesynergy/cli/` | `vibesynergy` command (Typer): `init`, `serve`, `sync`, `db upgrade`, `mcp …` |
| `src/vibesynergy/api/` | HTTP service: webhooks, OAuth server for MCP apps (`oauth.py`) |
| `src/vibesynergy/mcp/` | MCP server (`server.py`, tools in `tools/`), permission scopes (`scopes.py`) |
| `src/vibesynergy/portal/` | Web portal; plain ES modules in `static/`, no build step |
| `src/vibesynergy/query/` | All reads and writes for the portal and MCP go through here |
| `src/vibesynergy/sources/` | One package per source: `garmin`, `intervals_icu`, `strava`, `withings`, `caldav`, `files`, `demo` |
| `src/vibesynergy/sync/` | Job queue, worker and scheduler |
| `src/vibesynergy/coach_runner/` | The AI coach: task registry, context, runner, schedule (off by default) |
| `src/vibesynergy/store/` | SQLite schema (`schema.py`), Alembic migrations, raw archive and blobs |
| `src/vibesynergy/normalize/`, `derive/` | Raw source data to one unified daily and activity view |
| `tests/` | pytest with synthetic data; browser tests with Playwright |
| `deploy/` | systemd unit and how to run it as a service |
| `docs/coach-prompts/` | Generic seeds for the coach's instructions (an install's own go in `<data_dir>/coach-prompts`) |

## Running

- **Ports:** API, webhooks and MCP on `127.0.0.1:8448`; the portal on `127.0.0.1:8447`.
- **Exposure:** optional, through Tailscale Funnel on the path `/vibesynergy` only (MCP at
  `/vibesynergy/mcp`); the portal stays on your own network.
- **Config:** `~/.config/vibesynergy/config.toml`; credentials entered in the portal are
  stored encrypted under the config folder.
- **Updates:** `vibesynergy db upgrade` before restarting the service when a version brings a
  migration; the service refuses to start on an old schema.

## Integrations

- **intervals.icu:** a sync source in `sources/intervals_icu/`. Plan writes (create, update,
  move, delete, revert, each with a reason and an audit snapshot) in `query/icu_plan.py`.
- **Training plan:** VibeSynergy's own model (`query/plan_model.py`), mirrored one way to
  intervals.icu. Exports as CSV, Excel and PDF (`query/plan_export.py`).
- **Garmin:** a sync source in `sources/garmin/`. VibeSynergy delivers planned workouts to
  the Garmin calendar itself (`query/garmin_strength.py`, `query/garmin_endurance.py`).
- **Knowledge base:** `query/knowledge.py` (versioned articles, rules and sources, full-text
  search). A proposal changes an article only once the athlete approves it.

## App permissions

Each MCP app (OAuth client or personal access token) has its own scopes, see
`mcp/scopes.py`. They are managed in the portal under Settings → Sharing → Coach apps, or:

```bash
vibesynergy mcp clients                                   # OAuth apps
vibesynergy mcp scopes <client_id> --add plan.write       # an OAuth app; live tokens follow
vibesynergy mcp token scopes <name> --remove plan.write   # a personal access token
```
