"""Settings → Sources → Calendar: sign in to an iCal / CalDAV account, choose calendars, disconnect.

Everything is write-only for secrets: the page gets ``connected``/``password_set``/
``client_set`` flags, never a password, token or client secret. Errors carry a ``code`` the
page translates; messages never include what was typed. Google signs in through OAuth: the
browser is sent to Google and back to ``/api/caldav/google/callback`` on this portal.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from vibesynergy.core.config import DEFAULT_PORTAL_PORT
from vibesynergy.core.timeutil import today_in
from vibesynergy.portal.hosts import tailnet_address
from vibesynergy.query import calendar_context, calendar_mirror, views
from vibesynergy.runtime import Runtime
from vibesynergy.sources.caldav import account, google
from vibesynergy.sources.caldav.discovery import PRESETS, CalDAVError
from vibesynergy.sources.caldav.writer import CalendarWriter
from vibesynergy.sources.http_timeout import http_timeout_seconds, requests_timeout_pair

log = logging.getLogger(__name__)

CALLBACK_PATH = "/api/caldav/google/callback"


class Connect(BaseModel):
    provider: str
    username: str
    password: str
    url: str | None = None


class GoogleApp(BaseModel):
    client_id: str
    client_secret: str


class Selection(BaseModel):
    urls: list[str]


class Classification(BaseModel):
    url: str
    classification: str


class GarminTarget(BaseModel):
    url: str | None = None


class MirrorEnabled(BaseModel):
    enabled: bool


def redirect_uri(settings) -> str:
    """Where Google sends the browser back: this portal (tailnet address, else localhost).

    Always the tailnet address, never a domain from ``[portal] hosts``: this exact URI is
    registered with the Google OAuth client, and Google refuses a sign-in whose redirect URI
    differs. Adding a domain later must not move it."""
    port = settings.portal.get("port", DEFAULT_PORTAL_PORT)
    base = tailnet_address(settings) or f"http://localhost:{port}"
    return base + CALLBACK_PATH


def build_caldav_router(rt: Runtime, http=None) -> APIRouter:
    """``http``: a requests-like object for tests; None uses real sessions."""
    router = APIRouter()
    settings = rt.settings

    def full_status() -> dict[str, Any]:
        data = account.status(settings)
        datasets = views.sync_status(rt.engine, "caldav")["datasets"]
        calendar = next((d for d in datasets if d["dataset"] == "calendar"), None)
        data["sync"] = (
            {
                "last_attempt_at": calendar["last_attempt_at"],
                "last_success_at": calendar["last_success_at"],
                "problem": calendar["problem"],
            }
            if calendar
            else None
        )
        data["providers"] = [
            {"id": pid, "name": p["name"], "url": p["url"], "kind": p["kind"]}
            for pid, p in PRESETS.items()
        ]
        data["google"]["redirect_uri"] = redirect_uri(settings)
        return data

    def guarded(call):
        try:
            return {"data": call()}
        except CalDAVError as exc:
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=400)

    @router.get("/caldav")
    def get_status() -> dict[str, Any]:
        return {"data": full_status()}

    @router.post("/caldav/connect")
    def connect(request: Connect):
        def run():
            account.sign_in(
                settings, request.provider, request.url, request.username, request.password, http
            )
            return full_status()

        return guarded(run)

    @router.post("/caldav/discover")
    def discover():
        return guarded(lambda: (account.rediscover(settings, http), full_status())[1])

    @router.post("/caldav/calendars")
    def save_calendars(request: Selection):
        return guarded(lambda: (account.save_selection(settings, request.urls), full_status())[1])

    @router.post("/caldav/classification")
    def save_classification(request: Classification):
        def run():
            account.save_classification(settings, request.url, request.classification)
            calendar_context.classify_calendar(rt.engine, request.url, request.classification)
            return full_status()

        return guarded(run)

    @router.post("/caldav/garmin-target")
    def save_garmin_target(request: GarminTarget):
        return guarded(
            lambda: (account.save_garmin_target(settings, request.url), full_status())[1]
        )

    @router.post("/caldav/mirror")
    def save_mirror(request: MirrorEnabled):
        return guarded(
            lambda: (account.save_mirror_enabled(settings, request.enabled), full_status())[1]
        )

    @router.get("/caldav/mirror-preview")
    def mirror_preview():
        def run():
            target = account.garmin_target(settings)
            if not target:
                raise CalDAVError("no_target")
            store = account.store_for(settings)
            stored = store.get(account.ACCOUNT)
            if not stored:
                raise CalDAVError("not_connected")
            request_timeout = requests_timeout_pair(http_timeout_seconds(settings, "caldav"))
            writer = CalendarWriter(
                account.client_for(store, stored, http, request_timeout=request_timeout)
            )
            return calendar_mirror.preview(
                rt.engine,
                writer,
                calendar_url=target,
                account_id=calendar_mirror.account_id(stored),
                today=today_in(settings.timezone),
            )

        return guarded(run)

    @router.post("/caldav/disconnect")
    def disconnect():
        return guarded(lambda: (account.disconnect(settings), full_status())[1])

    @router.post("/caldav/google/app")
    def google_app(request: GoogleApp):
        return guarded(
            lambda: (
                google.save_app(
                    account.store_for(settings), request.client_id, request.client_secret
                ),
                full_status(),
            )[1]
        )

    @router.post("/caldav/google/start")
    def google_start():
        return guarded(
            lambda: {
                "authorization_url": google.start(
                    account.store_for(settings), redirect_uri(settings)
                )
            }
        )

    @router.get(CALLBACK_PATH.removeprefix("/api"), include_in_schema=False)
    def google_callback(code: str = "", state: str = "", error: str = "") -> RedirectResponse:
        """Google sends the browser here. The result goes back to the page as ``?google=``
        (ok or an error code); codes and tokens never appear in the page address."""
        store = account.store_for(settings)
        try:
            if error:
                store.delete(google.LOGIN)
                raise CalDAVError("google_denied")
            google.complete(store, redirect_uri(settings), code, state, http)
            account.sign_in_google(settings, http)
            result = "ok"
        except CalDAVError as exc:
            result = exc.code
        log.info(
            "OAuth callback google-calendar: outcome=%s has_code=%s has_error=%s has_state=%s",
            result if result.replace("_", "").isalnum() else "other",
            bool(code),
            bool(error),
            bool(state),
        )
        return RedirectResponse(f"/#/settings/calendar?google={result}", status_code=303)

    return router
