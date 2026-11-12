"""Populated-portal accessibility review for the design contract.

Seeds synthetic data only, serves a throwaway portal, and checks the main routes with
axe-core (WCAG 2.2 AA tags) in Dutch, in light and dark, at desktop and mobile widths.
A 320px Dutch pass covers reflow. English is a smaller sample. Keyboard tasks cover
today's advice, an activity's evidence, a coach setting, and a form error.

Skipped when Playwright or Chrome is missing so the default pytest run stays fast.
CI runs this file on its own. Set VIBESYNERGY_A11Y_REQUIRED=1 to fail instead of skip.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests
import uvicorn
from sqlalchemy import update
from tests_helpers import midday_timezone

from vibesynergy.core.config import load_settings
from vibesynergy.core.timeutil import Window, today_in, utc_now
from vibesynergy.derive import unify
from vibesynergy.model import (
    Activity,
    ActivityLap,
    ActivityZoneTime,
    BodyMeasurement,
    DailyMetric,
    PlannedItem,
    SleepSession,
    Stream,
    ZoneSet,
)
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach, coach_workouts, journal
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import activity_unified, dataset_state

pytestmark = pytest.mark.integration

AXE_VERSION = "4.10.3"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
ADVICE = "Keep today's easy run. You are recovered enough to do it as planned."
# The dashboard leads with the stored workout. A saved review that no longer matches
# the inputs sits behind the earlier-advice disclosure, in the active language.
DASHBOARD_COPY = {
    "nl": {
        "advice": "Easy run van vandaag is gedaan; herstel goed.",
        "earlier": "Dit advies is opgeslagen voordat er nieuwere gegevens binnenkwamen.",
        "label": "Eerder advies",
    },
    "en": {
        "advice": "Today's Easy run is done; recover well.",
        "earlier": "This advice was saved before newer data arrived.",
        "label": "Earlier advice",
    },
}
RUN_NAME = "Synthetic easy run"
SOURCE = "garmin"
CHROME = ("/usr/bin/google-chrome", "/usr/local/bin/google-chrome")
# The athlete's zone for the run: about noon now, so "today" cannot flip mid-run.
# Server, seeded data and browser all use it. Set at the start of the test.
TIMEZONE = "Europe/Amsterdam"

# Dutch is the athlete language: both themes and both widths. 320px is the reflow check.
# English is a sample so a language-only label problem is still caught.
COMBOS = [
    ("nl", "light", (1280, 800)),
    ("nl", "dark", (1280, 800)),
    ("nl", "light", (390, 844)),
    ("nl", "dark", (390, 844)),
    ("nl", "light", (320, 640)),
    ("en", "light", (1280, 800)),
    ("en", "dark", (390, 844)),
]


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ready(url: str) -> None:
    for _ in range(100):
        try:
            requests.get(url, timeout=0.2)
            return
        except requests.RequestException:
            time.sleep(0.1)
    pytest.fail(f"Temporary portal did not start: {url}")


def _chrome() -> str | None:
    for path in CHROME:
        if Path(path).is_file():
            return path
    return shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def _axe_script() -> str:
    configured = os.environ.get("VIBESYNERGY_AXE_SCRIPT")
    if configured and Path(configured).is_file():
        return Path(configured).read_text()
    dest = Path("/tmp/vibesynergy-axe") / AXE_VERSION
    script = dest / "node_modules" / "axe-core" / "axe.min.js"
    if not script.is_file():
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["npm", "install", "--prefix", str(dest), f"axe-core@{AXE_VERSION}"],
            check=True,
            capture_output=True,
            text=True,
        )
    return script.read_text()


def _put(rt: Runtime, *records: object, key: str) -> None:
    raw = rt.raw.put(
        source=SOURCE,
        dataset="synthetic",
        item_key=key,
        payload=b"synthetic",
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=SOURCE, raw_item_id=raw.id)


def _local(day, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock(hour, minute), tzinfo=tz).astimezone(UTC)


def _icu_plan(rt: Runtime, today) -> None:
    """A synthetic intervals.icu plan: weekly notes and targets, workouts and a race.

    Today's "Easy run" is the workout intervals.icu pushed to Garmin, so the dashboard
    advice stays the same.
    """
    monday = today - timedelta(days=today.weekday())
    kinds = ["laadweek", "laadweek", "herstelweek", "raceweek", "laadweek"]
    events: list[dict] = []
    for index in range(5):
        start = monday + timedelta(weeks=index - 1)
        number = index + 1
        phase = "Base 1" if index < 3 else "Build"
        common = {"start_date_local": f"{start}T00:00:00", "for_week": True}
        events.append(
            {
                "id": 9000 + index,
                "category": "NOTE",
                "name": f"Wk {number} · {phase} · {kinds[index]} · ~6u",
                "description": "Synthetic week note: easy volume, one harder session.",
                "external_id": f"syn:week:{number:02d}",
                **common,
            }
        )
        for offset, (sport, seconds) in enumerate((("Ride", 7200), ("Run", 9000))):
            events.append(
                {
                    "id": 9100 + index * 10 + offset,
                    "category": "TARGET",
                    "type": sport,
                    "name": f"{sport} target",
                    "time_target": seconds,
                    "external_id": f"syn:target:{number:02d}:{sport.lower()}",
                    **common,
                }
            )
    for offset, (name, sport, seconds) in enumerate(
        (("Easy run", "Run", 2400), ("Endurance ride", "Ride", 3600), ("Long run", "Run", 5400))
    ):
        day = today + timedelta(days=offset)
        events.append(
            {
                "id": 9200 + offset,
                "category": "WORKOUT",
                "type": sport,
                "name": name,
                "start_date_local": f"{day}T00:00:00",
                "moving_time": seconds,
                "description": "Warmup\n- 10m Z1\n\nMain set\n- 20m Z2",
                "external_id": f"syn:{day}:{sport.lower()}",
            }
        )
    race_day = today + timedelta(days=60)
    events.append(
        {
            "id": 9300,
            "category": "RACE_A",
            "type": "Run",
            "name": "Synthetic A race",
            "start_date_local": f"{race_day}T00:00:00",
            "external_id": f"syn:{race_day}:race",
        }
    )
    from vibesynergy.sources.intervals_icu.mappers import map_events

    raw = rt.raw.put(
        source="intervals_icu",
        dataset="events",
        item_key="calendar",
        payload=json.dumps(events).encode(),
        content_type="application/json",
    )
    with rt.engine.begin() as conn:
        write_records(
            conn,
            list(map_events(rt.raw.load(raw.id))),
            source="intervals_icu",
            raw_item_id=raw.id,
        )


def populate(rt: Runtime) -> dict:
    """Synthetic athlete week. No real health data."""
    tz = ZoneInfo(rt.settings.timezone)
    today = today_in(rt.settings.timezone)
    # Steps and resting heart rate from the demo source; the week's activities are the
    # ones built below, not the demo's own runs and rides.
    rt.sync.sync("demo", ["daily", "profile"], Window(today - timedelta(days=20), today))
    run_start = _local(today, 7, 30, tz)
    ride_start = _local(today - timedelta(days=2), 18, 0, tz)
    offset = int(run_start.astimezone(tz).utcoffset().total_seconds())
    run = Activity(
        "run1",
        run_start,
        today,
        "running",
        tz_offset_s=offset,
        name=RUN_NAME,
        elapsed_s=3000,
        moving_s=2900,
        distance_m=8000,
        ascent_m=40,
        avg_hr=142,
        max_hr=158,
        avg_speed=2.76,
        calories=480,
        training_load=45,
        device="Synthetic watch",
        fit_id="synthetic-run-1",
    )
    ride = Activity(
        "ride1",
        ride_start,
        today - timedelta(days=2),
        "cycling",
        tz_offset_s=offset,
        name="Synthetic steady ride",
        elapsed_s=3600,
        moving_s=3500,
        distance_m=25000,
        avg_hr=128,
        max_hr=150,
        avg_power=160,
        avg_speed=7.1,
        device="Synthetic watch",
        fit_id="synthetic-ride-1",
    )
    unzoned_start = _local(today - timedelta(days=1), 8, 0, tz)
    unzoned = Activity(
        "unzoned2",
        unzoned_start,
        today - timedelta(days=1),
        "running",
        tz_offset_s=offset,
        name="Synthetic HR-only zones",
        elapsed_s=2400,
        moving_s=2300,
        distance_m=6000,
        avg_hr=135,
        max_hr=155,
        avg_speed=2.6,
        device="Synthetic watch",
        fit_id="synthetic-unzoned-2",
    )
    records: list = [
        run,
        ride,
        unzoned,
        ActivityLap(
            "run1", 0, run_start, elapsed_s=1500, moving_s=1450, distance_m=4000, avg_hr=138
        ),
        ActivityLap(
            "run1",
            1,
            run_start + timedelta(seconds=1500),
            elapsed_s=1500,
            moving_s=1450,
            distance_m=4000,
            avg_hr=146,
        ),
        ActivityZoneTime("run1", "hr", 1, 400, 100),
        ActivityZoneTime("run1", "hr", 2, 900, 125),
        ActivityZoneTime("run1", "hr", 3, 1200, 145),
        ActivityZoneTime("run1", "hr", 4, 400, 160),
        ActivityZoneTime("run1", "hr", 5, 100, 175),
        ActivityZoneTime("run1", "pace", 0, 120, 0.0),
        ActivityZoneTime("run1", "pace", 1, 600, 2.2),
        ActivityZoneTime("run1", "pace", 2, 1800, 2.6),
        ActivityZoneTime("run1", "pace", 3, 500, 3.0),
        ActivityZoneTime("ride1", "power", 1, 200, 0),
        ActivityZoneTime("ride1", "power", 2, 800, 120),
        ActivityZoneTime("ride1", "power", 3, 1600, 160),
        ActivityZoneTime("ride1", "power", 4, 700, 200),
        ActivityZoneTime("ride1", "power", 5, 200, 240),
        ActivityZoneTime("ride1", "power", 6, 80, 280),
        ActivityZoneTime("ride1", "power", 7, 20, 320),
        ActivityZoneTime("unzoned2", "hr", 1, 300, 110),
        ActivityZoneTime("unzoned2", "hr", 2, 900, 130),
        ActivityZoneTime("unzoned2", "hr", 3, 800, 145),
        ActivityZoneTime("unzoned2", "hr", 4, 300, 160),
        ActivityZoneTime("unzoned2", "hr", 5, 100, 175),
        ZoneSet("hr", "running", today - timedelta(days=30), (100, 125, 145, 160, 175, 190)),
        PlannedItem("plan-easy", today, "workout", title="Easy run", sport="running"),
        PlannedItem(
            "commit-1",
            today + timedelta(days=1),
            "commitment",
            title="Team meeting",
            sport=None,
        ),
    ]
    for age in range(14):
        morning = today - timedelta(days=age)
        start = _local(morning - timedelta(days=1), 23, 15, tz)
        end = _local(morning, 6, 45, tz)
        records.append(
            SleepSession(
                f"night{age}",
                morning,
                start_utc=start,
                end_utc=end,
                duration_s=27000,
                deep_s=5400,
                light_s=13500,
                rem_s=6300,
                awake_s=1800,
                score=78 + (age % 5),
            )
        )
        records.append(DailyMetric(morning, "sleep.duration", 27000, "s"))
        records.append(DailyMetric(morning, "sleep.score", 80, "score"))
        records.append(DailyMetric(morning, "hrv.night_avg", 55, "ms"))
        weigh_at = _local(morning, 7, 0, tz)
        records.append(BodyMeasurement(f"weight{age}", weigh_at, "body.weight", 72.4, "kg"))
        records.append(DailyMetric(morning, "body.weight", 72.4, "kg"))
    _put(rt, *records, key="week")
    _icu_plan(rt, today)
    from vibesynergy.portal import share

    share.create(rt.engine, "Synthetic coach", ["dashboard", "activities"])
    # Gear: shoes in the replacement range, a retired pair and a bike.
    rt.raw.put(
        source="strava",
        dataset="gear",
        item_key="current",
        payload=json.dumps(
            {
                "g1": {"name": "Synthetic trail shoes", "brand_name": "Syn", "distance": 760_000},
                "g2": {"name": "Synthetic old shoes", "distance": 900_000, "retired": True},
                "b1": {"name": "Synthetic road bike", "model_name": "R1", "distance": 4_000_000},
            }
        ).encode(),
        content_type="application/json",
    )
    unify.rebuild(rt.engine)
    samples = 90
    rt.parquet.write_stream(
        Stream(
            "synthetic-run-1",
            {
                "t": [run_start + timedelta(seconds=i * 30) for i in range(samples)],
                "hr": [132 + (i % 12) for i in range(samples)],
                "speed": [2.7 + (i % 5) * 0.05 for i in range(samples)],
            },
        )
    )
    # The ride's power (VibeSynergy counts time in power zones from it): every zone once.
    rt.parquet.write_stream(
        Stream(
            "synthetic-ride-1",
            {
                "t": [ride_start + timedelta(seconds=i * 10) for i in range(samples * 4)],
                "hr": [125 + (i % 20) for i in range(samples * 4)],
                "power": [(100, 170, 220, 260, 300, 360, 420)[i % 7] for i in range(samples * 4)],
            },
        )
    )
    rt.parquet.write_stream(
        Stream(
            "synthetic-unzoned-2",
            {
                "t": [unzoned_start + timedelta(seconds=i * 30) for i in range(samples)],
                "hr": [128 + (i % 10) for i in range(samples)],
                "speed": [2.5 + (i % 4) * 0.04 for i in range(samples)],
            },
        )
    )
    journal.add_note(
        rt.engine,
        text="Synthetic note: the easy run felt comfortable.",
        start=today,
        activity_id="garmin:run1",
        source="portal",
    )
    journal.set_checkin(
        rt.engine,
        today,
        sleep_quality=4,
        soreness=2,
        mood=4,
        stress=2,
        motivation=4,
    )
    policy = coach.Policy().model_dump()
    policy["language"] = "nl"
    coach.save_draft(rt.engine, "policy", policy, expected_version=0, reason="synthetic review")
    coach.activate(rt.engine, "policy", expected_version=1, reason="synthetic review")
    # A morning note from the connected coach, marked against an older policy, so the
    # dashboard keeps it behind Earlier advice.
    coach_workouts.coach_write(
        rt.engine,
        entry_type="morning",
        summary=ADVICE,
        idempotency_key="a11y-morning",
        for_date=today,
        # A row by the AI coach: shown as the AI coach.
        author="ai_coach",
        meta={"policy_version": 0},
        # The verdict per session: every decision once, so axe checks each chip.
        sessions=[
            {"title": f"Synthetic session {decision}", "decision": decision}
            for decision in coach_workouts.SESSION_DECISIONS
        ],
    )
    with rt.engine.begin() as conn:
        conn.execute(
            update(activity_unified)
            .where(activity_unified.c.group_id == "garmin:run1")
            .values(distance_m=8200)
        )
        conn.execute(
            update(dataset_state)
            .where(dataset_state.c.source == "demo", dataset_state.c.dataset == "profile")
            .values(last_error="connection timed out talking to the synthetic source")
        )
        stale_at = utc_now() - timedelta(days=12)
        conn.execute(
            update(dataset_state)
            .where(dataset_state.c.source == "demo", dataset_state.c.dataset == "daily")
            .values(last_success_at=stale_at, last_attempt_at=stale_at)
        )
    # Zones & thresholds: a plan for cycling and one open proposal.
    from vibesynergy.query import athlete_threshold, thresholds

    for metric, value in (("ftp", 265), ("lthr", 150), ("max_hr", 179)):
        thresholds.set_value(rt.engine, sport="bike", metric=metric, value=value, today=today)
    athlete_threshold.propose_threshold(
        rt.engine, sport="swim", metric="css", value=150, unit="s/100m", method="estimate"
    )
    # Coach → Knowledge, Methods & rules and Change log: one evidence article with a
    # rule, edited once so the log has a diff, an archived article and the routine prompts.
    from vibesynergy.query import coach_prompts, knowledge

    knowledge.upsert_article(
        rt.engine,
        slug="evidence-synthetic-hrv",
        title="Synthetic HRV evidence",
        body_md="Synthetic line one.\nSynthetic line two.",
        topic="hrv-guided-training",
        evidence_grade="B",
        author="client:coach",
        rules=[{"id": "SYN-1", "text": "Synthetic rule: never decide on HRV alone.", "grade": "B"}],
        sources=[{"citation": "Synthetic source, 2020.", "url": "https://example.test/syn"}],
    )
    knowledge.edit_article(
        rt.engine,
        "evidence-synthetic-hrv",
        author="user",
        expected_version=1,
        body_md="Synthetic line one.\nSynthetic line 2, edited.",
    )
    knowledge.upsert_article(
        rt.engine,
        slug="synthetic-old-loop",
        title="Synthetic old working method",
        body_md="No longer used.",
        topic="old-loop",
        status="retired",
        author="client:coach",
    )
    coach_prompts.seed(rt.engine)
    # Yesterday's coach note with long ids in its Details: opening it must not widen the
    # page on a phone.
    coach_workouts.coach_write(
        rt.engine,
        entry_type="note",
        summary="Synthetic note: keep it easy.",
        idempotency_key="a11y-long-details",
        for_date=today - timedelta(days=1),
        author="client:coach",
        meta={"details": "synthetic_identifier_without_spaces_" + "x" * 140},
    )
    return {
        "activity": "garmin:run1",
        "ride": "garmin:ride1",
        "unzoned": "garmin:unzoned2",
        "today": today.isoformat(),
        "advice": ADVICE,
    }


def _routes(activity: str) -> list[str]:
    return [
        "#/dashboard",
        "#/today",
        "#/calendar",
        "#/activities",
        "#/activity/" + activity,
        "#/planning",
        "#/schedule",
        "#/schedule?view=week&date=2026-11-04",
        "#/planning/analysis",
        "#/planning/builder",
        "#/zones",
        "#/zones/form",
        "#/zones/bests",
        "#/recovery",
        "#/injuries",
        "#/sleep",
        "#/body",
        "#/metrics",
        "#/settings/system",
        "#/settings/appearance",
        "#/coach",
        "#/coach/knowledge",
        "#/coach/knowledge/evidence-synthetic-hrv",
        "#/coach/methods",
        "#/coach/memory",
        "#/inbox",
        "#/search?q=synthetic",
        "#/settings/sharing",
        "#/gear",
        "#/coach/tasks",
        "#/coach/log",
        "#/coach/settings",
        "#/settings/data/sources",
        "#/settings/data/sync",
    ]


def _set_language(page, language: str) -> None:
    page.evaluate(
        """async (language) => {
          await fetch('/api/prefs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-VibeSynergy': 'portal'},
            body: JSON.stringify({changes: {language}}),
          });
        }""",
        language,
    )


def _open(page, base: str, route: str) -> None:
    # A hash route changes in the same document: the previous page's h1 is still there
    # until the app swaps the view. Mark it, so the wait below needs the new page's h1
    # (a flake on 5-11-2026: "#/sleep" counted 0 h1 while the view was being swapped).
    if page.url.startswith(base):
        page.evaluate(
            "(route) => location.hash !== route && document.querySelectorAll('#view h1')"
            ".forEach((h) => { h.dataset.a11yOld = '1'; })",
            route,
        )
    page.goto(base + "/" + route, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        """() => {
          // Every route has exactly one h1 (checked below). Waiting for an h2 as well let
          // the check run on the previous page under load (2 Nov 2026).
          const view = document.querySelector('#view');
          const heading = view && view.querySelector('h1');
          if (!heading || heading.dataset.a11yOld) return false;
          const text = view.innerText || '';
          return !/Loading…|Laden…|Beoordelingsstatus laden|lezen…|Reading /.test(text);
        }""",
        timeout=20000,
    )
    page.evaluate(
        """() => new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    )


def _axe(page) -> list[dict]:
    raw = page.evaluate(
        """async (tags) => {
          const result = await axe.run(document, {runOnly: {type: 'tag', values: tags}});
          return result.violations.map((v) => ({
            id: v.id,
            impact: v.impact,
            help: v.help,
            count: v.nodes.length,
            nodes: v.nodes.slice(0, 4).map((n) => ({
              target: n.target,
              html: (n.html || '').slice(0, 180),
              summary: (n.failureSummary || '').slice(0, 280),
            })),
          }));
        }""",
        AXE_TAGS,
    )
    return raw


def _structure(page) -> dict:
    return page.evaluate(
        """() => {
          const levels = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
            .filter((h) => h.offsetParent !== null || h.getClientRects().length)
            .map((h) => Number(h.tagName.slice(1)));
          const jumps = [];
          let prev = 0;
          for (const level of levels) {
            if (prev && level > prev + 1) jumps.push([prev, level]);
            prev = level;
          }
          const doc = document.documentElement;
          const x = window.scrollX;
          window.scrollTo(20, 0);
          const canScroll = window.scrollX;
          window.scrollTo(x, 0);
          return {
            lang: doc.lang,
            h1: document.querySelectorAll('h1').length,
            jumps,
            canScroll,
          };
        }"""
    )


def _focus_ring(page) -> dict:
    return page.evaluate(
        """() => {
          const style = getComputedStyle(document.activeElement);
          return {
            tag: document.activeElement.tagName,
            name: document.activeElement.getAttribute('aria-label')
              || document.activeElement.textContent.slice(0, 40),
            style: style.outlineStyle,
            width: style.outlineWidth,
          };
        }"""
    )


def _tab_until(page, expression: str, arg=None, steps: int = 60) -> bool:
    for _ in range(steps):
        page.keyboard.press("Tab")
        hit = page.evaluate(expression) if arg is None else page.evaluate(expression, arg)
        if hit:
            return True
    return False


def _obscured(page, steps: int = 25) -> list[str]:
    covered = []
    for _ in range(steps):
        page.keyboard.press("Tab")
        hit = page.evaluate(
            """() => {
              const el = document.activeElement;
              if (!el || el === document.body) return null;
              if (el.closest('header') || el.classList.contains('skip-main')) return null;
              const header = document.querySelector('header');
              const rect = el.getBoundingClientRect();
              const limit = header ? header.getBoundingClientRect().bottom : 0;
              const name = (el.getAttribute('aria-label') || el.innerText || el.name || el.tagName)
                .trim().slice(0, 60);
              if (rect.height > 0 && rect.bottom <= limit + 1) return name;
              return null;
            }"""
        )
        if hit:
            covered.append(hit)
    return covered


def _require_tools() -> str:
    chrome = _chrome()
    if shutil.which("npm") is None or chrome is None:
        if os.environ.get("VIBESYNERGY_A11Y_REQUIRED"):
            pytest.fail("Portal accessibility check needs npm and Google Chrome")
        pytest.skip("npm or Google Chrome is unavailable")
    pytest.importorskip("playwright.sync_api")
    return chrome


def test_populated_portal_accessibility(tmp_path, monkeypatch) -> None:
    global TIMEZONE
    chrome = _require_tools()
    from playwright.sync_api import sync_playwright

    axe = _axe_script()
    port = _port()
    TIMEZONE = midday_timezone()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[user]\ntimezone = "{TIMEZONE}"\nunits = "metric"\n'
        f"[portal]\nport = {port}\n[sources.demo]\nenabled = true\n"
    )
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings())
    rt.migrate()
    server = None
    thread = None
    findings: list[dict] = []
    try:
        seeded = populate(rt)
        from vibesynergy.query import agent_events

        agent_events.save_subscription(
            rt,
            {"name": "Coach na training", "auth": "claude_routine", "url": "trig_01SYNTHETIC"},
            secret="synthetic-routine-token",
        )
        routes = _routes(seeded["activity"])
        server = uvicorn.Server(
            uvicorn.Config(create_portal_app(rt), host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        _ready(base)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            _scan(browser, base, routes, axe, findings)
            _states(browser, base, axe, findings)
            _checkin_reflow(browser, base, axe, findings)
            _tasks(browser, base, seeded, findings)
            _coach_tabs(browser, base, findings)
            _calendar_keyboard(browser, base, findings)
            _offline_page(browser, base, findings)
            _quick_add_keyboard(browser, base, axe, findings)
            _search_keyboard(browser, base, findings)
            _bests_keyboard(browser, base, axe, findings)
            _share_keyboard(browser, base, findings)
            _zones_keyboard(browser, base, findings)
            _gear_keyboard(browser, base, findings)
            _zone_contract(browser, base, seeded, findings)
            browser.close()
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=5)
        rt.close()
    assert not findings, json.dumps(findings, indent=2, ensure_ascii=False)


def _context(browser, axe: str, language: str, theme: str, width: int, height: int):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=theme,
        locale="nl-NL" if language == "nl" else "en-GB",
        timezone_id=TIMEZONE,
    )
    context.add_init_script(axe)
    context.add_init_script(f"localStorage.setItem('vibesynergy-theme', {json.dumps(theme)})")
    return context


def _scan(browser, base: str, routes: list[str], axe: str, findings: list[dict]) -> None:
    for language, theme, (width, height) in COMBOS:
        context = _context(browser, axe, language, theme, width, height)
        page = context.new_page()
        _open(page, base, "#/dashboard")
        _set_language(page, language)
        page.reload(wait_until="domcontentloaded")
        for route in routes:
            label = f"{language} {theme} {width} {route}"
            try:
                _open(page, base, route)
            except Exception as exc:
                findings.append({"where": label, "error": str(exc)})
                continue
            violations = _axe(page)
            structure = _structure(page)
            if violations:
                extra = {}
                if any(item["id"] == "scrollable-region-focusable" for item in violations):
                    extra["tables"] = page.evaluate(
                        """() => [...document.querySelectorAll('.table-wrap')].map((wrap) => ({
                          w: wrap.scrollWidth,
                          c: wrap.clientWidth,
                          sh: wrap.scrollHeight,
                          ch: wrap.clientHeight,
                          tab: wrap.getAttribute('tabindex'),
                          oy: getComputedStyle(wrap).overflowY,
                          text: (wrap.closest('section')?.querySelector('h2')?.textContent || '')
                            .slice(0, 40),
                        }))"""
                    )
                findings.append({"where": label, "axe": violations, **extra})
            if structure["lang"] != language:
                findings.append({"where": label, "lang": structure["lang"]})
            if structure["h1"] != 1:
                findings.append({"where": label, "h1": structure["h1"]})
            if structure["jumps"]:
                findings.append({"where": label, "heading_jumps": structure["jumps"]})
            if structure["canScroll"] > 0:
                findings.append({"where": label, "horizontal_scroll": structure["canScroll"]})
        context.close()


def _states(browser, base: str, axe: str, findings: list[dict]) -> None:
    context = _context(browser, axe, "nl", "light", 1280, 800)
    context.add_init_script(
        """(() => {
          const orig = window.fetch.bind(window);
          window.fetch = (url, opts) => String(url).includes("ai/reports")
            ? new Promise((resolve) => setTimeout(() => resolve(orig(url, opts)), 15000))
            : orig(url, opts);
        })();"""
    )
    page = context.new_page()
    _open(page, base, "#/settings/system")
    _set_language(page, "nl")
    # A hash change does not reload, so the catalogue stays on the previous language
    # until the document loads again.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.documentElement.lang === 'nl' && document.querySelector('#view h1')"
    )
    page.evaluate("() => { location.hash = '#/dashboard'; }")
    try:
        page.get_by_text("Laden…").first.wait_for(timeout=8000)
    except Exception:
        findings.append(
            {
                "where": "loading dashboard",
                "lang": page.evaluate("() => document.documentElement.lang"),
                "text": page.locator("#view").inner_text()[:800],
            }
        )
    else:
        loading = _axe(page)
        if loading:
            findings.append({"where": "nl light loading dashboard", "axe": loading})
    _open(page, base, "#/activities?text=zzz-no-such-workout")
    if "Geen activiteiten die aan deze filters voldoen." not in page.locator("#view").inner_text():
        findings.append({"where": "empty activities", "missing": "empty explanation"})
    empty = _axe(page)
    if empty:
        findings.append({"where": "nl light empty activities", "axe": empty})
    _open(page, base, "#/settings/data/sync")
    # The status panel fills in after the page heading, so give it a moment (seen racing on CI).
    for _ in range(50):
        text = page.locator("#view").inner_text()
        if "konden niet worden bijgewerkt" in text or "couldn't be updated" in text:
            break
        page.wait_for_timeout(100)
    if "konden niet worden bijgewerkt" not in text and "couldn't be updated" not in text:
        findings.append({"where": "sync error", "missing": "actionable sync failure"})
    _agent_link_axe(page, base, findings)
    # The feed names the AI coach.
    _open(page, base, "#/coach")
    page.locator(".coach-entry").first.wait_for(timeout=8000)
    feed = page.locator("[data-coach-feed]").inner_text()
    if "AI-coach" not in feed:
        findings.append({"where": "coach feed author", "text": feed[:400]})
    context.close()


def _show_dashboard(page, base: str, language: str) -> None:
    """Apply a language preference and load the dashboard in that language.

    Opening the same hash again does not reload, so the preference is applied with a
    reload before the dashboard is read.
    """
    if not page.url.startswith(base):
        _open(page, base, "#/dashboard")
    _set_language(page, language)
    page.reload(wait_until="domcontentloaded")
    _open(page, base, "#/dashboard")
    page.wait_for_function(
        "(language) => document.documentElement.lang === language",
        arg=language,
    )


def _dashboard_copy(page, base: str, language: str, findings: list[dict]) -> str:
    """Visible dashboard guidance and the earlier-advice disclosure for one language."""
    _show_dashboard(page, base, language)
    view = page.locator("#view").inner_text()
    expected = DASHBOARD_COPY[language]
    if expected["advice"] not in view:
        findings.append(
            {
                "where": f"dashboard advice {language}",
                "missing": expected["advice"],
                "saw": view[:500],
            }
        )
    # The note about outdated advice sits inside the closed "Earlier advice" disclosure.
    full = page.locator("#view").text_content() or ""
    if expected["earlier"] not in full or expected["label"] not in view:
        findings.append(
            {
                "where": f"dashboard earlier advice {language}",
                "missing": expected["label"],
                "saw": view[:800],
            }
        )
    return view


def _zone_contract(browser, base: str, seeded: dict, findings: list[dict]) -> None:
    """Zone colours, labels and configured-boundary messaging on Activity and Training."""
    missing_copy = {
        "en": ("Zones not configured", "Set your zones"),
        "nl": ("Zones niet ingesteld", "Stel je zones in"),
    }
    for language, theme in (("en", "light"), ("nl", "dark")):
        context = _context(browser, "void 0;", language, theme, 1280, 800)
        page = context.new_page()
        _open(page, base, "#/activity/" + seeded["activity"])
        _set_language(page, language)
        page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_selector(".zone-badge", timeout=15000)
        except Exception as exc:
            findings.append({"where": f"zone activity {language} {theme}", "error": str(exc)})
            context.close()
            continue
        report = page.evaluate(
            """() => {
              const normalize = (color) => {
                if (!color) return '';
                const raw = color.trim().toLowerCase();
                const hex = raw.match(/^#([0-9a-f]{6})$/);
                if (hex) {
                  const h = hex[1];
                  const r = parseInt(h.slice(0, 2), 16);
                  const g = parseInt(h.slice(2, 4), 16);
                  const b = parseInt(h.slice(4, 6), 16);
                  return `rgb(${r},${g},${b})`;
                }
                const rgb = raw.match(/^rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (rgb) return `rgb(${rgb[1]},${rgb[2]},${rgb[3]})`;
                return raw.replace(/\\s/g, '');
              };
              const swatches = [...document.querySelectorAll('.zone-swatch')];
              const tokens = swatches.map((el) =>
                (el.style.background || '').match(/var\\(--([^)]+)\\)/)?.[1]
              );
              const badToken = tokens.some((t) => !t || !t.startsWith('zone-'));
              const codes = [...document.querySelectorAll('.zone-code')]
                .map((el) => el.textContent);
              const missing = swatches.length > 0 && codes.length < swatches.length;
              const paceZero = codes.includes('Z0');
              const streamKey = document.querySelector('.stream-zone-key');
              const streamUsesZone = streamKey
                && streamKey.innerHTML.includes('var(--zone-');
              const badgeColors = [...document.querySelectorAll('.zone-swatch')]
                .slice(0, 8)
                .map((el) => normalize(getComputedStyle(el).backgroundColor))
                .filter(Boolean);
              const legendColors = [...document.querySelectorAll('.stream-zone-key .zone-swatch')]
                .map((el) => normalize(getComputedStyle(el).backgroundColor))
                .filter(Boolean);
              const streamStrokeColors = [...document.querySelectorAll('.streams path')]
                .map((path) => normalize(path.getAttribute('stroke') || ''))
                .filter((color) => color && color !== 'none');
              const zoneStrokeColors = [...new Set(
                streamStrokeColors.filter((color) => legendColors.includes(color))
              )];
              const streamUsesPalette = legendColors.length > 0
                && zoneStrokeColors.length >= 2
                && legendColors.every((color) => streamStrokeColors.includes(color));
              return {
                badges: swatches.length,
                badToken,
                missingCodes: missing,
                paceZero,
                streamUsesZone: Boolean(streamUsesZone),
                streamUsesPalette,
                streamVisibleSeries: zoneStrokeColors.length,
              };
            }"""
        )
        if report["badges"] < 5 or report["missingCodes"] or report["badToken"]:
            findings.append({"where": f"zone activity {language} {theme}", "zone": report})
        if not report["paceZero"]:
            findings.append(
                {"where": f"zone activity pace zero {language} {theme}", "zone": report}
            )
        if not report["streamUsesZone"]:
            findings.append(
                {"where": f"zone activity stream legend {language} {theme}", "zone": report}
            )
        if report["streamVisibleSeries"] < 2 or not report["streamUsesPalette"]:
            findings.append(
                {"where": f"zone activity stream colours {language} {theme}", "zone": report}
            )
        _open(page, base, "#/activity/" + seeded["ride"])
        page.wait_for_selector(".zone-badge", timeout=15000)
        power = page.evaluate(
            """() => {
              const tokens = new Set(
                [...document.querySelectorAll('.zone-swatch')]
                  .map((el) => (el.style.background || '').match(/var\\(--([^)]+)\\)/)?.[1])
                  .filter(Boolean)
              );
              return { count: tokens.size, tokens: [...tokens] };
            }"""
        )
        if power["count"] < 7:
            findings.append({"where": f"zone power ride {language} {theme}", "power": power})
        _open(page, base, "#/activity/" + seeded["unzoned"])
        page.wait_for_selector(".zone-missing", timeout=15000)
        unzoned = page.evaluate(
            """() => {
              const blocks = [...document.querySelectorAll('.zone-missing')];
              const text = blocks.map((el) => el.innerText).join(' ');
              const links = blocks.map((el) => el.querySelector('a')?.getAttribute('href') || '');
              const hrBadges = [...document.querySelectorAll('.zone-badge .zone-code')]
                .map((el) => el.textContent);
              return {
                count: blocks.length,
                text,
                links,
                hrBadges: hrBadges.length,
              };
            }"""
        )
        title, sources_label = missing_copy[language]
        if unzoned["count"] < 1 or title not in unzoned["text"]:
            findings.append({"where": f"zone unzoned pace {language} {theme}", "unzoned": unzoned})
        if not any(href == "#/zones" for href in unzoned["links"]):
            findings.append(
                {"where": f"zone unzoned zones link {language} {theme}", "unzoned": unzoned}
            )
        if sources_label not in unzoned["text"]:
            findings.append(
                {"where": f"zone unzoned sources label {language} {theme}", "unzoned": unzoned}
            )
        if unzoned["hrBadges"] < 3:
            findings.append(
                {"where": f"zone unzoned hr labels {language} {theme}", "unzoned": unzoned}
            )
        _open(page, base, "#/training")
        page.wait_for_selector("[data-zone-kind]", timeout=15000)
        if not _tab_until(
            page,
            "() => document.activeElement?.matches('[data-zone-kind]')",
        ):
            findings.append(
                {"where": f"zone training keyboard focus {language} {theme}", "missing": "kind"}
            )
        else:
            before = page.evaluate(
                """() => ({
                  kind: document.querySelector('[data-zone-kind]').value,
                  heading: document.querySelector('[data-zone-heading]')?.textContent || '',
                })"""
            )
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(250)
            after = page.evaluate(
                """() => ({
                  kind: document.querySelector('[data-zone-kind]').value,
                  heading: document.querySelector('[data-zone-heading]')?.textContent || '',
                  panelVisible: !document.querySelector(
                    `[data-zone-panel="${document.querySelector('[data-zone-kind]').value}"]`
                  )?.hidden,
                })"""
            )
            if after["kind"] == before["kind"] or not after["panelVisible"]:
                findings.append(
                    {
                        "where": f"zone training keyboard switch {language} {theme}",
                        "before": before,
                        "after": after,
                    }
                )
        page.select_option("[data-zone-kind]", "power")
        try:
            page.wait_for_function(
                """() => {
                  const el = document.querySelector('[data-zone-panel="power"] .zone-week');
                  return el && el.dataset.ready === '1';
                }""",
                timeout=15000,
            )
        except Exception as exc:
            findings.append(
                {"where": f"zone training chart ready {language} {theme}", "error": str(exc)}
            )
        training = page.evaluate(
            """() => {
              const normalize = (color) => {
                if (!color) return '';
                const raw = color.trim().toLowerCase();
                const hex = raw.match(/^#([0-9a-f]{6})$/);
                if (hex) {
                  const h = hex[1];
                  const r = parseInt(h.slice(0, 2), 16);
                  const g = parseInt(h.slice(2, 4), 16);
                  const b = parseInt(h.slice(4, 6), 16);
                  return `rgb(${r},${g},${b})`;
                }
                const rgb = raw.match(/^rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (rgb) return `rgb(${rgb[1]},${rgb[2]},${rgb[3]})`;
                return raw.replace(/\\s/g, '');
              };
              const badges = [...document.querySelectorAll(
                '[data-zone-panel="power"] .zone-badge .zone-swatch'
              )];
              const badgeTokens = badges.map((el) =>
                (el.style.background || '').match(/var\\(--([^)]+)\\)/)?.[1]
              ).filter(Boolean);
              const chart = document.querySelector('[data-zone-panel="power"] .zone-week');
              const chartColors = chart
                ? [...chart.querySelectorAll('path')]
                  .map((p) => normalize(p.getAttribute('fill')))
                  .filter(Boolean)
                : [];
              const distinctChart = new Set(chartColors);
              const badgeColors = badges.map((el) =>
                normalize(getComputedStyle(el).backgroundColor)
              ).filter(Boolean);
              const expected = [...new Set(badgeColors)];
              const matched = [...distinctChart].filter((fill) => expected.includes(fill));
              const chartUsesZoneTokens = matched.length >= Math.min(7, expected.length);
              const noteEl = document.querySelector('[data-zones] .sub:not(.card-range)');
              const note = noteEl?.textContent || '';
              return {
                badgeTokens: [...new Set(badgeTokens)],
                chartColors: distinctChart.size,
                chartUsesZoneTokens,
                distinctNote: /heart rate|pace|power|hartslag|tempo|vermogen/i.test(note),
              };
            }"""
        )
        min_badges = 7
        min_chart = min(min_badges, len(training["badgeTokens"]))
        if len(training["badgeTokens"]) < min_badges or not training["distinctNote"]:
            findings.append(
                {"where": f"zone training power {language} {theme}", "training": training}
            )
        if training["chartColors"] < min_chart or not training["chartUsesZoneTokens"]:
            findings.append(
                {"where": f"zone training chart colours {language} {theme}", "training": training}
            )
        context.close()


def _checkin_reflow(browser, base: str, axe: str, findings: list[dict]) -> None:
    """The open start-page check-in at 320px: no horizontal scroll, 44px dots, by keyboard.

    The seeded check-in is answered, so the route scan only sees the compact summary; open
    the rows with the keyboard the way an athlete edits today's answers.
    """
    for language in ("nl", "en"):
        where = f"{language} 320 check-in"
        context = _context(browser, axe, language, "light", 320, 640)
        page = context.new_page()
        _open(page, base, "#/dashboard")
        _set_language(page, language)
        page.reload(wait_until="domcontentloaded")
        _open(page, base, "#/dashboard")
        if not _tab_until(
            page, "() => document.activeElement?.hasAttribute('data-edit-checkin')", steps=80
        ):
            findings.append({"where": where, "missing": "edit check-in button by keyboard"})
            context.close()
            continue
        page.keyboard.press("Enter")
        for _ in range(50):
            if page.evaluate("() => document.querySelectorAll('.dash-checkin .ql-q').length"):
                break
            time.sleep(0.1)
        layout = page.evaluate(
            """() => ({
              scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
              rows: [...document.querySelectorAll('.dash-checkin .ql-q')].map((row) => {
                const dots = [...row.querySelectorAll('.ql-dot')]
                  .map((dot) => dot.getBoundingClientRect());
                return {
                  label: row.querySelector('.ql-label').textContent,
                  smallest: Math.min(...dots.map((d) => Math.min(d.width, d.height))),
                  right: Math.max(...dots.map((d) => d.right)),
                };
              }),
            })"""
        )
        if not layout["rows"]:
            findings.append({"where": where, "missing": "check-in rows after edit"})
        if layout["scroll"] > 0:
            findings.append({"where": where, "horizontal_scroll": layout["scroll"]})
        for row in layout["rows"]:
            if row["smallest"] < 44 or row["right"] > 320:
                findings.append({"where": where, "row": row})
        if not _tab_until(page, "() => document.activeElement?.classList.contains('ql-dot')"):
            findings.append({"where": where, "missing": "first dot by keyboard"})
        else:
            ring = _focus_ring(page)
            box = page.evaluate("() => document.activeElement.getBoundingClientRect().right")
            if ring["style"] == "none" or float(ring["width"].rstrip("px")) < 3 or box > 320:
                findings.append({"where": f"{where} dot focus", "ring": ring, "right": box})
        violations = _axe(page)
        if violations:
            findings.append({"where": where, "axe": violations})
        context.close()


def _tasks(browser, base: str, seeded: dict, findings: list[dict]) -> None:
    context = _context(browser, "void 0", "nl", "light", 1280, 800)
    page = context.new_page()
    view = _dashboard_copy(page, base, "nl", findings)
    _dashboard_copy(page, base, "en", findings)
    _show_dashboard(page, base, "nl")
    year, month, day = seeded["today"].split("-")
    numeric_date = f"{int(day)}-{int(month)}-{year}"
    if numeric_date not in view:
        findings.append({"where": "dashboard date", "missing": numeric_date})
    page.keyboard.press("Tab")
    skip = page.locator(".skip-main")
    if page.evaluate("() => document.activeElement.className") != "skip-main":
        findings.append({"where": "keyboard", "focus": "skip link was not first"})
    else:
        box = skip.bounding_box()
        ring = _focus_ring(page)
        ring_px = float(ring["width"].rstrip("px"))
        if not box or box["y"] < 0 or ring["style"] == "none" or ring_px < 3:
            findings.append({"where": "skip focus", "ring": ring, "box": box})
        page.keyboard.press("Enter")
        if page.evaluate("() => document.activeElement.id") != "view":
            findings.append({"where": "skip", "focus": "main was not focused"})
    covered = _obscured(page)
    if covered:
        findings.append({"where": "dashboard focus obscured", "controls": covered})
    _open(page, base, "#/dashboard")
    # A link, not <body>: past the last control the page body has focus, and its text holds
    # every name on the (longer) start page.
    if not _tab_until(
        page,
        """(name) => document.activeElement?.tagName === 'A'
          && (document.activeElement.textContent || '').includes(name)""",
        RUN_NAME,
        steps=120,
    ):
        findings.append({"where": "keyboard activity", "missing": RUN_NAME})
    else:
        page.keyboard.press("Enter")
        try:
            page.wait_for_function(
                """(name) => document.querySelector('#view h1')?.textContent === name""",
                arg=RUN_NAME,
                timeout=15000,
            )
        except Exception as exc:
            findings.append({"where": "open activity", "error": str(exc)})
        else:
            if not _tab_until(
                page,
                """() => document.activeElement?.tagName === 'SUMMARY'
                  && (document.activeElement.textContent || '').includes('Details')""",
            ):
                findings.append({"where": "activity evidence", "missing": "Details summary"})
            else:
                page.keyboard.press("Enter")
                opened = page.evaluate(
                    """() => {
                      const el = document.activeElement.closest('details');
                      return Boolean(el && el.open);
                    }"""
                )
                if not opened:
                    findings.append(
                        {"where": "activity evidence", "missing": "Details did not open"}
                    )
            covered = _obscured(page, steps=12)
            if covered:
                findings.append({"where": "activity focus obscured", "controls": covered})
    _open(page, base, "#/coach/memory")
    if not _tab_until(page, "() => document.activeElement?.name === 'tone'"):
        findings.append({"where": "coach setting", "missing": "feedback style control"})
    else:
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Tab")
        try:
            page.get_by_text("Opgeslagen.", exact=True).first.wait_for(timeout=8000)
        except Exception as exc:
            findings.append({"where": "coach setting", "error": str(exc)})
    _agent_link_events(page, base, findings)
    _open(page, base, "#/settings/calendar")
    if not _tab_until(
        page,
        """() => document.activeElement?.type === 'submit'
          && (document.activeElement.textContent || '').includes('Verbinden')""",
    ):
        findings.append({"where": "calendar error summary", "missing": "Connect button"})
    else:
        page.keyboard.press("Enter")
        try:
            page.locator("#caldav-error").wait_for(timeout=8000)
        except Exception as exc:
            findings.append({"where": "calendar error summary", "error": str(exc)})
        else:
            focused = page.evaluate("() => document.activeElement.id")
            if focused != "caldav-error":
                findings.append({"where": "calendar error summary", "focus": focused})
            described = page.locator("#caldav-username").get_attribute("aria-describedby") or ""
            if "caldav-error" not in described:
                findings.append({"where": "calendar field error", "describedby": described})
    context.close()


def _share_keyboard(browser, base: str, findings: list[dict]) -> None:
    """an open link changes to Everything by keyboard, saved in place."""
    context = _context(browser, "void 0", "nl", "light", 390, 844)
    page = context.new_page()
    _open(page, base, "#/settings/sharing")
    if not _tab_until(
        page,
        "() => Boolean(document.activeElement?.closest('[data-share-edit] summary'))",
        steps=120,
    ):
        findings.append({"where": "share edit", "missing": "not reached by Tab"})
        context.close()
        return
    page.keyboard.press("Enter")
    if not _tab_until(
        page, "() => (document.activeElement?.name || '').endsWith('-all')", steps=10
    ):
        findings.append({"where": "share edit", "missing": "Everything not reached by Tab"})
    else:
        page.keyboard.press("Space")
        try:
            page.locator("[data-share-edit] [data-share-status]").get_by_text(
                "Opgeslagen."
            ).wait_for(timeout=8000)
        except Exception as exc:
            findings.append({"where": "share edit save", "error": str(exc)})
        locked = page.evaluate(
            "() => [...document.querySelectorAll('[data-share-edit] .share-pages input')]"
            ".every((box) => box.disabled && box.checked)"
        )
        if not locked:
            findings.append({"where": "share edit", "problem": "single pages not locked"})


def _gear_keyboard(browser, base: str, findings: list[dict]) -> None:
    """an item's settings open and save by keyboard alone."""
    context = _context(browser, "void 0", "nl", "light", 390, 844)
    page = context.new_page()
    _open(page, base, "#/gear")
    if not _tab_until(
        page, "() => Boolean(document.activeElement?.closest('.gear-item summary'))", steps=80
    ):
        findings.append({"where": "gear", "missing": "item settings not reached by Tab"})
        context.close()
        return
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    if page.evaluate("() => document.activeElement?.name") != "status":
        findings.append({"where": "gear", "focus": "status not next after the summary"})
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Tab")
    try:
        page.locator(".gear-item [data-gear-status]").get_by_text("Opgeslagen.").first.wait_for(
            timeout=8000
        )
    except Exception as exc:
        findings.append({"where": "gear save", "error": str(exc)})
    context.close()


def _zones_keyboard(browser, base: str, findings: list[dict]) -> None:
    """a sport tile shows its values closed and opens by keyboard to change them."""
    context = _context(browser, "void 0", "nl", "light", 390, 844)
    page = context.new_page()
    _open(page, base, "#/zones")
    if page.locator("[data-sport-card] .zones-summary").count() == 0:
        findings.append({"where": "zones", "missing": "summary line on the tiles"})
    if not _tab_until(
        page, "() => Boolean(document.activeElement?.closest('.zones-more > summary'))", steps=80
    ):
        findings.append({"where": "zones", "missing": "tile not reached by Tab"})
    else:
        page.keyboard.press("Enter")
        if not _tab_until(
            page, "() => document.activeElement?.hasAttribute('data-edit')", steps=15
        ):
            findings.append({"where": "zones", "missing": "Change not reached in the open tile"})
    if "Zwift" in page.locator("#view").inner_text():
        findings.append({"where": "zones", "problem": "Zwift notice still shown"})
    context.close()


def _search_keyboard(browser, base: str, findings: list[dict]) -> None:
    """ "/" opens Search with the field focused; Enter shows the results."""
    context = _context(browser, "void 0", "nl", "light", 390, 844)
    page = context.new_page()
    _open(page, base, "#/dashboard")
    page.locator("body").press("/")
    try:
        page.wait_for_url("**#/search", timeout=8000)
        page.locator("#search-q").wait_for(timeout=8000)
    except Exception as exc:
        findings.append({"where": "search", "error": str(exc)})
        context.close()
        return
    if page.evaluate("() => document.activeElement?.id") != "search-q":
        findings.append({"where": "search", "focus": "not in the field"})
    page.keyboard.type("Synthetic")
    page.keyboard.press("Enter")
    try:
        page.locator("[data-search-results] section, [data-search-results] .empty").first.wait_for(
            timeout=8000
        )
    except Exception as exc:
        findings.append({"where": "search results", "error": str(exc)})


def _bests_keyboard(browser, base: str, axe: str, findings: list[dict]) -> None:
    """the best-efforts block switches sport by keyboard and stays accessible."""
    context = _context(browser, axe, "nl", "dark", 390, 844)
    page = context.new_page()
    _open(page, base, "#/dashboard")
    sport = page.locator("[data-bests-sport]")
    if sport.count() == 0:
        findings.append({"where": "bests", "missing": "sport choice"})
        context.close()
        return
    sport.focus()
    page.keyboard.press("ArrowDown")  # Running -> Cycling
    try:
        page.locator("[data-bests-table] table, [data-bests-table] .empty").first.wait_for(
            timeout=15000
        )
    except Exception as exc:
        findings.append({"where": "bests bike", "error": str(exc)})
    violations = _axe(page)
    if violations:
        findings.append({"where": "bests bike", "axe": violations})
    context.close()


def _quick_add_keyboard(browser, base: str, axe: str, findings: list[dict]) -> None:
    """+ opens the quick log in a dialog by keyboard; Escape closes it, focus returns."""
    for width in (1280, 390):
        context = _context(browser, axe, "nl", "light", width, 844)
        page = context.new_page()
        _open(page, base, "#/activities")
        button = page.locator(".quick-add")
        button.focus()
        page.keyboard.press("Enter")
        try:
            body = ".quick-dialog[open] [data-quick-body]"
            page.locator(f"{body} form, {body} button").first.wait_for(timeout=8000)
        except Exception as exc:
            findings.append({"where": f"quick add {width}", "error": str(exc)})
            context.close()
            continue
        inside = page.evaluate("() => Boolean(document.activeElement?.closest('.quick-dialog'))")
        if not inside:
            findings.append({"where": f"quick add {width}", "focus": "not in the dialog"})
        violations = _axe(page)
        if violations:
            findings.append({"where": f"quick add {width}", "axe": violations})
        page.keyboard.press("Escape")
        back = page.evaluate("() => document.activeElement?.classList.contains('quick-add')")
        if page.locator(".quick-dialog[open]").count() or not back:
            findings.append(
                {"where": f"quick add {width}", "close": "Escape or focus return failed"}
            )
        context.close()


def _offline_page(browser, base: str, findings: list[dict]) -> None:
    """with the connection gone, a page shows VibeSynergy's offline notice."""
    context = _context(browser, "void 0", "nl", "light", 390, 844)
    page = context.new_page()
    _open(page, base, "#/dashboard")
    try:
        page.evaluate(
            "() => navigator.serviceWorker.ready.then(() => new Promise((r) => setTimeout(r, 300)))"
        )
        context.set_offline(True)
        page.goto(base + "/", wait_until="domcontentloaded")
        page.get_by_text("VibeSynergy is niet bereikbaar").wait_for(timeout=8000)
    except Exception as exc:
        findings.append({"where": "offline page", "error": str(exc)})
    finally:
        context.set_offline(False)
        context.close()


def _calendar_keyboard(browser, base: str, findings: list[dict]) -> None:
    """arrow keys move the chosen day; the day panel follows."""
    context = _context(browser, "void 0", "nl", "light", 1280, 900)
    page = context.new_page()
    _open(page, base, "#/schedule?view=month&date=2026-11-04")
    page.locator("[data-sched-day] .sched-panel").wait_for(timeout=8000)
    page.locator(".sched-cell[aria-pressed='true']").focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowDown")
    state = page.evaluate(
        """() => ({focus: document.activeElement.dataset.day,
          pressed: document.querySelector(".sched-cell[aria-pressed='true']")?.dataset.day,
          panel: document.querySelector("#sched-day-title")?.textContent || ""})"""
    )
    if (
        state["focus"] != "2026-11-12"
        or state["pressed"] != "2026-11-12"
        or "12" not in state["panel"]
    ):
        findings.append({"where": "calendar keys", "state": state})
    context.close()


def _coach_tabs(browser, base: str, findings: list[dict]) -> None:
    """Details on a phone, the change log by keyboard, an in-place rule edit."""
    context = _context(browser, "void 0", "nl", "light", 390, 844)
    page = context.new_page()
    _open(page, base, "#/coach")
    page.locator(".coach-entry details > summary").first.wait_for(timeout=8000)
    summary = page.locator(".coach-entry details > summary").last
    summary.scroll_into_view_if_needed()
    page.evaluate("() => window.scrollBy(0, -200)")
    before = page.evaluate("() => window.scrollY")
    summary.focus()
    page.keyboard.press("Enter")
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 200)))")
    after = page.evaluate(
        """() => ({y: window.scrollY, wide: document.documentElement.scrollWidth
          > document.documentElement.clientWidth, open: document.activeElement.parentElement.open,
          focus: document.activeElement.tagName})"""
    )
    if after["wide"] or abs(after["y"] - before) > 2 or after["focus"] != "SUMMARY":
        findings.append({"where": "coach details on a phone", "before": before, "after": after})
    if not after["open"]:
        findings.append({"where": "coach details on a phone", "missing": "Details did not open"})

    _open(page, base, "#/knowledge")
    if "#/coach/knowledge" not in page.url:
        findings.append({"where": "knowledge redirect", "url": page.url})

    _open(page, base, "#/coach/log")
    if not _tab_until(
        page, "() => Boolean(document.activeElement?.closest('.coach-log summary'))", steps=80
    ):
        findings.append({"where": "change log", "missing": "entry not reached by Tab"})
    else:
        page.keyboard.press("Enter")
        try:
            page.locator(".coach-log details[open] [data-diff] > :not([aria-busy])").first.wait_for(
                timeout=8000
            )
        except Exception as exc:
            findings.append({"where": "change log diff", "error": str(exc)})
        if page.evaluate("() => document.activeElement.tagName") != "SUMMARY":
            findings.append({"where": "change log", "focus": "left the entry"})
    entry = page.locator(
        '.coach-log details[data-target="evidence-synthetic-hrv"][data-latest="1"]'
    ).first
    entry.locator("summary").click()
    try:
        entry.get_by_role("button", name="Deze wijziging ongedaan maken").wait_for(timeout=8000)
        entry.locator(".coach-diff ins").first.wait_for(timeout=8000)
    except Exception as exc:
        findings.append({"where": "change log undo", "error": str(exc)})

    _open(page, base, "#/coach/methods")
    page.locator('summary:has-text("Regels")').first.click()
    box = page.locator('[data-rule="SYN-1"] textarea')
    box.focus()
    page.keyboard.press("End")
    page.keyboard.type(" Synthetic.")
    page.keyboard.press("Tab")
    try:
        page.locator('[data-rule="SYN-1"]').get_by_text("Opgeslagen.").wait_for(timeout=8000)
    except Exception as exc:
        findings.append({"where": "rule edit", "error": str(exc)})
    context.close()


def _agent_link_events(page, base: str, findings: list[dict]) -> None:
    """Keyboard only: open a coach link's settings and choose when it gets a message."""
    _open(page, base, "#/coach/settings")
    page.locator("[data-agent-link]").first.wait_for(timeout=8000)
    # Start the Tab order at the section (a click on its heading moves no control). The click
    # focuses <main> (tabindex -1), so Tab starts at the top of the page: wait for the API
    # coach card above, and allow for its controls (on, time and days per report, and since
    # a key per AI provider and a provider and model per task).
    page.locator("#coach-runner-title").wait_for(timeout=8000)
    page.locator("[data-sync-agents] h2").click()
    if not _tab_until(
        page,
        """() => document.activeElement?.tagName === 'SUMMARY'
          && !!document.activeElement.closest('[data-agent-link]')""",
        steps=120,
    ):
        findings.append({"where": "agent link", "missing": "link settings summary"})
        return
    page.keyboard.press("Enter")
    if not _tab_until(
        page,
        "() => document.activeElement?.value === 'activity.ready'",
        steps=20,
    ):
        findings.append({"where": "agent link", "missing": "event type choice"})
        return
    page.keyboard.press("Space")
    try:
        page.get_by_text("Opgeslagen.", exact=True).first.wait_for(timeout=8000)
    except Exception as exc:
        findings.append({"where": "agent link events", "error": str(exc)})
        return
    state = page.evaluate(
        """() => ({
          focus: document.activeElement?.value,
          open: !!document.activeElement?.closest('details')?.open,
          checked: [...document.querySelectorAll('[name="event_type"]:checked')]
            .map((box) => box.value),
        })"""
    )
    if state != {
        "focus": "activity.ready",
        "open": True,
        "checked": ["activity.published", "activity.ready"],
    }:
        findings.append({"where": "agent link events", "state": state})


def _agent_link_axe(page, base: str, findings: list[dict]) -> None:
    """axe on a coach link with its settings open (the event choice is inside)."""
    _open(page, base, "#/coach/settings")
    page.locator("[data-agent-link] details summary").first.click()
    violations = _axe(page)
    if violations:
        findings.append({"where": "nl light agent link settings open", "axe": violations})
