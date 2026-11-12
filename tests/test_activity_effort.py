"""RPE and Feel per activity: Garmin's value, and your own entry, which always wins (12-11)."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from vibesynergy.query import activity_effort as effort
from vibesynergy.sources.intervals_icu.client import (
    API_KEY_ENV,
    ATHLETE_ID_ENV,
    IntervalsIcuClient,
)
from vibesynergy.store.schema import activity

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


def test_scales() -> None:
    # Garmin effort 10-100 is RPE x 10; Garmin feel 100 (very strong) is Feel 1 (Strong).
    assert [effort.garmin_rpe(v) for v in (10, 30, 80, 100)] == [1, 3, 8, 10]
    assert [effort.garmin_feel(v) for v in (100, 75, 50, 25, 0)] == [1, 2, 3, 4, 5]
    assert effort.garmin_rpe(None) is None and effort.garmin_feel("x") is None
    assert effort.RPE_LABELS[1] == "Nothing at all" and effort.RPE_LABELS[10] == "Max effort"
    assert effort.FEEL_LABELS == {1: "Strong", 2: "Good", 3: "Normal", 4: "Poor", 5: "Weak"}
    with pytest.raises(effort.EffortError):
        effort.validate("rpe", 11)
    with pytest.raises(effort.EffortError):
        effort.validate("feel", 0)


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


def test_your_entry_wins_over_garmin_and_clearing_brings_garmin_back(runtime) -> None:
    key = _garmin(runtime, "101", extra={"rpe": 30.0, "feel": 75.0})
    got = effort.for_activity(runtime.engine, runtime.raw, key)
    assert got["rpe"] == {
        "value": 3,
        "source": "garmin",
        "label": "Easy",
        "editable": True,
        "others": [],
    }
    saved = effort.set_effort(runtime.engine, runtime.raw, key, rpe=7)
    assert (saved["rpe"]["value"], saved["rpe"]["source"]) == (7, "vibesynergy")
    assert saved["rpe"]["others"] == [{"source": "garmin", "value": 3}]
    assert (saved["feel"]["value"], saved["feel"]["source"]) == (2, "garmin")
    cleared = effort.set_effort(runtime.engine, runtime.raw, key, rpe=None, fields={"rpe"})
    assert (cleared["rpe"]["value"], cleared["rpe"]["source"]) == (3, "garmin")


def test_nothing_goes_to_intervals_icu(runtime, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("intervals.icu must not be called for RPE/Feel")

    monkeypatch.setattr(IntervalsIcuClient, "update_activity_effort", refuse, raising=False)
    key = _garmin(runtime, "202")
    effort.set_effort(runtime.engine, runtime.raw, key, rpe=5, feel=2)
    assert not hasattr(effort, "push") and not hasattr(effort, "retry_pending")


def test_portal_and_mcp_show_and_save_effort(runtime) -> None:
    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app

    key = _garmin(runtime, "505", extra={"rpe": 20.0})
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    headers = {"x-vibesynergy": "portal"}
    page = client.get("/api/activity", params={"id": key}, headers=headers).json()["data"]
    assert page["effort"]["rpe"]["source"] == "garmin" and page["effort"]["rpe"]["editable"]
    saved = client.post(
        "/api/activity-effort", json={"id": key, "feel": 3, "rpe": 9}, headers=headers
    ).json()["data"]
    assert saved["feel"]["source"] == saved["rpe"]["source"] == "vibesynergy"
    listed = client.get("/api/activities", params={"period": "all"}, headers=headers).json()[
        "data"
    ]["activities"]
    assert listed[0]["effort"] == {
        "rpe": 9,
        "rpe_source": "vibesynergy",
        "feel": 3,
        "feel_source": "vibesynergy",
    }
