"""Share links: chosen portal pages behind a secret public address."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from vibesynergy.api.app import create_app
from vibesynergy.core.timeutil import utc_now
from vibesynergy.portal import share
from vibesynergy.runtime import Runtime

PUBLIC = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{PUBLIC}"}


@pytest.fixture
def public(runtime):
    settings = replace(runtime.settings, server={"public_url": f"https://{PUBLIC}"})
    rt = Runtime.open(settings)
    yield rt, TestClient(create_app(rt, with_mcp=False), base_url=f"https://{PUBLIC}")
    rt.close()


def test_link_life_cycle(runtime) -> None:
    code, row = share.create(runtime.engine, "Coach Anna", ["training", "activities"])
    assert len(code) >= 43 and row["state"] == "active" and "token_hash" not in row
    link = share.resolve(runtime.engine, code)
    assert link.title == "Coach Anna" and link.pages == ("activities", "training")
    assert share.list_links(runtime.engine)[0]["uses"] == 1
    later = utc_now() + timedelta(days=8)
    assert share.resolve(runtime.engine, code, now=later) is None  # expired after 7 days
    assert share.revoke(runtime.engine, row["id"]) and not share.revoke(runtime.engine, row["id"])
    assert share.resolve(runtime.engine, code) is None
    assert share.resolve(runtime.engine, "x" * 43) is None
    for bad in (("", ["training"]), ("Anna", []), ("Anna", ["settings"])):
        with pytest.raises(ValueError):
            share.create(runtime.engine, *bad)


def test_shared_view_shows_only_its_pages(public) -> None:
    rt, client = public
    code, _ = share.create(rt.engine, "Coach Anna", ["training"])
    base = f"/vibesynergy/share/{code}"
    first = client.get(base, follow_redirects=False)
    assert first.status_code == 307 and first.headers["location"] == f"{code}/"
    assert client.get(f"/share/{code}/api/meta").status_code == 200  # as the Funnel forwards
    page = client.get(f"{base}/")
    assert page.status_code == 200 and 'js/app.js"' in page.text
    assert "noindex" in page.headers["x-robots-tag"]
    version = client.get(f"{base}/version")
    assert version.status_code == 200 and version.headers["cache-control"] == "no-store"
    assert client.get(f"{base}/static/js/app.js").status_code == 200
    meta = client.get(f"{base}/api/meta").json()["data"]
    assert meta["share"] == {
        "title": "Coach Anna",
        "pages": ["training"],
        "all": False,
        "can_write": False,
    }
    assert client.get(f"{base}/api/training").status_code == 200
    for blocked in ("/api/body", "/api/settings", "/api/mcp_clients", "/api/share_links"):
        assert client.get(f"{base}{blocked}").status_code == 403
    assert client.get("/vibesynergy/share/nope/").status_code == 404
    assert client.get(f"/vibesynergy/share/{'a' * 43}/").status_code == 404


def test_data_history_and_sources_shares_gate_their_apis(public) -> None:
    rt, client = public
    data_code, _ = share.create(rt.engine, "Sync reader", ["data"])
    data_base = f"/vibesynergy/share/{data_code}"
    for path in ("/api/data", "/api/coverage", "/api/files", "/api/history"):
        assert client.get(f"{data_base}{path}").status_code == 200
    for blocked in ("/api/settings", "/api/mcp_clients", "/api/prefs"):
        assert client.get(f"{data_base}{blocked}").status_code == 403

    history_code, _ = share.create(rt.engine, "History reader", ["history"])
    history_base = f"/vibesynergy/share/{history_code}"
    for path in ("/api/data", "/api/history"):
        assert client.get(f"{history_base}{path}").status_code == 200
    assert client.get(f"{history_base}/api/settings").status_code == 403

    with pytest.raises(ValueError):
        share.create(rt.engine, "Sources reader", ["sources"])


def test_diary_and_day_shares_gate_their_apis(public) -> None:
    rt, client = public
    diary_code, _ = share.create(rt.engine, "Diary reader", ["diary"])
    diary_base = f"/vibesynergy/share/{diary_code}"
    assert client.get(f"{diary_base}/api/diary").status_code == 200
    assert client.get(f"{diary_base}/api/today").status_code == 403
    assert client.get(f"{diary_base}/api/day", params={"date": "2026-09-01"}).status_code == 403

    day_code, _ = share.create(rt.engine, "Day reader", ["today"])
    day_base = f"/vibesynergy/share/{day_code}"
    assert client.get(f"{day_base}/api/today").status_code == 200
    assert client.get(f"{day_base}/api/diary").status_code == 403


def test_write_only_when_the_link_may(public) -> None:
    rt, client = public
    body = {"source": "withings", "start": "2026-09-01", "end": "2026-09-02"}
    read, _ = share.create(rt.engine, "Dad", ["data"])
    reply = client.post(f"/vibesynergy/share/{read}/api/resync", json=body, headers=WRITE)
    assert reply.status_code == 403
    write, _ = share.create(rt.engine, "Me on the road", ["data"], can_write=True)
    reply = client.post(f"/vibesynergy/share/{write}/api/resync", json=body, headers=WRITE)
    assert reply.status_code != 403  # reaches the endpoint (which judges the request itself)
    assert client.post(f"/vibesynergy/share/{write}/api/resync", json=body).status_code == 403


def test_revoked_link_stops_at_once(public) -> None:
    rt, client = public
    code, row = share.create(rt.engine, "Coach Anna", ["training"])
    assert client.get(f"/vibesynergy/share/{code}/api/meta").status_code == 200
    share.revoke(rt.engine, row["id"])
    assert client.get(f"/vibesynergy/share/{code}/api/meta").status_code == 404


def test_personal_calendar_context_is_not_added_to_existing_shared_pages(public):
    from vibesynergy.core.timeutil import today_in
    from vibesynergy.model import PlannedItem
    from vibesynergy.normalize.normaliser import write_records

    rt, client = public
    day = today_in(rt.settings.timezone)
    raw = rt.raw.put(
        source="caldav",
        dataset="calendar",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    with rt.engine.begin() as conn:
        write_records(
            conn,
            [PlannedItem("private", day, "commitment", title="Private calendar title")],
            source="caldav",
            raw_item_id=raw.id,
        )
    code, _ = share.create(rt.engine, "Existing reader", ["today", "dashboard", "calendar"])
    for path in ("today", "dashboard", "calendar"):
        reply = client.get(f"/vibesynergy/share/{code}/api/{path}")
        assert reply.status_code == 200
        assert "Private calendar title" not in reply.text
        assert "calendar_url" not in reply.text


def test_everything_link_follows_new_pages_and_edits_save_in_place(public) -> None:
    from vibesynergy.portal.app import create_portal_app

    rt, client = public
    owner = TestClient(create_portal_app(rt), base_url="http://localhost")
    code, row = share.create(rt.engine, "Coach A", ["training"])
    base = f"/vibesynergy/share/{code}"
    assert client.get(f"{base}/api/gear").status_code == 403
    # The owner ticks Everything on the open link: same address, saved directly.
    edit = owner.post(
        f"/api/share_links/{row['id']}", json={"pages": ["*"]}, headers={"x-vibesynergy": "portal"}
    )
    assert edit.status_code == 200 and edit.json()["data"]["link"]["pages"] == ["*"]
    meta = client.get(f"{base}/api/meta").json()["data"]["share"]
    assert meta["all"] and {"coach", "zones", "gear", "dashboard"} <= set(meta["pages"])
    for allowed in ("/api/gear", "/api/thresholds", "/api/coach/feed", "/api/daily-coaching"):
        assert client.get(f"{base}{allowed}").status_code != 403, allowed
    for blocked in ("/api/settings", "/api/coach", "/api/share_links", "/api/prefs"):
        assert client.get(f"{base}{blocked}").status_code == 403, blocked
    renamed = owner.post(
        f"/api/share_links/{row['id']}",
        json={"title": "Coach A (coach)"},
        headers={"x-vibesynergy": "portal"},
    )
    assert renamed.json()["data"]["link"]["title"] == "Coach A (coach)"
    listing = owner.get("/api/share_links").json()["data"]
    assert "gear" in listing["pages"] and listing["all"] == "*"
    share.revoke(rt.engine, row["id"])
    gone = owner.post(
        f"/api/share_links/{row['id']}", json={"pages": ["*"]}, headers={"x-vibesynergy": "portal"}
    )
    assert gone.status_code >= 400
