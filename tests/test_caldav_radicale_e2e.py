"""Optional browser/Radicale flow with isolated storage and localhost ports."""

import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

import pytest
import requests
import uvicorn
from sqlalchemy import select

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import Window
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.sources.caldav import client as caldav_client
from vibesynergy.store.schema import planned_item

pytestmark = pytest.mark.integration


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ready(url):
    for _ in range(100):
        try:
            requests.get(url, timeout=0.2)
            return
        except requests.RequestException:
            time.sleep(0.1)
    pytest.fail(f"Temporary server did not start: {url}")


def test_radicale_portal_selection_and_sync(tmp_path, monkeypatch):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is unavailable")
    if not shutil.which("radicale") or not shutil.which("google-chrome"):
        pytest.skip("Radicale or Chrome is unavailable")
    monkeypatch.setattr(caldav_client, "ALLOW_LOCAL_HTTP", True)
    dav_port, portal_port = port(), port()
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
    server = None
    thread = None
    try:
        dav = f"http://127.0.0.1:{dav_port}"
        ready(dav)
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
        config.write_text(
            f'[user]\ntimezone = "Europe/Amsterdam"\n[portal]\nport = {portal_port}\n'
        )
        monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
        monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "vibesynergy-data"))
        rt = Runtime.open(load_settings())
        rt.migrate()
        server = uvicorn.Server(
            uvicorn.Config(
                create_portal_app(rt), host="127.0.0.1", port=portal_port, log_level="error"
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        portal = f"http://127.0.0.1:{portal_port}"
        ready(portal)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=shutil.which("google-chrome"), headless=True
            )
            page = browser.new_page()
            axe_path = os.environ.get("VIBESYNERGY_AXE_SCRIPT")
            if axe_path:
                page.add_init_script(script=Path(axe_path).read_text())
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
                if axe_path:
                    findings = page.evaluate(
                        "async () => await axe.run(document, "
                        "{runOnly: {type: 'tag', values: "
                        "['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']}})"
                    )
                    assert not findings["violations"], [
                        (v["id"], len(v["nodes"])) for v in findings["violations"]
                    ]
                detail = page.locator("details:has(summary:text('Details'))").first
                detail.locator("summary").focus()
                page.keyboard.press("Enter")
                assert detail.get_attribute("open") is not None
            browser.close()
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=5)
        if rt:
            rt.close()
        process.terminate()
        process.wait(timeout=5)
