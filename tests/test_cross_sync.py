"""Settings → Sync between sources: status read from local ledgers only."""

from __future__ import annotations

from datetime import UTC, datetime

from vibesynergy.query import cross_sync
from vibesynergy.store.schema import calendar_mirror, job, publication_ledger, publication_mirror


def test_overview_is_empty_without_history(runtime) -> None:
    data = cross_sync.overview(runtime.engine)
    assert data["publication"]["strava"] == {"count": 0, "last_at": None}
    assert data["publication"]["watching"] == 0
    assert data["publication"]["problem"] is None
    assert data["calendar"] == {"copies": 0, "conflicts": 0, "last_run": None}


def test_overview_counts_publications_watches_and_calendar_copies(runtime) -> None:
    now = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    ledger = {
        "assessment_id": "a",
        "preview_version": "v",
        "owned_block": "[/*]\nx\n[*/]",
        "base_description": "",
        "description": "[/*]\nx\n[*/]",
        "updated_at": now,
    }
    with runtime.engine.begin() as conn:
        conn.execute(
            publication_ledger.insert(),
            [
                {**ledger, "source": "strava", "source_id": "1", "state": "verified"},
                {**ledger, "source": "strava", "source_id": "2", "state": "pending"},
                {**ledger, "source": "garmin", "source_id": "9", "state": "verified"},
            ],
        )
        conn.execute(
            publication_mirror.insert().values(
                group_id="garmin:9",
                strava_id="1",
                garmin_id="9",
                language="nl",
                state="active",
                first_published_at=now,
                watch_until=now,
                next_check_at=now,
                last_error="Garmin did not return the mirrored description",
                updated_at=now,
            )
        )
        conn.execute(
            job.insert().values(
                kind="calendar_mirror",
                payload={},
                status="done",
                priority=9,
                run_after=now,
                attempts=1,
                max_attempts=3,
                created_at=now,
                finished_at=now,
            )
        )
    with runtime.engine.begin() as conn:
        for origin, state in (("a", "verified"), ("b", "conflict")):
            conn.execute(
                calendar_mirror.insert().values(
                    origin_id=f"garmin:{origin}",
                    calendar_url="https://cal.example/home/",
                    ledger_id=origin,
                    intent_id=origin,
                    idempotency_key=origin,
                    account_id="acct",
                    uid=f"uid-{origin}",
                    href=f"/home/{origin}.ics",
                    sequence=0,
                    state=state,
                    revision=1,
                    updated_at=now,
                )
            )

    data = cross_sync.overview(runtime.engine)
    pub = data["publication"]
    assert pub["strava"]["count"] == 1 and pub["strava"]["last_at"].startswith("2026-10-31")
    assert pub["garmin"]["count"] == 1
    assert pub["uncertain"] == 1
    assert pub["watching"] == 1
    assert pub["problem"] == "Garmin did not return the mirrored description"
    assert data["calendar"]["copies"] == 1
    assert data["calendar"]["conflicts"] == 1
    assert data["calendar"]["last_run"]["status"] == "done"
