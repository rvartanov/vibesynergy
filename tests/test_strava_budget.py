"""Strava's API budget is for new activities: no API history, recent-only syncs, a plain
status when Strava makes us wait, the archive import in the portal, and Garmin not waiting."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from strava_fake import RIDE, FakeStravaAPI
from test_strava_export import ROWS, archive

from vibesynergy import history
from vibesynergy.core.timeutil import Window, today_in, utc_now
from vibesynergy.model import Activity
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import views
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth
from vibesynergy.sources.strava import source as strava_source
from vibesynergy.sources.strava.client import StravaClient, StravaRateLimited, budget_wait
from vibesynergy.store.schema import activity, activity_unified, dataset_state, job
from vibesynergy.sync.scheduler import minutes_for

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
TODAY = date(2026, 9, 28)


@pytest.fixture
def strava(runtime):
    settings = replace(
        runtime.settings,
        sources={"strava": {"enabled": True}},
        server={"public_url": f"https://{TAILNET}"},
    )
    rt = Runtime.open(settings)
    source = rt.registry.get("strava")
    fake = FakeStravaAPI()
    store = source.ctx.secrets
    store.set(oauth.APP, {"client_id": "12345", "client_secret": "ssecret"})
    oauth.save_tokens(store, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    source._client = StravaClient(store, http=fake, sleep=lambda _: None, clock=lambda: 1000.0)
    yield rt, fake, store
    rt.close()


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


def _state(rt, dataset, **values):
    with rt.engine.begin() as conn:
        conn.execute(dataset_state.insert().values(source="strava", dataset=dataset, **values))


# No API history ------------------------------------------------------------------------------


def test_strava_cannot_be_filled_over_the_api(strava) -> None:
    rt, fake, _ = strava
    with pytest.raises(ValueError, match="Import the Strava archive"):
        history.start_api(rt, "strava", date(2012, 1, 1), date(2026, 9, 1), today=TODAY)
    assert fake.calls == [] and history.fills(rt) == []
    assert history.api_history_refused("strava", TODAY - timedelta(days=60), TODAY)
    assert history.api_history_refused("strava", TODAY - timedelta(days=3), TODAY) is None
    assert history.api_history_refused("garmin", date(2012, 1, 1), TODAY) is None  # unchanged


def test_a_queued_strava_run_is_cancelled_and_nothing_is_deleted(strava) -> None:
    rt, fake, _ = strava
    rt.sync.sync("strava", ["activities"], Window(date(2026, 9, 20), date(2026, 9, 20)))
    before = len(_rows(rt, activity))
    assert before
    calls = len(fake.calls)
    left_over = history._insert(
        rt, source="strava", method="api", status="waiting", start=date(2012, 1, 1),
        end=date(2026, 9, 25), next_month=date(2025, 2, 1), total=177, done=157,
    )  # fmt: skip
    queued_job = rt.jobs.enqueue(history.JOB, {"fill": left_over})
    garmin = history._insert(
        rt, source="garmin", method="api", status="running", start=date(2020, 1, 1),
        end=date(2020, 2, 1), next_month=date(2020, 1, 1), total=2,
    )  # fmt: skip
    history.run_step(rt, left_over)  # a job queued before the change
    assert history.get(rt, left_over)["status"] == "cancelled"
    assert _rows(rt, job, job.c.id == queued_job)[0]["status"] == "cancelled"
    assert len(_rows(rt, activity)) == before and len(fake.calls) == calls
    assert history.get(rt, garmin)["status"] == "running"  # other sources are untouched
    stuck = history._insert(rt, source="strava", method="api", status="stuck")
    paused = history._insert(rt, source="strava", method="api", status="paused")
    assert history.cancel_api_history(rt) == 2  # the service does this at start
    assert {history.get(rt, i)["status"] for i in (stuck, paused)} == {"cancelled"}
    with pytest.raises(ValueError, match="Strava archive"):
        history.act(rt, paused, "resume")


def test_the_portal_refuses_strava_history_but_allows_recent_syncs(strava) -> None:
    rt, _, _ = strava
    http = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    today = today_in(rt.settings.timezone)
    old = {"source": "strava", "start": "2012-01-01", "end": str(today)}
    assert http.post("/api/history/start", json=old, headers=WRITE).status_code == 400
    refused = http.post("/api/resync", json=old, headers=WRITE)
    assert refused.status_code == 400 and "Strava archive" in refused.json()["error"]
    recent = {"source": "strava", "start": str(today - timedelta(days=3)), "end": str(today)}
    assert http.post("/api/resync", json=recent, headers=WRITE).status_code == 200
    page = http.get("/api/history").json()["data"]
    (entry,) = [s for s in page["sources"] if s["source"] == "strava"]
    assert entry["api_off"] and entry["recommend"] == "export" and entry["export"]
    assert page["exports"]["strava"]["url"].startswith("https://support.strava.com/")


# Recent-only syncs ---------------------------------------------------------------------------


def test_the_sync_window_is_the_last_sync_with_a_small_overlap(strava) -> None:
    rt, _, _ = strava
    today = today_in(rt.settings.timezone)
    spec = rt.registry.get("strava").dataset("activities")
    assert rt.sync.plan_window("strava", spec) == Window(today - timedelta(days=6), today)
    _state(rt, "activities", synced_until=today - timedelta(days=1))
    assert rt.sync.plan_window("strava", spec).start == today - timedelta(days=3)  # 2 days back
    with rt.engine.begin() as conn:
        conn.execute(dataset_state.update().values(synced_until=date(2013, 1, 1)))
    # e.g. Strava switched off for a year: still only recent days, never a backfill
    assert rt.sync.plan_window("strava", spec).start == today - timedelta(
        days=strava_source.RECENT_DAYS
    )


def test_a_plain_sync_spares_the_budget(strava) -> None:
    rt, fake, _ = strava
    window = Window(date(2026, 9, 20), date(2026, 9, 20))
    rt.sync.sync("strava", None, window)
    first = list(fake.calls)
    assert any(c.endswith("/laps") for c in first) and "/athlete" in first
    fake.calls.clear()
    rt.registry.get("strava")._lists.clear()  # ten minutes later: the list is fetched again
    rt.sync.sync("strava", None, window)  # nothing new
    # one list call; no profile, gear, routes, clubs again; no details for known activities
    assert fake.calls == ["/athlete/activities"]
    # asked for by name, a snapshot is fetched (a manual refresh)
    rt.sync.sync("strava", ["clubs"])
    assert fake.calls[-1] == "/athlete/clubs"


def test_a_changed_activity_is_fetched_again(strava, monkeypatch) -> None:
    rt, fake, _ = strava
    window = Window(date(2026, 9, 20), date(2026, 9, 20))
    rt.sync.sync("strava", ["activities"], window)
    fake.calls.clear()
    monkeypatch.setitem(RIDE, "name", "Zwift - Renamed")
    rt.sync.sync("strava", ["activities"], window)
    assert "/activities/9001" in fake.calls and "/activities/9002" not in fake.calls


# What Sources says -----------------------------------------------------------------------


def test_the_wait_is_persisted_with_its_kind(strava) -> None:
    rt, fake, store = strava
    client = rt.registry.get("strava").client
    assert budget_wait(store, now=1000.0) is None
    fake.status_for["/athlete/clubs"] = 429
    fake.usage = "10,2000"
    with pytest.raises(StravaRateLimited):
        client.get("/athlete/clubs")
    assert budget_wait(store, now=1000.0)["kind"] == "daily"
    assert budget_wait(store, now=1000.0 + 2 * 86400) is None  # over: no status
    fake.usage = "200,100"
    client._limited_until = None
    client._hard_until = None
    client._hard_message = ""
    store.set("request_budget", {})
    with pytest.raises(StravaRateLimited):
        client.get("/athlete/clubs")
    assert budget_wait(store, now=1000.0)["kind"] == "window"


def test_sources_tells_whether_strava_waits(strava) -> None:
    rt, _, store = strava
    http = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")

    def wait():
        sources = http.get("/api/settings").json()["data"]["sources"]
        return {s["id"]: s["budget_wait"] for s in sources}["strava"]

    assert wait() is None
    until = time.time() + 3600
    store.set("request_budget", {"until": until, "message": "daily request budget used"})
    assert wait()["kind"] == "daily"
    store.set("request_budget", {"until": until, "message": "short request budget used"})
    assert wait()["kind"] == "window"


def test_the_plain_wait_texts_are_in_both_languages() -> None:
    from test_i18n import STATIC, dutch

    page = (STATIC / "js" / "settings_sources.js").read_text()
    assert page.count("Strava is busy; new activities will come in after {time}.") == 1
    nl = dutch()
    assert nl["Strava is busy; new activities will come in after {time}."] == (
        "Strava is druk; nieuwe activiteiten komen na {time} binnen."
    )


# The import in Sources -------------------------------------------------------------------


@pytest.fixture
def portal(runtime):
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"})
    rt = Runtime.open(settings)
    yield rt, TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    rt.close()


def _upload(http, data: bytes, name: str, monkeypatch, chunk: int = 700) -> dict:
    from vibesynergy import uploads

    # CI /tmp is a small tmpfs; starting the upload must not depend on the 5 GiB spare.
    monkeypatch.setattr(uploads, "check_space", lambda path, size: None)
    started = http.post(
        "/api/history/uploads", json={"name": name, "size": len(data)}, headers=WRITE
    )
    upload_id = started.json()["data"]["id"]
    received = 0
    while received < len(data):
        reply = http.post(
            f"/api/history/uploads/{upload_id}?offset={received}",
            content=data[received : received + chunk],
            headers=WRITE,
        )
        assert reply.status_code == 200
        received = reply.json()["data"]["received"]
    return http.post(f"/api/history/uploads/{upload_id}/finish", headers=WRITE).json()["data"]


def _settle(rt, fill_id: int) -> dict:
    for _ in range(500):
        if not rt.worker().run_once():
            break
    return history.get(rt, fill_id)


def _known_copies(rt) -> None:
    """The same sessions already here: 1001 from the API (same id) and 1004 from Garmin
    (a different id; found by start time and distance)."""
    api = Activity(
        "1001", datetime(2025, 3, 2, 9, tzinfo=UTC), date(2025, 3, 2), "Ride",
        elapsed_s=30.0, distance_m=210.5,
    )  # fmt: skip
    garmin = Activity(
        "G-1", datetime(2025, 3, 1, 9, 0, 20, tzinfo=UTC), date(2025, 3, 1), "cycling",
        elapsed_s=31.0, distance_m=201.0, name="Saturday ride",
    )  # fmt: skip
    for source, record in (("strava", api), ("garmin", garmin)):
        stored = rt.raw.put(
            source=source, dataset="activities", item_key=record.source_id, payload=b"{}",
            content_type="application/json",
        )  # fmt: skip
        with rt.engine.begin() as conn:
            write_records(conn, [record], source=source, raw_item_id=stored.id)


def test_the_strava_zip_is_imported_from_the_portal_with_progress(
    portal, tmp_path, monkeypatch
) -> None:
    rt, http = portal
    _known_copies(rt)
    data = archive(tmp_path).read_bytes()
    started = _upload(http, data, "Strava Archief.zip", monkeypatch)
    assert started["source"] == "strava_export" and started["method"] == "archive"
    fill = _settle(rt, started["id"])
    assert fill["status"] == "done"
    assert (fill["done"], fill["total"], fill["progress"]) == (3, 3, 1.0)  # activities with a file
    page = http.get("/api/history").json()["data"]
    assert page["fills"][0]["source"] == "strava_export"
    # the same Strava id is one activity, the Garmin recording is found by time and distance
    unified = {r["group_id"]: r for r in _rows(rt, activity_unified)}
    sources = {s for r in unified.values() for s in r["sources"]}
    assert {"strava_export:1001", "strava_export:1004", "strava:1001", "garmin:G-1"} <= sources
    group_of = {s: g for g, r in unified.items() for s in r["sources"]}
    assert group_of["strava_export:1001"] == group_of["strava:1001"]
    assert group_of["strava_export:1004"] == group_of["garmin:G-1"]
    assert group_of["strava_export:1002"] not in (group_of["strava:1001"], group_of["garmin:G-1"])
    total = len(unified)
    # importing it again is harmless
    again = _settle(rt, _upload(http, data, "Strava Archief.zip", monkeypatch)["id"])
    assert again["status"] == "done"
    assert len(_rows(rt, activity_unified)) == total
    assert len(_rows(rt, activity, activity.c.source == "strava_export")) == len(ROWS)


def test_the_import_reports_how_far_it_is(runtime, tmp_path, monkeypatch) -> None:
    from vibesynergy import imports

    seen = []
    real = history._update

    def spy(rt, fill_id, **values):
        if "total" in values:
            seen.append((values["done"], values["total"]))
        real(rt, fill_id, **values)

    monkeypatch.setattr(history, "_update", spy)
    monkeypatch.setattr(imports, "STRAVA_PROGRESS_EVERY", 1)
    run = history.start_archive(runtime, archive(tmp_path))
    assert _settle(runtime, run["id"])["status"] == "done"
    assert seen[0] == (0, 3) and seen[-1] == (3, 3)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)  # never backwards


def test_a_broken_zip_ends_in_a_plain_failure(portal, tmp_path, monkeypatch) -> None:
    rt, http = portal
    started = _upload(http, archive(tmp_path).read_bytes(), "Strava.zip", monkeypatch)
    assert _settle(rt, started["id"])["status"] == "done"
    bad = http.post("/api/history/uploads", json={"name": "notes.txt", "size": 5}, headers=WRITE)
    assert bad.status_code == 400  # not a zip: said before anything is uploaded


# Garmin first ----------------------------------------------------------------------------


def test_garmin_runs_at_least_every_fifteen_minutes_by_default(runtime) -> None:
    unset = replace(runtime.settings, schedule={})
    assert minutes_for(unset, "garmin") <= 15
    assert minutes_for(replace(runtime.settings, schedule={"garmin": 10}), "garmin") == 10


def test_a_garmin_activity_shows_up_while_strava_waits(strava) -> None:
    rt, fake, _ = strava
    fake.status_for["/athlete/activities"] = 429
    fake.usage = "10,2000"
    results = rt.sync.sync("strava", ["activities"], Window(TODAY, TODAY))
    assert results[0].retry_at  # Strava is waiting until tomorrow

    now = utc_now().replace(microsecond=0)
    garmin = Activity("77", now, now.date(), "running", elapsed_s=1800.0, distance_m=5000.0)
    stored = rt.raw.put(
        source="garmin", dataset="activities", item_key="77", payload=b"{}",
        content_type="application/json",
    )  # fmt: skip
    with rt.engine.begin() as conn:
        write_records(conn, [garmin], source="garmin", raw_item_id=stored.id)
    from vibesynergy.sync.engine import DatasetResult

    rt.sync.changed([DatasetResult("garmin", "activities", None, records=1)])  # as after a sync
    (shown,) = views.activities(rt.engine, Window(now.date() - timedelta(days=1), now.date()))[
        "activities"
    ]
    assert shown["distance_m"] == 5000.0  # on the Activities page at once
    (row,) = _rows(rt, activity_unified)
    assert row["primary_source"] == "garmin" and row["distance_m"] == 5000.0
