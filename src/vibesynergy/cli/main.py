"""``vibesynergy`` command line."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.exc import IntegrityError

from vibesynergy import __version__
from vibesynergy.core.config import (
    ConfigError,
    default_config_path,
    detect_timezone,
    load_settings,
    write_default_config,
)
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.runtime import Runtime
from vibesynergy.sources.registry import SourceError
from vibesynergy.store import db
from vibesynergy.sync.engine import DatasetResult

app = typer.Typer(help="VibeSynergy: Personal Health & Fitness Store.", no_args_is_help=True)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
app.add_typer(db_app, name="db")
import_app = typer.Typer(help="Import files: account exports, old FIT files.", no_args_is_help=True)
app.add_typer(import_app, name="import")
webhooks_app = typer.Typer(help="Instant updates from Withings and Strava.", no_args_is_help=True)
app.add_typer(webhooks_app, name="webhooks")
mcp_app = typer.Typer(help="MCP server for AI apps: run, connector address, access.")
app.add_typer(mcp_app, name="mcp")
mcp_token_app = typer.Typer(help="Personal access tokens for MCP (scripts and agents).")
mcp_app.add_typer(mcp_token_app, name="token")
tiles_app = typer.Typer(help="Map tiles cached for the portal.", no_args_is_help=True)
app.add_typer(tiles_app, name="tiles")
coaching_app = typer.Typer(help="Coaching activity classification.", no_args_is_help=True)
app.add_typer(coaching_app, name="coaching")
kb_app = typer.Typer(help="Coach knowledge base.", no_args_is_help=True)
app.add_typer(kb_app, name="kb")
plan_app = typer.Typer(help="The training plan as VibeSynergy's own model.", no_args_is_help=True)
app.add_typer(plan_app, name="plan")
load_app = typer.Typer(help="VibeSynergy's own training load.", no_args_is_help=True)
app.add_typer(load_app, name="load")
jobs_app = typer.Typer(help="The job queue: recent jobs, and pruning old ones.")
app.add_typer(jobs_app, name="jobs")
secrets_app = typer.Typer(help="The credential store.", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")
console = Console()

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Config file (default: VIBESYNERGY_CONFIG).")
]


def _runtime(config: Path | None, *, require_schema: bool = True) -> Runtime:
    try:
        settings = load_settings(config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    rt = Runtime.open(settings)
    if require_schema and not rt.schema_is_current():
        console.print("[red]Database schema is not current.[/] Run [bold]vibesynergy init[/].")
        raise typer.Exit(1)
    return rt


def _score_open_predictions(rt: Runtime) -> None:
    """Score forecasts whose target day is already stored. A miss does not stop startup."""
    try:
        rt.score_predictions()
    except Exception:
        logging.getLogger(__name__).exception("scoring coach predictions failed")


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/]")
    raise typer.Exit(1)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command()
def version() -> None:
    """Show the installed version."""
    console.print(__version__)


@app.command()
def init(config: ConfigOpt = None) -> None:
    """Create the config file (if missing), data directories and database schema."""
    path = config or default_config_path()
    if not path.exists():
        write_default_config(path, detect_timezone())
        console.print(f"Created config {path}")
    rt = _runtime(path, require_schema=False)
    for directory in (rt.settings.data_dir, rt.settings.blob_dir, rt.settings.db_path.parent):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    rt.migrate()
    console.print(f"Data directory {rt.settings.data_dir}")
    console.print(f"Database schema at revision {db.current_revision(rt.engine)}")


def _seconds(value: float | None) -> str:
    return f"{value:.0f}s" if value is not None else "—"


@app.command()
def latency(config: ConfigOpt = None, limit: int = 20) -> None:
    """List recent workouts and passive finish-to-dashboard delivery times."""
    from vibesynergy.query import latency as latency_q

    rt = _runtime(config)
    table = Table(
        "source",
        "id",
        "finished",
        "notice",
        "fetched",
        "processed",
        "dashboard",
        "vibesynergy",
        "total",
    )
    for item in latency_q.recent(rt.engine, limit):
        delays = item["delays_s"]
        table.add_row(
            item["source"],
            item["source_id"],
            item["finished_at"] or "—",
            item["received_at"] or "—",
            item["fetched_at"] or "—",
            item["processed_at"] or "—",
            item["dashboard_at"] or "—",
            _seconds(delays.get("vibesynergy_s")),
            _seconds(delays.get("total_s")),
        )
    console.print(table)
    console.print(
        "vibesynergy: webhook receipt → activity available. "
        "total: workout finish → first dashboard query. "
        "Provider upload before the webhook is outside VibeSynergy. — means not observed."
    )
    rt.close()


@app.command()
def status(config: ConfigOpt = None) -> None:
    """Show configuration, sources, sync state, archive size and jobs."""
    rt = _runtime(config)
    s = rt.settings
    console.print(f"[bold]VibeSynergy {__version__}[/]")
    console.print(f"config   {s.config_path}")
    console.print(f"data     {s.data_dir}")
    console.print(f"schema   {db.current_revision(rt.engine)}")
    console.print(f"timezone {s.timezone}, units {s.units}")

    sources = Table("source", "enabled", "datasets", title="Sources", title_justify="left")
    for sid, cls in rt.registry.available().items():
        enabled = sid in rt.registry.enabled_ids()
        specs = rt.registry.get(sid, require_enabled=False).datasets()
        names = ", ".join(d.name for d in specs) if len(specs) <= 4 else f"{len(specs)} datasets"
        sources.add_row(f"{sid} ({cls.name})", "yes" if enabled else "no", names)
    console.print(sources)

    states = Table(
        "dataset", "synced", "last success", "error", title="Sync state", title_justify="left"
    )
    for st in rt.sync.states():
        synced = f"{st['synced_from']}..{st['synced_until']}" if st["synced_until"] else "-"
        states.add_row(
            f"{st['source']}.{st['dataset']}",
            synced,
            _fmt_time(st["last_success_at"]),
            (st["last_error"] or "")[:60],
        )
    console.print(states)

    raw = Table(
        "dataset",
        "items",
        "versions",
        "size",
        "map errors",
        title="Raw archive",
        title_justify="left",
    )
    for r in rt.raw.stats():
        raw.add_row(
            f"{r['source']}.{r['dataset']}",
            str(r["items"]),
            str(r["versions"]),
            _fmt_bytes(r["bytes"] or 0),
            str(r["errors"]),
        )
    console.print(raw)

    from vibesynergy import history

    runs = history.fills(rt)
    if runs:
        fills = Table("#", "source", "period", "status", "progress", title="History fills")
        for f in runs[:10]:
            period = f"{f['start']}..{f['end']}" if f["method"] == "api" else f["file"] or ""
            progress = f"{f['done']}/{f['total']} months" if f["total"] else ""
            fills.add_row(str(f["id"]), f["name"], period[-40:], f["status"], progress)
        console.print(fills)

    counts = rt.jobs.counts()
    console.print(
        "jobs     " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none")
    )


@app.command()
def sources(config: ConfigOpt = None) -> None:
    """List available source plugins and their datasets."""
    rt = _runtime(config, require_schema=False)
    table = Table("source", "dataset", "kind", "description")
    for sid in rt.registry.available():
        for spec in rt.registry.get(sid, require_enabled=False).datasets():
            table.add_row(sid, spec.name, spec.kind.value, spec.description)
    console.print(table)


@app.command()
def sync(
    source: Annotated[str | None, typer.Argument(help="Source id; default: all enabled.")] = None,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-d")] = None,
    start: Annotated[str | None, typer.Option(help="First date (YYYY-MM-DD).")] = None,
    end: Annotated[str | None, typer.Option(help="Last date (default today).")] = None,
    days: Annotated[int | None, typer.Option(help="Last N days, instead of start/end.")] = None,
    queue: Annotated[
        bool, typer.Option(help="Enqueue for the worker instead of running now.")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Collect data. Without a date range: incremental since the last sync."""
    rt = _runtime(config)
    window = _window(rt, start, end, days)
    if window:
        from vibesynergy import history as _history

        for sid in [source] if source else rt.registry.enabled_ids():
            if reason := _history.api_history_refused(
                sid, window.start, today_in(rt.settings.timezone)
            ):
                _fail(reason)
    source_ids = [source] if source else rt.registry.enabled_ids()
    if not source_ids:
        _fail("No sources enabled. Enable one under [sources.<id>] in the config file.")
    if dataset and len(source_ids) != 1:
        _fail("--dataset needs a single source.")

    if queue:
        for sid in source_ids:
            for ds in dataset or [None]:
                payload = {"source": sid, "dataset": ds, "window": str(window) if window else None}
                job_id = rt.jobs.enqueue("sync", payload, dedupe_key=f"sync:{sid}:{ds}:{window}")
                console.print(
                    f"queued {sid}.{ds or '*'}: "
                    + (f"job {job_id}" if job_id else "already queued")
                )
        return

    failed = False
    results: list[DatasetResult] = []
    for sid in source_ids:
        try:
            results.extend(rt.sync.sync(sid, dataset, window))
        except (SourceError, KeyError) as exc:
            _fail(str(exc))
    failed = _print_results(results)
    if failed:
        raise typer.Exit(1)


@app.command()
def backfill(
    source: Annotated[str, typer.Argument(help="Source id.")],
    start: Annotated[str, typer.Option(help="First date (YYYY-MM-DD).")],
    end: Annotated[str | None, typer.Option(help="Last date (default today).")] = None,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-d")] = None,
    background: Annotated[
        bool,
        typer.Option(
            help="Let the service fill it (resumes after restarts, waits out rate limits; "
            "progress on Data & sync) instead of running here."
        ),
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Collect history month by month, oldest first, so progress is kept if it stops.

    Re-running is cheap: unchanged items create no new versions and original files are
    not downloaded again. Stops when every dataset of a month fails (login or rate limit).
    """
    rt = _runtime(config)
    window = _window(rt, start, end, None)
    assert window is not None
    from vibesynergy import history as _history

    if reason := _history.api_history_refused(source, window.start, today_in(rt.settings.timezone)):
        _fail(reason)
    if background:
        from vibesynergy import history

        try:
            fill = history.start_api(
                rt,
                source,
                window.start,
                window.end,
                today=today_in(rt.settings.timezone),
                datasets=dataset or None,
            )
        except (ValueError, SourceError) as exc:
            _fail(str(exc))
        console.print(
            f"Filling {source} from {fill['start']} to {fill['end']} ({fill['total']} months) "
            "in the background; follow it on Data & sync or with `vibesynergy status`."
        )
        return
    try:
        specs = (
            [rt.registry.get(source).dataset(d) for d in dataset]
            if dataset
            else rt.registry.get(source).sync_datasets()
        )
    except (SourceError, KeyError) as exc:
        _fail(str(exc))
    history = [s for s in specs if s.kind.value != "snapshot"]
    for month in window.months():
        chunk = Window(max(month.start, window.start), min(month.end, window.end))
        results = rt.sync.sync(source, [s.name for s in history], chunk)
        errors = sum(r.error is not None for r in results)
        new = sum(r.new_versions for r in results)
        console.print(f"{chunk}: {len(results) - errors} ok, {errors} failed, {new} new items")
        if results and errors == len(results):
            _print_results(results)
            _fail("Every dataset failed; stopping. Fix the error and re-run the same command.")


@app.command()
def auth(source: str, config: ConfigOpt = None) -> None:
    """Log in to a source; only tokens are stored (under the config directory)."""
    if source == "caldav":
        # Integration setup goes through the portal, never the shell: nothing is prompted.
        _fail("iCal / CalDAV calendars are set up in the portal: Settings → Calendar.")
    rt = _runtime(config, require_schema=False)
    try:
        rt.registry.get(source, require_enabled=False).authenticate()
    except SourceError as exc:
        _fail(str(exc))
    console.print(f"Logged in to {source}.")
    _offer_history(rt, source)


def _say(text: str, *_counts: int | None) -> None:
    """A progress line (imports also pass done and total, which are for pages)."""
    console.print(text)


def _offer_history(rt: Runtime, source: str) -> None:
    """After a first login: offer to fill the source's history in the background."""
    from vibesynergy import history

    if not sys.stdin.isatty() or not rt.schema_is_current() or source in history.API_HISTORY_OFF:
        return
    try:
        if source not in rt.registry.enabled_ids() or any(
            f["source"] == source for f in history.fills(rt)
        ):
            return
        today = today_in(rt.settings.timezone)
        offer = history.suggest(rt, source, today)
    except Exception:  # an offer only; the login itself worked
        return
    if offer is None:
        return
    question = (
        f"Fill {source}'s history from {offer['start']} to {offer['end']} "
        f"({offer['months']} months) in the background?"
    )
    if typer.confirm(question, default=True):
        fill = history.start_api(
            rt,
            source,
            date.fromisoformat(offer["start"]),
            date.fromisoformat(offer["end"]),
            today=today,
        )
        console.print(
            f"Started (run {fill['id']}); the service fetches it month by month. "
            "Follow it on Data & sync, or change the period there."
        )


@import_app.command("garmin-export")
def import_garmin_export(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Export zip.")],
    config: ConfigOpt = None,
) -> None:
    """Import the Garmin account data export zip (all files; FIT files by content)."""
    from vibesynergy import imports

    rt = _runtime(config)
    results = imports.import_garmin_export(rt, path, progress=_say)
    if _print_results(results):
        raise typer.Exit(1)


@import_app.command("strava-export")
def import_strava_export(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Archive zip.")],
    config: ConfigOpt = None,
) -> None:
    """Import the Strava account archive zip (activities.csv and the original files)."""
    from vibesynergy import imports

    rt = _runtime(config)
    try:
        results = imports.import_strava_export(rt, path, progress=_say)
    except ValueError as exc:
        _fail(str(exc))
    if _print_results(results):
        raise typer.Exit(1)


@import_app.command("files")
def import_files(
    path: Annotated[Path, typer.Argument(exists=True, help="A FIT file, zip or directory.")],
    config: ConfigOpt = None,
) -> None:
    """Import FIT files (e.g. from old head units); files already archived are skipped."""
    from vibesynergy import imports

    rt = _runtime(config)
    if _print_results([imports.import_files(rt, path, progress=_say)]):
        raise typer.Exit(1)


def _move_icu_key_from_env(rt) -> None:
    """One-time step: the intervals.icu key moves from the environment to the store."""
    from vibesynergy.sources.intervals_icu import credentials

    try:
        if credentials.migrate_from_env(rt.settings.secrets_dir):
            console.print(
                "intervals.icu: the API key from the environment is now stored encrypted; "
                "remove it from the service environment"
            )
    except Exception as exc:  # never stop the service over this
        console.print(f"intervals.icu: could not move the API key from the environment: {exc}")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port (the Funnel forwards /vibesynergy here).")] = 8448,
    worker: Annotated[bool, typer.Option(help="Run queued jobs in this process.")] = True,
    schedule: Annotated[bool, typer.Option(help="Queue periodic syncs ([schedule]).")] = True,
    portal: Annotated[
        bool, typer.Option(help="Serve the web portal on its own local port ([portal]).")
    ] = True,
    config: ConfigOpt = None,
) -> None:
    """Run the service: webhook receivers, the job worker, the scheduler and the portal.

    The portal listens on its own port on 127.0.0.1 (default 8447), separate from the port
    the Funnel forwards to, so it is only reachable through `tailscale serve` on the tailnet.
    """
    import logging
    import os
    import threading

    import uvicorn

    from vibesynergy.api.access_log import log_config
    from vibesynergy.api.app import create_app
    from vibesynergy.cli.server import ServiceServer
    from vibesynergy.portal.app import create_portal_app, portal_enabled, portal_port
    from vibesynergy.sync.scheduler import Scheduler

    rt = _runtime(config)
    _score_open_predictions(rt)
    _move_icu_key_from_env(rt)
    stop = threading.Event()
    threads = []
    portal_server = None
    if worker:
        from vibesynergy import history
        from vibesynergy.sync.worker import recover_stale_jobs

        if recovered := recover_stale_jobs(rt.jobs):
            console.print(f"Re-queued {recovered} job(s) a previous run left running")

        if stopped := history.cancel_api_history(rt):
            console.print(f"Cancelled {stopped} Strava API history run(s); nothing is deleted")
        if resumed := history.resume_archives(rt):
            console.print(f"Continuing {resumed} archive import(s) a restart cut off")
        # Same durable queue and scheduler. Assessment, archive import and Strava
        # webhook fetches each have a lane, so none of those waits on the others.
        # A source sync that has already started still runs to completion.
        from vibesynergy.sync import lanes

        for lane in ("sync", "webhook", "archive", "coach"):
            thread = threading.Thread(
                target=rt.worker(lane=lane).run, kwargs={"stop": stop}, name=f"vibesynergy-{lane}"
            )
            lanes.register(lane, thread)
            threads.append(thread)
    if schedule:
        from vibesynergy.coach_runner import schedule as coach_schedule
        from vibesynergy.query import (
            agent_events,
            calendar_mirror,
            garmin_strength,
            publication_auto,
            publication_mirror,
            quote_sources,
        )

        scheduler = Scheduler(
            rt.settings,
            rt.registry,
            rt.jobs,
            services={
                # Strava -> Garmin mirror watches: due ones are checked every 5 minutes.
                publication_mirror.JOB: lambda: publication_mirror.due(rt),
                # Garmin → Strava: new activities, when Settings → Sync switched it on.
                publication_auto.JOB: lambda: publication_auto.due(rt),
                # Garmin → CalDAV mirror. Off until Settings → Calendar turns it on.
                calendar_mirror.JOB: lambda: calendar_mirror.due(rt),
                # Strength and pilates with exercise steps to Garmin.
                garmin_strength.JOB: lambda: garmin_strength.due(rt),
                # Quotes from Wikiquote, Quotable and ZenQuotes when a theme runs low.
                quote_sources.JOB: lambda: quote_sources.due_service(rt),
                # Agent events: debounced activity.ready and webhook deliveries.
                agent_events.JOB: lambda: agent_events.due(rt),
                # API coach runner: morning, evening, weekly. Off until Coach → Settings.
                coach_schedule.SCHEDULE_JOB: lambda: coach_schedule.due(rt),
            },
        )
        rt.scheduler = scheduler
        threads.append(threading.Thread(target=scheduler.run, args=(stop,)))
    if portal and portal_enabled(rt.settings):
        if portal_port(rt.settings) == port:
            _fail("[portal] port must differ from the service port (the Funnel forwards that).")
        portal_server = uvicorn.Server(
            uvicorn.Config(
                create_portal_app(rt),
                host="127.0.0.1",
                port=portal_port(rt.settings),
                log_level="info",
                log_config=log_config(),
                timeout_graceful_shutdown=3,
            )
        )
        # Not the main thread, so uvicorn leaves signal handling to the main server; the
        # thread is a daemon and ends with the process.
        threads.append(threading.Thread(target=portal_server.run, name="portal"))
        console.print(f"Portal on http://127.0.0.1:{portal_port(rt.settings)} (tailnet only)")
    for thread in threads:
        thread.daemon = True
        thread.start()
    server = ServiceServer(
        uvicorn.Config(
            create_app(rt),
            host=host,
            port=port,
            log_level="info",
            log_config=log_config(),
            timeout_graceful_shutdown=3,
        ),
        stop,
    )
    server.portal_server = portal_server
    try:
        server.run()
    finally:
        stop.set()
        if portal_server is not None:
            portal_server.should_exit = True
        for thread in threads:
            thread.join(timeout=0.5)
        rt.close()
        if server._exit_watchdog_started and any(
            thread.is_alive() and not thread.daemon and thread is not threading.current_thread()
            for thread in threading.enumerate()
        ):
            # Python's normal interpreter exit joins AnyIO's synchronous workers.
            # The database is closed and logging is flushed before bypassing that join.
            logging.shutdown()
            os._exit(0)


@mcp_app.callback(invoke_without_command=True)
def mcp(
    ctx: typer.Context,
    url: Annotated[bool, typer.Option(help="Print the connector address instead.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Run the MCP server over stdio (for local clients), or print the connector address.

    The address (--url) is safe to share: apps must be approved with your passphrase.
    """
    if ctx.invoked_subcommand:
        return
    rt = _runtime(config)
    base = rt.settings.public_url or "http://127.0.0.1:8448"
    if url:
        if not rt.settings.public_url:
            _fail("Set [server] public_url first; OAuth needs the public address.")
        print(f"{base}/vibesynergy/mcp")
        if not SecretStore(rt.settings.secrets_dir, "mcp").get("passphrase"):
            console.print(
                "[yellow]No passphrase yet. Set it in the portal: Settings → Coach apps.[/]"
            )
        return
    from vibesynergy.mcp.identity import reset_connection, set_connection
    from vibesynergy.mcp.server import build_server

    _score_open_predictions(rt)
    tokens = set_connection("stdio", "user")
    try:
        build_server(rt).run("stdio")
    finally:
        reset_connection(tokens)


@mcp_app.command("set-passphrase")
def mcp_set_passphrase(config: ConfigOpt = None) -> None:
    """Set (or change) the passphrase that approves apps. Existing approvals stay valid."""
    import getpass

    from vibesynergy.api import oauth

    rt = _runtime(config, require_schema=False)
    first = getpass.getpass(f"New passphrase (at least {oauth.MIN_PASSPHRASE} characters): ")
    if getpass.getpass("Again: ") != first:
        _fail("The two entries differ; nothing changed.")
    try:
        oauth.set_passphrase(SecretStore(rt.settings.secrets_dir, "mcp"), first)
    except ValueError as exc:
        _fail(str(exc))
    console.print("Passphrase set.")


@mcp_app.command("clients")
def mcp_clients(config: ConfigOpt = None) -> None:
    """List the apps that registered, and whether they were approved or revoked."""
    from vibesynergy.api import oauth

    rt = _runtime(config)
    table = Table("client id", "name", "returns to", "registered", "approved", "revoked")
    for c in oauth.list_clients(rt.engine):
        table.add_row(
            c["client_id"],
            c["name"] or "-",
            c["redirect"],
            _fmt_time(c["created"]),
            _fmt_time(c["approved"]),
            _fmt_time(c["revoked"]),
        )
    console.print(table)


@mcp_app.command("revoke")
def mcp_revoke(client_id: str, config: ConfigOpt = None) -> None:
    """Withdraw an app's access immediately (its tokens stop working)."""
    from vibesynergy.api import oauth

    rt = _runtime(config)
    if not oauth.revoke_client(rt.engine, client_id):
        _fail(f"No active app with id {client_id}; see `vibesynergy mcp clients`.")
    console.print(f"Revoked {client_id}.")


@mcp_app.command("scopes")
def mcp_scopes(
    client_id: str,
    add: Annotated[
        list[str] | None,
        typer.Option("--add", help="Permission to add. Repeat to add more."),
    ] = None,
    remove: Annotated[
        list[str] | None,
        typer.Option("--remove", help="Permission to remove. Repeat to remove more."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Change an approved app's permissions. Its live tokens follow at once."""
    from vibesynergy.api import oauth

    rt = _runtime(config)
    try:
        scopes = oauth.update_oauth_client_scopes(rt.engine, client_id, add=add, remove=remove)
    except ValueError as exc:
        _fail(str(exc))
    console.print(" ".join(scopes))


@mcp_token_app.command("create")
def mcp_token_create(
    name: str,
    days: Annotated[int, typer.Option(help="30, 90 or 365 days. There is no never.")] = 90,
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", help="Permission to grant. Repeat for more than one."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Create a personal access token. Prefer Settings → Coach apps. Printed once."""
    from vibesynergy.api import oauth

    rt = _runtime(config)
    try:
        token = oauth.create_personal_access_token(rt.engine, name, scopes=scope, days=days)
    except ValueError as exc:
        _fail(str(exc))
    except IntegrityError:
        _fail("name already used")
    console.print(
        "Connect a coach in the portal: Settings → Coach apps. "
        f"This token expires in {days} days. It is shown once:"
    )
    print(token)


@mcp_token_app.command("list")
def mcp_token_list(config: ConfigOpt = None) -> None:
    """List personal access tokens (prefix only, never the full secret)."""
    from vibesynergy.api import oauth

    rt = _runtime(config)
    table = Table("name", "id", "prefix", "created", "expires", "last used", "revoked")
    for row in oauth.list_personal_access_tokens(rt.engine):
        table.add_row(
            row["display_name"] or row["name"],
            row["id"],
            row["prefix"],
            _fmt_time(row["created"]),
            _fmt_time(row["expires_at"]),
            _fmt_time(row["last_used"]),
            _fmt_time(row["revoked"]),
        )
    console.print(table)


@mcp_token_app.command("scopes")
def mcp_token_scopes(
    name: str,
    add: Annotated[
        list[str] | None,
        typer.Option("--add", help="Permission to add. Repeat to add more."),
    ] = None,
    remove: Annotated[
        list[str] | None,
        typer.Option("--remove", help="Permission to remove. Repeat to remove more."),
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Change a token's permissions. The token itself stays the same."""
    from vibesynergy.api import oauth

    rt = _runtime(config)
    try:
        scopes = oauth.update_personal_access_token_scopes(rt.engine, name, add=add, remove=remove)
    except ValueError as exc:
        _fail(str(exc))
    console.print(" ".join(scopes))


@mcp_token_app.command("revoke")
def mcp_token_revoke(name_or_id: str, config: ConfigOpt = None) -> None:
    """Revoke a personal access token by name or id."""
    from vibesynergy.api import oauth

    rt = _runtime(config)
    if not oauth.revoke_personal_access_token(rt.engine, name_or_id):
        _fail(f"No active token named or id {name_or_id}; see `vibesynergy mcp token list`.")
    console.print(f"Revoked token {name_or_id}.")


def _print_results(results: list[DatasetResult]) -> bool:
    """Print a results table; returns whether anything failed."""
    table = Table("dataset", "window", "fetched", "skipped", "new", "records", "result")
    failed = False
    for r in results:
        failed |= r.error is not None
        outcome = r.error or (f"{r.normalise_errors} map errors" if r.normalise_errors else "ok")
        table.add_row(
            f"{r.source}.{r.dataset}",
            str(r.window or "-"),
            str(r.fetched),
            str(r.skipped),
            str(r.new_versions),
            str(r.records),
            outcome,
        )
    console.print(table)
    return failed


@app.command()
def normalise(
    source: str,
    dataset: Annotated[str | None, typer.Option("--dataset", "-d")] = None,
    config: ConfigOpt = None,
) -> None:
    """Rebuild canonical records from the raw archive (after a mapper change)."""
    rt = _runtime(config)
    items, errors = rt.normaliser.rebuild(source, dataset)
    console.print(f"normalised {items} raw items, {errors} errors")
    if errors:
        raise typer.Exit(1)


@app.command()
def drop(
    source: str,
    dataset: Annotated[list[str], typer.Option("--dataset", "-d")],
    config: ConfigOpt = None,
) -> None:
    """Remove a dataset's records, streams and intraday files from the database (e.g. data
    you don't want from this source). The raw archive is kept: `vibesynergy normalise` brings
    them back. Also limit `[sources.<id>] datasets` so they aren't synced again."""
    from vibesynergy.query import best_source

    rt = _runtime(config)
    known = {d.name for d in rt.registry.get(source, require_enabled=False).datasets()}
    unknown = [d for d in dataset if d not in known]
    if unknown:
        _fail(f"{source} has no dataset {', '.join(unknown)}")
    for name in dataset:
        items, rows, files = rt.normaliser.drop(source, name)
        console.print(f"{source}.{name}: {rows} records and {files} files from {items} raw items")
    stats = best_source.rebuild(rt.engine, rt.settings)
    _score_open_predictions(rt)
    console.print(f"{stats.groups} unified activities; {stats.daily_values} daily values")


@app.command()
def derive(config: ConfigOpt = None) -> None:
    """Rebuild unified activities and daily values (runs automatically after syncs)."""
    from vibesynergy.query import best_source

    rt = _runtime(config)
    stats = best_source.rebuild(rt.engine, rt.settings)
    _score_open_predictions(rt)
    from vibesynergy.query import journal

    notes = journal.import_activity_notes(rt.engine)
    console.print(
        f"{stats.activities} activities in {stats.groups} unified activities; "
        f"{stats.daily_values} daily values; {notes} activity notes updated"
    )


@app.command("data-quality")
def data_quality_cmd(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    limit: Annotated[int, typer.Option(help="Max rows per issue list.")] = 200,
    config: ConfigOpt = None,
) -> None:
    """Read-only audit of Strava/Garmin activity linking."""
    from vibesynergy.query import data_quality

    rt = _runtime(config)
    report = data_quality.activity_quality(rt.engine, limit=limit)
    if as_json:
        console.print_json(json.dumps(report, ensure_ascii=False, default=str))
        return
    console.print(
        f"{report['activities']} activities, {report['unified']} unified; "
        f"{report['groups_with_strava']} groups with Strava, "
        f"{report['groups_with_garmin']} with Garmin"
    )
    table = Table("issue", "count")
    table.add_row("same-source groups", str(len(report["same_source_groups"])))
    table.add_row("unlinked candidates", str(len(report["unlinked_candidates"])))
    table.add_row("suspect double counts", str(len(report["suspect_double_counts"])))
    console.print(table)
    if report["unlinked_candidates"]:
        detail = Table("a", "b", "start_gap_s", "sport")
        for row in report["unlinked_candidates"][:20]:
            detail.add_row(row["a"], row["b"], f"{row['start_gap_s']:.0f}", row["sport"])
        console.print(detail)


@app.command()
def worker(
    once: Annotated[bool, typer.Option(help="Run due jobs, then exit.")] = False,
    poll: Annotated[float, typer.Option(help="Seconds between queue checks.")] = 5.0,
    config: ConfigOpt = None,
) -> None:
    """Run queued jobs."""
    rt = _runtime(config)
    _score_open_predictions(rt)
    w = rt.worker()
    if once:
        ran = 0
        while w.run_once():
            ran += 1
        console.print(f"ran {ran} jobs")
    else:
        w.run(poll_seconds=poll)


@jobs_app.callback(invoke_without_command=True)
def jobs(
    ctx: typer.Context,
    status_filter: Annotated[str | None, typer.Option("--status")] = None,
    limit: int = 20,
    config: ConfigOpt = None,
) -> None:
    """Show recent jobs (or `vibesynergy jobs prune`)."""
    if ctx.invoked_subcommand is not None:
        return
    rt = _runtime(config)
    table = Table("id", "kind", "status", "attempts", "payload", "error")
    for j in rt.jobs.recent(status_filter, limit):
        table.add_row(
            str(j["id"]),
            j["kind"],
            j["status"],
            f"{j['attempts']}/{j['max_attempts']}",
            str(j["payload"]),
            (j["last_error"] or "")[:60],
        )
    console.print(table)


@db_app.command("upgrade")
def db_upgrade(config: ConfigOpt = None) -> None:
    """Apply pending schema migrations."""
    rt = _runtime(config, require_schema=False)
    rt.migrate()
    console.print(f"schema at {db.current_revision(rt.engine)}")


@db_app.command("revision")
def db_revision(
    message: Annotated[str, typer.Option("--message", "-m")], config: ConfigOpt = None
) -> None:
    """Autogenerate a migration from schema changes (development)."""
    db.new_revision(_runtime(config).engine, message)


def _window(rt: Runtime, start: str | None, end: str | None, days: int | None) -> Window | None:
    today = today_in(rt.settings.timezone)
    try:
        if days:
            return Window(today - timedelta(days=days - 1), today)
        if start:
            return Window(date.fromisoformat(start), date.fromisoformat(end) if end else today)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if end:
        raise typer.BadParameter("--end needs --start")
    return None


def _fmt_time(value) -> str:
    from vibesynergy.core.user_datetime import format_user_datetime

    return format_user_datetime(value) if value else "-"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# Command groups in their own modules; importing them registers the commands.
from vibesynergy.cli import ops_cmds, plan_cmds, webhook_cmds  # noqa: E402, F401
