"""Map tile cache: the portal serves OpenStreetMap tiles from the server's own disk.

The browser asks the portal for ``/tiles/{z}/{x}/{y}.png``; the portal answers from the
cache, fetching a tile from the upstream server (``[portal] tiles``, OpenStreetMap by
default) only the first time and again when it is older than ``tile_max_age_days``. When
the upstream cannot be reached, an older copy is served, so maps that were viewed once stay
viewable offline. The browser itself never contacts the tile server.

The cache is a plain XYZ folder, ``<data dir>/tiles/<name>/{z}/{x}/{y}.png``, which other
tools (QGIS, GDAL, other map apps) can read directly or through the portal's tile URL.

OpenStreetMap's tile usage policy (https://operations.osmfoundation.org/policies/tiles/)
asks for an identifying User-Agent, local caching, and no bulk downloading: upstream
requests are made one at a time, and prefetching is limited to the tiles along a route.
"""

from __future__ import annotations

import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from vibesynergy import __version__

DEFAULT_UPSTREAM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_MAX_AGE_DAYS = 30
MAX_ZOOM = 19
PNG = b"\x89PNG\r\n\x1a\n"
USER_AGENT = f"VibeSynergy/{__version__} (self-hosted personal fitness archive; map tile cache)"


class TileError(ValueError):
    """Not a valid tile address."""


def check(z: int, x: int, y: int) -> None:
    if not 0 <= z <= MAX_ZOOM:
        raise TileError(f"zoom must be 0 to {MAX_ZOOM}")
    if not (0 <= x < 2**z and 0 <= y < 2**z):
        raise TileError("tile outside the map")


def tile_of(lat: float, lon: float, z: int) -> tuple[int, int]:
    """The (x, y) of the tile containing a point at zoom z (Web Mercator)."""
    lat = max(min(lat, 85.0511), -85.0511)
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def cache_name(upstream: str) -> str:
    """A folder name per tile server, so a different map style never mixes with OSM."""
    host = urlsplit(upstream).hostname or "tiles"
    return "osm" if host.endswith("openstreetmap.org") else host.replace(":", "_")


@dataclass
class CacheStats:
    tiles: int
    bytes: int


class TileCache:
    def __init__(
        self,
        root: Path,
        upstream: str = DEFAULT_UPSTREAM,
        *,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS,
        user_agent: str = USER_AGENT,
        get: Callable[..., requests.Response] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.root = root / cache_name(upstream)
        self.upstream = upstream
        self.max_age_s = max_age_days * 86400
        self._headers = {"User-Agent": user_agent}
        self._get = get or requests.get
        self._timeout = timeout
        self._lock = threading.Lock()  # one upstream request at a time

    def path(self, z: int, x: int, y: int) -> Path:
        return self.root / str(z) / str(x) / f"{y}.png"

    def get(self, z: int, x: int, y: int) -> bytes | None:
        """The tile's PNG: cached if fresh, else fetched (and cached), else a stale copy."""
        check(z, x, y)
        path = self.path(z, x, y)
        cached = path.exists()
        if cached and time.time() - path.stat().st_mtime < self.max_age_s:
            return path.read_bytes()
        fetched = self._fetch(z, x, y)
        if fetched is not None:
            self._store(path, fetched)
            return fetched
        return path.read_bytes() if cached else None  # offline: an older copy is fine

    def has(self, z: int, x: int, y: int) -> bool:
        return self.path(z, x, y).exists()

    def _fetch(self, z: int, x: int, y: int) -> bytes | None:
        url = self.upstream.format(z=z, x=x, y=y)
        with self._lock:
            try:
                reply = self._get(url, headers=self._headers, timeout=self._timeout)
            except requests.RequestException:
                return None
        if reply.status_code != 200 or not reply.content.startswith(PNG):
            return None
        return reply.content

    def _store(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)  # readers never see a half-written tile

    def stats(self) -> CacheStats:
        tiles = size = 0
        if self.root.exists():
            for path in self.root.rglob("*.png"):
                tiles += 1
                size += path.stat().st_size
        return CacheStats(tiles, size)


# Prefetch ----------------------------------------------------------------------------------


def fit_zoom(
    lats: list[float], lons: list[float], width_tiles: int = 4, height_tiles: int = 2
) -> int:
    """The zoom at which the route's bounding box fits in about the activity page's map."""
    for z in range(16, 0, -1):
        x0, y0 = tile_of(max(lats), min(lons), z)
        x1, y1 = tile_of(min(lats), max(lons), z)
        if x1 - x0 < width_tiles and y1 - y0 < height_tiles:
            return z
    return 1


def route_tiles(
    lats: list[float], lons: list[float], *, extra_zooms: int = 2, max_zoom: int = 16
) -> set[tuple[int, int, int]]:
    """Tiles a route's map needs: the whole view at the page's opening zoom (with a margin),
    and at the next zoom levels only the tiles the route passes through. Keeps prefetching
    to tens of tiles per activity instead of whole areas."""
    points = [(a, o) for a, o in zip(lats, lons, strict=True) if a is not None and o is not None]
    if not points:
        return set()
    la = [p[0] for p in points]
    lo = [p[1] for p in points]
    z0 = min(fit_zoom(la, lo), max_zoom)
    tiles = set()
    x0, y0 = tile_of(max(la), min(lo), z0)
    x1, y1 = tile_of(min(la), max(lo), z0)
    n = 2**z0
    for x in range(max(x0 - 1, 0), min(x1 + 1, n - 1) + 1):
        for y in range(max(y0 - 1, 0), min(y1 + 1, n - 1) + 1):
            tiles.add((z0, x, y))
    for z in range(z0 + 1, min(z0 + extra_zooms, max_zoom) + 1):
        for lat, lon in points:
            tiles.add((z, *tile_of(lat, lon, z)))
    return tiles


def prefetch(
    cache: TileCache,
    tiles: Iterable[tuple[int, int, int]],
    *,
    limit: int,
    pause_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int, int]:
    """Download missing tiles, politely. Returns (downloaded, already cached, failed)."""
    downloaded = cached = failed = 0
    for z, x, y in sorted(tiles):
        if cache.has(z, x, y):
            cached += 1
            continue
        if downloaded + failed >= limit:
            break
        if cache.get(z, x, y) is None:
            failed += 1
        else:
            downloaded += 1
        sleep(pause_s)
    return downloaded, cached, failed
