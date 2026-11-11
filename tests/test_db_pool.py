"""SQLite engine pooling: one shared engine across portal, API, workers and MCP."""

from __future__ import annotations

import os
import socket
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.pool import NullPool

from vibesynergy.store import db


def test_create_db_engine_uses_null_pool(settings) -> None:
    engine = db.create_db_engine(settings.db_path)
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        engine.dispose()


def test_many_concurrent_connections_do_not_exhaust_pool(runtime) -> None:
    assert isinstance(runtime.engine.pool, NullPool)

    def ping() -> int:
        with runtime.engine.connect() as conn:
            return conn.exec_driver_sql("SELECT 1").scalar_one()

    with ThreadPoolExecutor(max_workers=32) as pool:
        assert list(pool.map(lambda _: ping(), range(32))) == [1] * 32


def test_recover_stale_jobs_before_workers(tmp_path, monkeypatch) -> None:
    from vibesynergy.core.config import load_settings
    from vibesynergy.runtime import Runtime
    from vibesynergy.sync.worker import Worker, recover_stale_jobs

    config = tmp_path / "config.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{tmp_path / "data"}"\n[user]\ntimezone = "Europe/Amsterdam"\n'
        "[sources.demo]\nenabled = true\n"
    )
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    rt = Runtime.open(load_settings(config))
    rt.migrate()
    try:
        job_id = rt.jobs.enqueue("sync", {"source": "demo"})
        dead_owner = f"{socket.gethostname()}:{os.getpid() + 999_999}"
        rt.jobs.claim(dead_owner)
        assert rt.jobs.recent()[0]["status"] == "running"
        assert recover_stale_jobs(rt.jobs) == 1
        worker = Worker(rt.jobs, {"sync": lambda _job: None})
        assert worker.run_once()
        assert rt.jobs.recent()[0]["status"] == "done"
        assert rt.jobs.recent()[0]["id"] == job_id
    finally:
        rt.close()
