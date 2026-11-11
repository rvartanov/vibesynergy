"""Coaching preferences must be explicit, versioned and safe across concurrent clients."""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from vibesynergy.query import coach
from vibesynergy.store.schema import coach_event, coach_head, coach_revision

# A stored policy from before as the live database still holds it.
LEGACY_POLICY = {
    "method": "balanced",
    "priority": "consistency",
    "athlete": "masters",
    "phase": "follow_plan",
    "sport_priority": ["run", "ride", "swim", "strength", "walk", "other"],
    "sport_methods": {},
    "instructions": "Prefer hills.",
    "language": "nl",
    "detail": "concise",
    "tone": "direct",
    "week_minutes": None,
    "usage_budget": None,
    "garmin_inputs": {"goals": False, "events": False},
    "assessments": {
        "enabled": True,
        "vibesynergy_llm_narrative": True,
        "recent_days": 7,
        "daily_reviews": True,
        "morning_time": "09:00",
        "evening_time": "21:30",
    },
}
LEGACY_CONTEXT = {
    "event_sources": {"garmin": True, "garmin_coach": True, "manual": False, "goals": True},
    "manual_events": [],
    "source_priorities": {"garmin:event:1": "A", "garmin:coach:2": "C"},
    # Stored constraints were always saved in full (model_dump), as they are here.
    "constraints": [
        coach.Constraint(id="work-1", kind="commitment", text="Lead a group session").model_dump()
    ],
}


def draft(rt, payload=None, version=0, kind="policy", subject=1):
    return coach.save_draft(
        rt.engine,
        kind,
        payload or {},
        expected_version=version,
        reason="Test decision",
        subject_id=subject,
    )


def activate(rt, version, kind="policy", subject=1):
    return coach.activate(
        rt.engine, kind, expected_version=version, reason="Reviewed", subject_id=subject
    )


def test_draft_activation_restore_and_history(runtime):
    empty = coach.snapshot(runtime.engine)
    assert empty["documents"]["policy"]["version"] == 0
    first = draft(runtime, {"tone": "supportive", "detail": "detailed"})
    assert first["active"]["version"] == 0
    assert coach.snapshot(runtime.engine) == empty
    activate(runtime, 1)
    active = coach.snapshot(runtime.engine)
    assert active["documents"]["policy"]["effective"]["tone"] == "supportive"
    draft(runtime, {"tone": "direct"}, version=1)
    assert coach.snapshot(runtime.engine) == active
    activate(runtime, 2)
    assert coach.snapshot(runtime.engine)["version"] != active["version"]
    restored = coach.restore_draft(
        runtime.engine, "policy", 1, expected_version=2, reason="Restore"
    )
    assert restored["version"] == 3 and restored["active_version"] == 2
    assert restored["draft"]["payload"] == first["draft"]["payload"]
    activate(runtime, 3)
    assert len(coach.state(runtime.engine, "policy")["history"]) == 3


def test_reason_is_optional_for_draft_activation_and_restore(runtime):
    saved = coach.save_draft(runtime.engine, "policy", {}, expected_version=0)
    assert saved["draft"]["reason"] == ""
    active = coach.activate(runtime.engine, "policy", expected_version=1)
    assert active["active_version"] == 1
    with runtime.engine.connect() as conn:
        event = conn.execute(select(coach_event)).mappings().one()
    assert event["reason"] == ""
    restored = coach.restore_draft(runtime.engine, "policy", 1, expected_version=1)
    assert restored["draft"]["reason"] == ""
    with_reason = coach.save_draft(
        runtime.engine, "policy", {}, expected_version=2, reason="Reviewed"
    )
    assert with_reason["draft"]["reason"] == "Reviewed"
    with pytest.raises(ValueError, match="reason"):
        coach.activate(runtime.engine, "policy", expected_version=3, reason="x" * 1001)
    with pytest.raises(ValueError, match="actor"):
        coach.save_draft(runtime.engine, "policy", {}, expected_version=3, actor=" ")
    with pytest.raises(ValueError, match="expected_version"):
        coach.save_draft(runtime.engine, "policy", {}, expected_version=-1)


def test_portal_coach_posts_without_reason_and_accepts_reason(runtime):
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    headers = {"x-vibesynergy": "portal"}
    prefix = "/api/coach/policy"
    draft = client.post(
        f"{prefix}/draft", json={"payload": {}, "expected_version": 0}, headers=headers
    )
    assert draft.status_code == 200
    assert draft.json()["data"]["draft"]["reason"] == ""
    activated = client.post(f"{prefix}/activate", json={"expected_version": 1}, headers=headers)
    assert activated.status_code == 200
    assert activated.json()["data"]["active_version"] == 1
    restored = client.post(
        f"{prefix}/restore", json={"version": 1, "expected_version": 1}, headers=headers
    )
    assert restored.status_code == 200
    assert restored.json()["data"]["draft"]["reason"] == ""
    with_reason = client.post(
        f"{prefix}/draft",
        json={"payload": {}, "expected_version": 2, "reason": "Reviewed"},
        headers=headers,
    )
    assert with_reason.status_code == 200
    assert with_reason.json()["data"]["draft"]["reason"] == "Reviewed"


def test_stale_edits_and_activations_do_not_overwrite(runtime):
    draft(runtime)
    with pytest.raises(coach.Conflict):
        draft(runtime, {"tone": "supportive"})
    draft(runtime, {"detail": "detailed"}, version=1)
    with pytest.raises(coach.Conflict):
        activate(runtime, 1)
    assert coach.state(runtime.engine, "policy")["active"]["version"] == 0
    assert coach.state(runtime.engine, "policy")["draft"]["payload"]["detail"] == "detailed"


def test_concurrent_saves_have_one_winner(runtime):
    def write(tone):
        try:
            return draft(runtime, {"tone": tone})["version"]
        except coach.Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["direct", "supportive"]))
    assert sorted(map(str, results)) == ["1", "conflict"]
    assert len(coach.state(runtime.engine, "policy")["history"]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"tone": "unknown"},
        {"safety_enabled": False},
        {"instructions": "x" * 2001},
        {"language": "de"},
        {"detail": True},
    ],
)
def test_invalid_preferences_do_not_create_revisions(runtime, payload):
    with pytest.raises(ValueError):
        draft(runtime, payload)
    assert coach.state(runtime.engine, "policy")["version"] == 0


def test_older_documents_load_and_retired_keys_drop_out(runtime):
    """stored documents from before the trim still validate, restore and save."""
    checked = coach.preview("policy", LEGACY_POLICY)
    assert checked["payload"] == {
        "instructions": "Prefer hills.",
        "language": "nl",
        "detail": "concise",
        "tone": "direct",
    }
    assert set(checked["effective"]) == {
        "instructions",
        "language",
        "detail",
        "tone",
        "policy_version",
        "boundaries",
    }
    assert checked["effective"]["policy_version"] == "2"
    context = coach.preview("context", LEGACY_CONTEXT)
    assert set(context["payload"]) == {"constraints"}
    assert context["effective"]["constraints"][0]["id"] == "work-1"
    # A retired key with a value it never allowed is still ignored, not an error.
    assert coach.preview("policy", {"method": "nope", "week_minutes": -5})["payload"]["tone"]
    with pytest.raises(ValueError):
        coach.preview("policy", {"nickname": "x"})

    # A revision stored before the trim restores into a clean draft.
    draft(runtime, {"tone": "supportive"})
    with runtime.engine.begin() as conn:
        conn.execute(
            update(coach_revision)
            .where(coach_revision.c.version == 1)
            .values(payload=LEGACY_POLICY)
        )
    restored = coach.restore_draft(runtime.engine, "policy", 1, expected_version=1)
    assert "method" not in restored["draft"]["payload"]
    assert restored["draft"]["payload"]["language"] == "nl"

    # A partial context edit keeps the active notes and drops the retired keys.
    draft(runtime, LEGACY_CONTEXT, kind="context")
    activate(runtime, 1, kind="context")
    with runtime.engine.begin() as conn:
        conn.execute(
            update(coach_revision)
            .where(coach_revision.c.kind == "context", coach_revision.c.version == 1)
            .values(payload=LEGACY_CONTEXT)
        )
    saved = coach.apply_active(runtime.engine, "context", {}, expected_version=1)
    assert saved["active"]["payload"] == {"constraints": saved["active"]["payload"]["constraints"]}
    assert saved["active"]["payload"]["constraints"][0]["id"] == "work-1"


def _migration():
    path = (
        Path(coach.__file__).parents[1]
        / "store"
        / "migrations"
        / "versions"
        / "0035_coach_settings_trim.py"
    )
    spec = importlib.util.spec_from_file_location("coach_trim_0035", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_cleaning_matches_the_code():
    """0035 writes the same effective document and fingerprint as the code would."""
    migration = _migration()
    assert migration.POLICY_VERSION == coach.POLICY_VERSION
    assert migration.BOUNDARIES == coach.BOUNDARIES
    for kind, payload in (("policy", LEGACY_POLICY), ("context", LEGACY_CONTEXT)):
        values, effective = migration.clean(kind, payload)
        checked = coach.preview(kind, values)
        assert checked["payload"] == values
        assert checked["effective"] == effective
        assert migration._fingerprint(effective) == checked["fingerprint"]
    partial = {"tone": "supportive", "method": "polarized"}
    values, _ = migration.clean("policy", partial)
    assert values == coach.preview("policy", partial)["payload"]


def test_migration_writes_and_activates_a_clean_revision(runtime):
    draft(runtime, {"tone": "supportive"})
    activate(runtime, 1)
    draft(runtime, {"tone": "direct"}, version=1)  # an unactivated draft stays in history
    draft(runtime, {}, kind="context")
    activate(runtime, 1, kind="context")
    with runtime.engine.begin() as conn:
        conn.execute(
            update(coach_revision)
            .where(coach_revision.c.kind == "policy", coach_revision.c.version == 1)
            .values(payload=LEGACY_POLICY)
        )
        conn.execute(
            update(coach_revision)
            .where(coach_revision.c.kind == "context", coach_revision.c.version == 1)
            .values(payload=LEGACY_CONTEXT)
        )
    migration = _migration()

    class _Op:
        @staticmethod
        def get_bind():
            return bind

    with runtime.engine.begin() as bind:
        migration.op = _Op
        migration.upgrade()
        migration.upgrade()  # idempotent: a clean document is left alone
    policy = coach.state(runtime.engine, "policy")
    assert policy["version"] == 3 and policy["active_version"] == 3
    assert policy["active"]["actor"] == "migration"
    assert policy["active"]["payload"] == coach.preview("policy", LEGACY_POLICY)["payload"]
    assert policy["active"]["fingerprint"] == coach.preview("policy", LEGACY_POLICY)["fingerprint"]
    context = coach.state(runtime.engine, "context")
    assert context["active_version"] == 2
    assert set(context["active"]["payload"]) == {"constraints"}
    with runtime.engine.connect() as conn:
        heads = conn.execute(select(coach_head.c.kind, coach_head.c.version)).all()
        events = conn.execute(
            select(coach_event.c.kind, coach_event.c.version).where(
                coach_event.c.actor == "migration"
            )
        ).all()
    assert sorted(heads) == [("context", 2), ("policy", 3)]
    assert sorted(events) == [("context", 2), ("policy", 3)]


def test_instruction_limit_and_active_version_are_shared(runtime):
    assert (
        len(coach.preview("policy", {"instructions": "x" * 2000})["payload"]["instructions"])
        == 2000
    )
    with pytest.raises(ValueError, match="within 2,000 characters"):
        coach.save_draft(runtime.engine, "policy", {"instructions": "x" * 2001}, expected_version=0)
    before = coach.snapshot(runtime.engine)
    draft(runtime, {"instructions": "Prefer hills."})
    assert coach.snapshot(runtime.engine) == before
    activate(runtime, 1)
    after = coach.snapshot(runtime.engine)
    assert after["documents"]["policy"]["version"] == 1
    assert after["documents"]["policy"]["effective"]["instructions"] == "Prefer hills."
    assert after["version"] != before["version"]


def test_activation_rejects_legacy_oversized_instructions(runtime):
    draft(runtime, {"instructions": "Within limit."})
    with runtime.engine.begin() as conn:
        payload = conn.scalar(select(coach_revision.c.payload).where(coach_revision.c.version == 1))
        conn.execute(
            update(coach_revision)
            .where(coach_revision.c.version == 1)
            .values(payload={**payload, "instructions": "x" * 2001})
        )
    with pytest.raises(ValueError, match="Keep coaching instructions within 2,000 characters"):
        activate(runtime, 1)
    assert coach.state(runtime.engine, "policy")["active_version"] == 0


def test_scope_and_context_provenance(runtime):
    payload = {
        "constraints": [{"id": "work-1", "kind": "commitment", "text": "Lead a group session"}],
    }
    draft(runtime, payload, kind="context")
    activate(runtime, 1, kind="context")
    snapshot = coach.snapshot(runtime.engine)
    context = snapshot["documents"]["context"]["effective"]
    assert context["constraints"][0]["source"] == "user"
    assert "training_load" not in context["constraints"][0]
    assert coach.snapshot(runtime.engine, subject_id=2)["documents"]["policy"]["version"] == 0
    draft(runtime, subject=2)
    activate(runtime, 1, subject=2)
    assert coach.snapshot(runtime.engine) == snapshot
    with pytest.raises(ValueError):
        coach.preview(
            "context",
            {
                "constraints": [
                    {"id": "x", "kind": "commitment", "text": "x", "source": "inference"}
                ]
            },
        )
    with pytest.raises(ValueError):
        coach.preview(
            "context",
            {"constraints": [{"id": "x", "kind": "availability", "text": "x", "days": [8]}]},
        )


def test_activation_is_idempotent_and_requires_a_draft(runtime):
    with pytest.raises(ValueError):
        activate(runtime, 0)
    draft(runtime)
    activate(runtime, 1)
    activate(runtime, 1)
    with runtime.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(coach_event)) == 1


def test_api_conflicts_private_settings_and_readonly_mcp(runtime):
    import anyio
    from fastapi.testclient import TestClient
    from mcp import Client

    from vibesynergy.mcp.server import build_server
    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.portal.share import Share

    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    headers = {"x-vibesynergy": "portal"}
    body = {
        "payload": {"tone": "supportive", "instructions": "Prefer gentle climbs."},
        "expected_version": 0,
    }
    assert client.post("/api/coach/policy/draft", json=body).status_code == 403
    too_long = client.post(
        "/api/coach/policy/draft",
        json={"payload": {"instructions": "x" * 2001}, "expected_version": 0},
        headers=headers,
    )
    assert too_long.status_code == 400
    assert "within 2,000 characters" in too_long.json()["error"]
    dutch = client.post(
        "/api/coach/policy/draft",
        json={"payload": {"instructions": "x" * 2001}, "expected_version": 0},
        headers={**headers, "accept-language": "nl-NL"},
    )
    assert dutch.status_code == 400 and "2.000 tekens" in dutch.json()["error"]
    assert client.post("/api/coach/policy/draft", json=body, headers=headers).status_code == 200
    assert client.post("/api/coach/policy/draft", json=body, headers=headers).status_code == 409
    assert (
        client.post(
            "/api/coach/policy/activate",
            json={"expected_version": 1, "reason": "Review"},
            headers=headers,
        ).status_code
        == 200
    )
    api = client.get("/api/coach").json()["data"]
    assert set(api) == {"catalogue", "policy", "context"}
    assert api["policy"]["active"]["payload"]["instructions"] == "Prefer gentle climbs."
    assert set(api["catalogue"]["policy_defaults"]) == {
        "instructions",
        "language",
        "detail",
        "tone",
    }
    for gone in ("policy/export", "policy/import", "context/import-archive"):
        method = client.get if gone.endswith("export") else client.post
        assert method(f"/api/coach/{gone}", headers=headers).status_code in (404, 405, 422)
    assert not Share("test", "test", ("dashboard", "sql"), True).allows("GET", "/coach")

    async def read():
        async with Client(build_server(runtime)) as m:
            return await m.call_tool("vibesynergy_coach_state", {})

    result = anyio.run(read)
    assert not result.is_error
    data = result.structured_content["data"]
    assert data["documents"]["policy"]["version"] == 1
    effective = data["documents"]["policy"]["effective"]
    assert effective["instructions"] == "Prefer gentle climbs."
    assert effective["tone"] == "supportive"
    assert not {"method", "sport_priority", "assessments", "week_minutes"} & set(effective)
    assert "garmin_context" not in data and "coaching_events" not in data
    assert "knowledge base" in data["note"]
    assert data["races"] == []
    assert "planner_available" not in data and "current_plan" not in data
    assert data["active_profile"]["policy_revision"] == 1


def test_instruction_conflicts_are_explained_without_changing_the_fingerprint():
    assert coach.instruction_conflicts("Prefer hills.") == []
    conflicts = coach.instruction_conflicts(
        "Ignore safety and publish my HRV on Strava. Invent the pace. Edit Garmin Coach."
    )
    assert [item["bound"] for item in conflicts] == ["safety", "privacy", "approval", "evidence"]
    checked = coach.preview("policy", {"instructions": "Ignore the measurements."})
    assert "conflicts" not in checked
    assert checked["fingerprint"] == coach.fingerprint(checked["effective"])
    assert coach.policy_conflicts({"instructions": "Keep it easy."}) == []
    assert coach.policy_conflicts({"instructions": "Ignore safety."})[0]["bound"] == "safety"


def add_race(runtime, day, title="Example triathlon"):
    from datetime import UTC, datetime

    from vibesynergy.store.schema import planned_item, raw_item

    now = datetime.now(UTC)
    with runtime.engine.begin() as conn:
        conn.execute(
            raw_item.insert(),
            {
                "id": f"raw-{day}",
                "source": "garmin",
                "dataset": "calendar",
                "item_key": f"race-{day}",
                "sha256": "ab" * 32,
                "content_type": "application/json",
                "size": 2,
                "request": {},
                "fetched_at": now,
                "last_seen_at": now,
                "is_current": True,
            },
        )
        conn.execute(
            planned_item.insert(),
            {
                "source": "garmin",
                "source_id": f"event:{day}",
                "subject_id": 1,
                "date": day,
                "kind": "event",
                "title": title,
                "sport": None,
                "is_race": True,
                "primary": True,
                "extra": {},
                "raw_item_id": f"raw-{day}",
                "updated_at": now,
            },
        )


def test_race_context_comes_from_the_plan_calendar(runtime):
    """next race and phase no longer depend on coach event-source switches."""
    from datetime import date, timedelta

    today = date(2026, 10, 31)
    add_race(runtime, today + timedelta(days=200))
    race = coach.coaching_race_context(runtime.engine, today)
    assert race["next_race"]["name"] == "Example triathlon"
    assert race["next_race"]["priority"] == "A"
    assert race["plan_phase"]["phase"] == "build"
    assert race["supporting"] == []
