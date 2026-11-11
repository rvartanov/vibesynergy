"""The web portal: a tailnet-only app on its own port.

It is a separate ASGI app from the public one in ``vibesynergy.api.app`` and listens on its own
local port (default 127.0.0.1:8447), which ``tailscale serve`` publishes on the tailnet only.
The Funnel forwards ``/vibesynergy`` to the public app's port, so nothing here is reachable
from the internet unless someone deliberately funnels this port.

Pages are static files (plain ES modules, no build step) that call the JSON API under
``/api``; the API calls the same query layer as the MCP tools, so both show the same
numbers.

Guards, in addition to the tailnet boundary:
- Host check: only localhost and the tailnet name (from ``[server] public_url``) plus any
  ``[portal] hosts`` are served, which blocks DNS-rebinding attacks from web pages.
- Writes (POST) need the ``X-VibeSynergy: portal`` header and a same-origin ``Origin``; a
  foreign web page cannot send that header without a CORS preflight, which is refused.
- A strict Content-Security-Policy, no framing, no referrer.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vibesynergy.portal import share
from vibesynergy.portal.api import build_router, tile_cache, tile_cache_enabled, tile_url
from vibesynergy.portal.caldav_api import build_caldav_router
from vibesynergy.portal.coach_runner_api import build_coach_runner_router
from vibesynergy.portal.events_api import build_events_router
from vibesynergy.portal.hosts import allowed_hosts
from vibesynergy.portal.settings_api import build_settings_router
from vibesynergy.portal.start_api import build_start_router
from vibesynergy.portal.thresholds_api import build_thresholds_router
from vibesynergy.portal.tiles import TileError
from vibesynergy.runtime import Runtime

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"


def asset_version() -> str:
    """Fingerprint the entire served tree, including imported modules and translations."""
    digest = hashlib.sha256()
    for path in sorted(p for p in STATIC.rglob("*") if p.is_file()):
        digest.update(path.relative_to(STATIC).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


DEFAULT_PORT = 8447
SHARE_TILES_PER_HOUR = 1500  # per share link: plenty for viewing, not for bulk downloads
WRITE_HEADER = ("x-vibesynergy", "portal")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Tiles rarely change; let the browser keep them for a day (the pages are always revalidated).
TILE_HEADERS = {"cache-control": "private, max-age=86400"}

# Browsers run ES modules only when served as JavaScript; do not rely on the host's tables.
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")


def portal_port(settings) -> int:
    return int(settings.portal.get("port", DEFAULT_PORT))


def portal_enabled(settings) -> bool:
    return bool(settings.portal.get("enabled", True))


def content_security_policy(tile_origins: list[str]) -> str:
    tiles = " ".join(tile_origins)
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            # ECharts and MapLibre position their elements with inline styles.
            "style-src 'self' 'unsafe-inline'",
            f"img-src 'self' data: blob: {tiles}".strip(),
            f"connect-src 'self' {tiles}".strip(),
            "worker-src 'self' blob:",
            "font-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ]
    )


class PortalGuard:
    """Host check, write protection and security headers for every request."""

    def __init__(self, app: ASGIApp, hosts: set[str], csp: str) -> None:
        self.app, self.hosts, self.csp = app, hosts, csp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        if _hostname(host) not in self.hosts:
            await _reject(scope, receive, send, 421, "unknown host")
            return
        if scope["method"] not in SAFE_METHODS:
            origin = headers.get("origin")
            if headers.get(WRITE_HEADER[0]) != WRITE_HEADER[1]:
                await _reject(scope, receive, send, 403, "missing portal header")
                return
            # Same site: the page's own origin must be one of the accepted host names
            # (compared by name, so it holds even if a proxy rewrites Host).
            if origin and (urlsplit(origin).hostname or "") not in self.hosts:
                await _reject(scope, receive, send, 403, "cross-origin request")
                return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra = [
                    (b"content-security-policy", self.csp.encode()),
                    (b"x-frame-options", b"DENY"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                names = {k for k, _ in extra}
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                if not any(k.lower() == b"cache-control" for k, _ in kept):
                    path = scope["path"]
                    content_type = next((v for k, v in kept if k.lower() == b"content-type"), b"")
                    if "/static/v/" in path and not content_type.startswith(b"text/html"):
                        cache = b"public, max-age=31536000, immutable"
                    else:
                        cache = b"no-cache"
                    kept.append((b"cache-control", cache))
                message["headers"] = kept + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _hostname(host: str) -> str:
    """``server.x.ts.net:8447`` -> ``server.x.ts.net``; ``[::1]:8447`` -> ``::1``."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


async def _reject(scope: Scope, receive: Receive, send: Send, status: int, reason: str) -> None:
    response = JSONResponse({"error": reason}, status_code=status)
    await response(scope, receive, send)


def create_portal_app(rt: Runtime, *, share_mode: bool = False, caldav_http=None) -> ASGIApp:
    """The portal. With ``share_mode`` it is the variant served behind share links: no
    Settings API, and every request checked against the link (see ``ShareGuard``)."""
    settings = rt.settings
    version = asset_version()
    from starlette.middleware.gzip import GZipMiddleware

    from vibesynergy.query import about as _about  # noqa: F401  # fixes the start time now

    app = FastAPI(title="VibeSynergy portal", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=500)

    from vibesynergy.query.coach import Conflict

    @app.exception_handler(Conflict)
    async def coach_conflict(_request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def bad_input(request: Request, exc: ValueError) -> JSONResponse:
        # Input problems (unknown metric, bad date or id); the message is meant for people.
        message = str(exc)
        from vibesynergy.i18n import request_language

        if (
            message == "Keep coaching instructions within 2,000 characters."
            and request_language(request) == "nl"
        ):
            message = "Houd coachinstructies binnen 2.000 tekens."
        return JSONResponse({"error": message}, status_code=400)

    app.include_router(build_router(rt), prefix="/api")
    app.include_router(build_start_router(rt), prefix="/api")
    if not share_mode:
        app.include_router(build_settings_router(rt), prefix="/api")
        app.include_router(build_caldav_router(rt, caldav_http), prefix="/api")
        app.include_router(build_events_router(rt), prefix="/api")
        app.include_router(build_coach_runner_router(rt), prefix="/api")
        app.include_router(build_thresholds_router(rt), prefix="/api")

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        html = (STATIC / "index.html").read_text().replace('"static/', f'"static/v/{version}/')
        html = html.replace('<html lang="en">', f'<html lang="en" data-build-id="{version}">')
        return HTMLResponse(html, headers={"cache-control": "no-cache"})

    if not share_mode:

        @app.get("/sw.js", include_in_schema=False)
        def service_worker() -> Response:
            """The offline page's service worker, served from the root so it covers
            the whole portal. Never cached, so a new version takes over at once."""
            return Response(
                (STATIC / "sw.js").read_text(),
                media_type="text/javascript",
                headers={"cache-control": "no-cache", "service-worker-allowed": "/"},
            )

    @app.get("/version", include_in_schema=False)
    @app.get("/vibesynergy/version", include_in_schema=False)
    def current_version() -> JSONResponse:
        from vibesynergy.query import about

        build = about.build()
        return JSONResponse(
            {
                "build_id": version,
                "version": build["version"],
                "commit": build["commit_short"],
                "started_at": about.STARTED_AT.isoformat(),
            },
            headers={"cache-control": "no-store"},
        )

    if tile_cache_enabled(settings):
        tiles = tile_cache(settings)

        @app.get("/tiles/{z}/{x}/{y}.png", include_in_schema=False)
        def tile(z: int, x: int, y: int) -> Response:
            """A map tile from the server's cache (fetched from the tile server once)."""
            try:
                data = tiles.get(z, x, y)
            except TileError:
                return Response(status_code=404)
            if data is None:  # not cached and the tile server cannot be reached
                return Response(status_code=503, headers={"retry-after": "60"})
            return Response(data, media_type="image/png", headers=TILE_HEADERS)

    app.mount(f"/static/v/{version}", StaticFiles(directory=STATIC), name="versioned-static")
    # A tab opened before a deploy still asks for its own version's files. Serve them from
    # the current files, so clicking on in that tab keeps working ("Importing a module
    # script failed", 6-11-2026); the version notice offers the reload.
    old_versions = StaticFiles(directory=STATIC)

    @app.get("/static/v/", include_in_schema=False)
    @app.get("/static/v", include_in_schema=False)
    def old_home_screen_link() -> RedirectResponse:
        """A home-screen shortcut saved before 6-11-2026 points here (the manifest's start
        address was relative to the versioned folder): send it to the portal itself."""
        return RedirectResponse("../../", status_code=302)

    @app.get("/static/v/{old}/{path:path}", include_in_schema=False)
    async def old_version_static(old: str, path: str, request: Request) -> Response:
        if not re.fullmatch(r"[0-9a-f]{8,40}", old):
            return Response(status_code=404)
        return await old_versions.get_response(path, request.scope)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    guarded = PortalGuard(app, allowed_hosts(settings), content_security_policy(tile_origins(rt)))
    return ShareGuard(guarded) if share_mode else guarded


class ShareGuard:
    """Behind a share link: only the link's pages' API calls (writes only if it may write),
    the page itself, its static files and a limited number of map tiles; never indexed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.tiles: dict[str, list[float]] = {}  # link id -> recent tile request times

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        link = scope.get("vibesynergy_share")
        path = scope["path"][len(scope.get("root_path", "")) :] or "/"
        if link is None:
            await _reject(scope, receive, send, 404, "not found")
            return
        if path.startswith("/api/"):
            if not link.allows(scope["method"], path[4:]):
                await _reject(scope, receive, send, 403, "not part of this shared view")
                return
        elif path.startswith("/tiles/"):
            if not self._tile_allowed(link.id):
                await _reject(scope, receive, send, 429, "too many map tiles; try again later")
                return
        elif path not in ("/", "/version") and not path.startswith("/static/"):
            await _reject(scope, receive, send, 404, "not found")
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-robots-tag", b"noindex, nofollow, noarchive"),
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _tile_allowed(self, link_id: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self.tiles.get(link_id, []) if now - t < 3600]
        if len(recent) >= SHARE_TILES_PER_HOUR:
            self.tiles[link_id] = recent
            return False
        recent.append(now)
        self.tiles[link_id] = recent
        return True


class ShareGate:
    """Mounted on the public app at /vibesynergy/share: resolves /<code>/… to its link and
    hands the rest to the share-mode portal. Unknown, expired or revoked codes get a 404."""

    def __init__(self, rt: Runtime, portal: ASGIApp) -> None:
        self.rt, self.portal = rt, portal

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        root = scope.get("root_path", "")
        rest = scope["path"][len(root) :]
        code, slash, _ = rest.lstrip("/").partition("/")
        link = share.resolve(self.rt.engine, code) if code else None
        if link is None:
            await _reject(scope, receive, send, 404, "this link does not exist or has ended")
            return
        if not slash:  # the page's relative addresses need the trailing slash
            # Relative, because a proxy may have stripped part of the path (the Funnel
            # forwards /vibesynergy/share/… as /share/…).
            response = RedirectResponse(f"{code}/", status_code=307)
            await response(scope, receive, send)
            return
        child = dict(scope, root_path=f"{root}/{code}", vibesynergy_share=link)
        await self.portal(child, receive, send)


def tile_origins(rt: Runtime) -> list[str]:
    """External origins the map may load tiles from: none while the tile cache is on."""
    parts = urlsplit(tile_url(rt.settings))
    return [f"{parts.scheme}://{parts.netloc}"] if parts.scheme and parts.netloc else []
