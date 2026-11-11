"""Exercise signal delivery through the real CLI without touching the installed service."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vibesynergy.sync.worker import Worker, _owner_alive


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _distinct_ports() -> tuple[int, int]:
    first = _free_port()
    second = _free_port()
    while second == first:
        second = _free_port()
    return first, second


def _wait_marker(path: Path, expected: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists() and path.read_text() == expected:
            return
        time.sleep(0.05)
    pytest.fail(f"marker {path.name} did not contain {expected!r}")


def _cleanup(process: subprocess.Popen) -> None:
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _wait_for_port(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"serve exited before listening: {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("serve did not start listening")


def _spawn(command, env, port, portal_port, *, portal=False):
    """Retry a released port-0 allocation if another process wins the bind race."""
    config = Path(env["VIBESYNERGY_CONFIG"])
    for attempt in range(3):
        command[command.index("--port") + 1] = str(port)
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parents[1],
            start_new_session=True,
        )
        try:
            _wait_for_port(process, port)
            if portal:
                _wait_for_port(process, portal_port)
            return process, port
        except RuntimeError as exc:
            _cleanup(process)
            error = process.stderr.read().decode(errors="replace")
            process.stderr.close()
            if attempt == 2 or not any(
                marker in error.lower() for marker in ("address already in use", "errno 98")
            ):
                pytest.fail(f"{exc}: {error[-1000:]}")
            next_port, next_portal = _distinct_ports()
            config.write_text(
                config.read_text().replace(f"port = {portal_port}", f"port = {next_portal}")
            )
            port, portal_port = next_port, next_portal
    raise AssertionError("unreachable")


def _stop(process: subprocess.Popen, sig: signal.Signals = signal.SIGTERM) -> float:
    started = time.monotonic()
    try:
        process.send_signal(sig)
        assert process.wait(timeout=15) == 0
    finally:
        _cleanup(process)
    return time.monotonic() - started


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, portal_port: int) -> dict:
    config = tmp_path / "config.toml"
    data = tmp_path / "data"
    config.write_text(
        f'[paths]\ndata_dir = "{data}"\n[portal]\nport = {portal_port}\n'
        "[schedule]\ndropfolder = 0\nalerts = 0\n"
    )
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    from vibesynergy.core.config import load_settings
    from vibesynergy.runtime import Runtime

    rt = Runtime.open(load_settings(config))
    rt.migrate()
    rt.close()
    return os.environ.copy()


def _skip_extra_uv_run(via_uv: bool, *, keep: bool) -> None:
    """Pull requests run one ``uv run --frozen`` variant; main runs them all.

    Each ``uv run`` start costs seconds on the shared runner. CI sets
    VIBESYNERGY_FULL_MATRIX=1 on pushes to main.
    """
    if via_uv and not keep and not os.environ.get("VIBESYNERGY_FULL_MATRIX"):
        pytest.skip("extra uv run variant: runs on main (VIBESYNERGY_FULL_MATRIX=1)")


@pytest.mark.parametrize("via_uv", [False, True], ids=["entry-point", "uv-run-frozen"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["term", "int"])
def test_serve_exits_cleanly_on_signal(tmp_path, monkeypatch, via_uv, sig):
    _skip_extra_uv_run(via_uv, keep=sig == signal.SIGTERM)
    port, portal_port = _distinct_ports()
    env = _environment(tmp_path, monkeypatch, portal_port)
    entry = (
        ["uv", "run", "--frozen", "vibesynergy"]
        if via_uv
        else [str(Path(sys.executable).parent / "vibesynergy")]
    )
    process, port = _spawn(
        [*entry, "serve", "--host", "127.0.0.1", "--port", str(port)],
        env,
        port,
        portal_port,
        portal=True,
    )
    try:
        assert _stop(process, sig) < 15
    finally:
        _cleanup(process)
        process.stderr.close()


def test_running_job_is_recovered_after_shutdown(tmp_path, monkeypatch):
    port, portal_port = _distinct_ports()
    env = _environment(tmp_path, monkeypatch, portal_port)
    from vibesynergy.core.config import load_settings
    from vibesynergy.runtime import Runtime

    rt = Runtime.open(load_settings())
    marker = tmp_path / "job-started"
    env["VIBESYNERGY_TEST_MARKER"] = str(marker)
    job_id = rt.jobs.enqueue("synthetic-blocked", {}, max_attempts=1)
    script = """
import os
import threading
from pathlib import Path
from vibesynergy.cli.main import app
from vibesynergy.runtime import Runtime
from vibesynergy.sync.worker import Worker

def blocked(job):
    marker = Path(os.environ['VIBESYNERGY_TEST_MARKER'])
    temp = marker.with_suffix('.tmp')
    temp.write_text(str(job.id))
    os.replace(temp, marker)
    threading.Event().wait(30)

def worker(self, *, lane='all'):
    return Worker(self.jobs, {'synthetic-blocked': blocked}, kinds=('synthetic-blocked',))

Runtime.worker = worker
app()
"""
    process, port = _spawn(
        [
            sys.executable,
            "-c",
            script,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-portal",
            "--no-schedule",
        ],
        env,
        port,
        portal_port,
    )
    try:
        _wait_marker(marker, str(job_id))
        assert rt.jobs.recent()[0]["status"] == "running"
        assert _stop(process) < 15
        assert rt.jobs.recent()[0]["status"] == "running"
        assert rt.jobs.requeue_abandoned(_owner_alive) == 1
        assert rt.jobs.requeue_abandoned(_owner_alive) == 0
        handled = []
        replacement = Worker(rt.jobs, {"synthetic-blocked": lambda job: handled.append(job.id)})
        assert replacement.run_once()
        assert not replacement.run_once()
        assert handled == [job_id]
        assert rt.jobs.recent()[0]["status"] == "done"
    finally:
        _cleanup(process)
        process.stderr.close()
        rt.close()


def test_active_request_cannot_hold_shutdown_open(tmp_path, monkeypatch):
    port, portal_port = _distinct_ports()
    env = _environment(tmp_path, monkeypatch, portal_port)
    marker = tmp_path / "request-started"
    env["VIBESYNERGY_TEST_MARKER"] = str(marker)
    script = """
import asyncio
import importlib
import os
from pathlib import Path
from fastapi import FastAPI
from vibesynergy.cli.main import app

api = importlib.import_module('vibesynergy.api.app')
synthetic = FastAPI()

@synthetic.get('/blocked')
async def blocked():
    Path(os.environ['VIBESYNERGY_TEST_MARKER']).touch()
    await asyncio.sleep(30)

api.create_app = lambda rt: synthetic
app()
"""
    process, port = _spawn(
        [
            sys.executable,
            "-c",
            script,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-portal",
            "--no-worker",
            "--no-schedule",
        ],
        env,
        port,
        portal_port,
    )
    request = None
    try:
        request = socket.create_connection(("127.0.0.1", port), timeout=2)
        request.sendall(b"GET /blocked HTTP/1.1\r\nHost: localhost\r\n\r\n")
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        assert _stop(process) < 10
    finally:
        if request is not None:
            request.close()
        _cleanup(process)
        process.stderr.close()


@pytest.mark.parametrize("via_uv", [False, True], ids=["python", "uv-run-frozen"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["term", "int"])
def test_blocked_sync_request_and_job_exit_within_deadline(tmp_path, monkeypatch, via_uv, sig):
    _skip_extra_uv_run(via_uv, keep=False)
    port, portal_port = _distinct_ports()
    env = _environment(tmp_path, monkeypatch, portal_port)
    from vibesynergy.core.config import load_settings
    from vibesynergy.runtime import Runtime

    rt = Runtime.open(load_settings())
    rt.jobs.enqueue("synthetic-blocked", {}, max_attempts=1)
    request_marker = tmp_path / "sync-request-started"
    job_marker = tmp_path / "blocked-job-started"
    env["VIBESYNERGY_REQUEST_MARKER"] = str(request_marker)
    env["VIBESYNERGY_JOB_MARKER"] = str(job_marker)
    script = """
import importlib
import os
import threading
from pathlib import Path
from fastapi import FastAPI
from vibesynergy.cli.main import app
from vibesynergy.runtime import Runtime
from vibesynergy.sync.worker import Worker

def blocked_job(job):
    Path(os.environ['VIBESYNERGY_JOB_MARKER']).write_text('started')
    threading.Event().wait(60)

def worker(self, *, lane='all'):
    return Worker(self.jobs, {'synthetic-blocked': blocked_job}, kinds=('synthetic-blocked',))

Runtime.worker = worker
api = importlib.import_module('vibesynergy.api.app')
synthetic = FastAPI()

@synthetic.get('/blocked')
def blocked():
    Path(os.environ['VIBESYNERGY_REQUEST_MARKER']).write_text('started')
    threading.Event().wait(60)
    return {'ok': True}

api.create_app = lambda rt: synthetic
app()
"""
    entry = ["uv", "run", "--frozen", "python"] if via_uv else [sys.executable]
    process, port = _spawn(
        [
            *entry,
            "-c",
            script,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-portal",
            "--no-schedule",
        ],
        env,
        port,
        portal_port,
    )
    request = None
    try:
        _wait_marker(job_marker, "started")
        request = socket.create_connection(("127.0.0.1", port), timeout=2)
        request.sendall(b"GET /blocked HTTP/1.1\r\nHost: localhost\r\n\r\n")
        _wait_marker(request_marker, "started")
        assert _stop(process, sig) < 15
    finally:
        if request is not None:
            request.close()
        _cleanup(process)
        process.stderr.close()
        rt.close()
