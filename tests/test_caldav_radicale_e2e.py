"""A real CalDAV server (Radicale) chosen and synced from the portal, with isolated storage
and ports picked while the test runs.

CI runs it with ``uv run --with playwright --with radicale``; without Radicale it skips (or
fails under VIBESYNERGY_A11Y_REQUIRED=1).
"""

import os
import shutil
import subprocess
import time
from datetime import date

import pytest
import requests
from browser_helpers import (
    axe_script,
    free_port,
    portal_server,
    require_browser,
    run_axe,
)
from sqlalchemy import select

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import Window
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.sources.caldav import client as caldav_client
from vibesynergy.store.schema import planned_item

pytestmark = pytest.mark.integration


def _dav_ready(url: str, process: subprocess.Popen) -> None:
    """Radicale answers, and it is ours: a Radicale that lost the port to another run ends."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"Radicale stopped before it answered: {process.returncode}")
        try:
            requests.get(url, timeout=0.5, allow_redirects=False)
        except requests.RequestException:
            time.sleep(0.1)
            continue
        time.sleep(0.3)
        if process.poll() is None:
            return
    pytest.fail(f"Radicale did not start: {url}")


def test_radicale_portal_selection_and_sync(tmp_path, monkeypatch):
    chrome = require_browser()
    from playwright.sync_api import sync_playwright

    if not shutil.which("radicale"):
        if os.environ.get("VIBESYNERGY_A11Y_REQUIRED"):
            pytest.fail("Radicale is unavailable: run with `uv run --with radicale`")
        pytest.skip("Radicale is unavailable")
    monkeypatch.setattr(caldav_client, "ALLOW_LOCAL_HTTP", True)
    dav_port = free_port()
    auth = tmp_path / "users"
    auth.write_text("testuser:testpass\n")
    dav_config = tmp_path / "radicale.conf"
    dav_config.write_text(
        f"[server]\nhosts = 127.0.0.1:{dav_port}\n"
        f"[auth]\ntype = htpasswd\nhtpasswd_filename = {auth}\nhtpasswd_encryption = plain\n"
        f"[storage]\nfilesystem_folder = {tmp_path / 'dav'}\n"
    )
    process = subprocess.Popen(
        ["radicale", "--config", str(dav_config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rt = None
    try:
        dav = f"http://127.0.0.1:{dav_port}"
        _dav_ready(dav, process)
        session = requests.Session()
        session.auth = ("testuser", "testpass")
        for name in ("chosen", "other"):
            url = f"{dav}/testuser/{name}/"
            result = session.request("MKCALENDAR", url, timeout=5)
            assert result.status_code in (201, 204), result.status_code
            for index in range(2 if name == "chosen" else 1):
                event = (
                    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
                    f"UID:{name}-{index}@example.test\r\nDTSTAMP:20261001T000000Z\r\n"
                    f"DTSTART:2026101{index}T090000Z\r\n"
                    f"DTEND:2026101{index}T100000Z\r\n"
                    f"SUMMARY:Synthetic {name} {index}\r\n"
                    "END:VEVENT\r\nEND:VCALENDAR\r\n"
                )
                result = session.put(
                    url + f"{name}-{index}.ics",
                    data=event.encode(),
                    headers={"Content-Type": "text/calendar"},
                    timeout=5,
                )
                assert result.status_code in (201, 204), result.status_code
        config = tmp_path / "vibesynergy.toml"
        config.write_text('[user]\ntimezone = "Europe/Amsterdam"\n')
        monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
        monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "vibesynergy-data"))
        rt = Runtime.open(load_settings())
        rt.migrate()
        axe = axe_script()
        with portal_server(create_portal_app(rt)) as portal, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            page = browser.new_page()
            page.add_init_script(script=axe)
            page.goto(portal + "/#/settings/calendar")
            page.locator("#caldav-provider").select_option("other")
            page.locator("#caldav-url").fill(dav + "/")
            page.locator("#caldav-username").fill("testuser")
            page.locator("#caldav-password").fill("testpass")
            page.locator("[data-signin] button[type=submit]").click()
            page.locator('.cal-list input[value$="/chosen/"]').wait_for()
            assert page.locator('.cal-list input[value$="/other/"]').count() == 1
            page.locator('.cal-list input[value$="/chosen/"]').check()
            page.locator("[data-choose] button[type=submit]").click()
            page.get_by_text("Connected", exact=True).wait_for()
            window = Window(date(2026, 10, 1), date(2026, 10, 31))
            result = rt.sync.sync("caldav", ["calendar"], window)[0]
            assert result.error is None, result.error
            with rt.engine.connect() as conn:
                titles = [r.title for r in conn.execute(select(planned_item.c.title))]
            assert sorted(titles) == ["Synthetic chosen 0", "Synthetic chosen 1"]
            process.terminate()
            process.wait(timeout=5)
            assert rt.sync.sync("caldav", ["calendar"], window)[0].error
            for route, text in (
                ("#/settings/calendar", "Your calendar couldn't be read"),
                ("#/data", "Your calendar couldn't be read"),
            ):
                page.goto(portal + "/?error-state=1" + route)
                page.get_by_text(text, exact=False).first.wait_for()
                violations = run_axe(page)
                assert not violations, violations
                # The error's technical details open by keyboard.
                summary = page.locator("summary", has_text="details").first
                detail = summary.locator("xpath=..")
                summary.focus()
                page.keyboard.press("Enter")
                assert detail.get_attribute("open") is not None
            browser.close()
    finally:
        if rt:
            rt.close()
        process.terminate()
        process.wait(timeout=5)
