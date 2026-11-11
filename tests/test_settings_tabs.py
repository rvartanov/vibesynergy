"""Settings tabs and Coach → Settings: axe, keyboard, height, redirects.

Skipped without Chrome or Playwright. Uses a throwaway config and data directory,
its own free portal and serve ports.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests_helpers import free_port

PORTAL_PORT = free_port()
SERVE_PORT = free_port()
PORTAL = f"http://127.0.0.1:{PORTAL_PORT}"

pytestmark = pytest.mark.integration

TABS = ("sources", "sync", "alerts", "sharing", "system")
# System is long by design; the athlete tabs stay short on a phone. Coach settings holds one
# folded line per harness task, so it grows with the task list.
LONG_TABS = ("system", "coach-settings")
# Old tab addresses keep working; Coaching moved to the Coach page, its notes
# and what to remember to Coach → Memory.
NOTES_STATUS = '[data-coach-kind="policy"] [data-status]'
REDIRECTS = {
    "appearance": "system",  # display moved into System
    "advanced": "system",
    "coach-apps": "sharing",
    "ai": "system",
    "coach": "coach/memory",
}
# Settings → You was split up: its sport values are on Training → Zones & thresholds.
PAGE_REDIRECTS = {"you": "#/zones", "profile": "#/zones"}
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
CHROME = "/usr/bin/google-chrome"


def _require() -> str:
    if not Path(CHROME).is_file() or shutil.which("npm") is None:
        pytest.skip("Chrome or npm is unavailable")
    pytest.importorskip("playwright.sync_api")
    return str(Path(sys.executable).parent / "vibesynergy")


def _axe_script() -> str:
    dest = Path("/tmp/vibesynergy-axe") / "4.10.3"
    script = dest / "node_modules" / "axe-core" / "axe.min.js"
    if not script.is_file():
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["npm", "install", "--prefix", str(dest), "axe-core@4.10.3"],
            check=True,
            capture_output=True,
            text=True,
        )
    return script.read_text()


def _poll(page, expression: str, timeout: float = 15, arg=None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok = page.evaluate(expression) if arg is None else page.evaluate(expression, arg)
        if ok:
            return True
        page.wait_for_timeout(100)
    return False


def _ready(url: str) -> None:
    import requests

    for _ in range(100):
        try:
            requests.get(url, timeout=0.2)
            return
        except requests.RequestException:
            time.sleep(0.1)
    pytest.fail(f"portal did not start: {url}")


def test_settings_tabs_accessibility(tmp_path: Path) -> None:
    vibesynergy = _require()
    from playwright.sync_api import sync_playwright

    config = tmp_path / "config.toml"
    data = tmp_path / "data"
    config.write_text(
        '[user]\ntimezone = "Europe/Amsterdam"\nunits = "metric"\n'
        f"[portal]\nport = {PORTAL_PORT}\n[sources.demo]\nenabled = true\n"
    )
    env = {
        **os.environ,
        "VIBESYNERGY_CONFIG": str(config),
        "VIBESYNERGY_DATA_DIR": str(data),
    }
    subprocess.run([vibesynergy, "init"], env=env, check=True, capture_output=True, text=True)
    _tokens(config, data)
    _two_sources(config, data)
    proc = subprocess.Popen(
        [vibesynergy, "serve", "--port", str(SERVE_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    findings: list[dict] = []
    try:
        _ready(f"{PORTAL}/")
        axe = _axe_script()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROME, headless=True)
            _visit(browser, axe, findings)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    assert not findings, json.dumps(findings, indent=2, ensure_ascii=False)


def _tokens(config: Path, data: Path) -> None:
    """One live and one revoked token: Sharing lists only the live one."""
    from vibesynergy.api import oauth
    from vibesynergy.core.config import load_settings
    from vibesynergy.runtime import Runtime

    previous = os.environ.get("VIBESYNERGY_DATA_DIR")
    os.environ["VIBESYNERGY_DATA_DIR"] = str(data)
    try:
        rt = Runtime.open(load_settings(config))
    finally:
        if previous is None:
            os.environ.pop("VIBESYNERGY_DATA_DIR", None)
        else:
            os.environ["VIBESYNERGY_DATA_DIR"] = previous
    try:
        oauth.create_personal_access_token(rt.engine, "Live coach", days=30)
        oauth.create_personal_access_token(rt.engine, "Old coach", days=30)
        assert oauth.revoke_personal_access_token(rt.engine, "Old coach")
    finally:
        rt.close()


def _two_sources(config: Path, data: Path) -> None:
    """Garmin and Withings both deliver the sleep score, so there is an order to choose."""
    from datetime import date

    from vibesynergy.core.config import load_settings
    from vibesynergy.model import DailyMetric
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query import best_source
    from vibesynergy.runtime import Runtime

    previous = os.environ.get("VIBESYNERGY_DATA_DIR")
    os.environ["VIBESYNERGY_DATA_DIR"] = str(data)
    try:
        rt = Runtime.open(load_settings(config))
    finally:
        if previous is None:
            os.environ.pop("VIBESYNERGY_DATA_DIR", None)
        else:
            os.environ["VIBESYNERGY_DATA_DIR"] = previous
    try:
        for i, source in enumerate(("garmin", "withings")):
            stored = rt.raw.put(
                source=source,
                dataset="test",
                item_key=f"best-source-{i}",
                payload=source.encode(),
                content_type="text/plain",
            )
            with rt.engine.begin() as conn:
                record = DailyMetric(date(2026, 10, 20), "sleep.score", 80.0 + i, "score")
                write_records(conn, [record], source=source, raw_item_id=stored.id)
        best_source.rebuild(rt.engine, rt.settings)
    finally:
        rt.close()


def _best_source_keyboard(browser, findings: list[dict]) -> None:
    """System → Best source: move a source with the keyboard, focus follows, then reset."""
    context = browser.new_context(viewport={"width": 390, "height": 844}, locale="en-GB")
    page = context.new_page()
    page.goto(f"{PORTAL}/#/settings/system", wait_until="domcontentloaded")
    row = '[data-metric="sleep.score"]'
    if not _poll(page, f"() => Boolean(document.querySelector({row!r} + ' .bs-order'))"):
        findings.append({"where": "best source", "missing": "sleep.score row"})
        context.close()
        return
    # Collapsed by default: open the section and the row's group by keyboard.
    page.locator("[data-best-source-details] > summary").focus()
    page.keyboard.press("Enter")
    group = page.locator(f"details:has({row}) > summary").last
    group.focus()
    page.keyboard.press("Enter")
    order = (
        f"() => [...document.querySelectorAll({row!r} + ' [data-move=up]')]"
        ".map((b) => b.dataset.source)"
    )
    before = page.evaluate(order)
    page.locator(f'{row} [data-move="down"][data-source="{before[0]}"]').focus()
    page.keyboard.press("Enter")
    if not _poll(
        page,
        f"() => /Saved/.test(document.querySelector({row!r} + ' [data-bs-status]')?.textContent)",
    ):
        findings.append({"where": "best source", "missing": "saved status"})
    after = page.evaluate(order)
    focus = page.evaluate(
        "() => [document.activeElement?.dataset.source, document.activeElement?.dataset.move]"
    )
    if after != list(reversed(before)) or focus != [before[0], "up"]:
        findings.append(
            {"where": "best source move", "before": before, "after": after, "focus": focus}
        )
    page.locator(f"{row} [data-bs-reset]").focus()
    page.keyboard.press("Enter")
    if not _poll(page, f"() => !document.querySelector({row!r} + ' [data-bs-reset]')"):
        findings.append({"where": "best source reset", "missing": "reset applied"})
    elif page.evaluate("() => document.activeElement?.tagName") != "H4":
        findings.append({"where": "best source reset", "focus": "not on the metric heading"})
    context.close()


def _revoked(page, findings: list[dict]) -> None:
    """Sharing lists only what still has access; the revoke shows up in the log."""
    # A hash-only navigation keeps the previous tab's DOM until Sharing draws.
    _poll(page, "() => Boolean(document.querySelector('[data-pat-card] table'))")
    _poll(page, "() => /token revoked/.test(document.querySelector('[data-log]')?.innerText || '')")
    state = page.evaluate(
        """() => {
          const tables = [...document.querySelectorAll('#view table')].map((t) => t.innerText);
          return {tables: tables.join(' '),
                  disclosure: Boolean(document.querySelector('details[data-revoked]')),
                  log: document.querySelector('[data-log]')?.innerText || ''};
        }"""
    )
    if (
        not state
        or "Old coach" in state["tables"]
        or "Live coach" not in state["tables"]
        or state["disclosure"]
        or "Old coach" not in state["log"]
        or "token revoked" not in state["log"]
    ):
        findings.append({"where": "sharing revoked", "state": state})


def _visit(browser, axe: str, findings: list[dict]) -> None:
    counts: dict[str, int] = {}
    for width in (390, 1280):
        context = browser.new_context(viewport={"width": width, "height": 844}, locale="en-GB")
        context.add_init_script(axe)
        page = context.new_page()
        pages = [(tab, f"settings/{tab}") for tab in TABS] + [
            ("coach-memory", "coach/memory"),
            ("coach-settings", "coach/settings"),
        ]
        for tab, path in pages:
            label = f"{width} {tab}"
            page.goto(f"{PORTAL}/#/{path}", wait_until="domcontentloaded")
            if not _poll(
                page,
                """() => {
                  const view = document.querySelector('#view');
                  const heading = view && view.querySelector('h1');
                  if (!heading) return false;
                  return !/Loading…|Laden…/.test(view.innerText || '');
                }""",
            ):
                findings.append({"where": label, "missing": "settled settings page"})
                continue
            if tab == "sources" and not _poll(
                page,
                """() => Boolean(document.querySelector('[data-caldav] h2, #caldav-title'))""",
            ):
                findings.append({"where": label, "missing": "calendar card"})
            if tab == "sharing" and width == 1280:
                _revoked(page, findings)
            violations = page.evaluate(
                """async (tags) => {
                  const result = await axe.run(document, {runOnly: {type: 'tag', values: tags}});
                  return result.violations.map((v) => ({
                    id: v.id, impact: v.impact, help: v.help,
                    nodes: v.nodes.slice(0, 3).map((n) => n.target.join(' '))
                  }));
                }""",
                AXE_TAGS,
            )
            if violations:
                findings.append({"where": label, "axe": violations})
            height = page.evaluate("() => document.querySelector('#view').scrollHeight")
            if width == 390 and tab not in LONG_TABS and height > 2500:
                findings.append({"where": label, "height": height})
            if width == 1280:
                counts[tab] = page.evaluate(
                    """() => {
                      const view = document.querySelector('#view');
                      let n = 0;
                      for (const el of view.querySelectorAll('button, input, select, textarea')) {
                        if (el.closest('details:not([open])')) continue;
                        if (el.hidden || el.closest('[hidden]')) continue;
                        if (el.type === 'hidden') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        n += 1;
                      }
                      return n;
                    }"""
                )
        context.close()
    visible = sum(counts.get(tab, 0) for tab in TABS if tab not in LONG_TABS)
    if visible > 60:
        findings.append({"where": "visible controls", "counts": counts, "sum": visible})
    _redirects(browser, findings)
    _keyboard(browser, findings)
    _best_source_keyboard(browser, findings)


def _redirects(browser, findings: list[dict]) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 844}, locale="en-GB")
    page = context.new_page()
    for old, new in REDIRECTS.items():
        page.goto(f"{PORTAL}/#/settings/{old}", wait_until="domcontentloaded")
        if not _poll(
            page,
            """(tab) => {
              const link = document.querySelector('nav.tabs a[aria-current="page"]');
              return Boolean(link && (link.getAttribute('href') || '').endsWith('/' + tab));
            }""",
            arg=new,
        ):
            findings.append({"where": "redirect", "from": old, "expected": new})
    for old, new in PAGE_REDIRECTS.items():
        page.goto(f"{PORTAL}/#/settings/{old}", wait_until="domcontentloaded")
        if not _poll(page, "(hash) => location.hash === hash", arg=new):
            findings.append({"where": "redirect", "from": old, "expected": new})
    page.goto(f"{PORTAL}/#/settings/you", wait_until="domcontentloaded")
    if not _poll(page, "() => Boolean(document.querySelector('#about-you [name=max_hr]'))"):
        findings.append({"where": "redirect", "missing": "About you on Zones & thresholds"})
    # Sync is its own page under Sources: the panel, not the rest of Settings.
    page.goto(f"{PORTAL}/#/settings/data/sync", wait_until="domcontentloaded")
    if not _poll(
        page,
        """() => {
          const sub = document.querySelector('nav.settings-subnav a[aria-current="page"]');
          return Boolean(sub && sub.getAttribute('href').endsWith('/data/sync')
            && document.querySelector('[data-data-panel] h2')
            && !document.querySelector('[data-coach], [data-menu], [data-shares]'));
        }""",
    ):
        findings.append({"where": "sync page", "missing": "sync panel alone"})
    context.close()


def _keyboard(browser, findings: list[dict]) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 844}, locale="en-GB")
    page = context.new_page()
    page.goto(f"{PORTAL}/#/coach/memory", wait_until="domcontentloaded")
    if not _poll(page, "() => Boolean(document.querySelector('[name=instructions]'))"):
        findings.append({"where": "keyboard", "missing": "notes field"})
        context.close()
        return
    reached = False
    for _ in range(80):
        page.keyboard.press("Tab")
        if page.evaluate("() => document.activeElement?.name === 'instructions'"):
            reached = True
            break
    if not reached:
        findings.append({"where": "keyboard", "missing": "notes not reached by Tab"})
    else:
        page.keyboard.type("Keep the long run easy.")
        page.keyboard.press("Tab")
        if not _poll(
            page,
            f"""() => (document.querySelector({NOTES_STATUS!r})?.textContent || '')
              .includes('Saved.')""",
        ):
            status = page.evaluate(
                f"() => document.querySelector({NOTES_STATUS!r})?.textContent || ''"
            )
            findings.append({"where": "keyboard notes", "status": status})
    for tab in TABS:
        page.goto(f"{PORTAL}/#/settings/sources", wait_until="domcontentloaded")
        # Hash-only navigation keeps the previous settings DOM until the new one draws.
        # Wait until Sources is current, or Enter on the next tab races that draw and loses.
        if not _poll(
            page,
            """() => {
              const link = document.querySelector('nav.tabs a[aria-current="page"]');
              const href = link ? link.getAttribute('href') || '' : '';
              return location.hash.endsWith('/settings/sources') && href.endsWith('/sources');
            }""",
        ):
            findings.append({"where": "keyboard tab", "missing": "sources tab current"})
            break
        found = False
        for _ in range(60):
            page.keyboard.press("Tab")
            hit = page.evaluate(
                """(tab) => {
                  const el = document.activeElement;
                  if (!el || !el.matches('nav.tabs a')) return false;
                  return (el.getAttribute('href') || '').endsWith('/' + tab);
                }""",
                tab,
            )
            if hit:
                found = True
                page.keyboard.press("Enter")
                break
        if not found:
            findings.append({"where": "keyboard tab", "missing": tab})
            continue
        if not _poll(
            page,
            """(tab) => {
              const link = document.querySelector('nav.tabs a[aria-current="page"]');
              return Boolean(link && (link.getAttribute('href') || '').endsWith('/' + tab));
            }""",
            timeout=8,
            arg=tab,
        ):
            findings.append({"where": "keyboard tab", "not_current": tab})
    context.close()
