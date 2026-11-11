"""Small polish: contrast, activity titles, inline names, lazy pages, sync copy."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests_helpers import free_port

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.derive import unify
from vibesynergy.model import Activity
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import coach_workouts, journal, latency
from vibesynergy.runtime import Runtime

pytestmark = pytest.mark.integration

PORTAL_PORT = free_port()
SERVE_PORT = free_port()
UNNAMED = "Running · 1 h 05 · 31.2 km"


def _seed(rt: Runtime) -> date:
    today = today_in(rt.settings.timezone)
    start = datetime(today.year, today.month, today.day, 7, tzinfo=UTC)
    records = [
        Activity(
            "unnamed-run",
            start,
            today,
            "running",
            name="Running",
            moving_s=3900,
            distance_m=31200,
        ),
        Activity(
            "named-run",
            start + timedelta(hours=2),
            today,
            "running",
            name="Tempoloop",
            moving_s=2400,
            distance_m=10000,
        ),
    ]
    raw = rt.raw.put(
        source="garmin",
        dataset="synthetic",
        item_key="polish",
        payload=b"synthetic",
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source="garmin", raw_item_id=raw.id)
    unify.rebuild(rt.engine)
    coach_workouts.coach_write(
        rt.engine,
        entry_type="morning",
        summary="Skip the tempo: 40 min easy on flat ground.",
        caveat="Keep Saturday's long run.",
        for_date=today,
        author="client:coach",
        idempotency_key="polish-morning",
    )
    journal.set_checkin(rt.engine, today, soreness=4)
    old = datetime.now(UTC) - timedelta(hours=5)
    latency.stamp(rt.engine, "garmin", "old-run", finished_at=old, received_at=old)
    return today


def _serve(config: str, data: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["VIBESYNERGY_CONFIG"] = config
    env["VIBESYNERGY_DATA_DIR"] = data
    return subprocess.Popen(
        ["uv", "run", "vibesynergy", "serve", "--port", str(SERVE_PORT)],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def _drain(proc: subprocess.Popen, log: list[str]) -> None:
    def run() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            log.append(line)

    threading.Thread(target=run, daemon=True).start()


def _ready(url: str, proc: subprocess.Popen, log: list[str]) -> None:
    import requests

    for _ in range(150):
        if proc.poll() is not None:
            pytest.fail(f"vibesynergy serve exited {proc.returncode}: {''.join(log)[-2000:]}")
        try:
            requests.get(url, timeout=0.2)
            return
        except requests.RequestException:
            time.sleep(0.2)
    pytest.fail(f"Portal did not start: {url}\n{''.join(log)[-2000:]}")


def _settle(page, timeout: float = 20) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = page.locator("#view").inner_text()
        ready = "Loading…" not in last and "Laden…" not in last
        if last and ready and page.locator("#view h1").count():
            return last
        time.sleep(0.1)
    pytest.fail(f"page stayed loading: {last[:400]}")


def _wait_for_text(page, snippet: str, timeout: float = 25, quiet: bool = False) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = page.locator("#view").inner_text()
        if snippet in last and (not quiet or ("Loading…" not in last and "Laden…" not in last)):
            return last
        time.sleep(0.15)
    pytest.fail(f"missing {snippet!r} in {last[:800]}")


def test_small_polish_on_throwaway_portal(tmp_path, monkeypatch) -> None:
    from test_portal_a11y import _axe, _axe_script, _chrome

    chrome = _chrome()
    if chrome is None:
        pytest.skip("google-chrome is not installed")
    pytest.importorskip("playwright.sync_api")
    import requests
    from playwright.sync_api import sync_playwright

    config = tmp_path / "config.toml"
    data = tmp_path / "data"
    config.write_text(
        '[user]\ntimezone = "Europe/Amsterdam"\n'
        f"[portal]\nport = {PORTAL_PORT}\nauth = false\n"
        "[sources.demo]\nenabled = true\n"
    )
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(data))
    rt = Runtime.open(load_settings())
    rt.migrate()
    seeded = _seed(rt)
    rt.close()

    proc = _serve(str(config), str(data))
    log: list[str] = []
    _drain(proc, log)
    base = f"http://127.0.0.1:{PORTAL_PORT}"
    try:
        _ready(base, proc, log)
        # The API port starts next to the portal and can be a moment later (CI, 9-11).
        health = None
        for _ in range(60):
            try:
                health = requests.get(
                    f"http://127.0.0.1:{SERVE_PORT}/vibesynergy/health", timeout=5
                )
                break
            except requests.ConnectionError:
                time.sleep(0.5)
        assert health is not None and health.ok
        axe = _axe_script()
        dialogs: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)

            settings = browser.new_context(viewport={"width": 1280, "height": 900})
            settings.add_init_script(axe)
            settings_page = settings.new_page()
            settings_urls: list[str] = []
            settings_page.on("request", lambda request: settings_urls.append(request.url))

            def note_dialog(dialog) -> None:
                dialogs.append(dialog.message)
                dialog.dismiss()

            settings_page.on("dialog", note_dialog)
            settings_page.goto(base + "/#/settings", wait_until="domcontentloaded")
            _settle(settings_page)
            time.sleep(0.5)
            assert not any("echarts" in url for url in settings_urls), settings_urls
            settings.close()

            context = browser.new_context(
                viewport={"width": 1280, "height": 900}, color_scheme="light"
            )
            context.add_init_script(axe)
            page = context.new_page()
            page.on("dialog", note_dialog)

            # A run across midnight (CI at 00:00, 8-11) seeds yesterday: open that day then.
            same_day = today_in(rt.settings.timezone) == seeded
            target = "/#/dashboard" if same_day else f"/#/dashboard/{seeded.isoformat()}"
            page.goto(base + target, wait_until="domcontentloaded")
            text = _wait_for_text(page, UNNAMED)
            assert "Tempoloop" in text
            assert "Running · Running" not in text

            page.goto(base + "/#/settings/data/sync", wait_until="domcontentloaded")
            sync = _settle(page)
            assert "still on its way" not in sync
            assert "nog onderweg" not in sync
            assert page.locator("[data-latency]").inner_text().strip() == ""
            recent = datetime.now(UTC) - timedelta(minutes=20)
            fresh = Runtime.open(load_settings())
            latency.stamp(
                fresh.engine, "garmin", "fresh-run", finished_at=recent, received_at=recent
            )
            fresh.close()
            page.reload(wait_until="domcontentloaded")
            _wait_for_text(page, "still on its way")

            page.goto(base + "/#/sql", wait_until="domcontentloaded")
            _settle(page)
            page.locator("[data-save]").focus()
            page.keyboard.press("Enter")
            name = page.locator(".sql-save-name input")
            name.wait_for()
            assert page.evaluate(
                "() => document.activeElement === document.querySelector('.sql-save-name input')"
            )
            page.keyboard.type("Morning check")
            page.keyboard.press("Tab")
            page.keyboard.press("Enter")
            _wait_for_text(page, "Morning check")
            assert page.locator("[name=saved]").input_value() != ""

            page.emulate_media(color_scheme="light")
            page.goto(base + "/#/coach", wait_until="domcontentloaded")
            _wait_for_text(page, "Skip the tempo", quiet=True)
            light = [item for item in _axe(page) if item["id"] == "color-contrast"]
            assert not light, light
            page.emulate_media(color_scheme="dark")
            page.reload(wait_until="domcontentloaded")
            _wait_for_text(page, "Skip the tempo", quiet=True)
            dark = [item for item in _axe(page) if item["id"] == "color-contrast"]
            assert not dark, dark

            saved = requests.post(
                base + "/api/prefs",
                json={"changes": {"language": "nl"}},
                headers={"x-vibesynergy": "portal"},
                timeout=5,
            )
            saved.raise_for_status()
            assert saved.json()["data"]["language"] == "nl"
            page.reload(wait_until="domcontentloaded")
            _wait_for_text(page, "Houd het rustig")
            lines = page.locator(".safety-line").all_inner_texts()
            assert lines
            assert any("Houd het rustig" in line for line in lines)
            assert all("Keep it easy" not in line and "Rest today" not in line for line in lines)

            assert dialogs == []
            browser.close()
    finally:
        _stop(proc)
