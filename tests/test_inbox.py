"""Waiting for you: decisions first, then notices."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import inbox, knowledge
from vibesynergy.store.schema import coach_decision_log


def test_proposals_count_and_a_missed_run_is_a_notice(runtime) -> None:
    assert inbox.count(runtime.engine) == 0
    # A morning run that happened once, then not: a notice, not a decision.
    with runtime.engine.begin() as conn:
        conn.execute(
            coach_decision_log.insert().values(
                subject_id=1,
                at=datetime(2026, 11, 1, 8, tzinfo=UTC),
                kind="note",
                author="Claude",
                entry_type="morning",
                idempotency_key="morning-2026-11-01",
                for_date=date(2026, 11, 1),
                text="x",
                meta={},
            )
        )
    out = inbox.inbox(runtime, date.today())
    assert out["count"] == 0 and out["decisions"] == []
    assert all(n["link"].startswith("#/") for n in out["notices"])

    knowledge.propose_change(
        runtime.engine,
        kind="article",
        target="evidence-synthetic",
        rationale="A new synthetic article",
        author="client:coach",
        changes={"title": "Synthetic", "body_md": "# Synthetic", "topic": "synthetic"},
    )
    out = inbox.inbox(runtime, date.today())
    assert inbox.count(runtime.engine) == 1 and out["count"] == 1
    (item,) = out["decisions"]
    assert item["kind"] == "kb_proposal" and item["link"] == "#/coach/knowledge"
    assert item["why"] == "A new synthetic article" and item["what"] == "article"


def test_portal_serves_the_inbox_and_the_count(runtime) -> None:
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = api.get("/api/inbox").json()["data"]
    assert set(data) == {"decisions", "notices", "count"}
    assert api.get("/api/inbox/count").json()["data"] == {"count": 0}
