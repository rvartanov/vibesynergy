"""Language lives on Settings → System (display), persists like theme, works without prefs."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests_helpers import free_port

pytestmark = pytest.mark.integration

CHROME = "/usr/bin/google-chrome"
PORTAL_PORT = free_port()
SERVE_PORT = free_port()
PORTAL = f"http://127.0.0.1:{PORTAL_PORT}"


def _require() -> str:
    if not Path(CHROME).is_file():
        pytest.skip("Chrome is unavailable")
    pytest.importorskip("playwright.sync_api")
    return str(Path(sys.executable).parent / "vibesynergy")


def _ready(url: str) -> None:
    import requests

    for _ in range(100):
        try:
            requests.get(url, timeout=0.2)
            return
        except requests.RequestException:
            time.sleep(0.1)
    pytest.fail(f"portal did not start: {url}")


def _poll(page, expression: str, timeout: float = 15, arg=None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok = page.evaluate(expression) if arg is None else page.evaluate(expression, arg)
        if ok:
            return True
        page.wait_for_timeout(100)
    return False


def _settled(page, heading: str) -> bool:
    return _poll(
        page,
        """(heading) => {
          const view = document.querySelector('#view');
          const h2 = [...(view?.querySelectorAll('h1, h2') || [])].some(
            (el) => el.textContent === heading
          );
          return Boolean(h2) && !/Loading…|Laden…/.test(view.innerText || '');
        }""",
        arg=heading,
    )


@pytest.fixture
def portal(tmp_path: Path):
    vibesynergy = _require()
    config = tmp_path / "config.toml"
    data = tmp_path / "data"
    config.write_text(
        '[user]\ntimezone = "Europe/Amsterdam"\nunits = "metric"\n'
        f"[portal]\nport = {PORTAL_PORT}\nauth = false\n[sources.demo]\nenabled = true\n"
    )
    env = {**os.environ, "VIBESYNERGY_CONFIG": str(config), "VIBESYNERGY_DATA_DIR": str(data)}
    subprocess.run([vibesynergy, "init"], env=env, check=True, capture_output=True, text=True)
    proc = subprocess.Popen(
        [vibesynergy, "serve", "--port", str(SERVE_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _ready(PORTAL + "/")
        yield env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_language_picker_on_appearance_persists_and_works_without_prefs(portal) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME, headless=True)
        context = browser.new_context(locale="en-GB", viewport={"width": 1280, "height": 800})
        page = context.new_page()
        # Display moved into System; the old address redirects there.
        page.goto(f"{PORTAL}/#/settings/appearance", wait_until="domcontentloaded")
        assert _settled(page, "System")
        language = page.locator('[data-section="appearance"] [name=language]')
        theme = page.locator('[data-section="appearance"] [name=theme]')
        assert language.count() == 1
        assert theme.count() == 1

        page.goto(f"{PORTAL}/#/settings/system", wait_until="domcontentloaded")
        assert _settled(page, "System")
        language.select_option("nl")
        assert _poll(page, "() => document.documentElement.lang === 'nl'")
        assert _settled(page, "Systeem")
        assert page.evaluate("() => localStorage.getItem('vibesynergy-language')") == "nl"

        page.goto(f"{PORTAL}/#/dashboard", wait_until="domcontentloaded")
        assert _poll(page, "() => document.documentElement.lang === 'nl'")
        page.reload(wait_until="domcontentloaded")
        assert _poll(page, "() => document.documentElement.lang === 'nl'")
        assert page.evaluate("() => localStorage.getItem('vibesynergy-language')") == "nl"

        def _prefs(route):
            if route.request.method == "POST":
                route.abort()
            else:
                route.continue_()

        page.route("**/api/prefs", _prefs)
        page.goto(f"{PORTAL}/#/settings/system", wait_until="domcontentloaded")
        assert _settled(page, "Systeem")
        page.locator('[data-section="appearance"] [name=language]').select_option("en")
        assert _poll(page, "() => document.documentElement.lang === 'en'")
        assert _settled(page, "System")
        assert page.evaluate("() => localStorage.getItem('vibesynergy-language')") == "en"
        browser.close()
