"""Archive internal AI rows and report files, then drop those tables.

Revision ID: 0027
Revises: 0026

The archive sits beside the database as ``llm-archive.json``. Downgrade restores
the tables and the report files from that archive. Nothing else is dropped.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

# Children before parents.
DROP_ORDER = (
    "assessment_event",
    "assessment_head",
    "assessment_revision",
    "assessment_alias",
    "assessment_identity",
    "daily_review",
)


def _archive_path(bind) -> Path:
    database = bind.engine.url.database
    if not database:
        raise RuntimeError("llm archive needs a file database")
    return Path(database).parent / "llm-archive.json"


def _report_dirs(bind) -> list[Path]:
    database = Path(bind.engine.url.database or "")
    found = []
    for folder in (database.parent / "reports", database.parent.parent / "reports"):
        if folder.is_dir() and folder not in found:
            found.append(folder)
    return found


def _mark(value):
    if isinstance(value, datetime):
        return {"__dt": value.isoformat()}
    if isinstance(value, date):
        return {"__date": value.isoformat()}
    return value


def _unmark(value, column):
    if isinstance(value, dict) and set(value) == {"__dt"}:
        parsed = datetime.fromisoformat(value["__dt"])
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    if isinstance(value, dict) and set(value) == {"__date"}:
        return date.fromisoformat(value["__date"])
    if value is None or column is None:
        return value
    if isinstance(value, str) and isinstance(column.type, UTCDateTime):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    if (
        isinstance(value, str)
        and isinstance(column.type, sa.Date)
        and not isinstance(column.type, sa.DateTime)
    ):
        return date.fromisoformat(value[:10])
    return value


def _export(bind) -> dict:
    inspector = sa.inspect(bind)
    tables = {}
    for name in DROP_ORDER:
        if not inspector.has_table(name):
            tables[name] = []
            continue
        reflected = sa.Table(name, sa.MetaData(), autoload_with=bind)
        tables[name] = [
            {key: _mark(val) for key, val in row.items()}
            for row in bind.execute(sa.select(reflected)).mappings()
        ]
    reports = []
    for folder in _report_dirs(bind):
        for path in sorted(folder.glob("rep_*.json")):
            reports.append({"name": path.name, "folder": str(folder), "body": path.read_text()})
    return {"revision": revision, "tables": tables, "reports": reports}


def upgrade() -> None:
    bind = op.get_bind()
    payload = _export(bind)
    path = _archive_path(bind)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    for name in DROP_ORDER:
        if sa.inspect(bind).has_table(name):
            op.drop_table(name)
    for report in payload["reports"]:
        file = Path(report["folder"]) / report["name"]
        if file.is_file():
            file.unlink()


def _tables(metadata: sa.MetaData) -> None:
    assessment_identity = sa.Table(
        "assessment_identity",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("subject_id", sa.Integer, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
    )
    sa.Table(
        "assessment_alias",
        metadata,
        sa.Column("subject_id", sa.Integer, primary_key=True),
        sa.Column("source_ref", sa.String, primary_key=True),
        sa.Column(
            "identity_id",
            sa.String,
            sa.ForeignKey(assessment_identity.c.id),
            nullable=False,
        ),
    )
    assessment_revision = sa.Table(
        "assessment_revision",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "identity_id",
            sa.String,
            sa.ForeignKey(assessment_identity.c.id),
            nullable=False,
        ),
        sa.Column("input_version", sa.String(64), nullable=False),
        sa.Column("inputs", sa.JSON, nullable=False),
        sa.Column("public_facts", sa.JSON, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("interpretation", sa.JSON),
        sa.Column("model", sa.JSON),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("completed_at", UTCDateTime),
        sa.Column("error", sa.String),
        sa.Index("uq_assessment_identity_input", "identity_id", "input_version", unique=True),
    )
    sa.Table(
        "assessment_head",
        metadata,
        sa.Column(
            "identity_id",
            sa.String,
            sa.ForeignKey(assessment_identity.c.id),
            primary_key=True,
        ),
        sa.Column(
            "revision_id", sa.String, sa.ForeignKey(assessment_revision.c.id), nullable=False
        ),
    )
    sa.Table(
        "assessment_event",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "identity_id",
            sa.String,
            sa.ForeignKey(assessment_identity.c.id),
            nullable=False,
        ),
        sa.Column(
            "revision_id", sa.String, sa.ForeignKey(assessment_revision.c.id), nullable=False
        ),
        sa.Column("previous_id", sa.String),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("created_at", UTCDateTime, nullable=False),
    )
    sa.Table(
        "daily_review",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("subject_id", sa.Integer, nullable=False),
        sa.Column("local_date", sa.Date, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("input_version", sa.String(64), nullable=False),
        sa.Column("inputs", sa.JSON, nullable=False),
        sa.Column("safety", sa.JSON, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("interpretation", sa.JSON),
        sa.Column("model", sa.JSON),
        sa.Column("previous_id", sa.String),
        sa.Column("changed", sa.JSON),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("completed_at", UTCDateTime),
        sa.Column("error", sa.String),
        sa.Index(
            "uq_daily_review_day_kind_input",
            "subject_id",
            "local_date",
            "kind",
            "input_version",
            unique=True,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    _tables(metadata)
    metadata.create_all(bind)
    path = _archive_path(bind)
    if not path.is_file():
        return
    payload = json.loads(path.read_text())
    for name in reversed(DROP_ORDER):
        table = metadata.tables[name]
        rows = payload.get("tables", {}).get(name, [])
        if not rows:
            continue
        cleaned = []
        for row in rows:
            cleaned.append({column.key: _unmark(row.get(column.key), column) for column in table.c})
        bind.execute(table.insert(), cleaned)
    for report in payload.get("reports", []):
        folder = Path(report["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / report["name"]).write_text(report["body"])
