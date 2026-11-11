"""Recovery page: the recovery series and the morning check-ins."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from vibesynergy.portal.app import create_portal_app
from vibesynergy.store.schema import checkin


def test_recovery_api_gives_series_and_checkins(runtime) -> None:
    with runtime.engine.begin() as conn:
        conn.execute(
            checkin.insert().values(
                subject_id=1,
                date=date(2026, 11, 3),
                sleep_quality=4,
                soreness=2,
                note="Synthetic",
                updated_at=datetime(2026, 11, 3, 7, tzinfo=UTC),
            )
        )
    api = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = api.get("/api/recovery", params={"start": "2026-11-01", "end": "2026-11-05"}).json()
    data = reply["data"]
    assert "hrv.night_avg" in data["series"]["columns"]  # no rows without data
    (row,) = data["checkins"]
    assert row["date"] == "2026-11-03" and row["sleep_quality"] == 4 and "subject_id" not in row
    assert reply["meta"]["period"] == {"start": "2026-11-01", "end": "2026-11-05"}
    too_long = api.get("/api/recovery", params={"start": "2024-01-01", "end": "2026-11-05"})
    assert too_long.status_code == 400
