"""Web portal: tailnet-only app, guards, and the same numbers as the MCP tools."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vibesynergy.api.app import create_app
from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import series as qs
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import daily_unified

TAILNET = "server.example.ts.net"


@pytest.fixture
def portal(runtime):
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"})
    rt = Runtime.open(settings)
    today = today_in(settings.timezone)
    rows = []
    for back in range(30):  # 30 days of HRV around 50, today 62; weight on a few days
        day = today - timedelta(days=back)
        hrv = 62.0 if back == 0 else 48.0 + back % 5
        rows.append((day, "hrv.night_avg", hrv, "ms", "garmin"))
        if back % 7 == 3:
            rows.append((day, "body.weight", 80.0 + back / 10, "kg", "withings"))
    with rt.engine.begin() as conn:
        conn.execute(
            daily_unified.insert(),
            [
                {"subject_id": 1, "date": d, "metric": m, "value": v, "unit": u, "source": s}
                for d, m, v, u, s in rows
            ],
        )
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    yield rt, client, today
    rt.close()


def test_today_matches_the_query_layer(portal) -> None:
    rt, client, today = portal
    reply = client.get("/api/today")
    assert reply.status_code == 200
    data = reply.json()["data"]
    assert data["date"] == str(today)
    assert data["day"]["metrics"]["hrv"]["hrv.night_avg"]["value"] == 62.0
    # the same baseline the MCP tools report
    assert data["baselines"]["hrv.night_avg"] == qs.baseline(rt.engine, "hrv.night_avg", today)
    assert data["baselines"]["hrv.night_avg"]["z"] > 1
    trend = data["trend"]
    assert trend["columns"][0] == "period" and trend["rows"][-1][0] == str(today)
    assert reply.json()["meta"]["period"]["start"] == str(today - timedelta(days=13))
    assert data["plan"] == []  # nothing planned in the fixture


def test_today_same_numbers_as_mcp(portal) -> None:
    import anyio
    from mcp import Client

    rt, client, today = portal

    async def mcp_day():
        async with Client(build_server(rt)) as mcp:
            result = await mcp.call_tool("vibesynergy_day", {"date": str(today)})
            return result.structured_content["data"]

    mcp = anyio.run(mcp_day)
    portal_day = client.get("/api/today").json()["data"]["day"]
    assert portal_day["metrics"] == mcp["metrics"]
    assert portal_day["vs_baseline_60d"] == mcp["vs_baseline_60d"]


def test_meta_and_bad_input(portal) -> None:
    _, client, today = portal
    meta = client.get("/api/meta").json()["data"]
    assert meta["today"] == str(today)
    assert meta["metrics"]["hrv.night_avg"]["unit"] == "ms"
    reply = client.get("/api/today", params={"date": "yesterday-ish"})
    assert reply.status_code == 400 and "YYYY-MM-DD" in reply.json()["error"]


def test_pages_and_modules_are_served(portal) -> None:
    _, client, _ = portal
    page = client.get("/")
    assert page.status_code == 200
    version = client.get("/version")
    build_id = version.json()["build_id"]
    assert re.fullmatch(r"[0-9a-f]{16}", build_id)
    assert client.get("/vibesynergy/version").json() == version.json()
    assert version.headers["cache-control"] == "no-store"
    assert f'data-build-id="{build_id}"' in page.text
    assert f'src="static/v/{build_id}/js/app.js"' in page.text
    assert f'href="static/v/{build_id}/css/design.css"' in page.text
    assert page.headers["cache-control"] == "no-cache"
    module = client.get(f"/static/v/{build_id}/vendor/maplibre/maplibre-gl.mjs")
    assert module.headers["content-type"].startswith("text/javascript")
    assert module.headers["cache-control"] == "public, max-age=31536000, immutable"
    script = client.get(f"/static/v/{build_id}/js/app.js")
    assert script.headers["content-type"].startswith("text/javascript")
    assert client.get("/static/js/app.js").headers["cache-control"] == "no-cache"
    app_js = script.text
    assert 'startsWith("daily:")' in app_js and "daily_coach" in app_js


def test_portal_menu_day_group_and_data_redirects(portal) -> None:
    from vibesynergy.portal.app import STATIC

    nav = (STATIC / "js/nav.js").read_text()
    assert 'label: N("Dashboard")' in nav  # the start page's section
    # One start page: Diary and Calendar are sections of it, old routes redirect.
    assert '[["dashboard", N("Today")]]' in nav
    assert '["calendar", N("Calendar")]' not in nav
    for old in ("today:", "day:", "diary:", "calendar:"):
        assert old in nav.split("REDIRECTS")[1]
    # Training and Training plan merged into Planning & progress; old routes redirect.
    assert '["planning", N("Planning & progress")]' in nav
    assert '["plan", N("Training plan")]' not in nav and '["training", N("Training")]' not in nav
    redirects = nav.split("REDIRECTS")[1]
    assert "training: (query) => `planning/analysis${query}`" in redirects
    assert "plan: (query) => `planning${query}`" in redirects
    assert '"data"' not in nav.split("SECTIONS")[1].split("];")[0]
    assert "settings/data/sync" in nav
    assert "settings/data/sources" in nav


def test_source_title_and_ui_login_hint(portal) -> None:
    _, client, _ = portal
    from vibesynergy.portal.app import STATIC

    settings = client.get("/api/settings").json()["data"]
    caldav = next(source for source in settings["sources"] if source["id"] == "caldav")
    assert caldav["name"] == "iCal / CalDAV calendar"
    assert caldav["login"]["setup"] == "#/settings/calendar"
    assert "command" not in caldav["login"]
    sources_js = (STATIC / "js/settings_sources.js").read_text()
    data_js = (STATIC / "js/pages/data.js").read_text()
    assert "<h2>${esc(sourceName(id))}</h2>" in sources_js
    assert "syncAdvice(p.source, p.last_error)" in data_js
    assert "syncDetails(p.last_error)" in data_js
    assert "uv run vibesynergy auth" not in data_js


def test_settings_use_autosave_not_draft_flow(portal) -> None:
    """Sections that had concept/preview/versions: Settings → Coach (policy + context).
    Data → Sources publication toggles already autosaved before this change."""
    from vibesynergy.portal.app import STATIC

    root = STATIC / "js"
    coach = (root / "coach_settings.js").read_text()
    assert "bindAutosave" in coach
    assert "Save draft" not in coach
    assert "data-preview" not in coach
    assert "data-activate" not in coach
    assert "Revision history" not in coach
    assert "persistKind" in coach

    autosave = (root / "settings_autosave.js").read_text()
    assert "bindAutosave" in autosave
    assert "Saved." in autosave or 't("Saved.")' in autosave

    settings = (root / "pages/settings.js").read_text()
    assert "bindAutosave" in settings
    assert 'from "../settings_autosave.js"' in settings
    assert 'type="button" data-save>${t("Save")}' not in settings
    assert "data-ai-models" not in settings
    assert "data-save-rules" not in settings


def test_security_headers(portal) -> None:
    _, client, _ = portal
    headers = client.get("/").headers
    csp = headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "https://" not in csp  # nothing external: map tiles come from the server's cache
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"


def test_unknown_hosts_are_refused(portal) -> None:
    rt, _, _ = portal
    app = create_portal_app(rt)
    for host, status in [
        ("evil.example.com", 421),  # DNS rebinding: a foreign name pointing at server
        (f"{TAILNET}:8447", 200),
        ("127.0.0.1:8447", 200),
        ("localhost", 200),
        ("[::1]:8447", 200),
    ]:
        client = TestClient(app, base_url=f"http://{host}")
        assert client.get("/api/meta").status_code == status, host


def test_writes_need_the_portal_header_and_same_origin(portal) -> None:
    _, client, _ = portal
    assert client.post("/api/anything").status_code == 403
    foreign = client.post(
        "/api/anything",
        headers={"x-vibesynergy": "portal", "origin": "https://evil.example.com"},
    )
    assert foreign.status_code == 403
    same = client.post(
        "/api/anything",
        headers={"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"},
    )
    assert same.status_code in (404, 405)  # passes the guard; no such route (yet)


def test_portal_is_not_on_the_public_app(portal) -> None:
    rt, _, _ = portal
    public = TestClient(create_app(rt, with_mcp=False), base_url=f"https://{TAILNET}")
    for path in ("/api/today", "/api/meta", "/vibesynergy/api/today", "/static/js/app.js"):
        assert public.get(path).status_code == 404, path
    assert "VibeSynergy" not in public.get("/").text


def test_portal_port_config(env, tmp_path) -> None:
    from conftest import write_config

    write_config(env, extra="[portal]\nport = 8447\nhosts = ['vibesynergy.lan']\n")
    settings = load_settings()
    from vibesynergy.portal.app import allowed_hosts, portal_port

    assert portal_port(settings) == 8447
    assert "vibesynergy.lan" in allowed_hosts(settings)
    write_config(env, extra="[portal]\nport = 'x'\n")
    with pytest.raises(ConfigError):
        load_settings()


def test_day_page_timeline(runtime) -> None:
    from datetime import date

    from garmin_fake import FakeGarmin

    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import training
    from vibesynergy.sources.garmin.source import Throttled

    day = date(2026, 9, 20)
    settings = replace(
        runtime.settings,
        sources={"garmin": {"enabled": True, "request_interval": 0}},
        server={"public_url": f"https://{TAILNET}"},
    )
    rt = Runtime.open(settings)
    rt.registry.get("garmin")._api = Throttled(FakeGarmin(), 0)
    rt.sync.sync("garmin", ["heart_rate", "stress", "sleep", "activities"], Window(day, day))
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    reply = client.get("/api/day", params={"date": str(day)})
    assert reply.status_code == 200
    t = reply.json()["data"]["timeline"]
    assert t["columns"] == ["time", "bodybattery.sample", "hr.sample", "stress.sample"]
    assert len(t["rows"]) == 288  # every 5 minutes of the local day
    # the same samples as the MCP tool vibesynergy_intraday
    same = training.intraday(rt.parquet, day, settings.timezone, resample_min=5)
    assert t["rows"] == same["rows"]
    # the night before midnight has negative minutes; stages come from the best night
    assert t["sleep_stages"] and t["sleep_stages"][0]["start"] < 0
    assert {s["stage"] for s in t["sleep_stages"]} <= {"deep", "light", "rem", "awake"}
    ride = t["activities"][0]
    assert ride["start"] == 8 * 60 and 9 * 60 <= ride["end"] <= 9 * 60 + 5  # 08:00, ~1 h
    assert ride["id"].startswith("garmin:")
    assert client.get("/api/day", params={"date": "2026-13-01"}).status_code == 400
    rt.close()


@pytest.fixture
def with_activities(runtime):
    from datetime import date

    from garmin_fake import FakeGarmin
    from strava_fake import FakeStravaAPI

    from vibesynergy.core.timeutil import Window
    from vibesynergy.sources import oauth
    from vibesynergy.sources.garmin.source import Throttled
    from vibesynergy.sources.strava.client import StravaClient

    day = date(2026, 9, 20)
    settings = replace(
        runtime.settings,
        sources={s: {"enabled": True, "request_interval": 0} for s in ("garmin", "strava")},
        server={"public_url": f"https://{TAILNET}"},
    )
    rt = Runtime.open(settings)
    rt.registry.get("garmin")._api = Throttled(FakeGarmin(), 0)
    strava = rt.registry.get("strava")
    store = strava.ctx.secrets
    store.set(oauth.APP, {"client_id": "12345", "client_secret": "s"})
    oauth.save_tokens(store, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    strava._client = StravaClient(store, http=FakeStravaAPI(), sleep=lambda s: None)
    rt.sync.sync("garmin", ["activities", "activity_files"], Window(day, day))
    rt.sync.sync("strava", ["activities", "activity_streams"], Window(day, day))
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    yield rt, client
    rt.close()


def test_activities_list_matches_the_query_layer(with_activities) -> None:
    from datetime import date

    from vibesynergy.core.timeutil import Window
    from vibesynergy.query import views

    rt, client = with_activities
    data = client.get("/api/activities").json()["data"]
    same = views.activities(rt.engine, Window(date(1900, 1, 1), date(2100, 1, 1)), limit=50)
    assert [a["group_id"] for a in data["activities"]] == [
        a["group_id"] for a in same["activities"]
    ]
    assert data["totals"] == same["totals"] and data["totals"]["count"] >= 2
    assert {s["sport"] for s in data["sports"]} >= {"cycling"}
    rides = client.get("/api/activities", params={"sport": "cycling"}).json()["data"]
    assert rides["activities"] and all("cycling" in a["sport"] for a in rides["activities"])
    by_distance = client.get("/api/activities", params={"sort": "distance"}).json()["data"]
    distances = [a["distance_m"] or 0 for a in by_distance["activities"]]
    assert distances == sorted(distances, reverse=True)
    assert client.get("/api/activities", params={"sort": "fastest"}).status_code == 400
    assert client.get("/api/activities", params={"period": "someday"}).status_code == 400
    page = client.get("/api/activities", params={"limit": 1}).json()["data"]
    assert len(page["activities"]) == 1 and page["next_offset"] == 1


def test_activity_detail_with_streams(with_activities) -> None:
    _, client = with_activities
    listed = client.get("/api/activities").json()["data"]["activities"]
    with_route = next(a for a in listed if a["sources"][0].startswith("strava:9001"))
    reply = client.get("/api/activity", params={"id": with_route["sources"][0]})
    assert reply.status_code == 200
    data = reply.json()["data"]
    assert data["activity"]["group_id"] == with_route["group_id"]
    cols = data["stream_data"]["columns"]
    assert {"elapsed_s", "lat", "lon", "hr"} <= set(cols)
    assert len(cols["lat"]) == len(cols["elapsed_s"])
    assert data["sources"] and data["sources"][0]["id"].startswith("strava:")
    assert client.get("/api/activity", params={"id": "nonsense"}).status_code == 400
    assert client.get("/api/activity", params={"id": "garmin:0"}).status_code == 400


def test_activity_detail_keeps_zone_kinds_from_different_copies(with_activities) -> None:
    from sqlalchemy import select, update

    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.query import views
    from vibesynergy.store.schema import activity_unified, activity_zone_time

    rt, client = with_activities
    activity_id = "garmin:21000000001"
    first = views.activity_detail(rt.engine, rt.parquet, activity_id)
    group_id = first["activity"]["group_id"]
    assert "hr" in {z["kind"] for z in first["zones"]}
    with rt.engine.begin() as conn:
        raw_id = conn.scalar(
            select(activity_zone_time.c.raw_item_id).where(
                activity_zone_time.c.source == "garmin",
                activity_zone_time.c.kind == "hr",
            )
        )
        assert raw_id
        conn.execute(
            update(activity_unified)
            .where(activity_unified.c.group_id == group_id)
            .values(sources=[*first["activity"]["sources"], "files:pace-copy"])
        )
        conn.execute(
            activity_zone_time.insert().values(
                source="files",
                activity_source_id="pace-copy",
                kind="pace",
                zone=1,
                seconds=60,
                low=3.0,
                raw_item_id=raw_id,
                updated_at=utc_now(),
            )
        )
    detail = client.get("/api/activity", params={"id": activity_id}).json()["data"]
    assert {z["kind"] for z in detail["zones"]} >= {"hr", "pace"}


def test_diary_shows_the_day_and_links_notes_to_their_activity(with_activities) -> None:
    from datetime import date

    from vibesynergy.query import journal

    rt, client = with_activities
    day = date(2026, 9, 20)
    listed = client.get("/api/activities").json()["data"]["activities"]
    with_route = next(a for a in listed if a["sources"][0].startswith("strava:9001"))
    group_id = with_route["group_id"]
    note = journal.add_note(
        rt.engine,
        text="Felt strong on the climb",
        start=day,
        activity_id=group_id,
        source="strava",
        source_ref="9001",
    )

    diary = client.get(
        "/api/diary", params={"start": day.isoformat(), "end": day.isoformat()}
    ).json()["data"]
    assert group_id in {e["group_id"] for e in diary["events"]}
    linked = next(n for n in diary["notes"] if n["id"] == note["id"])
    assert linked["activity_id"] == group_id

    activity = client.get("/api/activity", params={"id": with_route["sources"][0]}).json()["data"]
    assert [n["id"] for n in activity["notes"]] == [note["id"]]


def test_series_matches_mcp_and_compares(portal) -> None:
    import anyio
    from mcp import Client

    rt, client, today = portal
    params = {"metrics": ["hrv.night_avg", "body.weight"], "period": "last_28d"}
    reply = client.get("/api/series", params=params)
    assert reply.status_code == 200
    data = reply.json()["data"]

    async def mcp_series():
        async with Client(build_server(rt)) as mcp:
            args = {"metrics": params["metrics"], "period": "last_28d", "max_points": 400}
            result = await mcp.call_tool("vibesynergy_series", args)
            return result.structured_content["data"]

    assert data["series"]["rows"] == anyio.run(mcp_series)["rows"]  # the same numbers
    assert data["stats"]["hrv.night_avg"]["n"] == 28

    compared = client.get("/api/series", params={**params, "compare": "previous"}).json()["data"]
    other = compared["compare"]
    assert other["period"]["end"] == str(today - timedelta(days=28))
    # comparison rows are shifted onto this period's dates
    dates = [row[0] for row in other["series"]["rows"]]
    assert dates and min(dates) >= str(today - timedelta(days=27))
    assert client.get("/api/series", params={**params, "compare": "later"}).status_code == 400


def test_series_all_time_starts_at_first_value(portal) -> None:
    _, client, today = portal
    reply = client.get("/api/series", params={"metrics": ["hrv.night_avg"], "period": "all"})
    assert reply.json()["meta"]["period"]["start"] == str(today - timedelta(days=29))
    assert reply.json()["data"]["series"]["granularity"] == "day"


def test_series_input_errors(portal) -> None:
    _, client, _ = portal
    assert client.get("/api/series", params={"metrics": ["hrv.nightly"]}).status_code == 400
    too_many = {"metrics": ["hrv.night_avg"] * 7}
    assert client.get("/api/series", params=too_many).status_code == 400
    assert client.get("/api/series").status_code == 422  # metrics is required


def test_catalog_lists_metrics_with_data(portal) -> None:
    _, client, _ = portal
    metrics = {m["id"]: m for m in client.get("/api/catalog").json()["data"]["metrics"]}
    assert metrics["hrv.night_avg"]["coverage"]["days_with_data"] == 30
    assert "coverage" not in metrics["bp.systolic"]  # known, but no data yet


WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}


def test_data_page(portal) -> None:
    rt, client, _ = portal
    rt.sync.sync("demo", ["daily"], None)
    data = client.get("/api/data").json()["data"]
    demo = next(s for s in data["sources"] if s["id"] == "demo")
    assert demo["enabled"] and {d["name"] for d in demo["datasets"]} >= {"daily"}
    assert any(d["source"] == "demo" and d["dataset"] == "daily" for d in data["datasets"])
    assert any(a["dataset"] == "daily" and a["items"] > 0 for a in data["archive"])
    assert set(data["jobs"]) == {"counts", "recent"}


def test_coverage_map(portal) -> None:
    _, client, today = portal
    reply = client.get("/api/coverage", params={"months": 6}).json()
    months = reply["data"]["months"]
    assert months[-1] == str(today)[:7] and len(months) <= 6
    hrv = reply["data"]["days_with_data"]["hrv.night_avg"]
    assert sum(hrv.values()) == 30  # the 30 days in the fixture


def test_resync_queues_jobs(portal) -> None:
    rt, client, today = portal
    week = {
        "source": "demo",
        "dataset": "daily",
        "start": str(today - timedelta(days=6)),
        "end": str(today),
    }
    reply = client.post("/api/resync", json=week, headers=WRITE)
    assert reply.status_code == 200
    data = reply.json()["data"]
    assert not data["background"] and len(data["queued"]) == 1 and data["queued"][0]["job"]
    job = rt.jobs.recent()[0]
    assert job["priority"] == 10 and job["payload"]["window"] == f"{week['start']}..{week['end']}"
    # the same request while it is waiting is not queued twice
    again = client.post("/api/resync", json=week, headers=WRITE).json()["data"]
    assert again["queued"][0]["job"] is None

    year = {"source": "demo", "start": "2025-01-15", "end": "2025-12-31"}
    data = client.post("/api/resync", json=year, headers=WRITE).json()["data"]
    assert data["background"] and len(data["queued"]) == 12
    assert data["queued"][0]["window"] == "2025-01-15..2025-01-31"
    assert {j["priority"] for j in rt.jobs.recent(limit=12)} == {-1}  # below scheduled syncs

    # and the worker runs them like any sync job
    worker = rt.worker()
    while worker.run_once():
        pass
    assert rt.jobs.counts().get("done", 0) >= 13


def test_resync_rejects_bad_input(portal) -> None:
    _, client, today = portal
    ok = {"source": "demo", "start": str(today), "end": str(today)}
    for bad in (
        {**ok, "source": "nope"},
        {**ok, "dataset": "nope"},
        {**ok, "start": "2026-02-30"},
        {**ok, "start": str(today), "end": str(today - timedelta(days=1))},
        {**ok, "end": str(today + timedelta(days=1))},
    ):
        assert client.post("/api/resync", json=bad, headers=WRITE).status_code == 400, bad
    assert client.post("/api/resync", json=ok).status_code == 403  # no portal header


def test_training_page(with_activities) -> None:
    _, client = with_activities
    data = client.get("/api/training", params={"period": "all"}).json()["data"]
    # intervals.icu's model by default; without its data every day is labelled computed.
    assert data["load"]["load"] == "intervals" and data["load"]["columns"][0] == "date"
    assert {row[7] for row in data["load"]["rows"]} == {"computed"}
    assert data["load"]["source"] == "vibesynergy"
    trimp = client.get("/api/training", params={"period": "all", "load": "trimp"}).json()
    assert trimp["data"]["load"]["load"] == "trimp"
    assert {"buckets", "totals"} <= set(data["volume"]) and data["granularity"] in ("week", "month")
    assert "total_seconds_by_zone" in data["zones"] and "markers" in data["fitness"]
    garmin = client.get("/api/training", params={"period": "all", "load": "garmin"}).json()
    assert garmin["data"]["load"]["load"] == "garmin"
    assert client.get("/api/training", params={"load": "watts"}).status_code == 400
    assert client.get("/api/training", params={"load": "tss"}).status_code == 200
    assert client.get("/api/training", params={"sport": "chess"}).status_code == 400
    kinds = data["zone_kinds"]
    assert set(kinds) == {"hr", "power", "pace"}
    assert kinds["hr"]["basis"] == "hr" and "three_zone_share" not in kinds["power"]
    assert kinds["hr"]["total_seconds_by_zone"] == data["zones"]["total_seconds_by_zone"]


def test_training_keeps_power_and_pace_zone_counts(with_activities) -> None:
    from datetime import date

    from sqlalchemy import select

    from vibesynergy.core.timeutil import Window, utc_now
    from vibesynergy.query import views
    from vibesynergy.store.schema import activity_zone_time

    rt, client = with_activities
    with rt.engine.begin() as conn:
        sample = conn.execute(
            select(
                activity_zone_time.c.source,
                activity_zone_time.c.activity_source_id,
                activity_zone_time.c.raw_item_id,
            )
            .where(activity_zone_time.c.kind == "hr")
            .limit(1)
        ).one()
        now = utc_now()
        for zone in range(1, 8):
            conn.execute(
                activity_zone_time.insert().values(
                    source=sample.source,
                    activity_source_id=sample.activity_source_id,
                    kind="power",
                    zone=zone,
                    seconds=zone * 10,
                    low=100 + zone * 50,
                    raw_item_id=sample.raw_item_id,
                    updated_at=now,
                )
            )
        for zone, low in ((0, 0.0), (1, 2.5), (2, 3.0), (3, 3.5)):
            conn.execute(
                activity_zone_time.insert().values(
                    source=sample.source,
                    activity_source_id=sample.activity_source_id,
                    kind="pace",
                    zone=zone,
                    seconds=30 + zone,
                    low=low,
                    raw_item_id=sample.raw_item_id,
                    updated_at=now,
                )
            )
    window = Window(date(2026, 1, 1), date(2026, 12, 31))
    power = views.intensity_distribution(rt.engine, window, basis="power")
    assert list(power["total_seconds_by_zone"]) == list(range(1, 8))
    assert [power["total_seconds_by_zone"][zone] for zone in range(1, 8)] == [
        10,
        20,
        30,
        40,
        50,
        60,
        70,
    ]
    assert "three_zone_share" not in power
    pace = views.intensity_distribution(rt.engine, window, basis="pace")
    assert list(pace["total_seconds_by_zone"]) == [0, 1, 2, 3]
    assert "three_zone_share" not in pace
    with pytest.raises(ValueError):
        views.intensity_distribution(rt.engine, window, basis="cadence")

    payload = client.get("/api/training", params={"period": "all"}).json()["data"]["zone_kinds"]
    assert list(payload["power"]["total_seconds_by_zone"]) == [str(n) for n in range(1, 8)]
    assert list(payload["pace"]["total_seconds_by_zone"]) == ["0", "1", "2", "3"]
    assert "three_zone_share" not in payload["power"]
    assert "three_zone_share" not in payload["pace"]


def test_power_curve_page(with_activities) -> None:
    _, client = with_activities
    data = client.get("/api/power_curve", params={"period": "all"}).json()["data"]
    assert {"activities_with_power", "curve"} <= set(data)
    assert all({"seconds", "watts", "activity", "sport"} <= set(p) for p in data["curve"])
    assert client.get("/api/power_curve", params={"sport": ""}).status_code == 400


def test_sleep_page(portal) -> None:
    _, client, _ = portal
    reply = client.get("/api/sleep", params={"period": "last_28d"})
    assert reply.status_code == 200
    data = reply.json()["data"]
    assert {"nights", "summary", "series"} <= set(data)
    assert data["series"]["columns"][0] == "period"
    assert client.get("/api/sleep", params={"period": "someday"}).status_code == 400


def test_body_page(portal) -> None:
    _, client, _ = portal
    reply = client.get("/api/body", params={"period": "last_90d"})
    assert reply.status_code == 200
    data = reply.json()["data"]
    assert {"measurements", "trend", "stats", "latest"} <= set(data)
    assert client.get("/api/body", params={"period": "someday"}).status_code == 400


def test_correlate_page(portal) -> None:
    _, client, _ = portal
    params = {"x": "hrv.night_avg", "y": "hrv.night_avg", "period": "last_28d", "lag": 1}
    data = client.get("/api/correlate", params=params).json()["data"]
    assert [r["lag_days"] for r in data["by_lag"]] == list(range(8))
    assert data["lag"] == 1 and data["points"] and len(data["points"][0]) == 3
    day, x, _ = data["points"][0]
    assert isinstance(day, str) and isinstance(x, float)
    assert client.get("/api/correlate", params={**params, "lag": 9}).status_code == 400
    assert client.get("/api/correlate", params={**params, "x": "nope"}).status_code == 400


def test_sql_notebook(portal) -> None:
    _, client, _ = portal
    tables = client.get("/api/sql/schema").json()["data"]["tables"]
    assert "daily_unified" in tables and "job" not in tables
    query = {"query": "SELECT metric, count(*) AS n FROM daily_unified GROUP BY metric", "limit": 1}
    data = client.post("/api/sql", json=query, headers=WRITE).json()["data"]
    assert data["columns"] == ["metric", "n"] and len(data["rows"]) == 1 and data["truncated"]
    assert client.post("/api/sql", json=query).status_code == 403  # no portal header
    for bad in ("DELETE FROM daily_unified", "SELECT * FROM job", "SELECT 1; SELECT 2"):
        assert client.post("/api/sql", json={"query": bad}, headers=WRITE).status_code == 400


def test_settings_page(runtime, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[user]\ntimezone = "Europe/Amsterdam"\n\n[server]\n'
        f'public_url = "https://{TAILNET}"\n\n[sources.withings]\nenabled = true\n'
    )
    settings = replace(load_settings(config), data_dir=runtime.settings.data_dir)
    rt = Runtime.open(settings)
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    data = client.get("/api/settings").json()["data"]
    withings = next(s for s in data["sources"] if s["id"] == "withings")
    assert withings["enabled"] and withings["login"]["state"] == "not logged in"
    assert data["restart_needed"] is False
    assert data["read_only"]["mcp_url"] == f"https://{TAILNET}/vibesynergy/mcp"
    assert data["read_only"]["portal_url"] == f"https://{TAILNET}:8447"

    change = {
        "changes": {"user": {"sex": "female"}, "sources": {"withings": {"datasets": ["measures"]}}}
    }
    assert client.post("/api/settings", json=change).status_code == 403  # no portal header
    data = client.post("/api/settings", json=change, headers=WRITE).json()["data"]
    assert data["user"]["sex"] == "female" and data["restart_needed"] is True
    # The notice belongs to the tabs whose saved settings wait for a restart.
    assert data["restart_tabs"] == ["sources", "training"]
    synced = {
        d["name"]
        for s in data["sources"]
        if s["id"] == "withings"
        for d in s["datasets"]
        if d["synced"]
    }
    assert synced == {"measures"}
    schedule = {"withings.measures": 10, "alerts": 20, "dropfolder": 0}
    data = client.post("/api/settings", json={"changes": {"schedule": schedule}}, headers=WRITE)
    assert data.json()["data"]["schedule"]["configured"] == schedule
    assert data.json()["data"]["schedule"]["defaults"]["garmin.scheduled_workouts"] == 15
    for bad in (
        {"sources": {"nope": {"enabled": True}}},
        {"sources": {"withings": {"datasets": ["nope"]}}},
        {"user": {"timezone": "Mars/Olympus"}},
        {"schedule": {"withings.nope": 5}},
        {"schedule": {"nope": 5}},
    ):
        assert client.post("/api/settings", json={"changes": bad}, headers=WRITE).status_code == 400
    assert client.get("/api/mcp_clients").json()["data"]["clients"] == []
    reply = client.post("/api/mcp_clients/revoke", json={"client_id": "x"}, headers=WRITE)
    assert reply.status_code == 400
    rt.close()


def test_menu_order_is_kept_on_server(portal) -> None:
    _, client, _ = portal
    write = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
    assert client.get("/api/prefs").json()["data"] == {}
    nav = {"sections": ["health", "today"], "pages": {"health": ["diary", "sleep"]}}
    nav["hidden"] = ["sql"]
    assert client.post("/api/prefs", json={"changes": {"nav": nav}}).status_code == 403
    reply = client.post("/api/prefs", json={"changes": {"nav": nav}}, headers=write)
    assert reply.json()["data"] == {"nav": nav}
    assert client.get("/api/prefs").json()["data"] == {"nav": nav}
    for bad in (
        {"colour": "red"},
        {"nav": {"sections": ["today", "today"]}},
        {"nav": {"hidden": ["<script>"]}},
        {"nav": {"pages": {"health": "sleep"}}},
        {"nav": {"order": []}},
    ):
        assert client.post("/api/prefs", json={"changes": bad}, headers=write).status_code == 400
    reply = client.post("/api/prefs", json={"changes": {"nav": None}}, headers=write)
    assert reply.json()["data"] == {}
    reply = client.post("/api/prefs", json={"changes": {"day_view": "list"}}, headers=write)
    assert reply.json()["data"] == {"day_view": "list"}
    reply = client.post("/api/prefs", json={"changes": {"day_view": "grid"}}, headers=write)
    assert reply.status_code == 400
    hide = {"changes": {"dashboard_hidden": ["data"]}}
    reply = client.post("/api/prefs", json=hide, headers=write)
    assert reply.json()["data"]["dashboard_hidden"] == ["data"]
    reply = client.post("/api/prefs", json={"changes": {"refresh_min": 15}}, headers=write)
    assert reply.json()["data"]["refresh_min"] == 15
    reply = client.post("/api/prefs", json={"changes": {"language": "nl"}}, headers=write)
    assert reply.json()["data"]["language"] == "nl"
    reply = client.post("/api/prefs", json={"changes": {"language": "auto"}}, headers=write)
    assert reply.json()["data"]["language"] == "auto"
    assert (
        client.post("/api/prefs", json={"changes": {"language": "fr"}}, headers=write).status_code
        == 400
    )
    for bad in (-1, 1.5, True, 999):
        change = {"changes": {"refresh_min": bad}}
        assert client.post("/api/prefs", json=change, headers=write).status_code == 400


def test_diary_page_and_writes(portal) -> None:
    _, client, today = portal
    write = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
    note = client.post(
        "/api/diary/note", json={"text": "Cold", "tags": ["sick"]}, headers=write
    ).json()["data"]
    assert note["source"] == "portal" and note["start_date"] == str(today)
    checkin = client.post("/api/diary/checkin", json={"mood": 2}, headers=write).json()["data"]
    assert checkin["mood"] == 2
    injury = client.post(
        "/api/diary/injury",
        json={"body_part": "knee", "side": "left", "pain": 5, "timing": "after"},
        headers=write,
    ).json()["data"]
    assert injury["title"] == "Left knee" and {f["rule"] for f in injury["flags"]} == {
        "pain_above_3"
    }

    page = client.get("/api/diary").json()
    data = page["data"]
    assert [n["text"] for n in data["notes"]] == ["Cold"]
    assert data["checkins"][0]["mood"] == 2
    assert [i["id"] for i in data["injuries"]] == [injury["id"]]
    assert data["load"]["columns"] == ["period", "activity.load", "activity.duration"]
    assert "knee" in data["choices"]["body_parts"]
    assert page["meta"]["period"]["end"] == str(today)

    bad = client.post("/api/diary/injury", json={"body_part": "pinky"}, headers=write)
    assert bad.status_code == 400 and "body_part must be one of" in bad.json()["error"]
    future = client.post(
        "/api/diary/checkin",
        json={"date": str(today + timedelta(days=1)), "mood": 3},
        headers=write,
    )
    assert future.status_code == 400
    gone = client.post("/api/diary/note", json={"id": note["id"], "delete": True}, headers=write)
    assert gone.json()["data"]["deleted"] == note["id"]


def test_diary_can_be_shared() -> None:
    from vibesynergy.portal.share import PAGES, Share

    reader = Share("x", "coach", ("diary",), can_write=False)
    writer = Share("y", "physio", ("diary",), can_write=True)
    assert "diary" in PAGES
    assert reader.allows("GET", "/diary") and not reader.allows("POST", "/diary/injury")
    assert writer.allows("POST", "/diary/injury")
    assert not Share("z", "other", ("today",), can_write=True).allows("GET", "/diary")


def test_correlate_can_leave_out_sick_days(portal) -> None:
    from vibesynergy.query import journal

    rt, client, today = portal
    journal.add_note(rt.engine, text="Cold", start=today - timedelta(days=3), tags=["sick"])
    params = {"x": "hrv.night_avg", "y": "hrv.night_avg", "period": "last_28d"}
    everything = client.get("/api/correlate", params=params).json()["data"]
    healthy = client.get("/api/correlate", params={**params, "exclude_tags": "sick"}).json()["data"]
    assert len(healthy["points"]) == len(everything["points"]) - 1
    day = client.get("/api/day", params={"date": str(today - timedelta(days=3))}).json()["data"]
    assert day["day"]["journal"]["notes"][0]["text"] == "Cold"


def test_pace_curve_api(portal) -> None:
    _, client, _ = portal
    data = client.get("/api/pace_curve").json()["data"]
    assert {"activities_with_distance", "curve"} <= set(data)


def test_calendar_api(portal) -> None:
    _, client, today = portal
    data = client.get("/api/calendar", params={"metric": "hrv.night_avg"}).json()["data"]
    assert data["metric"] == "hrv.night_avg" and data["days"] and data["years"]
    assert data["span"]["start"] <= data["span"]["end"]
    load = client.get("/api/calendar", params={"metric": "load"}).json()["data"]
    assert load["metric"] == "load"
    year = client.get(
        "/api/calendar",
        params={"metric": "hrv.night_avg", "year": today.year - 1},
    ).json()["data"]
    assert year["year"] == today.year - 1
    week_ago = today - timedelta(days=6)
    custom = client.get(
        "/api/calendar",
        params={"metric": "hrv.night_avg", "from": str(week_ago), "to": str(today)},
    ).json()["data"]
    assert custom["span"]["end"] == str(today)
    assert client.get("/api/calendar", params={"metric": "nope"}).status_code == 400
    assert client.get("/api/calendar", params={"year": 1800}).status_code == 400
    bad = {"from": str(today), "to": str(today - timedelta(days=1))}
    assert client.get("/api/calendar", params=bad).status_code == 400
    agenda = client.get(
        "/api/calendar",
        params={"start": str(today), "end": str(today + timedelta(days=400))},
    )
    assert agenda.status_code == 400
    assert client.get("/api/calendar", params={"from": str(today)}).status_code == 400
    assert client.get("/api/calendar", params={"period": "1800"}).status_code == 400
    future = client.get(
        "/api/calendar",
        params={"agenda": "next_14d", "metric": "hrv.night_avg"},
    )
    assert future.status_code == 200
    over = client.get(
        "/api/calendar",
        params={"agenda_start": str(today), "agenda_end": str(today + timedelta(days=400))},
    )
    assert over.status_code == 400


def test_diary_api_accepts_start_end(portal) -> None:
    _, client, today = portal
    start = today - timedelta(days=10)
    reply = client.get("/api/diary", params={"start": str(start), "end": str(today)})
    assert reply.status_code == 200
    assert reply.json()["meta"]["period"]["start"] == str(start)
    bad = client.get(
        "/api/diary",
        params={"start": str(today), "end": str(today - timedelta(days=1))},
    )
    assert bad.status_code == 400


def test_portal_period_pages_import_period_picker() -> None:
    js = Path(__file__).parents[1] / "src" / "vibesynergy" / "portal" / "static" / "js" / "pages"
    pages = [
        "activities.js",
        "body.js",
        "calendar.js",
        "correlations.js",
        "data.js",
        "diary.js",
        "history.js",
        "metrics.js",
        "sleep.js",
        "training.js",
    ]
    missing = [p for p in pages if "period_picker.js" not in (js / p).read_text()]
    assert not missing, f"import period_picker.js in: {missing}"
    activities = (js / "activities.js").read_text()
    assert "apiParams(periodValue" in activities and "offset: next" in activities
    training = (js / "training.js").read_text()
    assert "sport: state.curve" in training


def test_mail_and_alert_settings(runtime, tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[user]\ntimezone = "Europe/Amsterdam"\n\n[server]\npublic_url = "https://{TAILNET}"\n'
    )
    settings = replace(load_settings(config), data_dir=runtime.settings.data_dir)
    rt = Runtime.open(settings)
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    change = {
        "changes": {
            "mail": {
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_security": "ssl",
                "username": "me@example.com",
            },
            "alerts": {"email_to": "me@example.com"},
        }
    }
    data = client.post("/api/settings", json=change, headers=WRITE).json()["data"]
    assert data["mail"]["smtp_security"] == "ssl" and data["alerts"]["email_to"] == "me@example.com"
    assert data["mail"]["password_set"] is False
    reply = client.post("/api/mail/password", json={"value": "pw-Zq81x"}, headers=WRITE)
    data = reply.json()["data"]
    assert data["mail"]["password_set"] is True and "pw-Zq81x" not in str(data)
    bad = {"changes": {"mail": {"smtp_security": "tls1"}}}
    assert client.post("/api/settings", json=bad, headers=WRITE).status_code == 400
    rt.close()


def test_whatsapp_endpoints_are_gone(portal) -> None:
    _, client, _ = portal
    assert client.get("/api/whatsapp/status").status_code == 404
    assert client.post("/api/whatsapp/unlink", headers=WRITE).status_code == 404


def test_file_upload(portal, monkeypatch) -> None:
    _, client, _ = portal
    from test_dropfolder import gpx

    from vibesynergy import uploads

    # CI /tmp is a small tmpfs; this import must not depend on the 5 GiB spare.
    monkeypatch.setattr(uploads, "check_space", lambda path, size: None)
    files = [("files", ("ride.gpx", gpx(sport="cycling"), "application/gpx+xml"))]
    reply = client.post("/api/files/upload", files=files, headers=WRITE)
    assert reply.status_code == 200 and reply.json()["data"]["results"][0]["imported"] == 1
    assert client.get("/api/files").json()["data"]["history"][0]["file"] == "ride.gpx"
    assert client.post("/api/files/upload", files=files).status_code == 403  # no portal header


def test_file_upload_streams_to_disk_and_refuses_big_loose_files(portal, monkeypatch) -> None:
    """Uploads are written in pieces; a loose file above the drop folder's limit gets a plain
    message pointing at History, and nothing is left behind."""
    from vibesynergy import dropfolder, uploads

    rt, client, _ = portal
    # Space has to pass so the size limit, and the History message, are what refuse the file.
    monkeypatch.setattr(uploads, "check_space", lambda path, size: None)
    monkeypatch.setattr(dropfolder, "MAX_BYTES", 1024)
    files = [("files", ("big.fit", b"x" * 5000, "application/octet-stream"))]
    reply = client.post("/api/files/upload?lang=nl", files=files, headers=WRITE)
    assert reply.status_code == 400 and "Geschiedenis" in reply.json()["error"]
    assert not [p for p in dropfolder.folder(rt.settings).iterdir() if p.name.startswith(".big")]


def test_activities_skip_coaching_sessions_when_fetching_latest(runtime) -> None:
    """Regression: find non-coaching activity even if 10+ newest are coaching sessions."""
    from datetime import UTC, date, datetime

    from vibesynergy.derive import unify
    from vibesynergy.model import Activity
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.portal.app import create_portal_app

    day = date(2026, 9, 29)
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"})
    rt = Runtime.open(settings)

    raw = rt.raw.put(
        source="garmin",
        dataset="test",
        item_key="coaching",
        payload=b"{}",
        content_type="application/json",
    )

    activities = []
    for i in range(15):
        activities.append(
            Activity(
                str(i),
                datetime(2026, 9, 29, 8 + i // 2, tzinfo=UTC),
                day,
                "running",
                name="Training geven" if i < 10 else f"My run {i}",
                elapsed_s=3600,
                moving_s=3600,
                avg_hr=128,
            )
        )

    with rt.engine.begin() as conn:
        write_records(conn, activities, source="garmin", raw_item_id=raw.id)
    unify.rebuild(rt.engine)

    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")

    reply = client.get("/api/activities", params={"limit": 100})
    assert reply.status_code == 200
    data = reply.json()["data"]

    activities_list = data["activities"]
    assert len(activities_list) == 15

    coaching_count = sum(1 for a in activities_list if a["kind"] == "coaching")
    non_coaching_count = sum(1 for a in activities_list if a["kind"] != "coaching")
    assert coaching_count >= 10, "Should have at least 10 coaching sessions"
    assert non_coaching_count >= 5, "Should have at least 5 non-coaching activities"

    latest_non_coaching = next(
        (a for a in activities_list if a["kind"] != "coaching"),
        None,
    )
    assert latest_non_coaching is not None, "Should find at least one non-coaching activity"

    rt.close()


def test_plan_builder_route_returns_defaults_and_keeps_choices(portal) -> None:
    _, client, _ = portal
    write = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
    data = client.get("/api/plan/builder").json()["data"]
    assert data["chosen"] == []
    assert data["settings"]["rhythm"] == "3:1" and data["settings"]["taper_weeks"] == 2
    assert set(data["choices"]["sports"]) == {"run", "triathlon"}
    change = {"plan_builder": {"sport": "run", "distance": "half", "type": "trail"}}
    assert client.post("/api/prefs", json={"changes": change}, headers=write).status_code == 200
    data = client.get("/api/plan/builder").json()["data"]
    assert data["chosen"] == ["distance", "sport", "type"]
    assert data["settings"]["distance"] == "half" and data["settings"]["type"] == "trail"
    bad = {"plan_builder": {"sport": "run", "distance": "full"}}
    assert client.post("/api/prefs", json={"changes": bad}, headers=write).status_code == 400
