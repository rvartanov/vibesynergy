# ruff: noqa: RUF001  (the changelog headings use an en dash)
"""Versions and "What changed" (#140): the order of the versions, the release that
`vibesynergy update` makes, the card once and then under Notices, and the items for AI apps
in the handbook and the MCP server information."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from vibesynergy import __version__, cut_release, releases, updater
from vibesynergy.cli.main import app
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_handbook, release_news
from vibesynergy.store.schema import auth_account, release_seen

ROOT = Path(__file__).resolve().parents[1]
NL = json.loads((ROOT / "src/vibesynergy/portal/static/i18n/nl.json").read_text())

FAKE_ITEMS = (
    releases.Item("1-old", "you", "Old item for you."),
    releases.Item("2-you", "you", "Item for you."),
    releases.Item("2-ai", "ai", "Item for AI apps."),
)
FAKE = (
    releases.Release("0.1.0-beta.0.3", date(2026, 12, 3), ("2-you", "2-ai")),
    releases.Release("0.1.0-beta.0.2", date(2026, 12, 2)),  # small fixes: a number only
    releases.Release("0.1.0-beta.0.1", date(2026, 12, 1), ("1-old",)),
)


@pytest.fixture
def fake_releases(monkeypatch):
    monkeypatch.setattr(releases, "RELEASES", FAKE)
    monkeypatch.setattr(releases, "ITEMS", FAKE_ITEMS)
    monkeypatch.setattr(releases, "VERSION", FAKE[0].version)


# --- the versions -----------------------------------------------------------------------------


def test_versions_are_newest_first_and_every_item_is_known() -> None:
    keys = [releases.version_key(r.version) for r in releases.RELEASES]
    assert keys == sorted(keys, reverse=True) and len(set(keys)) == len(keys)
    days = [r.day for r in releases.RELEASES]
    assert days == sorted(days, reverse=True)
    assert releases.VERSION == releases.RELEASES[0].version == __version__
    ids = [item.id for item in releases.ITEMS]
    assert len(ids) == len(set(ids))
    taken = [i for r in releases.RELEASES for i in r.items]
    assert len(taken) == len(set(taken)) and set(taken) <= set(ids)
    assert all(item.audience in releases.AUDIENCES for item in releases.ITEMS)
    assert releases.RELEASES[-1].version == releases.FIRST


def test_version_order_and_the_next_number() -> None:
    order = ["0.1.0-beta.0.1", "0.1.0-beta.0.2", "0.1.0-beta.0.10", "0.1.0-beta.1.1", "0.1.0"]
    order += ["0.1.1", "0.2.0-beta.0.1", "1.0.0"]
    assert sorted(order, key=releases.version_key) == order
    assert releases.next_version(None) == "0.1.0-beta.0.1"
    assert releases.next_version("0.1.0-beta.0.9") == "0.1.0-beta.0.10"
    assert releases.next_version("1.0.0") == "1.0.1"
    with pytest.raises(ValueError):
        releases.version_key("0.0.1a1")


def test_pyproject_keeps_a_pep440_version_for_the_line() -> None:
    from packaging.version import Version

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == releases.pep440(releases.VERSION) == "0.1.0b0"
    assert str(Version(project["version"])) == project["version"]


def test_every_item_has_a_dutch_text() -> None:
    missing = [item.text for item in releases.ITEMS if item.text not in NL]
    assert not missing, f"add to static/i18n/nl.json: {missing}"


def test_the_changelog_has_a_section_per_version() -> None:
    text = (ROOT / "CHANGELOG.md").read_text()
    assert "## Unreleased\n" in text
    for release in releases.RELEASES:
        day = f"{release.day.day}-{release.day.month}-{release.day.year}"
        assert f"## {release.version} {releases.DASH} {day}\n" in text


def test_version_everywhere(runtime) -> None:
    assert CliRunner().invoke(app, ["--version"]).output.strip() == releases.VERSION
    assert CliRunner().invoke(app, ["version"]).output.strip() == releases.VERSION
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    assert client.get("/version").json()["version"] == releases.VERSION
    assert client.get("/api/about").json()["data"]["version"] == releases.VERSION


# --- making a release -------------------------------------------------------------------------


def test_stamped_source_loads_and_keeps_its_format(tmp_path) -> None:
    text = (ROOT / "src/vibesynergy/releases.py").read_text()
    many = [f"{n}-a-rather-long-item-name" for n in range(141, 146)]
    for ids in ([], ["141-plan"], ["141-plan", "142-sleep"], many):
        stamped = releases.stamp_source(text, "0.1.0-beta.0.2", date(2026, 11, 23), ids)
        path = tmp_path / "releases.py"
        path.write_text(stamped)
        module = cut_release.load(path)
        assert module.VERSION == "0.1.0-beta.0.2"
        assert module.RELEASES[0].items == tuple(ids) and module.RELEASES[0].day.day == 23
        ruff = shutil.which("ruff")
        if ruff:
            out = subprocess.run(
                [ruff, "format", "--check", "--diff", str(path)],
                text=True,
                capture_output=True,
                cwd=ROOT,
            )
            assert out.returncode == 0, out.stdout + out.stderr


def test_stamped_changelog() -> None:
    text = "# Changelog\n\n## Unreleased\n\n- New thing (#141).\n\n## 0.1.0-beta.0.1 – 22-11-2026\n"
    out = releases.stamp_changelog(text, "0.1.0-beta.0.2", date(2026, 11, 23))
    assert out.startswith("# Changelog\n\n## Unreleased\n\n## 0.1.0-beta.0.2 – 23-11-2026\n\n")
    assert "- New thing (#141).\n\n## 0.1.0-beta.0.1" in out
    empty = releases.stamp_changelog(out, "0.1.0-beta.0.3", date(2026, 11, 24))
    assert (
        "## 0.1.0-beta.0.3 – 24-11-2026\n\nSmall fixes behind the scenes.\n\n## 0.1.0-beta.0.2"
        in empty
    )


GIT = [
    "git",
    "-c",
    "user.name=T",
    "-c",
    "user.email=t@example.test",
    "-c",
    "init.defaultBranch=main",
]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _add_item(work: Path, item_id: str, text: str, line: str) -> None:
    """What a pull request does: one item at the end of ITEMS, one line in the changelog."""
    path = work / cut_release.RELEASES_PY
    source = path.read_text()
    end = source.index("\n)\n", source.index("ITEMS: tuple[Item, ...] = ("))
    path.write_text(f'{source[:end]}\n    Item("{item_id}", "you", "{text}"),{source[end:]}')
    log = work / "CHANGELOG.md"
    log.write_text(log.read_text().replace("## Unreleased\n\n", f"## Unreleased\n\n- {line}\n", 1))
    _git(work, "commit", "-qam", f"PR {item_id}")
    _git(work, "push", "-q", "origin", "main")


@pytest.fixture
def origin(tmp_path) -> tuple[Path, Path]:
    """A bare 'GitHub' with this repository's releases.py and CHANGELOG.md, and a clone."""
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    (work / cut_release.RELEASES_PY).parent.mkdir(parents=True)
    shutil.copy(ROOT / cut_release.RELEASES_PY, work / cut_release.RELEASES_PY)
    shutil.copy(ROOT / "CHANGELOG.md", work / "CHANGELOG.md")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "start")
    _git(work, "push", "-q", "origin", "HEAD:main")
    return bare, work


def test_update_makes_one_release_of_several_pull_requests(origin) -> None:
    bare, work = origin
    # Items this branch adds itself (not released yet) come first.
    pending = releases.unreleased()
    _add_item(work, "141-plan", "Plan item.", "Plan line (#141).")
    _add_item(work, "142-sleep", "Sleep item.", "Sleep line (#142).")
    said: list[str] = []
    made = cut_release.cut(str(bare), today=date(2026, 11, 23), say=said.append)
    assert made == {
        "from": releases.VERSION,
        "to": releases.next_version(releases.VERSION),
        "version": releases.next_version(releases.VERSION),
        "new": True,
        "items": [*(i.text for i in pending), "Plan item.", "Sleep item."],
    }
    new = made["to"]
    assert said == []
    _git(work, "pull", "-q", "--tags", "origin", "main")
    assert _git(work, "log", "-1", "--format=%s").strip() == f"Release {new}"
    assert f"v{new}" in _git(work, "tag", "--points-at", "HEAD")
    module = cut_release.load(work / cut_release.RELEASES_PY)
    assert new == module.VERSION
    assert module.RELEASES[0].items == (*(i.id for i in pending), "141-plan", "142-sleep")
    assert module.RELEASES[0].day == date(2026, 11, 23) and not module.unreleased()
    log = (work / "CHANGELOG.md").read_text()
    head = f"## Unreleased\n\n## {new} – 23-11-2026\n\n- Sleep line (#142).\n- Plan line (#141).\n"
    assert head in log
    # The same main again (an update that failed after the release): no new number.
    again = cut_release.cut(str(bare), today=date(2026, 11, 24), say=said.append)
    assert again and again["to"] == new and not again["new"] and again["items"] == made["items"]


def test_without_push_rights_the_update_goes_on_without_a_number(origin) -> None:
    bare, _ = origin
    said: list[str] = []

    def refuse_push(args, cwd=None):
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 128, "", "Permission denied")
        return cut_release._git(args, cwd)

    assert (
        cut_release.cut(str(bare), today=date(2026, 11, 23), say=said.append, git=refuse_push)
        is None
    )
    assert said and said[0].startswith("No new version number") and "Permission denied" in said[0]
    assert "v0.1.0-beta.0.2" not in _git(bare, "tag")


def test_update_prints_old_to_new_and_the_items(runtime, monkeypatch, tmp_path) -> None:
    from test_updater import NEW, OLD, _db, _tool

    _tool(monkeypatch, tmp_path, OLD)
    _db(runtime.settings.db_path)
    monkeypatch.setattr(updater, "_uv", lambda: "/bin/uv")
    monkeypatch.setattr(updater, "_new_commit", lambda: NEW)
    monkeypatch.setattr(updater, "_service_active", lambda: False)
    order: list[str] = []

    def run(args, timeout=30):
        order.append(" ".join(args[1:3]))
        return subprocess.CompletedProcess(args, 0, "ok\n", "")

    def cut(url, *, today, say):
        order.append("cut")
        assert url == "https://github.com/o/r"
        return {
            "from": "0.1.0-beta.0.3",
            "to": "0.1.0-beta.0.4",
            "new": True,
            "items": ["A.", "B."],
        }

    monkeypatch.setattr(updater, "_run", run)
    said: list[str] = []
    record = updater.run(runtime.settings, restart=False, say=said.append, cut=cut)
    assert record["result"] == "ok" and record["release"]["to"] == "0.1.0-beta.0.4"
    assert order.index("cut") < order.index("tool upgrade")
    assert "0.1.0-beta.0.3 → 0.1.0-beta.0.4" in said and "  - A." in said and "  - B." in said


# --- the card once, then under Notices --------------------------------------------------------


def _account(runtime, created: datetime) -> int:
    with runtime.engine.begin() as conn:
        return conn.execute(
            auth_account.insert().values(
                email="tester@example.test", mfa="none", created_at=created, updated_at=created
            )
        ).inserted_primary_key[0]


def test_card_once_then_under_notices(runtime, fake_releases) -> None:
    key = _account(runtime, datetime(2026, 11, 1, tzinfo=UTC))
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    first = client.get("/api/releases/news").json()["data"]
    assert first["version"] == "0.1.0-beta.0.3"
    assert [r["version"] for r in first["releases"]] == ["0.1.0-beta.0.3", "0.1.0-beta.0.1"]
    texts = [i["text"] for r in first["releases"] for i in r["items"]]
    assert texts == ["Item for you.", "Old item for you."]  # not the one for AI apps
    assert client.get("/api/releases/news").json()["data"]["releases"] == []  # once
    with runtime.engine.connect() as conn:
        row = conn.execute(release_seen.select()).one()
    assert row.account_id == key and row.version == "0.1.0-beta.0.3"
    inbox = client.get("/api/inbox").json()["data"]
    assert [r["version"] for r in inbox["releases"]] == ["0.1.0-beta.0.3", "0.1.0-beta.0.1"]
    assert inbox["count"] == 0  # no decision, nothing on the bell


def test_only_versions_newer_than_seen_and_after_the_account(
    runtime, fake_releases, monkeypatch
) -> None:
    key = _account(runtime, datetime(2026, 11, 1, tzinfo=UTC))
    with runtime.engine.begin() as conn:
        conn.execute(
            release_seen.insert().values(
                account_id=key, version="0.1.0-beta.0.1", seen_at=datetime(2026, 12, 1, tzinfo=UTC)
            )
        )
    news = release_news.news(runtime.engine)
    assert [r["version"] for r in news["releases"]] == ["0.1.0-beta.0.3"]
    # A new version later: shown again, once.
    later = (releases.Release("0.1.0-beta.0.4", date(2026, 12, 9), ("2-you",)), *FAKE)
    monkeypatch.setattr(releases, "RELEASES", later)
    monkeypatch.setattr(releases, "VERSION", "0.1.0-beta.0.4")
    assert [r["version"] for r in release_news.news(runtime.engine)["releases"]] == [
        "0.1.0-beta.0.4"
    ]
    assert release_news.news(runtime.engine)["releases"] == []


def test_a_new_account_gets_no_old_news(runtime, fake_releases) -> None:
    _account(runtime, datetime(2026, 12, 5, tzinfo=UTC))
    assert release_news.news(runtime.engine)["releases"] == []
    assert len(release_news.history()) == 2  # still readable under Notices


# --- the items for AI apps --------------------------------------------------------------------


def test_items_for_ai_apps_in_the_handbook_and_the_mcp_information(runtime, fake_releases) -> None:
    data, _ = coach_handbook.handbook(runtime.engine, scopes=set(), include_protocol=False)
    assert data["what_changed"]["releases"] == [
        {
            "version": "0.1.0-beta.0.3",
            "date": "2026-12-03",
            "items": [{"id": "2-ai", "audience": "ai", "text": "Item for AI apps."}],
        }
    ]

    async def info():
        async with Client(build_server(runtime)) as client:
            return client.instructions, client.server_info

    instructions, server = anyio.run(info)
    assert "0.1.0-beta.0.3 (2026-12-03): Item for AI apps." in instructions
    assert "Item for you." not in instructions
    assert server.version == __version__


def test_the_real_ai_item_reaches_ai_apps() -> None:
    # The newest release with items for AI apps (the first one, "version number", has
    # dropped out of the last AI_RELEASES since 0.1.0-beta.0.8).
    texts = [i["text"] for r in release_news.for_ai() for i in r["items"]]
    _release, newest = releases.history("ai")[0]
    assert texts and all(item.text in texts for item in newest)
