"""Durable coaching context: provenance, corrections, and backup restore."""

import pytest
from sqlalchemy import select

from vibesynergy.query import coach
from vibesynergy.store.schema import (
    calendar_mirror as calendar_mirror_table,
)


def _active(runtime):
    state = coach.state(runtime.engine, "context")
    return state["active"]["payload"], state


def test_measurement_needs_a_link_and_retired_notes_drop_out_of_advice(runtime):
    with pytest.raises(ValueError, match="existing record"):
        coach.preview(
            "context",
            {
                "constraints": [
                    {
                        "id": "m1",
                        "kind": "self_report",
                        "text": "Low recovery",
                        "origin": "measurement",
                    }
                ]
            },
        )
    checked = coach.preview(
        "context",
        {
            "constraints": [
                {
                    "id": "m1",
                    "kind": "self_report",
                    "text": "Low recovery",
                    "origin": "measurement",
                    "links": [{"entity": "note", "ref": "note_example"}],
                    "active": False,
                }
            ]
        },
    )
    assert checked["payload"]["constraints"][0]["active"] is False
    assert checked["effective"]["constraints"] == []


def test_correcting_a_note_keeps_one_id(runtime):
    payload = {
        "constraints": [
            {"id": "note-1", "kind": "preference", "text": "Prefer hills.", "days": [1]}
        ]
    }
    coach.apply_active(runtime.engine, "context", payload, expected_version=0)
    payload["constraints"][0]["text"] = "Prefer hills on fresh legs."
    coach.apply_active(runtime.engine, "context", payload, expected_version=1)
    saved, state = _active(runtime)
    assert [row["id"] for row in saved["constraints"]] == ["note-1"]
    assert saved["constraints"][0]["text"] == "Prefer hills on fresh legs."
    assert saved["constraints"][0]["origin"] == "user_statement"
    assert len(state["history"]) == 2


def test_sqlite_restore_keeps_coaching_context_and_integration_state(
    runtime, tmp_path, monkeypatch
):
    import sqlite3
    from dataclasses import replace
    from datetime import date

    from test_evidence import put
    from test_publication import garmin_activity

    from vibesynergy.core import config_edit
    from vibesynergy.query import calendar_mirror, publication
    from vibesynergy.runtime import Runtime

    coach.apply_active(
        runtime.engine,
        "context",
        {
            "constraints": [
                {
                    "id": "pref-restore-proof",
                    "kind": "preference",
                    "text": "Keep long sessions for weekends.",
                }
            ]
        },
        expected_version=0,
    )

    put(runtime, "garmin", garmin_activity())
    config_edit.apply_changes(
        runtime.settings.config_path,
        {
            "publication": {
                "quote_enabled": False,
                "weather_enabled": False,
            }
        },
    )

    class _PublicationProvider:
        def __init__(self):
            self.text = "Human note"
            self.write_calls = 0

        def read(self, source_id):
            assert source_id == "1"
            return {"activityId": source_id, "description": self.text}

        def write(self, source_id, description):
            assert source_id == "1"
            self.write_calls += 1
            self.text = description

    provider = _PublicationProvider()
    preview = publication.preview(runtime, "garmin:1", "garmin", client=provider)
    publication.publish(runtime, "garmin:1", "garmin", preview["preview_version"], client=provider)
    expected_publication = publication.preview(runtime, "garmin:1", "garmin", client=provider)

    class _CalendarWriter:
        def __init__(self):
            self.objects = {}
            self.seq = 0

        def snapshot(self, url):
            return [dict(item) for item in self.objects.values() if item["url"].startswith(url)]

        def get(self, href):
            found = self.objects.get(href)
            return dict(found) if found else None

        def put(self, href, body, *, match):
            self.seq += 1
            etag = f'"e{self.seq}"'
            self.objects[href] = {"url": href, "etag": etag, "data": body.decode()}
            return etag

        def delete(self, href, match):
            self.objects.pop(href, None)

    writer = _CalendarWriter()
    sync = calendar_mirror.reconcile(
        runtime.engine,
        writer,
        calendar_url="https://calendar.example.test/account/workouts/",
        account_id="synthetic-account",
        today=date(2026, 10, 5),
        garmin_complete=True,
        covered_until=date(2026, 10, 10),
        desired=[calendar_mirror.Desired("garmin:workout:17", date(2026, 10, 10), "Tempo run")],
        unkeyed_future=0,
        stamp="20261005T120000Z",
    )
    assert sync["created"] == 1
    expected_calendar = calendar_mirror.summary(runtime.engine)
    with runtime.engine.connect() as conn:
        expected_calendar_rows = [
            dict(row)
            for row in conn.execute(
                select(calendar_mirror_table).order_by(
                    calendar_mirror_table.c.origin_id, calendar_mirror_table.c.calendar_url
                )
            ).mappings()
        ]

    expected_context = coach.snapshot(
        runtime.engine,
        raw=runtime.raw,
        timezone=runtime.settings.timezone,
    )
    restored_data_dir = tmp_path / "restored-data"
    backup = restored_data_dir / "db" / "vibesynergy.sqlite"
    backup.parent.mkdir(parents=True)
    with sqlite3.connect(runtime.settings.db_path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    restored_settings = replace(runtime.settings, data_dir=restored_data_dir)
    restored_runtime = Runtime.open(restored_settings)

    class _OriginalRawGuard:
        def __getattr__(self, _name):
            raise AssertionError("Read-back must use the restored runtime raw store")

    monkeypatch.setattr(runtime, "raw", _OriginalRawGuard())
    try:
        assert (
            coach.snapshot(
                restored_runtime.engine,
                raw=restored_runtime.raw,
                timezone=restored_runtime.settings.timezone,
            )
            == expected_context
        )
        assert (
            publication.preview(restored_runtime, "garmin:1", "garmin", client=provider)
            == expected_publication
        )
        assert calendar_mirror.summary(restored_runtime.engine) == expected_calendar
        with restored_runtime.engine.connect() as conn:
            restored_calendar_rows = [
                dict(row)
                for row in conn.execute(
                    select(calendar_mirror_table).order_by(
                        calendar_mirror_table.c.origin_id, calendar_mirror_table.c.calendar_url
                    )
                ).mappings()
            ]
        assert restored_calendar_rows == expected_calendar_rows

        retry_publication = publication.publish(
            restored_runtime,
            "garmin:1",
            "garmin",
            expected_publication["preview_version"],
            client=provider,
        )
        assert retry_publication == {"state": "verified", "destination": "garmin", "source_id": "1"}
        assert provider.write_calls == 1

        retry_sync = calendar_mirror.reconcile(
            restored_runtime.engine,
            writer,
            calendar_url="https://calendar.example.test/account/workouts/",
            account_id="synthetic-account",
            today=date(2026, 10, 5),
            garmin_complete=True,
            covered_until=date(2026, 10, 10),
            desired=[calendar_mirror.Desired("garmin:workout:17", date(2026, 10, 10), "Tempo run")],
            unkeyed_future=0,
            stamp="20261005T120000Z",
        )
        assert retry_sync == {
            "skipped": None,
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "adopted": 0,
            "conflicts": 0,
            "ignored_unreadable": 0,
            "batch_limited": False,
        }

    finally:
        restored_runtime.close()
