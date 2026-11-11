from datetime import date

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select

from vibesynergy.core.timeutil import Window
from vibesynergy.store.blobs import BlobStore
from vibesynergy.store.raw import encode_payload
from vibesynergy.store.schema import metadata, raw_item


def test_migrations_match_schema(runtime) -> None:
    with runtime.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diff == []
    assert runtime.schema_is_current()


def test_blob_roundtrip_and_dedupe(tmp_path) -> None:
    blobs = BlobStore(tmp_path)
    sha = blobs.put(b"hello" * 100)
    assert blobs.put(b"hello" * 100) == sha
    assert blobs.get(sha) == b"hello" * 100
    assert blobs.path_for(sha).stat().st_size < 500
    assert len(list(tmp_path.rglob("*.zst"))) == 1


def test_encode_payload_is_canonical() -> None:
    a, ct = encode_payload({"b": 1, "a": [1, 2]})
    b, _ = encode_payload({"a": [1, 2], "b": 1})
    assert a == b and ct == "application/json"
    assert encode_payload(b"\x00FIT") == (b"\x00FIT", "application/octet-stream")


def _put(rt, payload: dict, key: str = "2026-09-24"):
    data, ct = encode_payload(payload)
    return rt.raw.put(
        source="demo",
        dataset="daily",
        item_key=key,
        payload=data,
        content_type=ct,
        request={"date": key},
        window=Window(date(2026, 9, 24), date(2026, 9, 24)),
    )


def test_raw_versions(runtime) -> None:
    first = _put(runtime, {"steps": 1})
    same = _put(runtime, {"steps": 1})
    changed = _put(runtime, {"steps": 2})

    assert first.is_new and not same.is_new and same.id == first.id
    assert changed.is_new and changed.supersedes == first.id
    assert runtime.raw.load(changed.id).json() == {"steps": 2}
    assert runtime.raw.load(first.id).json() == {"steps": 1}  # history is kept

    with runtime.engine.connect() as conn:
        current = conn.execute(
            select(func.count()).select_from(raw_item).where(raw_item.c.is_current)
        ).scalar()
    assert current == 1
    assert list(runtime.raw.current_ids("demo")) == [changed.id]
    [stats] = runtime.raw.stats()
    assert (stats["items"], stats["versions"]) == (1, 2)


def test_calendar_intent_migration_preserves_existing_ownership(tmp_path):
    from sqlalchemy import text

    from vibesynergy.store.db import create_db_engine, upgrade

    engine = create_db_engine(tmp_path / "upgrade.db")
    try:
        upgrade(engine, "0018")
        with engine.begin() as conn:
            conn.execute(
                text("""
                INSERT INTO calendar_mirror (
                    origin_id, calendar_url, ledger_id, intent_id, idempotency_key,
                    account_id, uid, href, sequence, fingerprint, state, revision, updated_at
                ) VALUES (
                    'garmin:workout:synthetic', 'https://example.test/calendar/', 'ledger',
                    'intent', 'key', 'synthetic', 'uid', 'https://example.test/calendar/event.ics',
                    4, 'fingerprint', 'verified', 7, '2026-10-07 00:00:00'
                )
            """)
            )
        upgrade(engine)
        with engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM calendar_mirror")).mappings().one()
        assert row["state"] == "verified" and row["revision"] == 7
        assert row["sequence"] == 4 and row["fingerprint"] == "fingerprint"
        assert row["pending_write"] is None
    finally:
        engine.dispose()
