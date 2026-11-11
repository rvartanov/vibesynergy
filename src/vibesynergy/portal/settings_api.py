"""Settings page API: read and change the configuration, see source logins, manage the apps
connected to the MCP endpoint, and restart the service so changes apply.

Every change goes through ``core.config_edit`` (known settings only, validated, previous
file kept). The running service keeps its settings until it restarts; the page offers that.
"""

from __future__ import annotations

import queue
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from vibesynergy import alerts, bugs, dropfolder, mailer
from vibesynergy.api import oauth as mcp_oauth
from vibesynergy.core import config_edit
from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.derive.unify import DEFAULT_BEST_SOURCE
from vibesynergy.portal import prefs, share
from vibesynergy.portal.hosts import LOCAL_HOSTS, normalize_hostname, validate_portal_address
from vibesynergy.query import best_source as best_source_q
from vibesynergy.query import (
    coach,
    cross_sync,
    publication_auto,
    thresholds,
    views,
)
from vibesynergy.query.publication_format import DEFAULT_CLOSER, DEFAULT_OPENER, LABELS
from vibesynergy.query.publication_plugins import COLLECTIONS, PROVIDERS, THEMES, quotes_left
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth as source_oauth
from vibesynergy.sources.base import DatasetKind
from vibesynergy.sources.registry import SourceError
from vibesynergy.sources.strava.client import AUTHORIZE_URL, SCOPES, budget_wait
from vibesynergy.store.schema import audit_log
from vibesynergy.sync.scheduler import DEFAULT_MINUTES, SERVICE_MINUTES

LOGIN_ERRORS = ("AuthError", "not logged in", "invalid_grant", "401")


class SettingsChange(BaseModel):
    changes: dict[str, Any]


class BugReport(BaseModel):
    description: str
    page: str = ""
    errors: list[str] = []
    failed_requests: list[dict[str, Any]] = []
    browser: str = ""
    screenshot: str | None = None  # a data: URL


class SecretRequest(BaseModel):
    value: str


class StravaConnect(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None
    portal: str | None = None


class GarminConnect(BaseModel):
    email: str
    password: str


class IcuCredentials(BaseModel):
    """intervals.icu key and athlete id. An empty key keeps the stored one."""

    api_key: str = Field(default="", max_length=200)
    athlete_id: str = Field(min_length=1, max_length=40)


class GarminMfa(BaseModel):
    session: str
    code: str


_garmin_logins: dict[str, dict] = {}


def _start_garmin_login(settings, email: str, password: str) -> str:
    """Keep the one-time password only in a waiting worker, never in config or a response."""
    from vibesynergy.sources.garmin.client import _garmin_class, save_tokens

    session = secrets.token_urlsafe(24)
    state = {"state": "connecting", "created": time.time(), "mfa": queue.Queue(maxsize=1)}
    _garmin_logins[session] = state

    def run():
        try:
            store = SecretStore(settings.secrets_dir, "garmin")

            def prompt():
                state["state"] = "mfa"
                return state["mfa"].get(timeout=300)

            with store.lock("tokens"):
                api = _garmin_class()(email=email, password=password, prompt_mfa=prompt)
                api.login()
                name = api.display_name or email
                profile_id = api.profile_id if type(api.profile_id) is int else None
                prior = store.get("account") or {}
                if (prior.get("name") and prior["name"] != name) or (
                    prior.get("id") is not None
                    and profile_id is not None
                    and prior["id"] != profile_id
                ):
                    raise ValueError("A different Garmin account is connected")
                save_tokens(store, api)
                store.set(
                    "account",
                    {"name": name, **({"id": profile_id} if profile_id is not None else {})},
                )
            state["state"] = "connected"
        except Exception:
            state["state"] = "failed"

    threading.Thread(target=run, daemon=True).start()
    return session


TEST_TEXT = "This is a test from VibeSynergy's alerts. If you can read this, alerts reach you."


class ResetRequest(BaseModel):
    prefix: str | None = None


class MetricOrder(BaseModel):
    metric: str = Field(max_length=100)
    order: list[str] | None = Field(default=None, max_length=16)


class RevokeRequest(BaseModel):
    client_id: str


class HostChange(BaseModel):
    action: str  # add | remove
    host: str
    confirm: bool = False  # removing the name this page was opened on


# The portal answers only on the names it knows; removing the one in use locks this browser
# out until the config is fixed by hand (an outage on 31-10-2026). Ask first.
CURRENT_HOST_WARNING = (
    "You opened this page on that address. After the restart it will no longer work here."
)


class ShareRequest(BaseModel):
    title: str
    pages: list[str]
    can_write: bool = False
    expires_in_days: float | None = 7


class ShareEdit(BaseModel):
    title: str | None = None
    pages: list[str] | None = None
    can_write: bool | None = None


class RevokeShareRequest(BaseModel):
    id: str


class PatCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1)
    days: int
    display_name: str | None = Field(default=None, max_length=80)
    # The coach passphrase: a token grants access, so the tailnet alone is not enough.
    passphrase: str = Field(default="", max_length=1024)


class PatId(BaseModel):
    id: str


class PatRename(BaseModel):
    id: str
    display_name: str = Field(min_length=1, max_length=80)


class ClientRename(BaseModel):
    client_id: str
    display_name: str = Field(min_length=1, max_length=80)


class PushSubscription(BaseModel):
    endpoint: str = Field(max_length=2000)
    p256dh: str = Field(default="", max_length=200)
    auth: str = Field(default="", max_length=100)
    agent: str = Field(default="", max_length=300)


class UpdateBody(BaseModel):
    passphrase: str = Field(default="", max_length=1024)
    password: str = Field(default="", max_length=1024)  # the login's, when the install has one


class PassphraseBody(BaseModel):
    new: str
    current: str | None = None


class PrefsChange(BaseModel):
    changes: dict[str, Any]


class CoachEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    payload: dict[str, Any]
    expected_version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=1000)


class CoachActivate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1000)


class CoachRestore(CoachActivate):
    version: int = Field(ge=1)


class CoachPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    payload: dict[str, Any]


class AthleteThresholds(BaseModel):
    """Athlete edits on You. Stored locally. Nothing here is sent to Garmin."""

    model_config = ConfigDict(extra="forbid")
    lthr: float | None = None
    threshold_pace: float | None = None


def _with_conflicts(result: dict, kind: str) -> dict:
    if kind != "policy":
        return result
    for key in ("draft", "active"):
        revision = result.get(key)
        if isinstance(revision, dict) and isinstance(revision.get("payload"), dict):
            revision["conflicts"] = coach.policy_conflicts(revision["payload"])
    return result


def systemd_unit(cgroup_file: Path = Path("/proc/self/cgroup")) -> str | None:
    """The systemd user unit this process runs in (e.g. vibesynergy.service), if any."""
    try:
        line = cgroup_file.read_text().strip().splitlines()[-1]
    except OSError:
        return None
    last = line.rsplit("/", 1)[-1]
    return last if "user@" in line and re.fullmatch(r"[\w@.-]+\.service", last) else None


def build_settings_router(rt: Runtime) -> APIRouter:
    router = APIRouter()
    settings = rt.settings

    @router.post("/sources/strava/connect")
    def strava_connect(request: StravaConnect) -> dict[str, Any]:
        store = SecretStore(settings.secrets_dir, "strava")
        app = store.get(source_oauth.APP)
        if request.client_id or request.client_secret:
            if (
                not request.client_id
                or not request.client_id.isdigit()
                or not request.client_secret
            ):
                raise ValueError("Enter a valid Strava app ID and secret")
            if app and app["client_id"] != request.client_id and store.get("athlete"):
                raise ValueError("Disconnect the previous Strava account before changing its app")
            app = {"client_id": request.client_id, "client_secret": request.client_secret}
            store.set(source_oauth.APP, app)
        if not app:
            raise ValueError("Enter the Strava app ID and secret")
        # Migrate a pre-existing plain secret into encrypted storage.
        store.set(source_oauth.APP, app)
        state = secrets.token_urlsafe(32)
        portal = ""
        if request.portal:
            portal = validate_portal_address(settings, request.portal)
        pending_store = SecretStore(settings.secrets_dir, source_oauth.PUBLICATION_OAUTH)
        source_oauth.store_publication_oauth_pending(
            pending_store, state, source="strava", portal=portal, created=time.time()
        )
        params = {
            "client_id": app["client_id"],
            "response_type": "code",
            "redirect_uri": source_oauth.redirect_uri(settings, "strava"),
            "approval_prompt": "force",
            "scope": SCOPES,
            "state": state,
        }
        return {"data": {"url": f"{AUTHORIZE_URL}?{urlencode(params)}"}}

    @router.post("/sources/garmin/connect")
    def garmin_connect(request: GarminConnect) -> dict[str, Any]:
        if not request.email.strip() or not request.password:
            raise ValueError("Enter your Garmin email and password")
        return {"data": {"session": _start_garmin_login(settings, request.email, request.password)}}

    @router.get("/sources/garmin/connect/{session}")
    def garmin_connect_status(session: str) -> dict[str, Any]:
        state = _garmin_logins.get(session)
        if not state or time.time() - state["created"] > 600:
            raise ValueError("Garmin sign-in has ended; start again")
        return {"data": {"state": state["state"]}}

    @router.post("/sources/garmin/mfa")
    def garmin_mfa(request: GarminMfa) -> dict[str, Any]:
        state = _garmin_logins.get(request.session)
        if not state or state["state"] != "mfa" or not request.code.strip():
            raise ValueError("Start Garmin sign-in again")
        state["state"] = "connecting"
        state["mfa"].put_nowait(request.code.strip())
        return {"data": {"state": "connecting"}}

    @router.get("/sources/intervals_icu/credentials")
    def icu_credentials_status() -> dict[str, Any]:
        """Whether a key is set and the athlete id; never the key."""
        from vibesynergy.sources.intervals_icu import credentials

        return {"data": credentials.status(settings.secrets_dir)}

    @router.post("/sources/intervals_icu/credentials")
    def icu_credentials_save(request: IcuCredentials) -> dict[str, Any]:
        from vibesynergy.sources.intervals_icu import credentials

        credentials.save(settings.secrets_dir, request.api_key, request.athlete_id)
        with rt.engine.begin() as conn:
            conn.execute(
                audit_log.insert().values(
                    at=utc_now(),
                    actor="portal",
                    action="intervals_icu.credentials.saved",
                    detail={"athlete_id": request.athlete_id.strip()},
                )
            )
        return {"data": credentials.status(settings.secrets_dir)}

    @router.post("/sources/{source_id}/disconnect")
    def source_disconnect(source_id: str) -> dict[str, Any]:
        if source_id == "intervals_icu":
            from vibesynergy.sources.intervals_icu import credentials

            credentials.clear(settings.secrets_dir)
            return {"data": {"state": "disconnected"}}
        if source_id not in {"strava", "garmin"}:
            raise ValueError("Only Garmin and Strava can be disconnected here")
        store = SecretStore(settings.secrets_dir, source_id)
        with store.lock("tokens"):
            for name in ("tokens", "account", "athlete", "app", "request_budget"):
                store.delete(name)
        return {"data": {"state": "disconnected"}}

    def current() -> dict[str, Any]:
        """The settings as the config file says now (may differ from the running ones)."""
        path = settings.config_path
        on_disk = load_settings(path) if path.exists() else settings
        sources = []
        for sid, cls in rt.registry.available().items():
            if sid == "demo":
                continue
            source = rt.registry.get(sid, require_enabled=False)
            specs = [s for s in source.datasets() if s.kind is not DatasetKind.FILE]
            cfg = on_disk.source_config(sid)
            only, extra = cfg.get("datasets"), set(cfg.get("extra_datasets", []))
            row = {
                "id": sid,
                "name": getattr(cls, "name", sid),
                "enabled": on_disk.source_enabled(sid),
                "import_only": not specs,
                "datasets": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "default": s.default_enabled,
                        "synced": s.name in only if only else s.default_enabled or s.name in extra,
                    }
                    for s in specs
                ],
                "login": login_status(sid) if specs else None,
                "budget_wait": budget_wait(SecretStore(settings.secrets_dir, sid))
                if sid == "strava"
                else None,
            }
            if sid == "intervals_icu":
                from vibesynergy.query import icu_notes

                row["private_notes"] = icu_notes.enabled(settings.data_dir)
            if sid == "garmin":
                from vibesynergy.portal import prefs as portal_prefs

                row["garmin_delivery"] = (
                    portal_prefs.load(settings.data_dir).get("garmin_delivery") or []
                )
            sources.append(row)
        return {
            "config_path": str(path),
            "user": {
                "timezone": on_disk.timezone,
                "units": on_disk.units,
                **{
                    k: on_disk.athlete.get(k)
                    for k in ("sex", "max_hr", "resting_hr", "birth_date", "load_source")
                },
            },
            "sources": sources,
            "publication": {
                "auto_publish": on_disk.publication.get("auto_publish") is True,
                "language": on_disk.publication.get("language"),
                "auto": publication_auto.status(rt),
                # On by default; a saved false switches one off.
                "quote_enabled": bool(on_disk.publication.get("quote_enabled", True)),
                "weather_enabled": bool(on_disk.publication.get("weather_enabled", True)),
                "garmin_mirror_enabled": bool(
                    on_disk.publication.get("garmin_mirror_enabled", True)
                ),
                # On by default: the same block in intervals.icu.
                "icu_description_enabled": bool(
                    on_disk.publication.get("icu_description_enabled", True)
                ),
                "quote_themes": on_disk.publication.get("quote_themes", list(THEMES)),
                "quote_sources": on_disk.publication.get("quote_sources", list(COLLECTIONS)),
                "sources": list(COLLECTIONS),
                "quote_providers": on_disk.publication.get("quote_providers", list(PROVIDERS)),
                "providers": list(PROVIDERS),
                "quotes_left": quotes_left(
                    rt, on_disk.publication.get("quote_themes", list(THEMES))
                ),
                "themes": list(THEMES),
                "opener": on_disk.publication.get("opener") or DEFAULT_OPENER,
                "closer": on_disk.publication.get("closer") or DEFAULT_CLOSER,
                "label_stats": on_disk.publication.get("label_stats"),
                "label_quote": on_disk.publication.get("label_quote"),
                "defaults": {
                    "opener": DEFAULT_OPENER,
                    "closer": DEFAULT_CLOSER,
                    "labels": {
                        lang: {k: LABELS[lang][k] for k in ("stats", "quote")} for lang in LABELS
                    },
                },
            },
            # Every interval that can be set, with its default; the configured ones override.
            "schedule": {
                "defaults": {**DEFAULT_MINUTES, **SERVICE_MINUTES},
                "configured": dict(on_disk.schedule),
            },
            "best_source": best_source(on_disk, sources),
            "best_source_custom": sorted(on_disk.best_source),
            "portal": {
                "tile_cache": bool(on_disk.portal.get("tile_cache", True)),
                "tile_max_age_days": on_disk.portal.get("tile_max_age_days"),
                "port": on_disk.portal.get("port", 8447),
            },
            "read_only": {
                "mcp_url": f"{on_disk.public_url}/vibesynergy/mcp" if on_disk.public_url else None,
                "portal_url": portal_url(on_disk),
                "data_dir": str(on_disk.data_dir),
                "public_url": on_disk.public_url,
                "local_url": f"http://127.0.0.1:{on_disk.portal.get('port', 8447)}",
            },
            "portal_hosts": [str(h) for h in on_disk.portal.get("hosts", [])],
            "mail": {
                **{k: on_disk.mail.get(k) for k in config_edit.MAIL_KEYS},
                "password_set": bool(mailer._password(on_disk)),
            },
            "alerts": {
                **{k: on_disk.alerts.get(k) for k in config_edit.ALERT_KEYS},
                "channels": alerts.channels(on_disk, rt.engine),
                "open": alerts.open_alerts(settings),
                "every_minutes": on_disk.schedule.get("alerts", alerts.ALERT_MINUTES),
                "kinds": [
                    {"id": kind, "label": label, "default": default}
                    for kind, (label, default) in alerts.KINDS.items()
                ],
                "routes": alerts.routes(settings),
            },
            "dropfolder": {
                "dir": on_disk.dropfolder.get("dir"),
                "effective": str(dropfolder.folder(on_disk)),
            },
            "thresholds": _threshold_summary(rt.engine),
            "cross_sync": cross_sync.overview(rt.engine),
            "restart_needed": _differs(settings, on_disk),
            "restart_tabs": restart_tabs(settings, on_disk),
            "can_restart": systemd_unit() is not None,
        }

    def best_source(on_disk, sources: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Per metric prefix the order in use: the saved one, else the default for what is
        connected and has data."""
        prefixes = sorted({*DEFAULT_BEST_SOURCE, *on_disk.best_source})
        connected = {s["id"] for s in sources if s["enabled"] or s["import_only"]}
        connected |= {sid for sid in rt.registry.available() if on_disk.source_enabled(sid)}
        defaults = views.default_source_order(rt.engine, prefixes, connected)
        return {p: list(on_disk.best_source.get(p) or defaults[p]) for p in prefixes}

    def login_status(sid: str) -> dict[str, Any]:
        if sid == "caldav":  # set up in the portal, not with a command
            connected = bool(SecretStore(settings.secrets_dir, sid).get("account"))
            return {
                "state": "logged in" if connected else "not logged in",
                "error": None,
                "setup": "#/settings/calendar",
            }
        if sid == "intervals_icu":
            from vibesynergy.sources.intervals_icu import credentials

            found = credentials.status(settings.secrets_dir)
            return {
                "state": "logged in" if found["set"] else "not logged in",
                "account": found["athlete_id"],
                # "environment": still the legacy env file; saving on the card moves it.
                "origin": found["origin"],
                "error": None,
                "write_access": None,
                "app_set": None,
            }
        tokens = SecretStore(settings.secrets_dir, sid).get("tokens") or {}
        problems = [
            d
            for d in views.sync_status(rt.engine, sid)["problems"]
            if any(marker in (d["last_error"] or "") for marker in LOGIN_ERRORS)
        ]
        state = "logged in" if tokens else "not logged in"
        if tokens and problems:
            state = "login failing"
        return {
            "state": state,
            "account": _account_name(SecretStore(settings.secrets_dir, sid).get("account")),
            "error": problems[0]["last_error"][:200] if problems else None,
            "write_access": (
                "granted"
                if "activity:write" in source_oauth.scope_set(tokens.get("scope"))
                else "not granted"
                if sid == "strava"
                else "available"
                if sid == "garmin" and bool(tokens)
                else "not granted"
            )
            if sid in {"strava", "garmin"}
            else None,
            "app_set": bool(SecretStore(settings.secrets_dir, sid).get("app"))
            if sid == "strava"
            else None,
        }

    @router.get("/settings")
    def get_settings() -> dict[str, Any]:
        return {"data": current()}

    @router.post("/settings")
    def change_settings(request: SettingsChange) -> dict[str, Any]:
        changes = request.changes

        def known_source(sid: str):
            try:
                return rt.registry.get(sid, require_enabled=False)
            except SourceError as exc:
                raise ValueError(str(exc)) from exc

        for sid, values in (changes.get("sources") or {}).items():
            source = known_source(sid)
            wanted = (values or {}).get("datasets")
            if wanted is not None:
                known = {s.name for s in source.datasets() if s.kind is not DatasetKind.FILE}
                unknown = set(wanted) - known
                if unknown:
                    raise ValueError(f"{sid} has no dataset {', '.join(sorted(unknown))}")
                defaults = {s.name for s in source.datasets() if s.default_enabled} & known
                if set(wanted) == defaults:  # the default set: drop the override
                    values["datasets"] = None
        for key in changes.get("schedule") or {}:
            if key in SERVICE_MINUTES:
                continue
            sid, _, dataset = key.partition(".")
            source = known_source(sid)
            if dataset and dataset not in {s.name for s in source.datasets()}:
                raise ValueError(f"{sid} has no dataset {dataset}")
        publication_change = changes.get("publication")
        if isinstance(publication_change, dict) and "auto_publish" in publication_change:
            publication_change.pop("auto_publish_since", None)
            if publication_change["auto_publish"] is True:
                path = settings.config_path
                was_on = path.exists() and load_settings(path).publication.get("auto_publish")
                if was_on is not True:
                    # Only activities that start from now on: switching on never backfills.
                    publication_change["auto_publish_since"] = utc_now().isoformat()
            else:
                publication_change["auto_publish"] = None  # off is the default
        try:
            saved = config_edit.apply_changes(settings.config_path, changes)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
        if "schedule" in changes:
            # The scheduler reads this same dict. Reload its due times now.
            settings.schedule.clear()
            settings.schedule.update(saved.schedule)
            if rt.scheduler is not None:
                rt.scheduler.reload()
        return {"data": current()}

    @router.post("/athlete/thresholds")
    def save_athlete_thresholds(request: AthleteThresholds) -> dict[str, Any]:
        """Save threshold heart rate and pace for the athlete. Does not write to Garmin."""
        if request.lthr is not None:
            if not 80 <= request.lthr <= 230:
                raise ValueError("Enter a threshold heart rate between 80 and 230.")
            _store_threshold(rt.engine, "lthr", request.lthr, "bpm")
        if request.threshold_pace is not None:
            if not 1 <= request.threshold_pace <= 20:
                raise ValueError("Enter a threshold pace between 1 and 20 min/km.")
            _store_threshold(rt.engine, "threshold_pace", request.threshold_pace, "min/km")
        return {"data": current()}

    @router.get("/settings/best_source")
    def best_source_overview() -> dict[str, Any]:
        """Per daily metric: the sources that deliver it, the order in use, the winner now."""
        return {"data": best_source_q.overview(rt.engine, rt.settings)}

    @router.post("/settings/best_source/metric")
    def best_source_metric(request: MetricOrder) -> dict[str, Any]:
        """Save the order for one metric (best first), or drop it with an empty order. Saved
        at once; the next derive (after a sync that brings data) applies it."""
        best_source_q.save(rt.settings.data_dir, request.metric, request.order)
        return {"data": best_source_q.overview(rt.engine, rt.settings)}

    @router.post("/settings/best_source/reset")
    def reset_best_source(request: ResetRequest) -> dict[str, Any]:
        """Back to the default order: for one prefix, or for all when none is given."""
        path = settings.config_path
        saved = load_settings(path).best_source if path.exists() else {}
        prefixes = sorted(saved) if request.prefix is None else [request.prefix]
        prefixes = [p for p in prefixes if p in saved]
        if prefixes:
            try:
                config_edit.apply_changes(path, {"best_source": dict.fromkeys(prefixes)})
            except ConfigError as exc:
                raise ValueError(str(exc)) from exc
        return {"data": current()}

    @router.post("/settings/portal_hosts")
    def portal_hosts(change: HostChange, request: Request) -> dict[str, Any]:
        """Add or remove a portal (sub)domain in ``[portal] hosts``. Applies after a restart;
        DNS and the reverse proxy outside VibeSynergy must point the name at the portal too."""
        path = settings.config_path
        on_disk = load_settings(path) if path.exists() else settings
        hosts = [str(h).lower() for h in on_disk.portal.get("hosts", [])]
        if change.action == "add":
            host = normalize_hostname(change.host)
            public = urlsplit(on_disk.public_url).hostname if on_disk.public_url else None
            if host in hosts or host in LOCAL_HOSTS or host == public:
                raise ValueError("That address is already allowed.")
            hosts.append(host)
        elif change.action == "remove":
            host = str(change.host or "").strip().lower()
            if host not in hosts:
                raise ValueError("That address is not in the list.")
            if host == (request.url.hostname or "").lower() and not change.confirm:
                raise ValueError(CURRENT_HOST_WARNING)
            hosts.remove(host)
        else:
            raise ValueError("action must be add or remove")
        try:
            config_edit.set_portal_hosts(path, hosts)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
        return {"data": current()}

    @router.post("/mail/password")
    def mail_password(request: SecretRequest) -> dict[str, Any]:
        """Store the mail password (write-only: it is never sent back to the page)."""
        if not request.value:
            raise ValueError("the password is empty")
        mailer.save_password(settings, request.value)
        return {"data": current()}

    @router.get("/alerts")
    def open_alerts() -> dict[str, Any]:
        """For the portal's banner: open alerts and recent events routed to the portal."""
        return {"data": alerts.for_portal(settings)}

    @router.post("/alerts/test")
    def alerts_test() -> dict[str, Any]:
        """Send a test alert with the settings as saved (the config file, not the running
        service), so a new setup can be tried before restarting."""
        on_disk = load_settings(settings.config_path)
        sent, errors = [], []
        if on_disk.alerts.get("email_to"):
            try:
                mailer.send(
                    on_disk, on_disk.alerts["email_to"], "VibeSynergy: test alert", TEST_TEXT
                )
                sent.append("email")
            except mailer.MailError as exc:
                errors.append(f"email: {exc}")
        if not sent and not errors:
            raise ValueError("no alert channel set up: add an email address (and mail settings)")
        return {"data": {"sent": sent, "errors": errors}}

    # Push notifications on this device -------------------------------------------

    @router.get("/bugs")
    def bug_reports() -> dict[str, Any]:
        return {
            "data": {
                "reports": bugs.listing(settings),
                "token_set": bugs.token_set(settings),
                "connection": bugs.connection(settings),
                "can_connect": bugs.client_id(settings) is not None,
                "repo": bugs.repo(settings),
            }
        }

    @router.post("/bugs")
    def report_bug(request: BugReport) -> dict[str, Any]:
        """Keep the report on this server and file it as a GitHub issue if a token is set up."""
        return {"data": bugs.create(settings, request.model_dump())}

    @router.post("/bugs/token")
    def bug_token(request: SecretRequest) -> dict[str, Any]:
        """The GitHub token for filing issues (write-only)."""
        bugs.save_token(settings, request.value)
        return bug_reports()

    @router.post("/bugs/connect")
    def bug_connect() -> dict[str, Any]:
        """Start connecting a GitHub account: the code to enter on github.com."""
        try:
            return {"data": bugs.start_connect(settings)}
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

    @router.post("/bugs/connect/poll")
    def bug_connect_poll() -> dict[str, Any]:
        try:
            return {"data": bugs.poll_connect(settings)}
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

    @router.post("/bugs/disconnect")
    def bug_disconnect() -> dict[str, Any]:
        bugs.disconnect(settings)
        return bug_reports()

    @router.post("/bugs/{report_id}/send")
    def send_bug(report_id: str) -> dict[str, Any]:
        return {"data": bugs.send(settings, report_id)}

    @router.get("/push")
    def push_state() -> dict[str, Any]:
        """The key a browser subscribes with, and the devices that get alerts as push."""
        from vibesynergy import webpush

        return {
            "data": {
                "key": webpush.public_key(settings),
                "devices": [
                    {"endpoint": d["endpoint"], "agent": d["agent"], "since": str(d["created_at"])}
                    for d in webpush.subscriptions(rt.engine)
                ],
            }
        }

    @router.post("/push/subscribe")
    def push_subscribe(request: PushSubscription) -> dict[str, Any]:
        from vibesynergy import webpush

        webpush.subscribe(rt.engine, request.endpoint, request.p256dh, request.auth, request.agent)
        _audit_portal("portal.push.subscribe", {"agent": request.agent[:80]})
        return {"data": {"subscribed": True}}

    @router.post("/push/unsubscribe")
    def push_unsubscribe(request: PushSubscription) -> dict[str, Any]:
        from vibesynergy import webpush

        webpush.unsubscribe(rt.engine, request.endpoint)
        _audit_portal("portal.push.unsubscribe", {})
        return {"data": {"subscribed": False}}

    @router.post("/push/test")
    def push_test() -> dict[str, Any]:
        from vibesynergy import webpush

        sent = webpush.send_all(rt, "VibeSynergy", TEST_TEXT, url="/#/settings/alerts")
        if not sent:
            raise ValueError("No device took the test notification. Turn push on again.")
        return {"data": {"sent": sent}}

    @router.post("/settings/restart")
    def restart() -> dict[str, Any]:
        """Restart the systemd unit so the saved settings apply (the page reloads after)."""
        unit = systemd_unit()
        if unit is None:
            raise ValueError("not running as a systemd user service; restart it yourself")
        # Delayed, so this answer reaches the browser first; --no-block hands the restart to
        # systemd, which carries it out even though it stops this process (and this shell).
        subprocess.Popen(
            ["sh", "-c", f"sleep 1 && systemctl --user --no-block restart {unit}"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"data": {"restarting": unit}}

    @router.get("/coach")
    def coach_settings() -> dict[str, Any]:
        """Coach page → Settings: how the coach talks to you, notes, things to remember."""
        return {
            "data": {
                "catalogue": coach.catalogue(),
                "policy": _with_conflicts(coach.state(rt.engine, "policy"), "policy"),
                "context": coach.state(rt.engine, "context"),
            }
        }

    @router.post("/coach/{kind}/preview")
    def coach_preview(kind: str, request: CoachPreview) -> dict[str, Any]:
        result = coach.preview(kind, request.payload)
        if kind == "policy":
            result["conflicts"] = coach.policy_conflicts(result["payload"])
        return {"data": result}

    @router.post("/coach/{kind}/draft")
    def coach_save(kind: str, request: CoachEdit) -> dict[str, Any]:
        saved = coach.save_draft(rt.engine, kind, **request.model_dump(), actor="portal")
        return {"data": _with_conflicts(saved, kind)}

    @router.post("/coach/{kind}/apply")
    def coach_apply(kind: str, request: CoachEdit) -> dict[str, Any]:
        """Save and activate in one step (immediate settings)."""
        result = coach.apply_active(rt.engine, kind, **request.model_dump(), actor="portal")
        return {"data": _with_conflicts(result, kind)}

    @router.post("/coach/{kind}/activate")
    def coach_activate(kind: str, request: CoachActivate) -> dict[str, Any]:
        result = coach.activate(rt.engine, kind, **request.model_dump(), actor="portal")
        return {"data": _with_conflicts(result, kind)}

    @router.post("/coach/{kind}/restore")
    def coach_restore(kind: str, request: CoachRestore) -> dict[str, Any]:
        return {
            "data": _with_conflicts(
                coach.restore_draft(rt.engine, kind, **request.model_dump(), actor="portal"),
                kind,
            )
        }

    @router.get("/icu-notes/status")
    def icu_notes_status() -> dict[str, Any]:
        """Whether intervals.icu keeps wellness comments private (read live, nothing written)."""
        from vibesynergy.query import icu_notes
        from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient, IntervalsIcuError

        try:
            client = IntervalsIcuClient.from_env()
            return {"data": {"connected": True, **icu_notes.status(client)}}
        except IntervalsIcuError:
            return {"data": {"connected": False, "private": False}}

    @router.get("/prefs")
    def get_prefs() -> dict[str, Any]:
        return {"data": prefs.load(settings.data_dir)}

    @router.post("/prefs")
    def change_prefs(request: PrefsChange) -> dict[str, Any]:
        """The portal's own preferences (the menu order): saved at once, no restart."""
        return {"data": prefs.save(settings.data_dir, request.changes)}

    @router.get("/share_links")
    def share_links() -> dict[str, Any]:
        """Links that still open. Revoked and expired ones stay in the database and show
        up in the log on Settings -> Sharing."""
        links = [link for link in share.list_links(rt.engine) if link["state"] == "active"]
        return {"data": {"links": links, "pages": list(share.CHOICES), "all": share.ALL}}

    @router.post("/share_links")
    def create_share_link(request: ShareRequest) -> dict[str, Any]:
        if not settings.public_url:
            raise ValueError("share links need [server] public_url (the public address)")
        code, row = share.create(
            rt.engine,
            request.title,
            request.pages,
            can_write=request.can_write,
            expires_in_days=request.expires_in_days,
        )
        url = f"{settings.public_url}/vibesynergy/share/{code}/"
        return {"data": {"link": row, "url": url, **share_links()["data"]}}

    @router.post("/share_links/revoke")
    def revoke_share_link(request: RevokeShareRequest) -> dict[str, Any]:
        if not share.revoke(rt.engine, request.id):
            raise ValueError("no active link with that id")
        return share_links()

    @router.post("/share_links/{link_id}")
    def edit_share_link(link_id: str, request: ShareEdit) -> dict[str, Any]:
        """Change an open link in place (pages, title, write access); saved directly."""
        try:
            row = share.edit(rt.engine, link_id, **request.model_dump())
        except LookupError as exc:
            raise ValueError(str(exc)) from exc
        return {"data": {"link": row}}

    def _iso(value: Any) -> str | None:
        return value.isoformat() if value else None

    def _mcp_payload() -> dict[str, Any]:
        from vibesynergy.mcp.scopes import COACH_SCOPES, SCOPE_LABELS, SCOPES

        rows = mcp_oauth.list_clients(rt.engine)
        for row in rows:
            for key in ("created", "approved", "revoked"):
                row[key] = _iso(row[key])
        tokens = mcp_oauth.list_personal_access_tokens(rt.engine)
        for row in tokens:
            for key in ("created", "last_used", "expires_at", "revoked"):
                row[key] = _iso(row[key])
        names: dict[str, str] = {}
        for row in rows:
            label = row.get("display_name") or row.get("name")
            if label:
                names[f"client:{row['client_id']}"] = label
        for row in tokens:
            names[f"client:pat:{row['id']}"] = row["display_name"] or row["name"]
        store = SecretStore(settings.secrets_dir, "mcp")
        return {
            "clients": rows,
            "tokens": tokens,
            "names": names,
            "passphrase_set": bool(store.get("passphrase")),
            "scopes": [{"id": scope, "label": SCOPE_LABELS[scope]} for scope in SCOPES],
            "coach_scopes": list(COACH_SCOPES),
            "mcp_url": (f"{settings.public_url}/vibesynergy/mcp" if settings.public_url else None),
        }

    @router.get("/mcp_clients")
    def mcp_clients() -> dict[str, Any]:
        return {"data": _mcp_payload()}

    @router.post("/mcp_clients/revoke")
    def revoke(request: RevokeRequest) -> dict[str, Any]:
        if not mcp_oauth.revoke_client(rt.engine, request.client_id):
            raise ValueError("No active app with that id.")
        return {"data": _mcp_payload()}

    @router.post("/mcp_clients/rename")
    def rename_client(request: ClientRename) -> dict[str, Any]:
        if not mcp_oauth.rename_oauth_client(rt.engine, request.client_id, request.display_name):
            raise ValueError("No app with that id.")
        return {"data": _mcp_payload()}

    @router.get("/mcp/pats")
    def list_pats() -> dict[str, Any]:
        return {"data": _mcp_payload()}

    # Wrong passphrases on access-granting Settings writes: the same rule as the app
    # approve page (5 per 15 minutes), counted for the portal as one key.
    limiter = mcp_oauth.PassphraseLimiter()

    def confirm_passphrase(passphrase: str, action: str) -> None:
        store = SecretStore(settings.secrets_dir, "mcp")
        if not store.get("passphrase"):
            raise ValueError("Set the coach passphrase first.")
        if limiter.locked("portal"):
            _audit_portal("portal.passphrase.locked", {"action": action})
            raise ValueError("Too many wrong attempts. Try again in 15 minutes.")
        if not mcp_oauth.check_passphrase(store, passphrase):
            limiter.fail("portal")
            _audit_portal("portal.passphrase.denied", {"action": action})
            raise ValueError("The passphrase does not match.")

    def _audit_portal(action: str, detail: dict[str, Any]) -> None:
        with rt.engine.begin() as conn:
            conn.execute(
                audit_log.insert().values(
                    at=utc_now(), actor="portal", action=action, detail=detail
                )
            )

    @router.post("/settings/update")
    def update_now(request: UpdateBody) -> dict[str, Any]:
        """Settings → System → Update now: your password (or, without a login, the coach
        passphrase), then the update unit."""
        from vibesynergy.query import about, auth

        account = auth.account(rt.engine)
        if account and account["password_hash"]:
            if limiter.locked("portal"):
                raise ValueError("Too many wrong attempts. Try again in 15 minutes.")
            if not auth.verify_password(request.password, account["password_hash"]):
                limiter.fail("portal")
                _audit_portal("portal.password.denied", {"action": "update"})
                raise ValueError("Your current password is not right.")
        else:
            confirm_passphrase(request.passphrase, "update")
        started = about.start_update(rt)
        _audit_portal("portal.update.start", started)
        return {"data": started}

    @router.post("/mcp/pats")
    def create_pat(request: PatCreate) -> dict[str, Any]:
        from sqlalchemy.exc import IntegrityError

        confirm_passphrase(request.passphrase, "create_pat")
        try:
            token = mcp_oauth.create_personal_access_token(
                rt.engine,
                request.name,
                scopes=request.scopes,
                days=request.days,
                display_name=request.display_name,
            )
        except IntegrityError as exc:
            raise ValueError("That name is already used.") from exc
        payload = _mcp_payload()
        # The secret is in this response only. Later reads show the prefix.
        payload["token"] = token
        return {"data": payload}

    @router.post("/mcp/pats/revoke")
    def revoke_pat(request: PatId) -> dict[str, Any]:
        if not mcp_oauth.revoke_personal_access_token(rt.engine, request.id):
            raise ValueError("No active token with that id.")
        return {"data": _mcp_payload()}

    @router.post("/mcp/pats/rename")
    def rename_pat(request: PatRename) -> dict[str, Any]:
        if not mcp_oauth.rename_personal_access_token(rt.engine, request.id, request.display_name):
            raise ValueError("No token with that id.")
        return {"data": _mcp_payload()}

    @router.post("/mcp/passphrase")
    def set_mcp_passphrase(request: PassphraseBody) -> dict[str, Any]:
        store = SecretStore(settings.secrets_dir, "mcp")
        if store.get("passphrase"):
            if limiter.locked("portal"):
                raise ValueError("Too many wrong attempts. Try again in 15 minutes.")
            if not (request.current and mcp_oauth.check_passphrase(store, request.current)):
                limiter.fail("portal")
                _audit_portal("portal.passphrase.denied", {"action": "change_passphrase"})
                raise ValueError("The current passphrase does not match.")
        mcp_oauth.set_passphrase(store, request.new)
        return {"data": {"set": True}}

    @router.get("/mcp/passphrase")
    def passphrase_status() -> dict[str, Any]:
        store = SecretStore(settings.secrets_dir, "mcp")
        return {"data": {"set": bool(store.get("passphrase"))}}

    @router.get("/mcp/activity")
    def mcp_activity(client_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        from sqlalchemy import select

        capped = min(max(limit, 1), 200)
        query = (
            select(audit_log)
            .where(audit_log.c.actor == "mcp")
            .order_by(audit_log.c.id.desc())
            .limit(capped)
        )
        if client_id:
            query = query.where(audit_log.c.client_id == client_id)
        with rt.engine.connect() as conn:
            rows = conn.execute(query).all()
        events = [
            {
                "id": row.id,
                "at": _iso(row.at),
                "client_id": row.client_id,
                "author": row.author,
                "action": row.action,
                "scope": row.scope,
                "target": row.target,
                "outcome": row.outcome,
            }
            for row in rows
        ]
        if not client_id:
            events = sorted(events + _access_ended(), key=lambda e: e["at"] or "", reverse=True)[
                :capped
            ]
        return {"data": {"events": events}}

    def _access_ended() -> list[dict[str, Any]]:
        """When an app, a token or a share link stopped working, read from their own rows.
        The lists show only what still has access; this is where the history lives."""
        now = utc_now()
        out: list[dict[str, Any]] = []

        def add(kind: str, ident: str, name: str, at: Any) -> None:
            when = at if isinstance(at, str) else _iso(at)
            out.append({"id": f"{kind}:{ident}", "at": when, "action": kind, "author": name})

        for row in mcp_oauth.list_clients(rt.engine):
            if row["revoked"]:
                name = row["display_name"] or row["name"] or row["client_id"]
                add("access.app_revoked", row["client_id"], name, row["revoked"])
        for row in mcp_oauth.list_personal_access_tokens(rt.engine):
            name = row["display_name"] or row["name"]
            if row["revoked"]:
                add("access.token_revoked", row["id"], name, row["revoked"])
            elif row["expires_at"] and row["expires_at"] <= now:
                add("access.token_expired", row["id"], name, row["expires_at"])
        for link in share.list_links(rt.engine):
            if link["state"] == "revoked":
                add("access.link_revoked", link["id"], link["title"], link["revoked_at"])
            elif link["state"] == "expired":
                add("access.link_expired", link["id"], link["title"], link["expires_at"])
        return out

    return router


def _account_name(account: Any) -> str | None:
    if not isinstance(account, dict):
        return None
    for key in ("name", "email", "username"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _threshold_summary(engine) -> dict[str, Any]:
    run = thresholds.current_values(engine).get("run") or {}

    def pick(metric: str, *, per_minute: bool = False) -> dict[str, Any] | None:
        row = run.get(metric)
        if not row:
            return None
        if per_minute:  # this form asks for minutes per km
            return {"value": round(row["value"] / 60, 2), "unit": "min/km", "sport": "run"}
        return {"value": row["value"], "unit": row["unit"], "sport": "run"}

    return {"lthr": pick("lthr"), "threshold_pace": pick("threshold_pace", per_minute=True)}


def _store_threshold(engine, metric: str, value: float, unit: str) -> None:
    seconds = float(value) * 60 if unit == "min/km" else float(value)
    thresholds.set_value(engine, sport="run", metric=metric, value=seconds)


def portal_url(settings) -> str | None:
    """The portal's tailnet address: the machine's name from the public address, on the
    portal port (tailscale serve publishes that port on the tailnet only)."""
    if not settings.public_url:
        return None
    host = urlsplit(settings.public_url).hostname
    return f"https://{host}:{settings.portal.get('port', 8447)}" if host else None


# Settings tab that holds each config field the running service only reads at start, so
# the portal shows the restart notice only where a saved change still waits for a restart.
RESTART_TABS: dict[str, tuple[str, ...]] = {
    "timezone": ("system",),
    "units": ("system",),
    "athlete": ("training",),  # Training → Zones & thresholds, "About you"
    "sources": ("sources",),  # datasets live on the source cards
    "schedule": ("sources", "system"),  # per source on its card; service checks in System
    "portal": ("system",),
    "best_source": ("system",),
    "mail": ("alerts",),
    "alerts": ("alerts",),
    "dropfolder": ("system",),
}


def restart_tabs(running, on_disk) -> list[str]:
    """Settings tabs with a saved change that applies after the next restart."""
    tabs = {
        tab
        for field, owners in RESTART_TABS.items()
        if getattr(running, field) != getattr(on_disk, field)
        for tab in owners
    }
    return sorted(tabs)


def _differs(running, on_disk) -> bool:
    fields = (
        "timezone",
        "units",
        "athlete",
        "sources",
        "schedule",
        "portal",
        "best_source",
        "mail",
        "alerts",
        "dropfolder",
    )
    return any(getattr(running, f) != getattr(on_disk, f) for f in fields)
