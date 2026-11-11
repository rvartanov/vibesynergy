"""The CalDAV account kept in the encrypted secret store, and what the portal does with it.

``account`` (encrypted) holds the provider, username, app-specific password and the calendars
found at the last sign-in. The selection itself is not secret: it lives in
``[sources.caldav] calendars`` in the config file, which is where ``fetch()`` reads it.
A selection is only accepted for URLs that sign-in actually discovered for this account.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

import requests

from vibesynergy.core import config_edit
from vibesynergy.core.config import ConfigError, Settings, load_settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources.caldav import client as caldav_client
from vibesynergy.sources.caldav import google
from vibesynergy.sources.caldav.client import CalDAVClient
from vibesynergy.sources.caldav.discovery import PRESETS, CalDAVError, discover
from vibesynergy.sources.http_timeout import http_timeout_seconds, requests_timeout_pair

SOURCE = "caldav"
ACCOUNT = "account"
CLASSIFICATIONS = {"unclassified", "commitment", "coaching", "exercise"}


def store_for(settings: Settings) -> SecretStore:
    return SecretStore(settings.secrets_dir, SOURCE)


def client_for(
    store: SecretStore,
    account: dict,
    http=None,
    *,
    request_timeout: float | tuple[float, float] | None = None,
) -> CalDAVClient:
    session = _session(http)
    if account.get("kind") == "oauth":
        return CalDAVClient(
            bearer=lambda: google.access_token(store, http, request_timeout=request_timeout),
            http=session,
            request_timeout=request_timeout,
        )
    return CalDAVClient(
        account["username"],
        account["password"],
        http=session,
        request_timeout=request_timeout,
    )


def _session(http):
    return http if http is not None and hasattr(http, "request") else requests.Session()


def _resolve_url(provider: str, url: str | None) -> str:
    preset = PRESETS.get(provider)
    if preset is None or preset["kind"] != "password":
        raise CalDAVError("bad_provider")
    if preset["url"]:
        return preset["url"]
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme == "http" and not (
        caldav_client.ALLOW_LOCAL_HTTP and parts.hostname in ("localhost", "127.0.0.1")
    ):
        raise CalDAVError("insecure_url")
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise CalDAVError("bad_url")
    return url


def _run_discovery(
    store,
    account: dict,
    http,
    *,
    request_timeout: float | tuple[float, float] | None = None,
) -> dict:
    trusted = PRESETS[account["provider"]]["trusted"]
    found = discover(
        account["url"],
        client_for(store, account, http, request_timeout=request_timeout),
        trusted,
    )
    account = {**account, "discovered": found.calendars, "discovered_at": time.time()}
    store.set(ACCOUNT, account)
    return account


def _request_timeout(settings: Settings) -> tuple[float, float]:
    return requests_timeout_pair(http_timeout_seconds(settings, SOURCE))


def sign_in(
    settings: Settings, provider: str, url: str | None, username: str, password: str, http=None
) -> dict:
    username, password = username.strip(), password.strip()
    if not username or not password:
        raise CalDAVError("missing_fields")
    store = store_for(settings)
    request_timeout = _request_timeout(settings)
    account = {
        "provider": provider,
        "kind": "password",
        "url": _resolve_url(provider, url),
        "username": username,
        "password": password,
    }
    # Discover first: a wrong password never replaces the working account.
    trusted = PRESETS[provider]["trusted"]
    found = discover(
        account["url"],
        client_for(store, account, http, request_timeout=request_timeout),
        trusted,
    )
    store.set(ACCOUNT, {**account, "discovered": found.calendars, "discovered_at": time.time()})
    return status(settings)


def sign_in_google(settings: Settings, http=None) -> dict:
    """After the OAuth callback stored tokens: discover with the Bearer token."""
    store = store_for(settings)
    request_timeout = _request_timeout(settings)
    preset = PRESETS["google"]
    account = {"provider": "google", "kind": "oauth", "url": preset["url"], "username": ""}
    found = discover(
        account["url"],
        client_for(store, account, http, request_timeout=request_timeout),
        preset["trusted"],
    )
    store.set(ACCOUNT, {**account, "discovered": found.calendars, "discovered_at": time.time()})
    return status(settings)


def rediscover(settings: Settings, http=None) -> dict:
    """Refresh the list of calendars with the stored credentials (no re-entry)."""
    store = store_for(settings)
    account = store.get(ACCOUNT)
    if not account:
        raise CalDAVError("not_connected")
    _run_discovery(store, account, http, request_timeout=_request_timeout(settings))
    return status(settings)


def selected(settings: Settings) -> list[dict]:
    on_disk = load_settings(settings.config_path) if settings.config_path.exists() else settings
    return [dict(c) for c in on_disk.source_config(SOURCE).get("calendars", [])]


def mirror_enabled(settings: Settings) -> bool:
    """Portal switch for copying Garmin workouts. Default off; this alone does not write."""
    on_disk = load_settings(settings.config_path) if settings.config_path.exists() else settings
    return bool(on_disk.source_config(SOURCE).get("mirror_enabled"))


def writes_allowed(settings: Settings) -> bool:
    """Password accounts can update a selected calendar. Google sign-in is read-only."""
    stored = store_for(settings).get(ACCOUNT) or {}
    if not stored or stored.get("kind") == "oauth" or stored.get("provider") == "google":
        return False
    return bool(stored.get("password")) and garmin_target(settings) is not None


def garmin_target(settings: Settings) -> str | None:
    """Selected collection that may receive owned Garmin mirrors."""
    on_disk = load_settings(settings.config_path) if settings.config_path.exists() else settings
    target = on_disk.source_config(SOURCE).get("garmin_target_calendar")
    return target if on_disk.source_enabled(SOURCE) and target in _target_urls(settings) else None


def _target_urls(settings: Settings) -> set[str]:
    account = store_for(settings).get(ACCOUNT) or {}
    discovered = {c["url"] for c in account.get("discovered", [])}
    return discovered & {c["url"] for c in selected(settings)}


def save_garmin_target(settings: Settings, url: str | None) -> dict:
    if url is not None and url not in _target_urls(settings):
        raise CalDAVError("unknown_calendar")
    changes: dict = {"garmin_target_calendar": url}
    if url is None:
        changes["mirror_enabled"] = None
    config_edit.apply_changes(settings.config_path, {"sources": {SOURCE: changes}})
    return status(settings)


def save_mirror_enabled(settings: Settings, enabled: bool) -> dict:
    """Turn copying on only for a selected calendar this account is allowed to update."""
    if enabled and not writes_allowed(settings):
        raise CalDAVError("read_only_account" if _read_only(settings) else "no_target")
    config_edit.apply_changes(
        settings.config_path, {"sources": {SOURCE: {"mirror_enabled": bool(enabled)}}}
    )
    return status(settings)


def _read_only(settings: Settings) -> bool:
    stored = store_for(settings).get(ACCOUNT) or {}
    return not stored or stored.get("kind") == "oauth" or stored.get("provider") == "google"


def save_selection(settings: Settings, urls: list[str]) -> dict:
    account = store_for(settings).get(ACCOUNT)
    if not account:
        raise CalDAVError("not_connected")
    known = {c["url"]: c for c in account.get("discovered", [])}
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise CalDAVError("no_selection")
    if any(u not in known for u in urls):
        raise CalDAVError("unknown_calendar")
    prior = {c["url"]: c.get("classification", "unclassified") for c in selected(settings)}
    chosen = [
        {
            **{k: v for k, v in known[u].items() if v is not None},
            "classification": prior.get(u, "unclassified"),
        }
        for u in urls
    ]
    target = garmin_target(settings)
    try:
        config_edit.apply_changes(
            settings.config_path,
            {
                "sources": {
                    SOURCE: {
                        "enabled": True,
                        "calendars": chosen,
                        "garmin_target_calendar": target if target in urls else None,
                        "mirror_enabled": mirror_enabled(settings) if target in urls else None,
                    }
                }
            },
        )
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    return status(settings)


def save_classification(settings: Settings, url: str, classification: str) -> dict:
    if classification not in CLASSIFICATIONS:
        raise CalDAVError("bad_classification")
    if url not in _target_urls(settings):
        raise CalDAVError("unknown_calendar")
    chosen = selected(settings)
    for entry in chosen:
        if entry["url"] == url:
            entry["classification"] = classification
    config_edit.apply_changes(settings.config_path, {"sources": {SOURCE: {"calendars": chosen}}})
    return status(settings)


def disconnect(settings: Settings) -> dict:
    store = store_for(settings)
    for name in (ACCOUNT, google.TOKENS, google.LOGIN):
        store.delete(name)
    try:
        config_edit.apply_changes(
            settings.config_path,
            {
                "sources": {
                    SOURCE: {
                        "enabled": False,
                        "calendars": None,
                        "garmin_target_calendar": None,
                        "mirror_enabled": None,
                    }
                }
            },
        )
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    return status(settings)


def status(settings: Settings) -> dict[str, Any]:
    """Everything the page may know. Secrets are never included, only whether they are set."""
    store = store_for(settings)
    account = store.get(ACCOUNT)
    chosen = selected(settings)
    chosen_urls = {c["url"] for c in chosen}
    return {
        "connected": bool(account),
        "provider": account["provider"] if account else None,
        "kind": account["kind"] if account else None,
        "url": (account or {}).get("url"),
        "username": (account or {}).get("username") or None,
        "password_set": bool((account or {}).get("password")),
        "discovered": [
            {**c, "selected": c["url"] in chosen_urls}
            for c in (account or {}).get("discovered", [])
        ],
        "selected": chosen,
        "target_calendars": [c for c in chosen if c["url"] in _target_urls(settings)],
        "garmin_target_calendar": garmin_target(settings),
        "mirror_enabled": mirror_enabled(settings),
        "mirror_active": mirror_enabled(settings) and writes_allowed(settings),
        "google": {"client_set": google.app_configured(store)},
    }
