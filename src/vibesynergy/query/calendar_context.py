"""Explicit calendar context labels; they never grant ownership or measured load."""

from sqlalchemy import select, update

from vibesynergy.store.schema import planned_item


def classify_calendar(engine, url: str, classification: str) -> None:
    with engine.begin() as conn:
        rows = (
            conn.execute(
                select(planned_item).where(
                    planned_item.c.source == "caldav",
                    planned_item.c.extra["calendar_url"].as_string() == url,
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            extra = row["extra"] or {}
            if extra.get("mirror_key"):
                continue
            conn.execute(
                update(planned_item)
                .where(
                    planned_item.c.source == "caldav",
                    planned_item.c.source_id == row["source_id"],
                )
                .values(
                    extra={**extra, "classification": classification, "counts_as_exercise": False}
                )
            )
