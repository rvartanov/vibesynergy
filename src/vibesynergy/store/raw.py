"""Raw archive: every response a source returns, versioned and immutable.

A logical item is ``(source, dataset, item_key)``, e.g. ``("garmin", "sleep", "2026-09-24")``.
Fetching it again with identical content only refreshes ``last_seen_at``; different content
creates a new version that supersedes the previous one. Nothing is ever deleted, so the
normalised store can always be rebuilt and earlier values stay answerable.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, Integer, func, select, update

from vibesynergy.core.ids import new_id
from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.store.blobs import BlobStore
from vibesynergy.store.schema import raw_item

JSON_TYPE = "application/json"


def encode_payload(payload: Any) -> tuple[bytes, str]:
    """Bytes pass through untouched; anything else is stored as canonical JSON."""
    if isinstance(payload, bytes):
        return payload, "application/octet-stream"
    text = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return text.encode(), JSON_TYPE


@dataclass(frozen=True)
class StoredRaw:
    id: str
    is_new: bool  # False when the content was identical to the current version
    supersedes: str | None


@dataclass(frozen=True)
class RawRecord:
    id: str
    source: str
    dataset: str
    item_key: str
    sha256: str
    content_type: str
    request: Mapping[str, Any]
    window_start: date | None
    window_end: date | None
    fetched_at: datetime
    supersedes: str | None
    payload: bytes

    def json(self) -> Any:
        return json.loads(self.payload)


class RawStore:
    def __init__(self, engine: Engine, blobs: BlobStore) -> None:
        self._engine = engine
        self._blobs = blobs

    def put(
        self,
        *,
        source: str,
        dataset: str,
        item_key: str,
        payload: bytes,
        content_type: str,
        request: Mapping[str, Any] | None = None,
        window: Window | None = None,
        fetched_at: datetime | None = None,
    ) -> StoredRaw:
        sha256 = self._blobs.put(payload)
        now = fetched_at or utc_now()
        key = (
            (raw_item.c.source == source)
            & (raw_item.c.dataset == dataset)
            & (raw_item.c.item_key == item_key)
        )
        with self._engine.begin() as conn:
            current = conn.execute(
                select(raw_item.c.id, raw_item.c.sha256).where(key & raw_item.c.is_current)
            ).first()
            if current and current.sha256 == sha256:
                conn.execute(
                    update(raw_item).where(raw_item.c.id == current.id).values(last_seen_at=now)
                )
                return StoredRaw(current.id, is_new=False, supersedes=None)
            if current:
                conn.execute(
                    update(raw_item).where(raw_item.c.id == current.id).values(is_current=False)
                )
            item_id = new_id("raw")
            conn.execute(
                raw_item.insert().values(
                    id=item_id,
                    source=source,
                    dataset=dataset,
                    item_key=item_key,
                    sha256=sha256,
                    content_type=content_type,
                    size=len(payload),
                    request=dict(request or {}),
                    window_start=window.start if window else None,
                    window_end=window.end if window else None,
                    fetched_at=now,
                    last_seen_at=now,
                    supersedes=current.id if current else None,
                    is_current=True,
                )
            )
        return StoredRaw(item_id, is_new=True, supersedes=current.id if current else None)

    def load(self, item_id: str) -> RawRecord:
        with self._engine.connect() as conn:
            row = conn.execute(select(raw_item).where(raw_item.c.id == item_id)).one()
        return RawRecord(
            id=row.id,
            source=row.source,
            dataset=row.dataset,
            item_key=row.item_key,
            sha256=row.sha256,
            content_type=row.content_type,
            request=row.request,
            window_start=row.window_start,
            window_end=row.window_end,
            fetched_at=row.fetched_at,
            supersedes=row.supersedes,
            payload=self._blobs.get(row.sha256),
        )

    def current_ids(self, source: str, dataset: str | None = None) -> Iterator[str]:
        query = select(raw_item.c.id).where(raw_item.c.source == source, raw_item.c.is_current)
        if dataset:
            query = query.where(raw_item.c.dataset == dataset)
        with self._engine.connect() as conn:
            ids = conn.execute(query.order_by(raw_item.c.id)).scalars().all()
        yield from ids

    def all_ids(self, source: str, dataset: str) -> Iterator[str]:
        """Every version of every item, oldest first."""
        query = select(raw_item.c.id).where(
            raw_item.c.source == source, raw_item.c.dataset == dataset
        )
        with self._engine.connect() as conn:
            ids = conn.execute(query.order_by(raw_item.c.fetched_at, raw_item.c.id)).scalars().all()
        yield from ids

    def current_id(self, source: str, dataset: str, item_key: str) -> str | None:
        with self._engine.connect() as conn:
            return conn.execute(
                select(raw_item.c.id).where(
                    raw_item.c.source == source,
                    raw_item.c.dataset == dataset,
                    raw_item.c.item_key == item_key,
                    raw_item.c.is_current,
                )
            ).scalar()

    def current_request(self, source: str, dataset: str, item_key: str) -> Mapping[str, Any] | None:
        """How the current version of an item was requested (None: not archived)."""
        with self._engine.connect() as conn:
            return conn.execute(
                select(raw_item.c.request).where(
                    raw_item.c.source == source,
                    raw_item.c.dataset == dataset,
                    raw_item.c.item_key == item_key,
                    raw_item.c.is_current,
                )
            ).scalar()

    def has_content(self, sha256: str) -> bool:
        """Whether any raw item, of any source, holds exactly this content."""
        with self._engine.connect() as conn:
            return (
                conn.execute(
                    select(raw_item.c.id).where(raw_item.c.sha256 == sha256).limit(1)
                ).first()
                is not None
            )

    def needs_normalising(self, item_id: str) -> bool:
        """Identical archived content can still need processing after a failure or crash."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(raw_item.c.normalised_at, raw_item.c.normalise_error).where(
                    raw_item.c.id == item_id
                )
            ).one()
        return row.normalised_at is None or row.normalise_error is not None

    def mark_normalised(self, item_id: str, error: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(raw_item)
                .where(raw_item.c.id == item_id)
                .values(normalised_at=utc_now(), normalise_error=error)
            )

    def stats(self) -> list[dict[str, Any]]:
        query = (
            select(
                raw_item.c.source,
                raw_item.c.dataset,
                func.count().label("versions"),
                func.sum(raw_item.c.is_current.cast(Integer)).label("items"),
                func.sum(raw_item.c.size).label("bytes"),
                func.count(raw_item.c.normalise_error).label("errors"),
                func.max(raw_item.c.last_seen_at).label("last_seen_at"),
            )
            .group_by(raw_item.c.source, raw_item.c.dataset)
            .order_by(raw_item.c.source, raw_item.c.dataset)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(query)]
