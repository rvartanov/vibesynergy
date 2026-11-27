"""The start page's first load stays small (#208): preferences are asked for once, the
English catalogue holds only the few labels that differ from their key, and the map library
and its stylesheet load only on an activity that has a route."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from browser_helpers import portal_server, require_browser

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.derive import unify
from vibesynergy.model import Activity, Stream
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime

pytestmark = pytest.mark.integration

LANGUAGE = "() => document.documentElement.lang"
MAP_DRAWN = """() => ({
  map: !!document.querySelector(".map.maplibregl-map canvas"),
  css: [...document.styleSheets].some((s) => (s.href || "").endsWith("maplibre-gl.css")),
})"""


def _seed(rt: Runtime) -> None:
    """One synthetic run with a short made-up route."""
    today = today_in(rt.settings.timezone)
    start = datetime(today.year, today.month, today.day, 7, tzinfo=UTC)
    run = Activity(
        "route1",
        start,
        today,
        "running",
        moving_s=600,
        elapsed_s=600,
        distance_m=2000,
        name="Synthetic loop",
        fit_id="synthetic-route-1",
    )
    raw = rt.raw.put(
        source="garmin",
        dataset="synthetic",
        item_key="route",
        payload=b"s",
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, [run], source="garmin", raw_item_id=raw.id)
    unify.rebuild(rt.engine)
    samples = 60
    rt.parquet.write_stream(
        Stream(
            "synthetic-route-1",
            {
                "t": [start + timedelta(seconds=i * 10) for i in range(samples)],
                "lat": [10.0 + i * 0.0002 for i in range(samples)],
                "lon": [20.0 + i * 0.0001 for i in range(samples)],
                "hr": [130 + (i % 10) for i in range(samples)],
            },
        )
    )


def _poll(page, script: str, done, what: str, seconds: float = 20):
    # The portal's CSP allows no eval: poll with a function from here.
    deadline = time.time() + seconds
    value = None
    while time.time() < deadline:
        value = page.evaluate(script)
        if done(value):
            return value
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}: {value!r}")


def test_first_load_asks_once_and_loads_the_map_only_with_a_route(tmp_path, monkeypatch) -> None:
    chrome = require_browser(axe=False)
    from playwright.sync_api import sync_playwright

    config = tmp_path / "config.toml"
    config.write_text('[user]\ntimezone = "Europe/Amsterdam"\n')
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings())
    rt.migrate()
    try:
        _seed(rt)
        with portal_server(create_portal_app(rt)) as base, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            for language in ("en", "nl"):
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.add_init_script(
                    f"localStorage.setItem('vibesynergy-language', '{language}')"
                )
                context.route("**/tiles/**", lambda route: route.abort())  # no tile server
                page = context.new_page()
                seen: list[str] = []
                catalogue: dict[str, int] = {}

                def note(response, seen=seen, catalogue=catalogue) -> None:
                    url = response.url.split("?")[0]
                    seen.append(url)
                    if "/i18n/" in url:
                        catalogue[url.rsplit("/", 1)[1]] = len(response.body())

                page.on("response", note)
                page.goto(base + "/#/dashboard", wait_until="domcontentloaded")
                page.locator("#view h1").first.wait_for(timeout=20000)
                page.wait_for_load_state("networkidle")
                assert _poll(page, LANGUAGE, lambda v, want=language: v == want, "lang") == language

                # app.js, nav.js and the start page share one answer.
                assert sum(url.endswith("/api/prefs") for url in seen) == 1, seen
                assert not [url for url in seen if "maplibre" in url], seen
                assert list(catalogue) == [f"{language}.json"], catalogue
                if language == "en":
                    assert catalogue["en.json"] < 2000, catalogue  # not 260 KB of key: key
                else:
                    assert catalogue["nl.json"] > 100_000, catalogue

                page.goto(base + "/#/activity/garmin:route1", wait_until="domcontentloaded")
                drawn = _poll(page, MAP_DRAWN, lambda v: v["map"] and v["css"], "the map")
                assert drawn == {"map": True, "css": True}
                assert [url for url in seen if url.endswith("maplibre-gl.mjs")], seen
                # Once, before the portal's own stylesheets (as it was in index.html).
                order = page.evaluate(
                    "() => [...document.querySelectorAll('link[rel=stylesheet]')]"
                    ".map((l) => l.href.split('/').pop())"
                )
                assert order.count("maplibre-gl.css") == 1 and order[0] == "maplibre-gl.css"
                context.close()
            browser.close()
    finally:
        rt.close()
