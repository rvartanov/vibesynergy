"""Coaching classification, plan adherence and load on an isolated fixture database."""

from datetime import UTC, date, datetime

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from vibesynergy.core.timeutil import Window
from vibesynergy.derive import unify
from vibesynergy.model import Activity, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coaching, plan, training, views
from vibesynergy.store import db
from vibesynergy.store.schema import activity_unified

DAY = date(2026, 9, 29)
SPAN = Window(DAY, DAY)


def _seed(runtime):
    raw = runtime.raw.put(
        source="garmin",
        dataset="test",
        item_key="coaching",
        payload=b"{}",
        content_type="application/json",
    )
    records = [
        PlannedItem("tempo", DAY, "plan_workout", "Tempo", sport="running"),
        Activity(
            "coach",
            datetime(2026, 9, 29, 8, tzinfo=UTC),
            DAY,
            "running",
            name="Training geven LGS",
            elapsed_s=3600,
            moving_s=3600,
            avg_hr=128,
            training_load=60,
        ),
    ]
    with runtime.engine.begin() as conn:
        write_records(conn, records, source="garmin", raw_item_id=raw.id)
    unify.rebuild(runtime.engine)


def _status(runtime):
    return next(
        i
        for i in plan.plan(runtime.engine, SPAN, date(2026, 10, 1))["items"]
        if i["title"] == "Tempo"
    )


def test_kind_follows_the_purpose_and_a_choice_wins(runtime):
    # "training geven" is a training you give, by itself; the activity choice wins.
    assert coaching.is_coached_title("Training geven LGS")
    assert not coaching.is_coached_title("Tempo")
    _seed(runtime)
    with runtime.engine.connect() as conn:
        row = conn.execute(select(activity_unified.c.kind, activity_unified.c.purpose)).one()
    assert tuple(row) == ("coaching", "coached_by_me")
    assert _status(runtime)["status"] == "missed"  # a training you give is not your Tempo
    assert coaching.backfill(runtime.engine, dry_run=True)["changed"] == 0
    coaching.set_override(runtime.engine, "garmin:coach", False)
    assert _status(runtime)["status"] == "done"
    unify.rebuild(runtime.engine)
    assert _status(runtime)["status"] == "done"
    coaching.set_override(runtime.engine, "garmin:coach", True)
    assert _status(runtime)["status"] == "missed"
    coaching.set_override(runtime.engine, "garmin:coach", None)
    assert _status(runtime)["status"] == "missed"


def test_analyses_include_exclude(runtime):
    _seed(runtime)
    with runtime.engine.connect() as conn:
        assert conn.execute(select(activity_unified.c.kind)).scalar_one() == "coaching"
    included = training.volume(runtime.engine, SPAN)
    excluded = training.volume(runtime.engine, SPAN, include_coaching=False)
    assert included["totals"]["coaching"]["count"] == 1
    assert excluded["totals"] == {}
    own = training.training_load(runtime.engine, SPAN, load="garmin", include_coaching=False)
    both = training.training_load(runtime.engine, SPAN, load="garmin")
    assert own["rows"][-1][1] == 0
    assert both["rows"][-1][1] == 60
    assert both["coaching_daily_load"][str(DAY)] == 60
    assert (
        views.intensity_distribution(runtime.engine, SPAN, include_coaching=False)[
            "activities_with_zones"
        ]
        == 0
    )


def test_backfill_follows_the_stored_purpose(runtime):
    _seed(runtime)
    with runtime.engine.begin() as conn:
        conn.execute(activity_unified.update().values(kind=None))
    assert coaching.backfill(runtime.engine, dry_run=True)["changed"] == 1
    assert coaching.backfill(runtime.engine)["changed"] == 1
    assert coaching.backfill(runtime.engine)["changed"] == 0
    assert _status(runtime)["status"] == "missed"


def test_migration_carries_the_old_outcome_into_the_purpose(runtime):
    # 0045: an activity counted as coaching by an old rule keeps counting, via its purpose.
    from vibesynergy.query import session_purpose
    from vibesynergy.store.schema import activity_purpose_override

    raw = runtime.raw.put(
        source="garmin",
        dataset="test",
        item_key="g",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [Activity("group", datetime(2026, 9, 29, 18, tzinfo=UTC), DAY, "running", name="LGS")],
            source="garmin",
            raw_item_id=raw.id,
        )
    unify.rebuild(runtime.engine)
    cfg = db._alembic_config()
    with runtime.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0044")
        conn.execute(activity_unified.update().values(kind="coaching"))  # an old name rule
    db.upgrade(runtime.engine)
    with runtime.engine.connect() as conn:
        assert conn.execute(select(activity_unified.c.purpose)).scalar_one() == "coached_by_me"
        assert session_purpose._overrides(conn) == {"garmin:group": "coached_by_me"}
        assert conn.execute(select(activity_purpose_override.c.reason)).scalar_one().endswith("")
    unify.rebuild(runtime.engine)
    with runtime.engine.connect() as conn:
        assert conn.execute(select(activity_unified.c.kind)).scalar_one() == "coaching"


def test_migration_up_down_on_fixture(runtime):
    cfg = db._alembic_config()
    with runtime.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0010")
    assert "kind" not in {
        c["name"] for c in inspect(runtime.engine).get_columns("activity_unified")
    }
    assert "coaching_rule" not in inspect(runtime.engine).get_table_names()
    db.upgrade(runtime.engine)
    assert "kind" in {c["name"] for c in inspect(runtime.engine).get_columns("activity_unified")}
    assert "coaching_rule" in inspect(runtime.engine).get_table_names()


def test_portal_has_no_name_rules_and_the_purpose_decides(runtime):
    _seed(runtime)
    client = TestClient(create_portal_app(runtime), base_url="http://127.0.0.1:8447")
    headers = {"x-vibesynergy": "portal"}
    assert client.get("/api/coaching-rules").status_code == 404
    assert client.post("/api/activity-coaching", json={}, headers=headers).status_code in (404, 405)
    activity = client.get("/api/activity", params={"id": "garmin:coach"}).json()["data"]["activity"]
    assert activity["kind"] == "coaching"
    changed = client.post(
        "/api/activity-purpose", json={"id": "garmin:coach", "purpose": "training"}, headers=headers
    )
    assert changed.status_code == 200
    activity = client.get("/api/activity", params={"id": "garmin:coach"}).json()["data"]["activity"]
    assert activity["kind"] is None
    assert _status(runtime)["status"] == "done"
