"""About this version: footer and Settings → System read what is running."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import about


def test_about_reports_version_commit_schema_and_deploy(runtime) -> None:
    info = about.about(runtime)
    assert info["version"]
    assert info["commit"] and info["commit_short"] == info["commit"][:7]
    assert info["commit_url"].endswith(info["commit"])
    assert info["schema"] == info["schema_head"]
    assert info["started_at"] and info["uptime_s"] >= 0
    assert info["deploy"] is None
    (runtime.settings.data_dir / "deploy.json").write_text(
        json.dumps(
            {
                "at": "2026-11-02T09:06:05+01:00",
                "commit": "7674c57",
                "result": "ok",
                "migrated": True,
                "token": "never shown",
            }
        )
    )
    deploy = about.about(runtime)["deploy"]
    assert deploy == {
        "at": "2026-11-02T09:06:05+01:00",
        "commit": "7674c57",
        "result": "ok",
        "migrated": True,
    }


def test_broken_deploy_record_is_ignored(runtime) -> None:
    (runtime.settings.data_dir / "deploy.json").write_text("not json")
    assert about.last_deploy(runtime.settings.data_dir) is None


def test_portal_endpoints(runtime) -> None:
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/about").json()["data"]
    assert data["commit_short"] and data["schema"]
    version = client.get("/version").json()
    assert version["build_id"] and version["commit"] == data["commit_short"]
    assert version["version"] == data["version"]


def test_updates_counts_commits_and_lists_pr_titles(monkeypatch) -> None:
    """phase 2: how far main is ahead, cached for an hour."""
    from vibesynergy.query import about

    calls = []

    def fake_git(*args):
        calls.append(args[0])
        if args[0] == "log":
            return "Plan model: tables\n\nGarmin: swim"
        if args[0] == "rev-list":
            return "5"
        return ""

    monkeypatch.setattr(about, "_git", fake_git)
    about._UPDATES.update(at=0.0, value=None)
    out = about.updates(now=10_000.0)
    assert out and out["behind"] == 5
    assert out["titles"] == ["Plan model: tables", "Garmin: swim"]
    calls.clear()
    assert about.updates(now=10_100.0) is out and calls == []  # cached
    assert about.updates(now=10_010.0, fresh=True) is out and calls == []  # asked seconds ago
    fresh = about.updates(now=10_100.0, fresh=True)  # Check for updates: asks again
    assert fresh is not out and fresh["behind"] == 5 and "fetch" in calls


def test_update_now_starts_the_unit_only_when_it_may(runtime, monkeypatch) -> None:
    """phase 3: the unit, not the service, runs the deploy; refusals first."""
    import pytest

    state = {"LoadState": "loaded", "ActiveState": "inactive", "Result": "success"}
    started: list[tuple] = []

    def fake_systemctl(*args):
        if args[0] == "show":
            return "\n".join(f"{k}={v}" for k, v in state.items())
        started.append(args)
        return ""

    monkeypatch.setattr(about, "_systemctl", fake_systemctl)
    monkeypatch.setattr(about, "updates", lambda: {"behind": 2, "titles": [], "checked_at": ""})
    assert about.update_unit() == {
        "unit": about.UPDATE_UNIT,
        "running": False,
        "result": "success",
        "finished": None,
    }
    job_id = runtime.jobs.enqueue("sync", {})
    runtime.jobs.claim("w")
    with pytest.raises(ValueError, match="job is running"):
        about.start_update(runtime)
    runtime.jobs.complete(job_id)
    assert about.start_update(runtime) == {"started": about.UPDATE_UNIT, "behind": 2}
    assert started == [("start", "--no-block", about.UPDATE_UNIT)]
    state["ActiveState"] = "activating"
    with pytest.raises(ValueError, match="already running"):
        about.start_update(runtime)
    state.update(ActiveState="inactive")
    monkeypatch.setattr(about, "updates", lambda: {"behind": 0, "titles": [], "checked_at": ""})
    with pytest.raises(ValueError, match="up to date"):
        about.start_update(runtime)
    state["LoadState"] = "not-found"
    assert about.update_unit() is None
    with pytest.raises(ValueError, match="git pull"):
        about.start_update(runtime)
    assert len(started) == 1


def test_update_now_needs_the_passphrase(runtime, monkeypatch) -> None:
    monkeypatch.setattr(about, "_systemctl", lambda *args: (_ for _ in ()).throw(AssertionError))
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    reply = client.post(
        "/api/settings/update",
        json={"passphrase": "x"},
        headers={"x-vibesynergy": "portal", "origin": "http://localhost"},
    )
    assert reply.status_code == 400 and "passphrase" in reply.text


def test_last_update_log(runtime) -> None:
    reports = runtime.settings.data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "autodeploy-20261106-100000.log").write_text("old\n")
    (reports / "autodeploy-20261106-120000.log").write_text("a\nb\nhealth ok\n")
    out = about.last_update_log(runtime.settings.data_dir, lines=2)
    assert out == {"file": "autodeploy-20261106-120000.log", "tail": ["b", "health ok"]}
