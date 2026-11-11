"""The portal behind a login (query/auth.py): every page and API needs a session.

Open without a session: the login page and its assets (``/login``, ``/static/…``, the
service worker, manifest and icons) and the login API (``/auth/…``). Everything else
answers 401 (API) or redirects to ``/login`` (pages). ``[portal] auth = false`` turns the
gate off, for an installation that is only reachable on a private network.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "vs_session"
OPEN_PREFIXES = ("/static/", "/auth/")
OPEN_PATHS = {
    "/login",
    "/sw.js",
    "/manifest.webmanifest",
    "/favicon.ico",
    "/icon.svg",
    "/version",
    "/vibesynergy/version",
}


def auth_required(settings: Any) -> bool:
    return bool((settings.portal or {}).get("auth", True))


def cookie_value(scope: Scope) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE:
                    return val
    return None


class AuthGate:
    def __init__(self, app: ASGIApp, rt: Any) -> None:
        self.app, self.rt = app, rt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not auth_required(self.rt.settings):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return
        from vibesynergy.query import auth

        current = auth.session(self.rt.engine, cookie_value(scope))
        if current is not None and current.mfa_ok:
            scope.setdefault("state", {})["auth_session"] = current
            await self.app(scope, receive, send)
            return
        if path.startswith("/api/"):
            response = JSONResponse({"error": "Log in first."}, status_code=401)
        else:
            response = RedirectResponse(f"/login?next={quote(path, safe='/')}", status_code=303)
        await response(scope, receive, send)
