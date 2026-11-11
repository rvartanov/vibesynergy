"""One search over everything."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import search
from vibesynergy.store.schema import activity_unified, coach_decision_log, note


def _seed(runtime) -> None:
    now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    with runtime.engine.begin() as conn:
        conn.execute(
            activity_unified.insert().values(
                group_id="g:1",
                subject_id=1,
                start_utc=now,
                local_date=date(2026, 11, 2),
                tz_offset_s=3600,
                sport="running",
                name="Rondje Synthetisch 100%",
                primary_source="garmin",
                sources=["garmin:1"],
                updated_at=now,
            )
        )
        conn.execute(
            note.insert().values(
                id="note_1",
                subject_id=1,
                start_date=date(2026, 11, 3),
                end_date=date(2026, 11, 3),
                text="Kuit wat stijf na het Synthetische rondje.",
                tags=[],
                source="portal",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            coach_decision_log.insert().values(
                subject_id=1,
                at=now,
                kind="note",
                author="Claude",
                entry_type="morning",
                for_date=date(2026, 11, 3),
                text="Houd de synthetische duurloop rustig.",
                meta={},
            )
        )


def test_one_search_finds_activities_notes_and_coach_messages(runtime) -> None:
    _seed(runtime)
    out = search.search(runtime, "SYNTHET")
    kinds = {g["kind"]: g["hits"] for g in out["groups"]}
    assert kinds["activities"][0]["link"] == "#/activity/g:1"
    assert kinds["notes"][0]["link"] == "#/dashboard/2026-11-03?section=diary"
    assert kinds["coach"][0]["link"].startswith("#/coach/report/")
    assert search.search(runtime, "s")["groups"] == []  # too short
    # % and _ are letters here, not wildcards.
    assert [g["kind"] for g in search.search(runtime, "100%")["groups"]] == ["activities"]
    assert search.search(runtime, "_")["groups"] == []


def test_search_api(runtime) -> None:
    _seed(runtime)
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = api.get("/api/search", params={"q": "rondje"}).json()["data"]
    assert data["query"] == "rondje" and {g["kind"] for g in data["groups"]} >= {
        "activities",
        "notes",
    }
