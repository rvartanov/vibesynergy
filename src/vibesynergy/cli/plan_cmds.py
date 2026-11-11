"""CLI: plan, load, knowledge base, jobs and coaching (split from cli/main.py).
Registered when cli/main.py imports it."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from vibesynergy.cli.main import (
    ConfigOpt,
    _runtime,
    coaching_app,
    console,
    jobs_app,
    kb_app,
    load_app,
    plan_app,
)


@coaching_app.command("backfill")
def coaching_backfill(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Count changes without writing.")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Recompute which activities are trainings you give, from their session purpose."""
    from vibesynergy.query import coaching

    result = coaching.backfill(_runtime(config).engine, dry_run=dry_run)
    console.print(
        f"Scanned {result['scanned']}; changed {result['changed']}; coaching {result['coaching']}"
        + (" (dry run)" if dry_run else "")
    )


@jobs_app.command("prune")
def jobs_prune(
    days: Annotated[int, typer.Option(min=1, help="keep jobs finished in the last N days")] = 30,
    dead: Annotated[bool, typer.Option("--dead", help="also failed (dead) jobs")] = False,
    apply: Annotated[bool, typer.Option("--apply", help="delete; without it only count")] = False,
    config: ConfigOpt = None,
) -> None:
    """Remove old finished jobs from the queue. Counts only, unless --apply."""
    from datetime import timedelta

    from vibesynergy.sync.jobs import PRUNE_STATUSES

    statuses = (*PRUNE_STATUSES, "dead") if dead else PRUNE_STATUSES
    found = _runtime(config).jobs.prune(
        older_than=timedelta(days=days), statuses=statuses, apply=apply
    )
    total = sum(found.values())
    for kind, n in sorted(found.items(), key=lambda kv: -kv[1]):
        console.print(f"  {kind:<24} {n}")
    console.print(
        f"{'Removed' if apply else 'Would remove'} {total} jobs older than {days} days"
        + ("" if apply or not total else " (run again with --apply)")
    )


@plan_app.command("import")
def plan_import(
    prefix: Annotated[
        str | None, typer.Option(help="external_id prefix of the plan (default: the current plan)")
    ] = None,
    name: Annotated[str | None, typer.Option(help="plan name (first import)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="report only")] = False,
    config: ConfigOpt = None,
) -> None:
    """Fill the plan model from intervals.icu (shadow: nothing is written to intervals.icu)."""
    import json as _json

    from vibesynergy.query import plan_model

    engine = _runtime(config).engine
    prefix = prefix or plan_model.current_plan_id(engine)
    if not prefix:
        raise typer.BadParameter("no plan yet: name its external_id prefix", param_hint="--prefix")
    report = plan_model.import_from_intervals(engine, prefix=prefix, name=name, dry_run=dry_run)
    typer.echo(_json.dumps(report, indent=2, ensure_ascii=False))


@plan_app.command("export")
def plan_export(
    out: Annotated[
        Path | None, typer.Option("--out", help="file to write (default: stdout)")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """The plan as CSV: every week note and planned session, readable without VibeSynergy."""
    from vibesynergy.query import plan_model

    text = plan_model.export_csv(_runtime(config).engine)
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text, encoding="utf-8")
        console.print(f"wrote {out}")


@plan_app.command("booklet")
def plan_booklet(
    out: Annotated[Path | None, typer.Option("--out", help="HTML file (default: stdout)")] = None,
    lang: Annotated[str, typer.Option(help="nl or en")] = "nl",
    config: ConfigOpt = None,
) -> None:
    """The coach booklet: races, phases, weeks, thresholds and plan rules on paper."""
    from vibesynergy.core.timeutil import today_in
    from vibesynergy.query import booklet

    rt = _runtime(config)
    page = booklet.render(booklet.data(rt.engine, today_in(rt.settings.timezone)), lang)
    if out is None:
        typer.echo(page)
    else:
        out.write_text(page, encoding="utf-8")
        console.print(f"wrote {out}")


@plan_app.command("source")
def plan_source(
    value: Annotated[str | None, typer.Argument(help="intervals or vibesynergy")] = None,
    config: ConfigOpt = None,
) -> None:
    """Who leads the plan: intervals (now) or vibesynergy (one-way mirror to intervals.icu)."""
    from vibesynergy.query import plan_model

    engine = _runtime(config).engine
    typer.echo(plan_model.set_source(engine, value) if value else plan_model.source(engine))


@plan_app.command("mirror")
def plan_mirror(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="report only")] = False,
    config: ConfigOpt = None,
) -> None:
    """Write the model's sessions that differ to intervals.icu (VibeSynergy leads)."""
    import json as _json

    from vibesynergy.query import plan_model
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    rt = _runtime(config)
    client = None if dry_run else IntervalsIcuClient.from_env()
    out = plan_model.mirror(rt.engine, client, dry_run=dry_run)
    typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))


@plan_app.command("garmin-compare")
def plan_garmin_compare(
    days: Annotated[int, typer.Option(help="days ahead, from today")] = 9,
    config: ConfigOpt = None,
) -> None:
    """Per planned session: is intervals.icu's copy on the Garmin, and VibeSynergy's."""
    from vibesynergy.core.timeutil import today_in
    from vibesynergy.query import horizon

    rt = _runtime(config)
    table = Table("date", "sport", "session", "intervals.icu", "VibeSynergy", "also on Garmin")
    for row in horizon.compare(rt.engine, today_in(rt.settings.timezone), days):
        table.add_row(
            row["date"],
            row["sport"] or "",
            row["title"][:40],
            "yes" if row["intervals"] else "-",
            "yes" if row["vibesynergy"] else "-",
            ", ".join(o[:30] for o in row["other"]),
        )
    console.print(table)


@plan_app.command("garmin")
def plan_garmin(
    sports: Annotated[list[str] | None, typer.Argument(help="swim run ride, or 'off'")] = None,
    config: ConfigOpt = None,
) -> None:
    """Which sports VibeSynergy delivers to Garmin itself. Without arguments: show."""
    from vibesynergy.portal import prefs

    rt = _runtime(config)
    if sports:
        chosen = [] if sports == ["off"] else sports
        prefs.save(rt.settings.data_dir, {"garmin_delivery": chosen or None})
    typer.echo(", ".join(prefs.load(rt.settings.data_dir).get("garmin_delivery") or []) or "off")


@load_app.command("show")
def load_show(
    days: Annotated[int, typer.Option(help="how many days back")] = 14,
    config: ConfigOpt = None,
) -> None:
    """VibeSynergy's load per activity and how it was counted (power, pace or heart rate)."""
    import datetime as _dt

    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import training

    rt = _runtime(config)
    end = _dt.date.today()
    window = Window(end - _dt.timedelta(days=days - 1), end)
    items = training.activity_loads(rt.engine, window, rt.settings.athlete, parquet=rt.parquet)
    for item in sorted(items, key=lambda x: (x["date"], x["id"])):
        typer.echo(f"{item['date']} {item['family']:6} {item['load']:6.1f} {item['method']}")


@kb_app.command("seed-prompts")
def kb_seed_prompts(
    directory: Annotated[
        Path | None, typer.Option("--dir", help="Folder with morning.md, evening.md, …")
    ] = None,
    config: ConfigOpt = None,
) -> None:
    """Copy the routine prompts (docs/coach-prompts) into the knowledge base. Safe to repeat:
    a prompt edited since is kept, because the knowledge base is the live source."""
    from vibesynergy.query import coach_prompts

    rt = _runtime(config)
    for row in coach_prompts.seed(
        rt.engine, directory, overlay=coach_prompts.overlay_dir(rt.settings)
    ):
        version = f" v{row['version']}" if row["version"] else ""
        console.print(f"{row['job']}: {row['action']} ({row['slug']}{version})")
