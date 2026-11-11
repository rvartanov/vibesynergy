"""Bug reports: the bug button opens a new issue on GitHub, in the reporter's own account."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vibesynergy import bugs
from vibesynergy.portal.app import create_portal_app


def test_the_new_issue_page_and_its_repository(runtime) -> None:
    assert bugs.new_issue_url(runtime.settings) == (
        "https://github.com/rvartanov/vibesynergy/issues/new"
    )
    fork = replace(runtime.settings, bugs={"repo": "someone/fork"})
    assert bugs.new_issue_url(fork) == "https://github.com/someone/fork/issues/new"
    with pytest.raises(ValueError):
        bugs.repo(replace(runtime.settings, bugs={"repo": "../x"}))


def test_bug_api_says_where_to_go_and_keeps_nothing(runtime) -> None:
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    data = client.get("/api/bugs").json()["data"]
    assert data == {
        "repo": "rvartanov/vibesynergy",
        "new_issue_url": "https://github.com/rvartanov/vibesynergy/issues/new",
    }
    write = {"x-vibesynergy": "portal", "origin": "http://localhost"}
    assert client.post("/api/bugs", json={"description": "x"}, headers=write).status_code == 405
    assert not (runtime.settings.data_dir / "bug-reports").exists()
