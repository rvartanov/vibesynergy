"""CLI: instant updates (webhooks) (split from cli/main.py).
Registered when cli/main.py imports it."""

from __future__ import annotations

from vibesynergy.cli.main import (
    ConfigOpt,
    _fail,
    _runtime,
    console,
    webhooks_app,
)


@webhooks_app.command("subscribe")
def webhooks_subscribe(source: str, config: ConfigOpt = None) -> None:
    """Register for instant updates (`vibesynergy serve` must be running and reachable)."""
    rt = _runtime(config)
    src = rt.registry.get(source, require_enabled=False)
    try:
        if source == "withings":
            from vibesynergy.sources.withings import webhooks

            for label, outcome in webhooks.subscribe(
                src.client, rt.settings, src.ctx.secrets
            ).items():
                console.print(f"{label}: {outcome}")
        elif source == "strava":
            from vibesynergy.sources.strava import webhooks

            sub = webhooks.subscribe(src.client, rt.settings, src.ctx.secrets)
            console.print(f"Strava subscription {sub.get('id')} active")
        else:
            _fail(f"{source} has no webhooks")
    except Exception as exc:
        _fail(f"{type(exc).__name__}: {exc}")


@webhooks_app.command("list")
def webhooks_list(source: str, config: ConfigOpt = None) -> None:
    """Show the subscriptions the provider has for this app."""
    rt = _runtime(config)
    src = rt.registry.get(source, require_enabled=False)
    if source == "withings":
        from vibesynergy.sources.withings import webhooks

        console.print(webhooks.listing(src.client))
    elif source == "strava":
        from vibesynergy.sources.strava import webhooks

        console.print(webhooks.listing(src.client, src.ctx.secrets))
    else:
        _fail(f"{source} has no webhooks")


@webhooks_app.command("unsubscribe")
def webhooks_unsubscribe(source: str, config: ConfigOpt = None) -> None:
    """Remove the subscriptions."""
    rt = _runtime(config)
    src = rt.registry.get(source, require_enabled=False)
    if source == "withings":
        from vibesynergy.sources.withings import webhooks

        console.print(webhooks.unsubscribe(src.client, rt.settings, src.ctx.secrets))
    elif source == "strava":
        from vibesynergy.sources.strava import webhooks

        console.print(f"removed {webhooks.unsubscribe(src.client, src.ctx.secrets)}")
    else:
        _fail(f"{source} has no webhooks")
