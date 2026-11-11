"""RPE and Feel per activity: Garmin and intervals.icu values, VibeSynergy entries, the push."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import select

from vibesynergy.query import activity_effort as effort
from vibesynergy.sources.intervals_icu.client import (
    API_KEY_ENV,
    ATHLETE_ID_ENV,
    IntervalsIcuClient,
    IntervalsIcuError,
)
from vibesynergy.store.schema import activity, activity_effort, audit_log

DAY = date(2026, 10, 29)
START = "2026-10-29T18:32:35+00:00"


def _garmin(rt, source_id: str, *, start: str = START, extra: dict | None = None) -> str:
    stored = rt.raw.put(
        source="garmin",
        dataset="activities",
        item_key=source_id,
        payload=json.dumps({"id": source_id}).encode(),
        content_type="application/json",
    )
    with rt.engine.begin() as conn:
        conn.execute(
            activity.insert().values(
                source="garmin",
                source_id=source_id,
                subject_id=1,
                start_utc=datetime.fromisoformat(start),
                tz_offset_s=3600,
                local_date=DAY,
                sport="running",
                name="Easy run",
                elapsed_s=2400,
                moving_s=2400,
                distance_m=6000,
                extra=extra or {},
                raw_item_id=stored.id,
                updated_at=datetime.fromisoformat("2026-10-29T20:00:00+00:00"),
            )
        )
    rt.derive()
    return f"garmin:{source_id}"


def _icu_month(rt, items: list[dict]) -> None:
    rt.raw.put(
        source="intervals_icu",
        dataset="activities",
        item_key="2026-10",
        payload=json.dumps(items).encode(),
        content_type="application/json",
    )


def _connected(monkeypatch) -> None:
    """intervals.icu looks set up; every call in these tests goes to a mocked client."""
    monkeypatch.setenv(API_KEY_ENV, "secret-key")
    monkeypatch.setenv(ATHLETE_ID_ENV, "i000001")


class ActivityApi:
    """intervals.icu activity GET/PUT, mocked."""

    def __init__(self, record: dict) -> None:
        self.record = dict(record)
        self.calls: list[tuple] = []

    def exchange(self, method, url, params, headers, body):
        self.calls.append((method, url, json.loads(body) if body else None))
        assert "/api/v1/activity/" in url
        if method == "GET":
            return 200, {}, json.dumps(self.record).encode()
        if method == "PUT":
            self.record.update(json.loads(body))
            return 200, {}, json.dumps(self.record).encode()
        return 405, {}, b""

    def client(self) -> IntervalsIcuClient:
        return IntervalsIcuClient(
            "secret-key", "i000001", exchange=self.exchange, interval=0, write_interval=0
        )


def test_scales_follow_intervals_icu() -> None:
    # Garmin effort 10-100 is RPE x 10; Garmin feel 100 (very strong) is intervals.icu 1.
    assert [effort.garmin_rpe(v) for v in (10, 30, 80, 100)] == [1, 3, 8, 10]
    assert [effort.garmin_feel(v) for v in (100, 75, 50, 25, 0)] == [1, 2, 3, 4, 5]
    assert effort.garmin_rpe(None) is None and effort.garmin_feel("x") is None
    assert effort.RPE_LABELS[1] == "Nothing at all" and effort.RPE_LABELS[10] == "Max effort"
    assert effort.FEEL_LABELS == {1: "Strong", 2: "Good", 3: "Normal", 4: "Poor", 5: "Weak"}
    with pytest.raises(effort.EffortError):
        effort.validate("rpe", 11)
    with pytest.raises(effort.EffortError):
        effort.validate("feel", 0)


def test_garmin_value_is_shown_with_its_source_and_not_replaced(runtime) -> None:
    key = _garmin(runtime, "101", extra={"rpe": 30.0, "feel": 75.0})
    got = effort.for_activity(runtime.engine, runtime.raw, key)
    assert got["rpe"] | {"others": []} == {
        "value": 3,
        "source": "garmin",
        "label": "Easy",
        "editable": False,
        "others": [],
    }
    assert (got["feel"]["value"], got["feel"]["label"]) == (2, "Good")
    with pytest.raises(effort.EffortError, match="already recorded on garmin"):
        effort.set_effort(runtime.engine, runtime.raw, key, rpe=5, push=False)


def test_intervals_icu_value_matched_by_garmin_id(runtime) -> None:
    key = _garmin(runtime, "202")
    _icu_month(
        runtime,
        [
            {"id": "i9", "external_id": "202", "start_date": START, "icu_rpe": 6, "feel": 4},
            {"id": "i10", "external_id": "999", "start_date": "2026-10-30T08:00:00Z"},
        ],
    )
    got = effort.for_activity(runtime.engine, runtime.raw, key)
    assert (got["rpe"]["value"], got["rpe"]["source"], got["rpe"]["editable"]) == (
        6,
        "intervals_icu",
        False,
    )
    assert (got["feel"]["value"], got["feel"]["label"]) == (4, "Poor")


def test_entry_is_saved_and_pushed_without_overwriting(runtime, monkeypatch) -> None:
    _connected(monkeypatch)
    key = _garmin(runtime, "303", extra={"rpe": 40.0})
    _icu_month(runtime, [{"id": "i77", "external_id": "303", "start_date": START}])
    got = effort.for_activity(runtime.engine, runtime.raw, key)
    assert got["rpe"]["editable"] is False and got["feel"]["editable"] is True
    saved = effort.set_effort(runtime.engine, runtime.raw, key, feel=2, push=False)
    assert saved["feel"] | {"others": []} == {
        "value": 2,
        "source": "vibesynergy",
        "label": "Good",
        "editable": True,
        "others": [],
    }
    assert saved["intervals_icu"]["state"] == "pending"

    api = ActivityApi({"id": "i77", "icu_rpe": 4, "feel": None})
    assert effort.push(runtime.engine, runtime.raw, key, api.client()) is True
    puts = [call for call in api.calls if call[0] == "PUT"]
    assert puts == [("PUT", "https://intervals.icu/api/v1/activity/i77", {"feel": 2})]
    with runtime.engine.connect() as conn:
        row = conn.execute(select(activity_effort)).mappings().one()
        audit = (
            conn.execute(select(audit_log).where(audit_log.c.action == "icu.activity_effort"))
            .mappings()
            .one()
        )
    assert row["icu_error"] is None and row["icu_activity_id"] == "i77"
    assert row["icu_sent"] == {"feel": 2}
    assert audit["detail"]["after"] == {"feel": 2} and audit["detail"]["before"] == {"feel": None}

    # Changing it again replaces only what VibeSynergy sent itself.
    effort.set_effort(runtime.engine, runtime.raw, key, feel=3, push=False)
    assert effort.push(runtime.engine, runtime.raw, key, api.client()) is True
    assert api.record["feel"] == 3

    # Someone set another value on intervals.icu: kept, and reported as a conflict.
    api.record["feel"] = 5
    effort.set_effort(runtime.engine, runtime.raw, key, feel=1, push=False)
    assert effort.push(runtime.engine, runtime.raw, key, api.client()) is False
    assert api.record["feel"] == 5
    with runtime.engine.connect() as conn:
        assert conn.execute(select(activity_effort.c.icu_error)).scalar() == "conflict:feel"


def test_push_waits_until_the_activity_is_on_intervals_icu(runtime, monkeypatch) -> None:
    _connected(monkeypatch)
    key = _garmin(runtime, "404")
    effort.set_effort(runtime.engine, runtime.raw, key, rpe=7, feel=3, push=False)
    api = ActivityApi({"id": "i5"})
    assert effort.retry_pending(runtime.engine, runtime.raw, api.client()) == 0
    assert api.calls == []
    with runtime.engine.connect() as conn:
        assert conn.execute(select(activity_effort.c.icu_error)).scalar() == "not_found"
    got = effort.for_activity(runtime.engine, runtime.raw, key)
    assert got["intervals_icu"]["state"] == "waiting"
    _icu_month(runtime, [{"id": "i5", "external_id": "404", "start_date": START}])
    assert effort.retry_pending(runtime.engine, runtime.raw, api.client()) == 1
    assert (api.record["icu_rpe"], api.record["feel"]) == (7, 3)
    got = effort.for_activity(runtime.engine, runtime.raw, key)
    # intervals.icu now holds the copy VibeSynergy sent; the entry stays VibeSynergy's.
    assert got["rpe"]["source"] == "vibesynergy" and got["rpe"]["editable"] is True
    assert got["intervals_icu"]["state"] == "sent"


def test_client_writes_only_rpe_and_feel() -> None:
    api = ActivityApi({"id": "i1"})
    client = api.client()
    with pytest.raises(IntervalsIcuError):
        client.update_activity("i1", {"icu_rpe": 3, "name": "x"})
    with pytest.raises(IntervalsIcuError):
        client.update_activity("../athlete", {"feel": 2})
    assert api.calls == []


def test_portal_and_mcp_show_and_save_effort(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    key = _garmin(runtime, "505", extra={"rpe": 20.0})
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    headers = {"x-vibesynergy": "portal"}
    page = client.get("/api/activity", params={"id": key}, headers=headers).json()["data"]
    assert page["effort"]["rpe"]["source"] == "garmin"
    saved = client.post(
        "/api/activity-effort", json={"id": key, "feel": 3}, headers=headers
    ).json()["data"]
    assert saved["feel"]["value"] == 3 and saved["feel"]["source"] == "vibesynergy"
    refused = client.post("/api/activity-effort", json={"id": key, "rpe": 9}, headers=headers)
    assert refused.status_code == 400
    listed = client.get("/api/activities", params={"period": "all"}, headers=headers).json()[
        "data"
    ]["activities"]
    assert listed[0]["effort"] == {
        "rpe": 2,
        "rpe_source": "garmin",
        "feel": 3,
        "feel_source": "vibesynergy",
    }


def test_portal_note_about_an_activity_with_pain_tag(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.core.timeutil import Window
    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.query import journal

    key = _garmin(runtime, "606")
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    headers = {"x-vibesynergy": "portal"}
    saved = client.post(
        "/api/diary/note",
        json={"text": "Calf tight at 5 km", "tags": ["pain"], "activity_id": key, "date": str(DAY)},
        headers=headers,
    )
    assert saved.status_code == 200
    page = client.get("/api/activity", params={"id": key}, headers=headers).json()["data"]
    assert [n["text"] for n in page["notes"]] == ["Calf tight at 5 km"]
    assert page["notes"][0]["tags"] == ["pain"]
    assert journal.notes(runtime.engine, Window(DAY, DAY), tag="pain")
    assert "pain" in client.get("/api/diary", headers=headers).json()["data"]["choices"]["tags"]
    unknown = client.post(
        "/api/diary/note", json={"text": "x", "activity_id": "garmin:nope"}, headers=headers
    )
    assert unknown.status_code == 400
