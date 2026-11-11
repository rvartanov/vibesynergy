"""The coach booklet: the plan on paper, readable without VibeSynergy."""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_plan_model import EVENTS, _put

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import booklet, plan_model


def test_render_has_every_part_and_escapes() -> None:
    page = booklet.render(
        {
            "name": "Test <plan>",
            "today": "2026-11-07",
            "races": [{"date": "2027-07-03", "title": "Example triathlon", "priority": "A"}],
            "phases": [{"phase": "Base", "start": "2026-11-16", "end": "2027-01-03"}],
            "weeks": [
                {
                    "title": "Wk 3",
                    "start": "2026-11-16",
                    "end": "2026-11-22",
                    "text": "Rust.\n\nTest.",
                }
            ],
            "thresholds": {"run": {"threshold_pace": {"value": 310.0, "unit": "s/km"}}},
            "rules": [{"id": "PLAN-1", "text": "Max 2 harde dagen."}],
        },
        "nl",
    )
    assert "<title>Coachboekje: Test &lt;plan&gt;</title>" in page
    assert "Example triathlon (A)" in page and "Drempeltempo 5:10 /km" in page
    assert "<b>PLAN-1</b> Max 2 harde dagen." in page and "<p>Test.</p>" in page
    assert "<plan>" not in page


def test_portal_booklet_from_the_plan(runtime) -> None:
    _put(runtime, EVENTS)
    plan_model.import_from_intervals(runtime.engine, prefix="lake27")
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = api.get("/api/plan/booklet", params={"lang": "en"})
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/html")
    assert "Coach booklet: lake27" in reply.text and "Tests: looptest 30&#x27;." in reply.text
