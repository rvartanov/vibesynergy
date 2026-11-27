"""A chart drawn while its box is folded away (a collapsed start-page block) is fitted to its
box once the box opens, instead of staying a few pixels wide."""

from __future__ import annotations

import pytest
from browser_helpers import portal_server, require_browser

from vibesynergy.core.config import load_settings
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime

pytestmark = pytest.mark.integration

DRAW_FOLDED = """async () => {
  const charts = await import(new URL("static/js/charts.js", document.baseURI).href);
  const box = document.createElement("div");
  box.id = "folded-box";
  box.style.cssText = "display: none; width: 600px";
  const el = document.createElement("div");
  el.style.cssText = "width: 100%; height: 120px";
  box.append(el);
  document.body.append(box);
  charts.mount(el, {
    xAxis: { type: "category", data: ["a", "b", "c"] },
    yAxis: { type: "value" },
    series: [{ type: "line", data: [1, 3, 2] }],
  });
}"""

SVG_WIDTH = """() => {
  const svg = document.querySelector("#folded-box svg");
  return svg ? svg.getBoundingClientRect().width : 0;
}"""


def test_a_chart_drawn_folded_fills_its_box_once_opened(tmp_path, monkeypatch) -> None:
    chrome = require_browser(axe=False)
    from playwright.sync_api import sync_playwright

    config = tmp_path / "config.toml"
    config.write_text('[user]\ntimezone = "Europe/Amsterdam"\n')
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings())
    rt.migrate()
    try:
        with portal_server(create_portal_app(rt)) as base, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 800})
            page.goto(base + "/", wait_until="domcontentloaded")
            page.evaluate(DRAW_FOLDED)
            page.evaluate(
                "() => { document.querySelector('#folded-box').style.display = '';"
                " document.querySelector('#folded-box').style.display = 'block'; }"
            )
            width = 0.0
            for _ in range(50):  # the page's CSP allows no eval, so poll from here
                width = page.evaluate(SVG_WIDTH)
                if width > 500:
                    break
                page.wait_for_timeout(100)
            assert width == pytest.approx(600, abs=2)
            browser.close()
    finally:
        rt.close()
