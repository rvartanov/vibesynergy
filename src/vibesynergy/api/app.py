"""HTTP service: webhook receivers and the OAuth landing page.

Public routes live under ``/vibesynergy/`` so the Tailscale Funnel on port 443 can forward just
that path to this service (the Garmin MCP connector keeps the rest of 443). They are also
served without the prefix, so it does not matter whether the proxy strips it. Webhook
handlers only record a job and answer immediately, because providers expect a reply
within seconds; the worker does the fetching.

    GET  /vibesynergy/health
    GET  /vibesynergy/{source}/callback           OAuth landing page (paste-back login)
    HEAD|GET|POST /vibesynergy/withings/notify/{token}
    GET  /vibesynergy/strava/webhook              subscription validation (hub.challenge)
    POST /vibesynergy/strava/webhook              activity and athlete events
    *    /vibesynergy/mcp                         MCP (streamable HTTP; OAuth 2.1 or a PAT)
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import re
import time
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import Window, today_in, utc_now
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth as source_oauth
from vibesynergy.store.schema import audit_log

log = logging.getLogger(__name__)

_SOURCE_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_HTTP_STATUS_RE = re.compile(r"\((\d{3})\)")


def _safe_source(source_id: str) -> str:
    """The callback path segment as a log-safe name (it is caller-controlled)."""
    name = source_id.lower()[:32]
    return name if name and set(name) <= _SOURCE_NAME_CHARS else "other"


def _http_status(exc: BaseException) -> str:
    """The HTTP status of a failed token request, taken only from its ``(NNN)`` marker."""
    match = _HTTP_STATUS_RE.search(str(exc))
    return match.group(1) if match else "none"


WEBHOOK = "webhook"  # secret-store document holding each source's webhook secrets
PRIORITY = 10  # webhook jobs run before scheduled syncs and backfills

# Withings notification category (appli) -> datasets to re-sync for the notified window.
WITHINGS_APPLI = {
    1: ["measures"],  # weight and body composition
    4: ["measures", "heart"],  # blood pressure, pulse, SpO2
    16: ["activity", "intraday_activity", "workouts"],
    44: ["sleep_summary", "sleep_series"],
    54: ["heart"],  # ECG
    62: ["measures"],  # HRV / other measures
}


def create_app(rt: Runtime, *, with_mcp: bool = True) -> FastAPI:
    settings = rt.settings
    mcp_app = None
    if with_mcp:
        from mcp.server.transport_security import TransportSecuritySettings

        from vibesynergy.mcp.server import build_server

        hosts = ["127.0.0.1:*", "localhost:*"]
        if settings.public_url:
            hosts.append(settings.public_url.removeprefix("https://").split("/")[0])
        mcp_app = build_server(rt).streamable_http_app(
            streamable_http_path="/mcp",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[]
            ),
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mcp_app is None:
            yield
            return
        async with mcp_app.router.lifespan_context(mcp_app):  # runs the MCP session manager
            yield

    app = FastAPI(
        title="VibeSynergy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    router = APIRouter()
    if mcp_app is not None and settings.public_url:
        # MCP with OAuth 2.1 at <public_url>/vibesynergy/mcp; approval by passphrase.
        from starlette.routing import Route

        from vibesynergy.api import oauth

        provider = oauth.VibeSynergyOAuthProvider(
            rt.engine,
            SecretStore(settings.secrets_dir, "mcp"),
            issuer=f"{settings.public_url}/vibesynergy",
            resource=f"{settings.public_url}/vibesynergy/mcp",
        )
        app.state.oauth = provider
        app.router.routes.extend(oauth.routes(provider))
        from vibesynergy.mcp.asgi import McpIdentityMiddleware

        oauth_mcp = McpIdentityMiddleware(mcp_app, "oauth")
        guarded = oauth.BearerGuard(oauth_mcp, provider, "/mcp")
        app.router.routes.append(Route("/vibesynergy/mcp", guarded))
        app.router.routes.append(Route("/mcp", guarded))

    def secrets(source_id: str) -> SecretStore:
        return SecretStore(settings.secrets_dir, source_id)

    def audit(action: str, detail: dict[str, Any]) -> None:
        with rt.engine.begin() as conn:
            conn.execute(
                audit_log.insert().values(
                    at=utc_now(), actor="webhook", action=action, detail=detail
                )
            )

    @router.get("/health")
    def health() -> dict:
        from vibesynergy.sync import lanes

        lane_status = lanes.status()
        ok = lanes.all_alive() if lane_status else True
        return {"ok": ok, "jobs": rt.jobs.counts(), "lanes": lane_status}

    def _oauth_page(body: str) -> str:
        return (
            "<!doctype html><meta charset=utf-8><title>VibeSynergy login</title>"
            "<body style='font-family:system-ui;max-width:36em;margin:3em auto'>"
            f"{body}</body>"
        )

    def _strava_portal_redirect(pending: dict[str, Any], result: str) -> RedirectResponse | str:
        portal = (pending.get("portal") or "").strip()
        if portal:
            return RedirectResponse(
                source_oauth.strava_portal_return_url(portal, result), status_code=303
            )
        messages = {
            "connected": "<h1>Strava connected</h1>"
            "<p>Return to VibeSynergy → Settings → Sources.</p>",
            "denied": "<h1>Strava access was not approved</h1>"
            "<p>Return to VibeSynergy → Settings → Sources and try again.</p>",
            "write_missing": "<h1>Write access missing</h1>"
            "<p>Return to VibeSynergy → Settings → Sources and try again with "
            "&ldquo;Upload your activities&rdquo; checked.</p>",
            "expired": "<h1>Sign-in link expired</h1>"
            "<p>Return to VibeSynergy → Settings → Sources and try again.</p>",
            "failed": "<h1>Strava connection failed</h1>"
            "<p>Return to VibeSynergy → Settings → Sources and try again.</p>",
        }
        return _oauth_page(messages.get(result, messages["failed"]))

    # Withings checks the callback URL answers (not only with GET) when it is saved.
    @router.api_route(
        "/{source_id}/callback",
        methods=["GET", "HEAD", "POST"],
        response_class=HTMLResponse,
        response_model=None,
    )
    def oauth_landing(
        source_id: str,
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
        scope: str | None = None,
    ):
        # Outcome logging: names and booleans only, never codes, tokens, state or URLs.
        def _log_outcome(outcome: str, **extra: Any) -> None:
            details = "".join(f" {k}={v}" for k, v in extra.items())
            log.info(
                "OAuth callback %s: outcome=%s has_code=%s has_error=%s has_state=%s%s",
                _safe_source(source_id),
                outcome,
                bool(code),
                bool(error),
                bool(state),
                details,
            )

        if source_id == "strava" and state and source_oauth.STRAVA_PORTAL_STATE_RE.fullmatch(state):
            pending_store = SecretStore(settings.secrets_dir, source_oauth.PUBLICATION_OAUTH)
            pending = source_oauth.take_publication_oauth_pending(pending_store, state)
            if pending:
                now = time.time()
                result = source_oauth.strava_portal_oauth_result(
                    pending, code=code, error=error, scope=scope, now=now
                )
                if result == "connected":
                    from vibesynergy.sources.http_timeout import http_timeout_seconds
                    from vibesynergy.sources.strava.client import StravaClient

                    store = SecretStore(settings.secrets_dir, "strava")
                    try:
                        StravaClient(
                            store, timeout=http_timeout_seconds(settings, "strava")
                        ).exchange_code(source_oauth.load_app(store, "strava"), code, scope)
                    except Exception as exc:
                        result = "failed"
                        _log_outcome(
                            "failed",
                            reason="exchange_failed",
                            error_type=type(exc).__name__,
                            http_status=_http_status(exc),
                        )
                    else:
                        _log_outcome("connected")
                else:
                    reason = {
                        "denied": "error_param",
                        "expired": "state_expired",
                        "failed": "missing_code",
                        "write_missing": "scope_missing_write",
                    }.get(result, "other")
                    _log_outcome(result, reason=reason)
                return _strava_portal_redirect(pending, result)
            _log_outcome("expired", reason="state_unknown_or_used")
            return _oauth_page(
                "<h1>Sign-in link expired or already used</h1>"
                "<p>Return to VibeSynergy → Settings → Sources and try again.</p>"
            )
        # A `vibesynergy auth` waiting for this login takes the code from here by itself.
        handoff = SecretStore(settings.secrets_dir, source_oauth.HANDOFF)
        if state and source_oauth.complete_handoff(handoff, state, code=code, error=error):
            _log_outcome(
                "handoff_denied" if error else "handoff_stored",
                reason="error_param" if error else ("ok" if code else "missing_code"),
            )
            if error:
                return _oauth_page("<h1>Not approved</h1><p>The terminal shows what happened.</p>")
            return _oauth_page(
                "<h1>Logged in</h1><p>VibeSynergy received the approval; the terminal "
                "finishes the login. You can close this tab.</p>"
            )
        _log_outcome("paste_back", reason="state_mismatch" if state else "no_state")
        return _oauth_page(
            f"<h1>Almost done</h1><p>Copy this page's full address from the address bar "
            f"and paste it into the terminal where <code>vibesynergy auth {source_id}</code> is "
            "waiting.</p>"
        )

    # Withings ---------------------------------------------------------------------------

    def _digest_ok(expected: str, given: str) -> bool:
        """Constant-time token compare; bytes so a non-ASCII token (scanner noise,
        a truncated URL carrying '…') gets a 404/403 instead of a 500 TypeError."""
        return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))

    def check_withings_token(token: str) -> None:
        stored = (secrets("withings").get(WEBHOOK) or {}).get("token")
        if not stored or not _digest_ok(stored, token):
            raise HTTPException(404)

    @router.api_route("/withings/notify/{token}", methods=["GET", "HEAD"])
    def withings_ping(token: str) -> Response:
        check_withings_token(token)  # Withings checks the URL answers when subscribing
        return Response(status_code=200)

    @router.post("/withings/notify/{token}")
    async def withings_notify(token: str, request: Request) -> Response:
        check_withings_token(token)
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        appli = int(form.get("appli", 0) or 0)
        window = _withings_window(form, settings.timezone)
        enabled = {d.name for d in rt.registry.get("withings").sync_datasets()}
        datasets = [d for d in WITHINGS_APPLI.get(appli, ["measures"]) if d in enabled]
        for dataset in datasets:
            rt.jobs.enqueue(
                "sync",
                {"source": "withings", "dataset": dataset, "window": str(window)},
                priority=PRIORITY,
                dedupe_key=f"sync:withings:{dataset}:{window}",
            )
        audit("withings.notify", {"appli": appli, "window": str(window), "datasets": datasets})
        return Response(status_code=200)

    # Strava -----------------------------------------------------------------------------

    @router.get("/strava/webhook")
    def strava_validate(request: Request) -> JSONResponse:
        params = request.query_params
        expected = (secrets("strava").get(WEBHOOK) or {}).get("verify_token")
        given = params.get("hub.verify_token", "")
        if params.get("hub.mode") != "subscribe" or not expected:
            raise HTTPException(403)
        if not _digest_ok(expected, given):
            raise HTTPException(403)
        return JSONResponse({"hub.challenge": params.get("hub.challenge", "")})

    @router.post("/strava/webhook")
    async def strava_event(request: Request) -> Response:
        event = await request.json()
        hook = secrets("strava").get(WEBHOOK) or {}
        athlete = (secrets("strava").get("athlete") or {}).get("id")
        if hook.get("subscription_id") and event.get("subscription_id") != hook["subscription_id"]:
            raise HTTPException(403)
        if athlete and event.get("owner_id") != athlete:
            raise HTTPException(403)
        kind, aspect = event.get("object_type"), event.get("aspect_type")
        object_id = str(event.get("object_id"))
        event_time = event.get("event_time")
        event_time = event_time if type(event_time) is int and 0 <= event_time < 2**63 else None
        received_at = utc_now().isoformat()
        queued_jobs = []
        if kind == "activity" and aspect in ("create", "update"):
            from vibesynergy.query import latency

            latency.stamp(
                rt.engine,
                "strava",
                object_id,
                source_event_at=(
                    datetime.fromtimestamp(event_time, UTC)
                    if event_time is not None and event_time <= 253402300799
                    else None
                ),
                received_at=received_at,
            )
            datasets = ["activities", "activity_streams"] if aspect == "create" else ["activities"]
            for dataset in datasets:
                job_id = rt.jobs.enqueue(
                    "fetch_one",
                    {
                        "source": "strava",
                        "dataset": dataset,
                        "key": object_id,
                        "event_time": event_time,
                        "received_at": received_at,
                    },
                    priority=PRIORITY,
                    dedupe_key=f"fetch_one:strava:{dataset}:{object_id}",
                )
                queued_jobs.append({"dataset": dataset, "job_id": job_id})
                if dataset == "activities" and job_id is not None:
                    latency.stamp(rt.engine, "strava", object_id, queued_at=utc_now())
        elif kind == "activity" and aspect == "delete":
            rt.jobs.enqueue(
                "tombstone",
                {"source": "strava", "dataset": "activities", "key": object_id},
                priority=PRIORITY,
            )
        fields = ("object_type", "object_id", "aspect_type", "updates")
        audit(
            "strava.event",
            {
                **{k: event.get(k) for k in fields},
                "event_time": event_time,
                "received_at": received_at,
                "queued_jobs": queued_jobs,
            },
        )
        return Response(status_code=200)

    # Served with and without the /vibesynergy prefix, whichever way the Funnel forwards it.
    app.include_router(router, prefix="/vibesynergy")
    app.include_router(router)
    if settings.public_url:
        # Share links: chosen portal pages at <public_url>/vibesynergy/share/<code>/. The Funnel
        # strips /vibesynergy, so they arrive as /share/<code>/; both are served, like the
        # routes above. First in the route list, so no other mount can take these paths.
        from starlette.routing import Mount

        from vibesynergy.portal.app import ShareGate, create_portal_app

        gate = ShareGate(rt, create_portal_app(rt, share_mode=True))
        app.router.routes[0:0] = [Mount("/vibesynergy/share", app=gate), Mount("/share", app=gate)]
    return _gzip_api(app)


class _GzipApi:
    """GZip JSON responses on the API app, never MCP or the mounted share portal."""

    def __init__(self, app: FastAPI) -> None:
        from starlette.middleware.gzip import GZipMiddleware

        self._app = app
        self._gzip = GZipMiddleware(app, minimum_size=500)

    def __getattr__(self, name: str):
        return getattr(self._app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if (
            path == "/mcp"
            or path.endswith("/mcp")
            or path.startswith("/share")
            or path.startswith("/vibesynergy/share")
        ):
            await self._app(scope, receive, send)
            return
        await self._gzip(scope, receive, send)


def _gzip_api(app: FastAPI):
    return _GzipApi(app)


def _withings_window(form: dict[str, str], timezone: str) -> Window:
    """Local dates covered by a notification (startdate/enddate epochs, or date)."""
    tz = ZoneInfo(timezone)
    days: list[date] = []
    for key in ("startdate", "enddate"):
        if form.get(key, "").isdigit():
            days.append(datetime.fromtimestamp(int(form[key]), UTC).astimezone(tz).date())
    if not days and form.get("date"):
        with contextlib.suppress(ValueError):
            days.append(date.fromisoformat(form["date"][:10]))
    if not days:
        days.append(today_in(timezone))
    return Window(min(days), max(days))
