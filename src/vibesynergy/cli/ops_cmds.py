"""CLI: map tiles and the credential store (split from cli/main.py).
Registered when cli/main.py imports it."""

from __future__ import annotations

from typing import Annotated

import typer

from vibesynergy.cli.main import (
    ConfigOpt,
    _fail,
    _runtime,
    console,
    secrets_app,
    tiles_app,
)
from vibesynergy.core.config import (
    ConfigError,
    load_settings,
)
from vibesynergy.core.timeutil import today_in


@tiles_app.command("status")
def tiles_status(config: ConfigOpt = None) -> None:
    """Where the map tile cache is and how much it holds."""
    from vibesynergy.portal.api import tile_cache, tile_cache_enabled

    rt = _runtime(config)
    cache = tile_cache(rt.settings)
    stats = cache.stats()
    state = "on" if tile_cache_enabled(rt.settings) else "off ([portal] tile_cache = false)"
    console.print(f"cache    {state}")
    print(f"folder   {cache.root}/{{z}}/{{x}}/{{y}}.png")  # unwrapped: it gets copied
    console.print(f"upstream {cache.upstream}")
    console.print(f"tiles    {stats.tiles:,} ({stats.bytes / 1e6:,.1f} MB)")


@tiles_app.command("prefetch")
def tiles_prefetch(
    period: Annotated[str, typer.Option(help="Activities in this period, e.g. last_90d.")] = (
        "last_28d"
    ),
    limit: Annotated[int, typer.Option(help="Download at most this many new tiles.")] = 2000,
    config: ConfigOpt = None,
) -> None:
    """Cache the map tiles of recent activities' routes, so their maps work offline.

    Only the tiles an activity page opens with, plus the tiles along the route two zoom
    levels closer; tiles already cached are skipped. Downloads are one at a time with a
    pause, as OpenStreetMap's tile policy asks (no bulk downloading): keep periods modest.
    """
    from vibesynergy.portal.api import tile_cache
    from vibesynergy.portal.tiles import prefetch, route_tiles
    from vibesynergy.query import periods as qperiods
    from vibesynergy.query import views

    rt = _runtime(config)
    try:
        span = qperiods.resolve(period, today=today_in(rt.settings.timezone))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    cache = tile_cache(rt.settings)
    wanted: set[tuple[int, int, int]] = set()
    routes, offset = 0, 0
    while offset is not None:
        page = views.activities(rt.engine, span, limit=200, offset=offset)
        for a in page["activities"]:
            try:
                streams = views.activity_streams(
                    rt.engine, rt.parquet, a["group_id"], channels=["lat", "lon"], max_points=2000
                )
            except ValueError:
                continue  # no stream data
            cols = streams["columns"]
            if "lat" in cols and "lon" in cols:
                tiles = route_tiles(cols["lat"], cols["lon"])
                routes += bool(tiles)
                wanted |= tiles
        offset = page["next_offset"]
    console.print(f"{routes} routes in {span}: {len(wanted):,} tiles needed")
    downloaded, cached, failed = prefetch(cache, wanted, limit=limit)
    console.print(f"downloaded {downloaded:,}, already cached {cached:,}, failed {failed:,}")
    left = len(wanted) - downloaded - cached - failed
    if left > 0:
        console.print(f"{left:,} tiles left: run again to continue (limit {limit:,} per run).")


@secrets_app.command("encrypt")
def secrets_encrypt(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only list what would change; write nothing.")
    ] = False,
    config: ConfigOpt = None,
) -> None:
    """Encrypt plaintext credential documents left by older versions (safe to repeat).

    Stores also encrypt a plaintext document on its first read; this does all of them at
    once. Prints document names only, never their contents.
    """
    from vibesynergy.core.secrets import SecretError, encrypt_all

    try:
        settings = load_settings(config)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        done = encrypt_all(settings.secrets_dir, dry_run=dry_run)
    except SecretError as exc:
        _fail(str(exc))
    verb = {
        "encrypted": "would encrypt" if dry_run else "encrypted",
        "stale-plaintext-removed": (
            "would remove stale plaintext copy of" if dry_run else "removed stale plaintext copy of"
        ),
    }
    for item in done:
        console.print(f"{verb[item.action]} {item.source}/{item.name}", highlight=False)
    if not done:
        console.print("Nothing to do: every stored secret is encrypted.")
    elif dry_run:
        console.print(f"{len(done)} document(s) to change; run without --dry-run to apply.")
    else:
        console.print(f"{len(done)} document(s) changed.")
