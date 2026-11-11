"""SQLite engine and schema migrations."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool


def create_db_engine(db_path: Path) -> Engine:
    """Open SQLite for many concurrent readers/writers in one process.

    SQLAlchemy's default QueuePool (size 5, overflow 10) is a poor fit for file-backed
    SQLite: WAL and ``busy_timeout`` handle contention, while a small pool makes portal,
    health, MCP and several worker lanes block on ``QueuePool`` timeouts under load.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    return engine


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "vibesynergy.store:migrations")
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    cfg = _alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def new_revision(engine: Engine, message: str) -> None:
    """Autogenerate a migration from schema.py changes (development only)."""
    cfg = _alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.revision(cfg, message=message, autogenerate=True)
