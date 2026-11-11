"""The updater: what kind of install this is, whether GitHub has newer, and an update run."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from vibesynergy import updater

OLD, NEW = "a" * 40, "b" * 40


def _tool(monkeypatch, tmp_path: Path, commit: str = OLD) -> None:
    """Pretend to run from a uv tool install of ``commit``."""
    monkeypatch.delenv("VIBESYNERGY_CONTAINER", raising=False)
    monkeypatch.setattr(updater.sys, "prefix", str(tmp_path / "venv"))
    (tmp_path / "venv").mkdir(exist_ok=True)
    (tmp_path / "venv" / "uv-receipt.toml").write_text("")
    direct = {"url": "https://github.com/o/r", "vcs_info": {"vcs": "git", "commit_id": commit}}
    monkeypatch.setattr(updater, "_direct_url", lambda: direct)
    real = updater.installed
    monkeypatch.setattr(updater, "installed", lambda root=None: real(root or tmp_path / "pkg"))


def test_kinds_of_install(monkeypatch, tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("VIBESYNERGY_CONTAINER", raising=False)
    assert updater.installed(tmp_path)["kind"] == "checkout"
    _tool(monkeypatch, tmp_path)
    assert updater.installed() == {"kind": "tool", "url": "https://github.com/o/r", "commit": OLD}
    monkeypatch.setenv("VIBESYNERGY_CONTAINER", "1")
    monkeypatch.setenv("VIBESYNERGY_COMMIT", NEW)
    assert updater.installed()["kind"] == "container"
    assert "image" in (updater.how("container") or "")


def test_check_compares_with_main_on_github(monkeypatch, tmp_path) -> None:
    _tool(monkeypatch, tmp_path)
    monkeypatch.setattr(updater, "_titles", lambda url, base, head: ["Plan: faster"])
    news = updater.check(get_latest=lambda url: NEW)
    assert news and news["behind"] == 1 and news["titles"] == ["Plan: faster"]
    assert news["count_known"] and news["latest"] == NEW
    monkeypatch.setattr(updater, "_titles", lambda url, base, head: None)  # private repo
    news = updater.check(get_latest=lambda url: NEW)
    assert news and news["behind"] == 1 and not news["count_known"]
    assert updater.check(get_latest=lambda url: OLD)["behind"] == 0
    assert updater.check(get_latest=lambda url: None) is None  # GitHub unreachable


def test_latest_reads_ls_remote(monkeypatch) -> None:
    out = subprocess.CompletedProcess([], 0, f"{NEW}\trefs/heads/main\n", "")
    monkeypatch.setattr(updater, "_run", lambda args, timeout=30: out)
    assert updater.latest("https://github.com/o/r") == NEW


def _db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("create table t (x)")
        conn.execute("insert into t values (1)")


def test_an_update_run_backs_up_upgrades_migrates_and_restarts(
    runtime, monkeypatch, tmp_path
) -> None:
    _tool(monkeypatch, tmp_path)
    _db(runtime.settings.db_path)
    monkeypatch.setattr(updater, "_uv", lambda: "/bin/uv")
    monkeypatch.setattr(updater, "_new_commit", lambda: NEW)
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    calls = []

    def run(args, timeout=30):
        calls.append(args[1:3] if args[0] != "systemctl" else args[2:3])
        if args[:2] == ["systemctl", "--user"] and args[2] == "is-active":
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        return subprocess.CompletedProcess(args, 0, "ok\n", "")

    monkeypatch.setattr(updater, "_run", run)
    for _ in range(4):  # older backups: only the newest three stay
        record = updater.run(runtime.settings, restart=True, say=lambda text: None)
    assert record["result"] == "ok" and record["migrated"] and record["commit"] == NEW
    assert ["tool", "upgrade"] in calls and ["db", "upgrade"] in calls and ["restart"] in calls
    backups = list((runtime.settings.data_dir / "backups").glob("before-update-*.sqlite"))
    assert 1 <= len(backups) <= updater.KEEP_BACKUPS
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("select x from t").fetchone() == (1,)
    deploy = json.loads((runtime.settings.data_dir / "deploy.json").read_text())
    assert deploy["result"] == "ok"
    assert list((runtime.settings.data_dir / "reports").glob("update-*.log"))


def test_a_failed_migration_is_reported_and_keeps_the_backup(
    runtime, monkeypatch, tmp_path
) -> None:
    _tool(monkeypatch, tmp_path)
    _db(runtime.settings.db_path)
    monkeypatch.setattr(updater, "_uv", lambda: "/bin/uv")

    def run(args, timeout=30):
        code = 1 if args[1:3] == ["db", "upgrade"] else 0
        return subprocess.CompletedProcess(args, code, "boom\n", "")

    monkeypatch.setattr(updater, "_run", run)
    record = updater.run(runtime.settings, restart=True, say=lambda text: None)
    assert record["result"] == "failed" and not record["migrated"]
    assert Path(record["backup"]).exists()


def test_only_a_tool_install_updates_itself(monkeypatch, runtime, tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("VIBESYNERGY_CONTAINER", raising=False)
    real = updater.installed
    monkeypatch.setattr(updater, "installed", lambda root=None: real(tmp_path))
    with pytest.raises(ValueError, match="git pull"):
        updater.run(runtime.settings, restart=False)


def test_update_now_runs_in_its_own_unit(monkeypatch) -> None:
    seen = []

    def run(args, timeout=30):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(updater, "_run", run)
    updater.start()
    assert seen[0][:4] == ["systemd-run", "--user", f"--unit={updater.UNIT}", "--collect"]
    assert seen[0][-3:] == ["update", "--yes", "--restart"]
