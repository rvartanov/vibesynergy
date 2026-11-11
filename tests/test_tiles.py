"""Map tile cache: fetched once, served from disk, still served offline; polite prefetch."""

from __future__ import annotations

import os
import time
from dataclasses import replace

import pytest
import requests
from fastapi.testclient import TestClient

from vibesynergy.portal import tiles as t
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime

PNG = t.PNG + b"fake image data"
TAILNET = "server.example.ts.net"


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.offline = False
        self.body = PNG
        self.status = 200

    def __call__(self, url: str, headers: dict, timeout: float) -> requests.Response:
        self.calls.append((url, headers))
        if self.offline:
            raise requests.ConnectionError("no network")
        reply = requests.Response()
        reply.status_code = self.status
        reply._content = self.body
        return reply


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


def test_fetched_once_then_served_from_disk(tmp_path, upstream) -> None:
    cache = t.TileCache(tmp_path, get=upstream)
    assert cache.get(10, 525, 336) == PNG
    assert cache.get(10, 525, 336) == PNG
    assert len(upstream.calls) == 1
    url, headers = upstream.calls[0]
    assert url == "https://tile.openstreetmap.org/10/525/336.png"
    assert headers["User-Agent"].startswith("VibeSynergy/")  # OSM asks for an identifying UA
    # a plain XYZ folder other tools can read
    assert (tmp_path / "osm" / "10" / "525" / "336.png").read_bytes() == PNG


def test_old_tiles_are_refreshed_but_kept_when_offline(tmp_path, upstream) -> None:
    cache = t.TileCache(tmp_path, get=upstream, max_age_days=30)
    cache.get(12, 2100, 1346)
    old = time.time() - 40 * 86400
    os.utime(cache.path(12, 2100, 1346), (old, old))
    upstream.offline = True
    assert cache.get(12, 2100, 1346) == PNG  # stale copy while offline
    upstream.offline = False
    upstream.body = t.PNG + b"newer"
    assert cache.get(12, 2100, 1346) == t.PNG + b"newer"  # refreshed once back online
    assert cache.path(12, 2100, 1346).read_bytes() == t.PNG + b"newer"


def test_nothing_cached_and_offline(tmp_path, upstream) -> None:
    upstream.offline = True
    assert t.TileCache(tmp_path, get=upstream).get(3, 4, 2) is None


def test_error_pages_are_not_cached(tmp_path, upstream) -> None:
    cache = t.TileCache(tmp_path, get=upstream)
    upstream.body = b"<html>blocked</html>"
    assert cache.get(3, 4, 2) is None
    upstream.status, upstream.body = 429, PNG
    assert cache.get(3, 4, 2) is None
    assert not cache.has(3, 4, 2)


def test_invalid_tiles(tmp_path, upstream) -> None:
    cache = t.TileCache(tmp_path, get=upstream)
    for z, x, y in [(20, 0, 0), (-1, 0, 0), (2, 4, 0), (2, 0, -1)]:
        with pytest.raises(t.TileError):
            cache.get(z, x, y)
    assert upstream.calls == []


def test_other_tile_servers_get_their_own_folder(tmp_path, upstream) -> None:
    cache = t.TileCache(tmp_path, "https://tiles.example.org/{z}/{x}/{y}.png", get=upstream)
    assert cache.root == tmp_path / "tiles.example.org"


def test_tile_math() -> None:
    assert t.tile_of(52.37, 4.89, 10) == (525, 336)  # Amsterdam
    assert t.tile_of(0.0, 0.0, 1) == (1, 1)
    assert t.tile_of(89.9, 179.99, 2) == (3, 0)  # clamped to the map


def test_route_tiles_stay_small() -> None:
    # a 30 km loop around Amsterdam
    lats = [52.37 + 0.06 * (i % 50) / 50 for i in range(500)]
    lons = [4.89 + 0.10 * (i % 70) / 70 for i in range(500)]
    tiles = t.route_tiles(lats, lons)
    zooms = sorted({z for z, _, _ in tiles})
    assert len(zooms) == 3 and zooms[-1] <= 16
    assert len(tiles) < 200  # tens of tiles per activity, not whole areas
    assert t.route_tiles([None], [None]) == set()


def test_prefetch_is_polite(tmp_path, upstream) -> None:
    cache = t.TileCache(tmp_path, get=upstream)
    pauses: list[float] = []
    wanted = {(10, 525, x) for x in range(330, 340)}
    cache.get(10, 525, 330)
    upstream.calls.clear()
    assert t.prefetch(cache, wanted, limit=4, sleep=pauses.append) == (4, 1, 0)
    assert len(upstream.calls) == 4 and pauses == [0.5] * 4
    assert t.prefetch(cache, wanted, limit=100, sleep=pauses.append) == (5, 5, 0)


def _portal(runtime, **portal):
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"}, portal=portal)
    rt = Runtime.open(settings)
    return rt, TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")


def test_portal_serves_tiles_from_the_cache(runtime, upstream, monkeypatch) -> None:
    monkeypatch.setattr(t.requests, "get", upstream)
    rt, client = _portal(runtime)
    reply = client.get("/tiles/10/525/336.png")
    assert reply.status_code == 200 and reply.content == PNG
    assert reply.headers["content-type"] == "image/png"
    assert "max-age=86400" in reply.headers["cache-control"]
    assert client.get("/tiles/10/525/336.png").content == PNG and len(upstream.calls) == 1
    assert (rt.settings.data_dir / "tiles" / "osm" / "10" / "525" / "336.png").exists()
    assert client.get("/tiles/25/0/0.png").status_code == 404
    upstream.offline = True
    assert client.get("/tiles/10/525/337.png").status_code == 503
    # the browser only ever talks to the portal
    assert client.get("/api/meta").json()["data"]["tiles"] == "tiles/{z}/{x}/{y}.png"
    csp = client.get("/").headers["content-security-policy"]
    assert "openstreetmap" not in csp
    assert client.get("/").headers["cache-control"] == "no-cache"  # pages still revalidate
    rt.close()


def test_tile_cache_can_be_switched_off(runtime) -> None:
    rt, client = _portal(runtime, tile_cache=False)
    assert client.get("/tiles/10/525/336.png").status_code == 404
    assert client.get("/api/meta").json()["data"]["tiles"] == t.DEFAULT_UPSTREAM
    assert "https://tile.openstreetmap.org" in client.get("/").headers["content-security-policy"]
    rt.close()


def test_tiles_commands(env) -> None:
    from typer.testing import CliRunner

    from vibesynergy.cli.main import app

    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    status = runner.invoke(app, ["tiles", "status"])
    assert status.exit_code == 0 and "tiles/osm/{z}/{x}/{y}.png" in status.output
    prefetch = runner.invoke(app, ["tiles", "prefetch", "--period", "last_7d"])
    assert prefetch.exit_code == 0 and "0 tiles needed" in prefetch.output
    assert runner.invoke(app, ["tiles", "prefetch", "--period", "someday"]).exit_code != 0
