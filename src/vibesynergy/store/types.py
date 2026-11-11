"""Column types SQLite lacks natively."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime stored as ISO 8601 text (sorts correctly as text)."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC values")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: str | None, dialect) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)
