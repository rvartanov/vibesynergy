"""Start page blocks in Chrome: reorder with the keyboard only, hide and show a block,
reset, choose the best-runs period, workout context on diary notes, and no axe violations
(WCAG 2.x A/AA) in English and Dutch, with the arrange panel open. And the Today card's
edge: always the current colour, also once the day is done; neutral without one (#217)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from browser_helpers import axe_script, portal_server, require_browser, run_axe
from sqlalchemy import insert
from tests_helpers import add_race

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in, utc_now
from vibesynergy.derive import unify
from vibesynergy.model import Activity, DailyMetric, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal import prefs
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import journal
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_decision_log

pytestmark = pytest.mark.integration


def _seed(rt: Runtime) -> None:
    today = today_in(rt.settings.timezone)
    records: list = [
        Activity(
            "start-run",
            datetime(today.year, today.month, today.day, 7, tzinfo=UTC),
            today,
            "running",
            moving_s=3000,
            distance_m=8000,
            name="Harbour loop",
        ),
        PlannedItem(
            "plan-tomorrow", today + timedelta(days=1), "workout", title="Long run", sport="running"
        ),
        PlannedItem("plan-today", today, "workout", title="Easy swim", sport="swimming"),
    ]
    for age in range(0, 60, 3):
        records.append(DailyMetric(today - timedelta(days=age), "body.weight", 79 + age / 30, "kg"))
    records.append(DailyMetric(today - timedelta(days=1), "body.fat_pct", 14.5, "pct"))
    raw = rt.raw.put(
        source="garmin",
        dataset="synthetic",
        item_key="start",
        payload=b"s",
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source="garmin", raw_item_id=raw.id)
    unify.rebuild(rt.engine)
    # Races live in VibeSynergy's own list since #111, not in the planned items.
    add_race(rt.engine, "Dune 15 km", today + timedelta(days=4), priority="A")
    with rt.engine.connect() as conn:
        group = conn.exec_driver_sql(
            "select group_id from activity_unified where name = 'Harbour loop'"
        ).scalar()
    journal.add_note(
        rt.engine,
        text="Strong finish on the hill.",
        start=today,
        activity_id=group,
        source="strava",
        source_ref="s-start",
    )


def _blocks(page) -> list[str]:
    return page.eval_on_selector_all("[data-block]", "els => els.map((el) => el.dataset.block)")


def _wait(predicate, what: str, seconds: float = 15) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


def _tab_to(page, test, limit: int = 250) -> None:
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate("() => document.activeElement && document.activeElement.outerHTML"):
            html = page.evaluate("() => document.activeElement.outerHTML.slice(0, 300)")
            if test(html):
                return
    pytest.fail("could not reach the control with Tab")


def test_start_page_blocks_by_keyboard(tmp_path, monkeypatch) -> None:
    chrome = require_browser()
    from playwright.sync_api import sync_playwright

    config = tmp_path / "config.toml"
    config.write_text('[user]\ntimezone = "Europe/Amsterdam"\n[sources.demo]\nenabled = true\n')
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings())
    rt.migrate()
    try:
        _seed(rt)
        axe = axe_script()
        with portal_server(create_portal_app(rt)) as base, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script(axe)
            page = context.new_page()
            page.goto(base + "/#/dashboard", wait_until="domcontentloaded")
            page.locator("[data-block='body'] .dash-body-weight").wait_for(timeout=20000)
            page.locator("[data-bests-range]:not(:empty)").wait_for(timeout=20000)
            page.locator(".dash-upcoming").wait_for(timeout=20000)
            default = _blocks(page)
            assert default[:2] == ["advice", "checkin"]
            assert "Long run" in page.locator("[data-block='upcoming']").inner_text()
            assert "Dune 15 km" in page.locator("[data-block='upcoming']").inner_text()
            # Today's plan shows once, in the advice block, not again under Coming up.
            assert "Easy swim" not in page.locator("[data-block='upcoming']").inner_text()
            assert "Easy swim" in page.locator("[data-block='advice']").inner_text()
            noted = page.locator("[data-block='noted']").inner_text()
            assert "Harbour loop" in noted  # the note says which workout it belongs to
            assert page.locator("[data-block='noted'] .note-activity a").count() == 1

            # Open the arrange panel with the keyboard.
            _tab_to(page, lambda html: "data-arrange-toggle" in html)
            page.keyboard.press("Enter")
            assert page.locator("#dash-arrange").is_visible()
            assert page.get_attribute("[data-arrange-toggle]", "aria-expanded") == "true"
            violations = run_axe(page)
            assert not violations, violations

            # Move "Body and measurements" up twice with the keyboard; focus stays on it.
            _tab_to(page, lambda html: 'data-move="-1" data-id="body"' in html)
            before = default.index("body")
            page.keyboard.press("Enter")
            _wait(lambda: _blocks(page).index("body") == before - 1, "body moved up")
            page.keyboard.press("Enter")
            _wait(lambda: _blocks(page).index("body") == before - 2, "body moved up twice")
            focused = page.evaluate("() => document.activeElement.dataset.id")
            assert focused == "body"
            status = page.locator("[data-arrange-status]").inner_text()
            assert "Body and measurements" in status
            saved = prefs.load(rt.settings.data_dir)
            assert saved["dashboard_order"].index("body") < saved["dashboard_order"].index(
                default[before - 2]
            )

            # Hide Best efforts with Space, then show it again.
            page.focus("input[data-show='bests']")
            page.keyboard.press("Space")
            _wait(lambda: "bests" not in _blocks(page), "best efforts hidden")
            assert prefs.load(rt.settings.data_dir)["dashboard_hidden"] == ["bests"]
            page.keyboard.press("Space")
            _wait(lambda: "bests" in _blocks(page), "best efforts shown again")
            _wait(
                lambda: page.evaluate("() => document.activeElement?.dataset?.show") == "bests",
                "focus back on the best efforts checkbox",
            )

            # The timeline has nothing to show: it is left out, named in one line and
            # unticked here. Ticking it pins it, so it stays although it is empty.
            assert not page.locator("[data-block='timeline']").is_visible()
            assert "Timeline" in page.locator("[data-dash-nodata]").inner_text()
            assert not page.is_checked("input[data-show='timeline']")
            page.focus("input[data-show='timeline']")
            page.keyboard.press("Space")
            _wait(lambda: page.locator("[data-block='timeline']").is_visible(), "timeline pinned")
            assert prefs.load(rt.settings.data_dir)["dashboard_pinned"] == ["timeline"]
            _wait(
                lambda: page.evaluate("() => document.activeElement?.dataset?.show") == "timeline",
                "focus back on the timeline checkbox",
            )
            assert "Timeline" not in page.locator("[data-dash-nodata]").inner_text()

            # Reset to the default layout.
            page.focus("[data-arrange-reset]")
            page.keyboard.press("Enter")
            _wait(lambda: _blocks(page) == default, "default order")
            assert "dashboard_order" not in prefs.load(rt.settings.data_dir)
            assert "dashboard_pinned" not in prefs.load(rt.settings.data_dir)

            # Folded, the advice keeps its one line; unfold it again.
            page.click("[data-collapse='advice']")
            assert page.locator("[data-block='advice'] [data-dash-main]").is_visible()
            assert not page.locator("[data-block='advice'] .dash-plan").is_visible()
            page.click("[data-collapse='advice']")
            # Folded, Coming up keeps its first day.
            page.click("[data-collapse='upcoming']")
            assert page.locator("[data-block='upcoming'] .dash-upcoming > li").first.is_visible()
            page.click("[data-collapse='upcoming']")
            # Folded, Body keeps the weight line with its value.
            page.click("[data-collapse='body']")
            assert page.locator("[data-block='body'] .dash-body-weight .value").is_visible()
            page.click("[data-collapse='body']")
            _wait(
                lambda: not prefs.load(rt.settings.data_dir).get("dashboard_collapsed"),
                "advice unfolded",
            )

            # Fold Coming up with the keyboard: the heading stays, the rest goes,
            # and it is remembered.
            page.focus("[data-collapse='upcoming']")
            page.keyboard.press("Enter")
            _wait(
                lambda: prefs.load(rt.settings.data_dir).get("dashboard_collapsed") == ["upcoming"],
                "fold saved",
            )
            assert page.get_attribute("[data-collapse='upcoming']", "aria-expanded") == "false"
            assert not page.locator("[data-block='upcoming'] [data-calendar-details]").is_visible()

            # Choose the best-runs period; it is saved and shown.
            page.select_option("[data-bests-period]", "all")
            _wait(
                lambda: prefs.load(rt.settings.data_dir).get("best_runs_period") == "all",
                "period saved",
            )
            page.locator("[data-bests-range]:not(:empty)").wait_for()

            # Dutch, on a phone, with the panel open: still no violations.
            page.evaluate("() => localStorage.setItem('vibesynergy-language', 'nl')")
            page.set_viewport_size({"width": 390, "height": 844})
            page.reload(wait_until="domcontentloaded")
            page.locator("[data-block='body'] .dash-body-weight").wait_for(timeout=20000)
            assert "Lichaam en metingen" in page.locator("[data-block='body']").inner_text()
            assert page.eval_on_selector("[data-bests-period]", "el => el.value") == "all"
            assert page.get_attribute("[data-collapse='upcoming']", "aria-expanded") == "false"
            page.click("[data-arrange-toggle]")
            violations = run_axe(page)
            assert not violations, violations
            width = page.evaluate("() => document.documentElement.scrollWidth")
            assert width <= 390
            browser.close()
    finally:
        rt.close()


def _done_advice(rt: Runtime) -> None:
    """An AI coach's advice for today, amber, that marks today's only session done: the
    start page says "Done for today"."""
    today = today_in(rt.settings.timezone)
    meta = {
        "safety": {"level": "ease", "colour": "amber", "reasons": ["Made-up reason for a test"]},
        "author_name": "AI coach (API)",
        "harness": {
            "version": 3,
            "via": "runner",
            "task": "morning",
            "target": today.isoformat(),
            "trigger": "schedule",
        },
        "sessions": [{"title": "Easy swim", "decision": "done"}],
    }
    with rt.engine.begin() as conn:
        conn.execute(
            insert(coach_decision_log).values(
                subject_id=1,
                at=utc_now(),
                kind="coach",
                author="client:coach-runner",
                entry_type="morning",
                idempotency_key="test-done-advice",
                for_date=today,
                text="The swim is done; nothing more today.",
                meta=meta,
            )
        )


# The card's left edge against the colour a token resolves to, both as the browser
# computes them.
_EDGE = """([selector, token]) => {
  const card = document.querySelector(selector);
  const probe = document.createElement("span");
  probe.style.color = `var(${token})`;
  document.body.append(probe);
  const want = getComputedStyle(probe).color;
  probe.remove();
  return { edge: getComputedStyle(card).borderLeftColor, want, classes: card.className };
}"""


def _edge(page, selector: str, token: str) -> dict:
    return page.evaluate(_EDGE, [selector, token])


def test_today_card_always_shows_the_status_colour(tmp_path, monkeypatch) -> None:
    chrome = require_browser()
    from playwright.sync_api import sync_playwright

    config = tmp_path / "config.toml"
    config.write_text('[user]\ntimezone = "Europe/Amsterdam"\n[sources.demo]\nenabled = true\n')
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings())
    rt.migrate()
    try:
        _seed(rt)
        _done_advice(rt)
        with portal_server(create_portal_app(rt)) as base, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            card = ".card.dash-today.verdict"

            # A done day keeps its colour on the edge; "Done for today" stays as text.
            page.goto(base + "/#/dashboard", wait_until="domcontentloaded")
            page.locator(f"{card} [data-advice-state='done']").wait_for(timeout=20000)
            assert "Done for today" in page.locator(card).inner_text()
            found = _edge(page, card, "--msg-warning-border")
            assert "amber" in found["classes"].split() and "done" not in found["classes"]
            assert found["edge"] == found["want"], found
            assert page.locator(f"{card} [data-advice-no-colour]").count() == 0

            # The Coach page: the same card, the same colour.
            coach = "[data-coach-advice]"
            page.goto(base + "/#/coach", wait_until="domcontentloaded")
            page.locator(f"{coach} [data-advice-state='done']").wait_for(timeout=20000)
            _wait(
                lambda: "amber" in _edge(page, coach, "--line")["classes"].split(),
                "the Coach card's colour",
            )
            found = _edge(page, coach, "--msg-warning-border")
            assert found["edge"] == found["want"], found

            # No colour known (daily advice off): a neutral edge, never green, with a line.
            prefs.save(rt.settings.data_dir, {"daily_advice": "off"})
            page.goto(base + "/#/dashboard", wait_until="domcontentloaded")
            page.locator(f"{card} [data-advice-off]").wait_for(timeout=20000)
            found = _edge(page, card, "--line")
            assert not {"green", "amber", "red"} & set(found["classes"].split()), found
            assert found["edge"] == found["want"], found
            assert found["edge"] != _edge(page, card, "--msg-success")["want"]
            page.goto(base + "/#/coach", wait_until="domcontentloaded")
            page.locator(f"{coach} [data-advice-off]").wait_for(timeout=20000)
            found = _edge(page, coach, "--line")
            assert found["edge"] == found["want"], found
            browser.close()
    finally:
        rt.close()
