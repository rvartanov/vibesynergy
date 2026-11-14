"""Phone dashboard: today's advice and plan fill the first screen, the check-in follows.

The start page leads with today's advice (docs/design/README.md, route map): the
advice-and-plan card must fit in the first screen of a 390x844 phone, and the check-in card
comes right after it, starting inside that first screen so it is in view without searching.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.derive import unify
from vibesynergy.model import Activity, DailyMetric, PlannedItem, ZoneSet
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime

pytestmark = pytest.mark.integration

RUN_NAME = "Phone easy run"
PHONE = {"width": 390, "height": 844}


def _seed(rt: Runtime) -> None:
    today = today_in(rt.settings.timezone)
    records: list = [
        ZoneSet(
            "hr",
            "running",
            today - timedelta(days=30),
            (120, 140, 155, 170, 185),
            max_value=190,
        ),
        Activity(
            "phone-run",
            datetime(today.year, today.month, today.day, 7, tzinfo=UTC),
            today,
            "running",
            moving_s=3000,
            distance_m=8000,
            avg_hr=140,
            name=RUN_NAME,
        ),
        PlannedItem(
            "plan-easy",
            today,
            "workout",
            title="Easy run",
            sport="running",
            extra={"duration": 3000, "distance": 8000, "ascent_m": 40},
        ),
        PlannedItem(
            "plan-long",
            today + timedelta(days=1),
            "workout",
            title="Long run",
            sport="running",
            extra={"duration": 5400, "distance": 16000, "ascent_m": 120},
        ),
        PlannedItem(
            "harbor",
            today + timedelta(days=40),
            "event",
            title="Harbor 10 km",
            is_race=True,
            primary=True,
        ),
    ]
    for age in range(14):
        morning = today - timedelta(days=age)
        records.append(DailyMetric(morning, "sleep.duration", 8 * 3600, "s"))
        records.append(DailyMetric(morning, "hrv.night_avg", 55, "ms"))
        records.append(DailyMetric(morning, "hr.resting", 48, "bpm"))
    raw = rt.raw.put(
        source="garmin",
        dataset="synthetic",
        item_key="phone",
        payload=b"synthetic",
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source="garmin", raw_item_id=raw.id)
    unify.rebuild(rt.engine)


def test_phone_dashboard_leads_with_today(tmp_path, monkeypatch) -> None:
    from test_portal_a11y import _axe, _axe_script, _port, _ready, _require_tools

    chrome = _require_tools()
    import uvicorn
    from playwright.sync_api import sync_playwright

    port = _port()
    config = tmp_path / "config.toml"
    config.write_text(
        '[user]\ntimezone = "Europe/Amsterdam"\n'
        f"[portal]\nport = {port}\n[sources.demo]\nenabled = true\n"
    )
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings())
    rt.migrate()
    server = None
    thread = None
    try:
        _seed(rt)
        server = uvicorn.Server(
            uvicorn.Config(create_portal_app(rt), host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        _ready(base)
        axe = _axe_script()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            context = browser.new_context(viewport=PHONE)
            context.add_init_script(axe)
            page = context.new_page()
            page.goto(base + "/#/dashboard", wait_until="domcontentloaded")
            page.locator(".dash-today").wait_for(timeout=20000)
            deadline = time.time() + 20
            while time.time() < deadline:
                text = page.locator("#view").inner_text()
                if "Loading…" not in text and "Laden…" not in text:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("dashboard stayed on Loading")
            checkin = page.locator(".dash-checkin").bounding_box()
            today = page.locator(".dash-today").bounding_box()
            assert checkin is not None and today is not None
            first_screen = PHONE["height"]
            today_bottom = today["y"] + today["height"]
            assert today_bottom <= first_screen, (today, first_screen)
            # The check-in follows the advice and starts in the first screen; it may scroll.
            assert checkin["y"] >= today_bottom, (checkin, today)
            assert checkin["y"] + 48 <= first_screen, (checkin, first_screen)
            # One long start page in the default block order: the day's cards,
            # then coming up and the calendar, and the diary.
            tops = page.evaluate(
                """() => ['.dash-today', '.dash-checkin', '.dash-week', '.dash-recovery',
                    '#calendar', '#diary'].map(
                    (sel) => document.querySelector(sel)?.getBoundingClientRect().top ?? null)"""
            )
            assert None not in tops and tops == sorted(tops), tops
            # Blocks with nothing to show take no space: one line names them instead.
            left_out = page.locator("[data-dash-nodata]")
            if page.locator("#day-details-title").count() == 0:
                assert left_out.is_visible() and "Key numbers" in left_out.inner_text()
            today_text = page.locator(".dash-today").inner_text()
            assert "Easy run" in today_text
            assert RUN_NAME in today_text
            violations = _axe(page)
            assert not violations, violations
            page.keyboard.press("Tab")
            assert "skip-main" in (page.locator(":focus").get_attribute("class") or "")
            found = False
            seen: list[str] = []
            for _ in range(80):
                page.keyboard.press("Tab")
                # Read the focused element directly: the native date picker in the header
                # takes several Tab stops, and a `:focus` locator stalls on its inner button.
                text = page.evaluate("() => document.activeElement?.innerText || ''")
                seen.append(text.replace("\n", " ")[:60])
                if RUN_NAME in text:
                    found = True
                    break
            assert found, seen
            # A header warning gets its own row: it used to sit on top of the name.
            page.evaluate(
                """() => {
                    const box = document.createElement('div');
                    box.className = 'header-warn-notices';
                    box.innerHTML = '<span class="header-sync-chip warn"><span>Rust vandaag.'
                        + '</span><button type="button" class="btn small">&times;</button></span>';
                    document.querySelector('header.top .brand').after(box);
                }"""
            )
            brand = page.locator("header.top .brand-name").bounding_box()
            notice = page.locator(".header-warn-notices").bounding_box()
            assert brand is not None and notice is not None
            assert notice["y"] >= brand["y"] + brand["height"], (brand, notice)
            assert notice["x"] + notice["width"] <= PHONE["width"], notice
            page.screenshot(path=str(tmp_path / "phone-header-notice.png"))
            browser.close()
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=5)
        rt.close()
