"""Shared set-up for the browser tests: Chrome, Playwright, axe and throwaway servers.

Every server here gets its port while the test runs, never when the file is imported, and
is only "ready" when it answers as VibeSynergy: an in-process portal listens on a socket
the test bound itself, and a ``vibesynergy serve`` process must answer ``/health``. So two
test runs side by side (two worktrees, or pytest-xdist) cannot take each other's server.

axe-core is installed once per version into the user's cache folder (CI caches that
folder), written there atomically so two runs installing at once cannot clash.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
from tests_helpers import free_port

__all__ = [
    "AXE_TAGS",
    "AXE_VERSION",
    "Served",
    "axe_script",
    "chrome",
    "free_port",
    "portal_server",
    "require_browser",
    "run_axe",
    "serve",
    "tab_until",
    "wait_for_health",
    "wait_for_portal",
]

AXE_VERSION = "4.10.3"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
CHROME = ("/usr/bin/google-chrome", "/usr/local/bin/google-chrome")
REQUIRED = "VIBESYNERGY_A11Y_REQUIRED"


def chrome() -> str | None:
    for path in CHROME:
        if Path(path).is_file():
            return path
    return shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def _missing(what: str) -> None:
    if os.environ.get(REQUIRED):
        pytest.fail(f"Browser test needs {what} ({REQUIRED} is set)")
    pytest.skip(f"{what} is unavailable")


def require_browser(*, axe: bool = True) -> str:
    """Chrome's path. Skips without Chrome or Playwright, or fails when
    VIBESYNERGY_A11Y_REQUIRED is set (as in CI). With ``axe``, axe must be cached or npm
    there to fetch it."""
    path = chrome()
    if path is None:
        _missing("Google Chrome")
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        _missing("Playwright")
    if axe and not _axe_path().is_file() and not _axe_override() and shutil.which("npm") is None:
        _missing("npm (to fetch axe-core once)")
    return path  # type: ignore[return-value]


def _axe_override() -> Path | None:
    configured = os.environ.get("VIBESYNERGY_AXE_SCRIPT")
    return Path(configured) if configured and Path(configured).is_file() else None


def _cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "vibesynergy-tests"


def _axe_path() -> Path:
    return _cache_root() / "axe-core" / AXE_VERSION / "axe.min.js"


def axe_script() -> str:
    """axe-core's source, from VIBESYNERGY_AXE_SCRIPT or the cache (filled from npm once)."""
    override = _axe_override()
    if override:
        return override.read_text()
    target = _axe_path()
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
            subprocess.run(
                ["npm", "install", "--no-save", "--prefix", scratch, f"axe-core@{AXE_VERSION}"],
                check=True,
                capture_output=True,
                text=True,
            )
            fetched = Path(scratch) / "node_modules" / "axe-core" / "axe.min.js"
            # Same folder, so the rename is atomic: a run installing at the same time
            # never reads half a file.
            os.replace(fetched, target)
    source = target.read_text()
    if f"axe v{AXE_VERSION}" not in source[:200]:
        target.unlink(missing_ok=True)
        pytest.fail(f"cached axe-core is not {AXE_VERSION}; removed it, run again")
    return source


def run_axe(page) -> list[dict]:
    """axe's WCAG 2.x A/AA violations on the page, trimmed to what a failure message needs."""
    return page.evaluate(
        """async (tags) => {
          const result = await axe.run(document, {runOnly: {type: 'tag', values: tags}});
          return result.violations.map((v) => ({
            id: v.id,
            impact: v.impact,
            help: v.help,
            count: v.nodes.length,
            nodes: v.nodes.slice(0, 4).map((n) => ({
              target: n.target,
              html: (n.html || '').slice(0, 180),
              summary: (n.failureSummary || '').slice(0, 280),
            })),
          }));
        }""",
        AXE_TAGS,
    )


def tab_until(page, expression: str, arg=None, steps: int = 60) -> bool:
    """Press Tab until ``expression`` is true in the page, at most ``steps`` times."""
    for _ in range(steps):
        page.keyboard.press("Tab")
        hit = page.evaluate(expression) if arg is None else page.evaluate(expression, arg)
        if hit:
            return True
    return False


def wait_for_portal(base: str, *, alive=None, log=None, seconds: float = 30) -> None:
    """Wait until ``base`` answers as a VibeSynergy portal (``/version`` has a build id)."""
    _wait_json(f"{base}/version", lambda body: "build_id" in body, alive, log, seconds)


def wait_for_health(url: str, *, alive=None, log=None, seconds: float = 30) -> dict:
    """Wait until a service's ``/health`` answers ``ok``; return its body."""
    return _wait_json(url, lambda body: body.get("ok") is True, alive, log, seconds)


def _wait_json(url, good, alive, log, seconds) -> dict:
    deadline = time.monotonic() + seconds
    last = "no answer"
    while time.monotonic() < deadline:
        if alive is not None and not alive():
            pytest.fail(f"server stopped before {url} answered: {_tail(log)}")
        try:
            reply = requests.get(url, timeout=1)
            body = reply.json() if reply.ok else {}
            if isinstance(body, dict) and good(body):
                return body
            last = f"{reply.status_code} {reply.text[:200]}"
        except (requests.RequestException, ValueError) as exc:
            last = str(exc)
        time.sleep(0.1)
    pytest.fail(f"{url} did not answer as VibeSynergy in {seconds:.0f}s ({last}): {_tail(log)}")


def _tail(log) -> str:
    return "".join(log or [])[-2000:]


@contextlib.contextmanager
def portal_server(app) -> Iterator[str]:
    """Serve ``app`` in a thread on a port bound here, and yield its base URL.

    The socket is bound before uvicorn starts and handed to it, so no other process can
    take the port in between.
    """
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                pytest.fail(f"Temporary portal did not start on {base}")
            time.sleep(0.02)
        wait_for_portal(base, alive=thread.is_alive)
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


class Served:
    """A running ``vibesynergy serve``: its portal and service URLs and its output."""

    def __init__(self, process: subprocess.Popen, portal: str, service: str, log: list[str]):
        self.process = process
        self.portal = portal
        self.service = service
        self.log = log


@contextlib.contextmanager
def serve(
    env: dict,
    *,
    portal_port: int,
    command: list[str] | None = None,
    cwd: Path | None = None,
) -> Iterator[Served]:
    """Run ``vibesynergy serve`` with ``env`` until the block ends.

    ``portal_port`` must be the one in the config. The service port is picked here. Ready
    means the service's ``/vibesynergy/health`` is ok and the portal answers, both from this
    process while it still runs.
    """
    service_port = free_port()
    while service_port == portal_port:
        service_port = free_port()
    entry = command or [str(Path(sys.executable).parent / "vibesynergy")]
    process = subprocess.Popen(
        [*entry, "serve", "--host", "127.0.0.1", "--port", str(service_port)],
        cwd=str(cwd) if cwd else None,
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log: list[str] = []

    def drain() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            log.append(line)

    threading.Thread(target=drain, daemon=True).start()

    def alive() -> bool:
        return process.poll() is None

    portal = f"http://127.0.0.1:{portal_port}"
    service = f"http://127.0.0.1:{service_port}"
    try:
        health = f"{service}/vibesynergy/health"
        wait_for_health(health, alive=alive, log=log, seconds=60)
        wait_for_portal(portal, alive=alive, log=log)
        # The portal is a thread of this process, on /health as a lane: a portal that lost
        # its port to another run ends that thread, and /health is no longer ok.
        wait_for_health(health, alive=alive, log=log, seconds=5)
        yield Served(process, portal, service, log)
    finally:
        _stop(process)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
