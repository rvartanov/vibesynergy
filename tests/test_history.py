"""The history filler: API runs month by month through the job queue, account archives in
the background, and the portal's controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from test_import import RIDE, export_zip

from vibesynergy import dropfolder, history
from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import activity, daily_metric, job
from vibesynergy.sync.engine import DatasetResult

TODAY = date(2026, 9, 28)
TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}


def drain(rt, limit: int = 50) -> int:
    """Run every job that is due; returns how many ran."""
    worker = rt.worker()
    ran = 0
    while ran < limit and worker.run_once():
        ran += 1
    return ran


def days_with_steps(rt, window: Window) -> int:
    with rt.engine.connect() as conn:
        return conn.execute(
            select(func.count()).where(
                daily_metric.c.metric == "daily.steps",
                daily_metric.c.date.between(window.start, window.end),
            )
        ).scalar()


def fake_sync(monkeypatch, rt, outcome):
    """Replace the sync with ``outcome(month) -> list[DatasetResult] | Exception``."""
    calls = []

    def sync(source, datasets, window, **kwargs):
        calls.append(window.start.strftime("%Y-%m"))
        result = outcome(window.start)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(rt.sync, "sync", sync)
    return calls


def ok(name="daily"):
    return DatasetResult("demo", name, None)


def failed(name="daily", retry_at=None):
    result = DatasetResult("demo", name, None)
    result.error, result.retry_at = "boom", retry_at
    return result


def test_an_api_fill_runs_month_by_month(runtime) -> None:
    fill = history.start_api(runtime, "demo", date(2026, 1, 15), date(2026, 3, 10), today=TODAY)
    assert (fill["status"], fill["total"], fill["next_month"]) == ("running", 3, "2026-01-01")
    assert drain(runtime) == 4  # three months, then the step that finishes
    done = history.get(runtime, fill["id"])
    assert (done["status"], done["done"], done["progress"]) == ("done", 3, 1.0)
    # only the period asked for, and no current-state snapshots
    assert days_with_steps(runtime, Window(date(2026, 1, 15), date(2026, 3, 10))) == 55
    assert days_with_steps(runtime, Window(date(2026, 1, 1), date(2026, 1, 14))) == 0
    assert list(runtime.raw.current_ids("demo", "profile")) == []

    with pytest.raises(ValueError, match="future"):
        history.start_api(runtime, "demo", date(2026, 1, 1), date(2026, 12, 1), today=TODAY)
    with pytest.raises(ValueError, match="not an enabled source"):
        history.start_api(runtime, "garmin", date(2026, 1, 1), date(2026, 2, 1), today=TODAY)


def test_partial_months_are_fetched_again_at_the_end(runtime, monkeypatch) -> None:
    tries = {"2026-02": 0}

    def outcome(month):
        key = month.strftime("%Y-%m")
        if key == "2026-02":
            tries[key] += 1
            if tries[key] == 1:
                return [ok(), failed("other")]
        return [ok(), ok("other")]

    calls = fake_sync(monkeypatch, runtime, outcome)
    fill = history.start_api(runtime, "demo", date(2026, 1, 1), date(2026, 3, 31), today=TODAY)
    drain(runtime)
    done = history.get(runtime, fill["id"])
    assert calls == ["2026-01", "2026-02", "2026-03", "2026-02"]  # once more at the end
    assert (done["status"], done["partial"], done["retry"]) == ("done", [], [])


def test_a_rate_limit_waits_and_other_failures_get_stuck(runtime, monkeypatch) -> None:
    reset = utc_now() + timedelta(hours=5)
    calls = fake_sync(monkeypatch, runtime, lambda m: [failed(retry_at=reset)])
    fill = history.start_api(runtime, "demo", date(2026, 1, 1), date(2026, 1, 31), today=TODAY)
    drain(runtime)
    waiting = history.get(runtime, fill["id"])
    assert waiting["status"] == "waiting" and "API limit" in waiting["message"]
    with runtime.engine.connect() as conn:
        queued = conn.execute(select(job.c.run_after).where(job.c.status == "queued")).scalar()
    assert abs((queued - reset).total_seconds()) < 1  # the next try waits for the reset
    history.run_step(runtime, fill["id"])  # an early job does nothing
    assert calls == ["2026-01"]

    sent = []
    monkeypatch.setattr(
        "vibesynergy.alerts.deliver", lambda rt, subject, body: sent.append(subject)
    )
    fake_sync(monkeypatch, runtime, lambda m: ConnectionError("no network"))
    history.act(runtime, fill["id"], "pause")
    history.act(runtime, fill["id"], "resume")
    for _ in range(history.STUCK_AFTER):
        history.run_step(runtime, fill["id"])  # the 30-minute waits skipped
    stuck = history.get(runtime, fill["id"])
    assert stuck["status"] == "stuck" and "no network" in stuck["last_error"]
    assert sent == ["VibeSynergy: filling demo stuck"]


def test_pause_resume_and_cancel(runtime, monkeypatch) -> None:
    calls = fake_sync(monkeypatch, runtime, lambda m: [ok()])
    fill = history.start_api(runtime, "demo", date(2026, 1, 1), date(2026, 6, 30), today=TODAY)
    history.act(runtime, fill["id"], "pause")
    drain(runtime)
    assert calls == [] and history.get(runtime, fill["id"])["status"] == "paused"
    with pytest.raises(ValueError, match="already being filled"):
        history.start_api(runtime, "demo", date(2025, 1, 1), date(2025, 2, 1), today=TODAY)
    with pytest.raises(ValueError, match="this run is paused"):
        history.act(runtime, fill["id"], "pause")
    history.act(runtime, fill["id"], "resume")
    history.run_step(runtime, fill["id"])
    assert calls == ["2026-01"]
    history.act(runtime, fill["id"], "cancel")
    drain(runtime)
    assert calls == ["2026-01"] and history.get(runtime, fill["id"])["status"] == "cancelled"
    with pytest.raises(ValueError, match="no partial months"):
        history.act(runtime, fill["id"], "retry")


def test_suggestion(runtime) -> None:
    assert history.suggest(runtime, "demo", TODAY) == {
        "source": "demo",
        "start": "2016-01-01",
        "end": str(TODAY),
        "months": 129,
    }
    runtime.sync.sync("demo", ["daily"], Window(date(2020, 3, 1), date(2020, 3, 2)))
    assert history.suggest(runtime, "demo", TODAY)["end"] == "2020-02-29"  # up to what's there


def settle(rt, fill_id: int) -> dict:
    drain(rt)
    return history.get(rt, fill_id)


def test_an_archive_in_the_drop_folder_is_imported_in_the_background(runtime, tmp_path) -> None:
    inbox = dropfolder.folder(runtime.settings)
    export = export_zip(tmp_path).rename(inbox / "garmin-export.zip")
    started = dropfolder.scan(runtime, settle_s=0)
    assert [r["status"] for r in started] == ["archive import started"]
    fill = settle(runtime, history.fills(runtime)[0]["id"])
    assert fill["status"] == "done" and fill["name"] == "Garmin account export"
    assert fill["file"].endswith("garmin-export.zip")
    # a summary of what the export brought: activities, their dates, days of daily data
    assert fill["message"] == "done: 1 activity from 3-5-2020 to 3-5-2020, daily data for 1 day"
    assert not export.exists()
    with runtime.engine.connect() as conn:
        assert conn.execute(select(activity.c.source_id)).scalars().all() == ["5000000001"]
    assert dropfolder.scan(runtime, settle_s=0) == []  # nothing left to do

    broken = inbox / "strava.zip"
    _zip_to(broken, {"export_1/activities.csv": b"no,such,columns\n1,2,3\n"})
    dropfolder.scan(runtime, settle_s=0)
    failed = settle(runtime, history.fills(runtime)[0]["id"])
    assert failed["status"] == "running" and failed["message"] == history.ARCHIVE_RETRY
    assert broken.exists()  # retryable failure retains the zip


def test_a_restart_continues_an_archive_import(runtime, tmp_path) -> None:
    path = export_zip(tmp_path)
    fill_id = history._insert(
        runtime, source="garmin_export", method="archive", status="running", file=str(path)
    )
    assert history.resume_archives(runtime) == 1
    assert settle(runtime, fill_id)["status"] == "done"
    assert path.exists()  # outside the upload inbox: left where it was


def test_uploaded_archive_is_deleted_after_success(runtime, tmp_path) -> None:
    path = export_zip(tmp_path).rename(dropfolder.folder(runtime.settings) / "export.zip")
    started = history.start_archive(runtime, path)
    assert settle(runtime, started["id"])["status"] == "done"
    assert not path.exists()
    assert list(runtime.raw.current_ids("garmin_export", "fit"))


def test_corrupt_zip_is_deleted_and_symlink_is_untouched(runtime, tmp_path) -> None:
    inbox = dropfolder.folder(runtime.settings)
    bad = inbox / "bad.zip"
    bad.write_bytes(b"not a zip")
    result = dropfolder.scan(runtime, settle_s=-1)
    assert result[0]["status"] == "failed" and not bad.exists()
    outside = tmp_path / "private.zip"
    outside.write_bytes(b"private")
    link = inbox / "link.zip"
    link.symlink_to(outside)
    assert not dropfolder.delete_archive(runtime.settings, link)
    assert outside.read_bytes() == b"private"
    assert dropfolder.scan(runtime, settle_s=-1) == []


def test_loose_activity_zip_is_imported_then_deleted(runtime) -> None:
    path = dropfolder.folder(runtime.settings) / "activities.zip"
    path.write_bytes(_zip_bytes({"ride.fit": RIDE}))
    result = dropfolder.scan(runtime, settle_s=-1)
    assert result[0]["status"] == "imported"
    assert not path.exists()
    assert list(runtime.raw.current_ids("files", "fit"))


def _due_archive_retry(rt) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            update(job)
            .where(job.c.kind == history.ARCHIVE_JOB, job.c.status == "queued")
            .values(run_after=datetime(2000, 1, 1, tzinfo=UTC))
        )


def test_archive_retry_then_success(runtime, tmp_path, monkeypatch) -> None:
    from vibesynergy import imports

    path = export_zip(tmp_path).rename(dropfolder.folder(runtime.settings) / "retry.zip")
    real = imports.import_garmin_export
    calls = 0

    def once(rt, file, progress=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary")
        return real(rt, file, progress=progress)

    monkeypatch.setattr(imports, "import_garmin_export", once)
    fill = history.start_archive(runtime, path)
    assert runtime.worker(lane="archive").run_once()
    assert path.exists() and history.get(runtime, fill["id"])["status"] == "running"
    _due_archive_retry(runtime)
    assert runtime.worker(lane="archive").run_once()
    assert calls == 2 and history.get(runtime, fill["id"])["status"] == "done"
    assert not path.exists()


def test_archive_final_failure_removes_file_and_reports_status(
    runtime, tmp_path, monkeypatch, caplog
) -> None:
    from vibesynergy import imports

    path = export_zip(tmp_path).rename(dropfolder.folder(runtime.settings) / "fails.zip")

    def broken(_rt, _file, progress=None):
        raise OSError("sensitive archive detail")

    monkeypatch.setattr(imports, "import_garmin_export", broken)
    fill = history.start_archive(runtime, path)
    for attempt in range(history.STUCK_AFTER):
        _due_archive_retry(runtime)
        assert runtime.worker(lane="archive").run_once()
        if attempt < history.STUCK_AFTER - 1:
            assert path.exists()
    result = history.get(runtime, fill["id"])
    assert result["status"] == "failed" and result["message"] == history.ARCHIVE_FAILURE
    assert result["last_error"] is None and not path.exists()
    assert "archive import failed; uploaded file removed" in caplog.text


def test_crash_orphans_follow_job_state(runtime, tmp_path) -> None:
    inbox = dropfolder.folder(runtime.settings)
    done = inbox / "done.zip"
    retry = export_zip(tmp_path).rename(inbox / "running.zip")
    done.write_bytes(b"left after crash")
    history._insert(
        runtime, source="garmin_export", method="archive", status="done", file=str(done)
    )
    active = history._insert(
        runtime, source="garmin_export", method="archive", status="running", file=str(retry)
    )
    assert history.resume_archives(runtime) == 1
    assert not done.exists() and retry.exists()
    assert settle(runtime, active)["status"] == "done" and not retry.exists()


def test_crash_after_final_job_failure_removes_orphan(runtime, tmp_path) -> None:
    path = export_zip(tmp_path).rename(dropfolder.folder(runtime.settings) / "dead.zip")
    fill = history._insert(
        runtime, source="garmin_export", method="archive", status="running", file=str(path)
    )
    with runtime.engine.begin() as conn:
        conn.execute(
            job.insert().values(
                kind=history.ARCHIVE_JOB,
                payload={"fill": fill},
                status="dead",
                priority=history.PRIORITY,
                run_after=datetime(2000, 1, 1, tzinfo=UTC),
                attempts=history.STUCK_AFTER,
                max_attempts=history.STUCK_AFTER,
                created_at=utc_now(),
                finished_at=utc_now(),
            )
        )
    assert history.resume_archives(runtime) == 0
    assert not path.exists()
    assert history.get(runtime, fill)["status"] == "failed"


@pytest.fixture
def client(runtime):
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"})
    rt = Runtime.open(settings)
    yield rt, TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    rt.close()


def test_portal_history(client, tmp_path) -> None:
    rt, http = client
    page = http.get("/api/history").json()["data"]
    assert page["fills"] == [] and page["sources"][0]["source"] == "demo"
    today = datetime.now(UTC).date()
    body = {"source": "demo", "start": str(today - timedelta(days=40)), "end": str(today)}
    fill = http.post("/api/history/start", json=body, headers=WRITE).json()["data"]
    assert fill["status"] == "running"
    paused = http.post(f"/api/history/{fill['id']}/pause", headers=WRITE).json()["data"]
    assert paused["status"] == "paused"
    bad = http.post(f"/api/history/{fill['id']}/explode", headers=WRITE)
    assert bad.status_code == 400
    assert http.post("/api/history/start", json=body).status_code == 403  # no write header

    upload = http.post(
        "/api/history/upload",
        content=export_zip(tmp_path).read_bytes(),
        headers={**WRITE, "x-filename": "my%20export.zip"},
    )
    started = upload.json()["data"]
    assert started["source"] == "garmin_export" and started["file"].endswith("my export.zip")
    assert settle(rt, started["id"])["status"] == "done"

    not_export = http.post(
        "/api/history/upload",
        content=_zip_bytes({"ride.txt": b"hi"}),
        headers={**WRITE, "x-filename": "rides.zip"},
    )
    assert not_export.status_code == 400 and "Import files" in not_export.json()["error"]
    leftovers = [p.name for p in dropfolder.folder(rt.settings).iterdir() if p.is_file()]
    assert leftovers == []  # no half-written .part file stays behind


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _zip_to(path, files: dict[str, bytes]) -> None:
    path.write_bytes(_zip_bytes(files))


def test_a_pause_while_a_month_runs_stands(runtime, monkeypatch) -> None:
    fill = history.start_api(runtime, "demo", date(2026, 1, 1), date(2026, 6, 30), today=TODAY)

    def outcome(month):  # Alex presses Pause while January is being fetched
        history.act(runtime, fill["id"], "pause")
        return [ok()]

    fake_sync(monkeypatch, runtime, outcome)
    drain(runtime)
    paused = history.get(runtime, fill["id"])
    assert paused["status"] == "paused" and paused["done"] == 1  # January kept, no next job
    assert paused["next_month"] == "2026-02-01"
    with runtime.engine.connect() as conn:
        assert conn.execute(select(func.count()).where(job.c.status == "queued")).scalar() == 0


def test_estimates_and_recommendations(runtime, monkeypatch) -> None:
    demo = history.estimate(runtime, "demo", TODAY)  # one daily dataset, no export
    assert demo["per_year_hours"] == 0.1 and demo["recommend"] == "api" and demo["export"] is None

    source = runtime.registry.get("demo")
    monkeypatch.setattr(history, "EXPORTS", {"demo": {"archive": "x", "url": "u", "steps": "s"}})
    monkeypatch.setitem(source.ctx.config, "request_interval", 30.0)  # slow: 3 h a year
    slow = history.estimate(runtime, "demo", TODAY)
    assert slow["hours"] > history.EXPORT_ABOVE_HOURS and slow["recommend"] == "export"

    runtime.sync.sync("demo", ["daily"], Window(date(2016, 1, 1), date(2016, 1, 1)))
    filled = history.estimate(runtime, "demo", TODAY)  # synced back to the account start
    assert filled["period"] is None and filled["recommend"] is None


def test_strava_has_no_api_estimate_only_the_archive(runtime) -> None:
    e = history.estimate(runtime, "strava", TODAY)
    assert e["api_off"] and e["recommend"] == "export" and e["export"]["archive"] == "strava_export"


def test_a_resumable_upload(client, tmp_path, monkeypatch) -> None:
    from vibesynergy import uploads

    # CI /tmp is a small tmpfs; resuming an upload must not depend on the 5 GiB spare.
    monkeypatch.setattr(uploads, "check_space", lambda path, size: None)
    rt, http = client
    data = export_zip(tmp_path).read_bytes()
    started = http.post(
        "/api/history/uploads", json={"name": "export.zip", "size": len(data)}, headers=WRITE
    ).json()["data"]
    upload = f"/api/history/uploads/{started['id']}"
    half = len(data) // 2
    first = http.post(f"{upload}?offset=0", content=data[:half], headers=WRITE).json()["data"]
    assert first["received"] == half
    # the connection dropped and the page lost count: the server says where it is
    wrong = http.post(f"{upload}?offset=0", content=data[:half], headers=WRITE)
    assert wrong.status_code == 409 and wrong.json()["received"] == half
    assert http.get(upload).json()["data"]["received"] == half
    early = http.post(f"{upload}/finish", headers=WRITE)
    assert early.status_code == 400 and "of" in early.json()["error"]
    http.post(f"{upload}?offset={half}", content=data[half:], headers=WRITE)
    fill = http.post(f"{upload}/finish", headers=WRITE).json()["data"]
    assert fill["source"] == "garmin_export"
    assert settle(rt, fill["id"])["status"] == "done"
    assert http.get(upload).status_code == 400  # finished: gone
    assert http.get("/api/history/uploads/..%2Fsecrets").status_code in (400, 404)

    too_big = http.post(
        "/api/history/uploads", json={"name": "x.zip", "size": 300 * 1024**3}, headers=WRITE
    )
    assert too_big.status_code == 400


def test_an_upload_on_the_garmin_card_must_be_the_garmin_export(client, tmp_path, monkeypatch):
    """The Garmin card asks for the Garmin export: a Strava archive there is refused (in the
    page's language) and removed, and the Garmin export is imported as one."""
    from vibesynergy import uploads

    monkeypatch.setattr(uploads, "check_space", lambda path, size: None)
    rt, http = client

    def upload(data: bytes, expect: str, lang: str = "en"):
        started = http.post(
            "/api/history/uploads", json={"name": "export.zip", "size": len(data)}, headers=WRITE
        ).json()["data"]
        url = f"/api/history/uploads/{started['id']}"
        http.post(f"{url}?offset=0", content=data, headers=WRITE)
        return http.post(f"{url}/finish?lang={lang}", json={"expect": expect}, headers=WRITE)

    strava = _zip_bytes({"export_1/activities.csv": b"Activity ID\n"})
    wrong = upload(strava, "garmin_export", lang="nl")
    assert wrong.status_code == 400 and "Garmin-export" in wrong.json()["error"]
    leftovers = [p.name for p in dropfolder.folder(rt.settings).iterdir() if p.is_file()]
    assert leftovers == []
    assert http.get("/api/history").json()["data"]["fills"] == []

    fill = upload(export_zip(tmp_path).read_bytes(), "garmin_export").json()["data"]
    assert fill["source"] == "garmin_export"
    done = settle(rt, fill["id"])
    assert done["status"] == "done" and done["done"] == done["total"]

    assert upload(strava, "nonsense").status_code in (400, 422)


def test_a_large_export_needs_free_space(client, monkeypatch) -> None:
    """Several GB is fine, as long as the disk keeps room to spare; otherwise a plain message
    (in the page's language) says how much to free up, before any byte is sent."""
    import shutil
    from collections import namedtuple

    from vibesynergy import uploads

    rt, http = client
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(100 * 1024**3, 0, 10 * 1024**3))
    ok = http.post(
        "/api/history/uploads", json={"name": "export.zip", "size": 4 * 1024**3}, headers=WRITE
    )
    assert ok.status_code == 200
    full = http.post(
        "/api/history/uploads?lang=nl",
        json={"name": "export.zip", "size": 8 * 1024**3},
        headers=WRITE,
    )
    assert full.status_code == 400
    assert "niet genoeg vrije ruimte" in full.json()["error"] and "3 GB" in full.json()["error"]
    assert len(list(uploads._dir(rt.settings).glob("*.part"))) == 1  # only the one that fit


def test_old_unfinished_uploads_are_cleaned(runtime, monkeypatch) -> None:
    import time as clock

    from vibesynergy import uploads

    # CI /tmp is a small tmpfs; creating the uploads must not depend on the 5 GiB spare.
    monkeypatch.setattr(uploads, "check_space", lambda path, size: None)
    kept = uploads.start(runtime.settings, "a.zip", 10)["id"]
    old = uploads.start(runtime.settings, "b.zip", 10)["id"]
    assert uploads.clean(runtime.settings, now=clock.time() + uploads.KEPT_S + 60) == 2
    assert uploads.clean(runtime.settings) == 0
    for upload_id in (kept, old):
        with pytest.raises(ValueError, match="unknown upload"):
            uploads.status(runtime.settings, upload_id)
