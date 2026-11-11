"""The offline page's service worker."""

from __future__ import annotations

from fastapi.testclient import TestClient

from vibesynergy.portal.app import STATIC, create_portal_app


def test_service_worker_is_served_from_the_root_and_caches_only_the_offline_page(runtime) -> None:
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = api.get("/sw.js")
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/javascript")
    assert reply.headers["cache-control"] == "no-cache"
    assert api.get("/static/offline.html").status_code == 200
    source = (STATIC / "sw.js").read_text()
    # Only navigations fall back to the offline page; API answers are never cached.
    assert 'event.request.mode !== "navigate"' in source and "/api" not in source


def test_a_shared_view_has_no_service_worker(runtime) -> None:
    api = TestClient(create_portal_app(runtime, share_mode=True), base_url="http://localhost")
    assert api.get("/sw.js").status_code in (403, 404)


def test_an_older_version_path_still_serves_the_current_files(runtime) -> None:
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    old = api.get("/static/v/0123456789abcdef/js/pages/zones.js")
    assert old.status_code == 200 and "export async function render" in old.text
    assert api.get("/static/v/not-a-version/js/app.js").status_code == 404
    assert api.get("/static/v/0123456789abcdef/../../app.py").status_code == 404


def test_home_screen_start_address_is_the_portal(runtime) -> None:
    import json

    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    # Served from /static/v/<build>/: three levels up is the portal (or the share root).
    assert manifest["start_url"] == "../../../" and manifest["scope"] == "../../../"
    old = api.get("/static/v/", follow_redirects=False)
    assert old.status_code == 302 and old.headers["location"] == "../../"
    assert api.get("/static/v/", follow_redirects=True).status_code == 200
