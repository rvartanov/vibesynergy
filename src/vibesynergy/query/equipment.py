"""Equipment register: locations and items."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Engine, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import equipment_item, equipment_location

STATUSES = frozenset({"confirmed", "unknown", "unavailable"})


def _validate_status(status: str) -> str:
    key = status.strip().lower()
    if key not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(sorted(STATUSES))}")
    return key


def _row_dict(row, *, location_key: str | None = None) -> dict[str, Any]:
    data = dict(row)
    if location_key is not None:
        data["location_key"] = location_key
    if data.get("last_verified") is not None:
        data["last_verified"] = data["last_verified"].isoformat()
    for key in ("created_at", "updated_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


def _location_id(conn, location_key: str) -> int | None:
    return conn.execute(
        select(equipment_location.c.id).where(equipment_location.c.key == location_key)
    ).scalar_one_or_none()


def upsert_location(engine: Engine, key: str, name: str) -> int:
    location_key = key.strip()
    if not location_key:
        raise ValueError("location key is required")
    display = (name or location_key).strip()
    now = utc_now()
    with engine.begin() as conn:
        existing = _location_id(conn, location_key)
        if existing is not None:
            conn.execute(
                update(equipment_location)
                .where(equipment_location.c.id == existing)
                .values(name=display, updated_at=now)
            )
            return existing
        row_id = conn.execute(
            equipment_location.insert().values(
                key=location_key,
                name=display,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
    return row_id


def upsert_item(
    engine: Engine,
    location_key: str,
    item: str,
    category: str,
    status: str,
    *,
    source: str | None = None,
    last_verified: date | None = None,
    gear_id: str | None = None,
    garmin_catalogue_key: str | None = None,
    notes: str | None = None,
) -> int:
    status_key = _validate_status(status)
    name = (item or "").strip()
    cat = (category or "").strip().lower()
    if not name or not cat:
        raise ValueError("item and category are required")
    loc_id = upsert_location(engine, location_key, location_key)
    now = utc_now()
    with engine.begin() as conn:
        existing = (
            conn.execute(
                select(equipment_item).where(
                    equipment_item.c.location_id == loc_id,
                    equipment_item.c.item == name,
                    equipment_item.c.category == cat,
                )
            )
            .mappings()
            .first()
        )
        values = {
            "status": status_key,
            "source": source,
            "last_verified": last_verified,
            "gear_id": gear_id,
            "garmin_catalogue_key": garmin_catalogue_key,
            "notes": notes,
            "updated_at": now,
        }
        if existing:
            conn.execute(
                update(equipment_item).where(equipment_item.c.id == existing["id"]).values(**values)
            )
            return existing["id"]
        return conn.execute(
            equipment_item.insert().values(
                location_id=loc_id,
                item=name,
                category=cat,
                created_at=now,
                **values,
            )
        ).inserted_primary_key[0]


def list_items(
    engine: Engine,
    location_key: str | None = None,
    status: str | None = None,
) -> list[dict]:
    query = (
        select(
            equipment_item,
            equipment_location.c.key.label("location_key"),
        )
        .join(equipment_location, equipment_item.c.location_id == equipment_location.c.id)
        .order_by(equipment_location.c.key, equipment_item.c.item)
    )
    if location_key:
        query = query.where(equipment_location.c.key == location_key)
    if status is not None:
        query = query.where(equipment_item.c.status == _validate_status(status))
    with engine.connect() as conn:
        return [
            _row_dict(row, location_key=row["location_key"])
            for row in conn.execute(query).mappings()
        ]


def confirmed_items(engine: Engine, location_key: str) -> list[dict]:
    return list_items(engine, location_key=location_key, status="confirmed")


def prescribable_items(engine: Engine, location_key: str) -> list[dict]:
    """Single gate for workout/coach prompts: confirmed items only (acceptance scenario 10)."""
    return confirmed_items(engine, location_key)
