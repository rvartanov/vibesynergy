"""Bug reports: kept on this server, filed as GitHub issues when a token is set up."""

from __future__ import annotations

import base64
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from vibesynergy import bugs
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


def test_kept_without_a_token_then_sent(runtime) -> None:
    calls = []

    def post(url, token, body):
        calls.append((url, token, body))
        return {"html_url": "https://github.com/rvartanov/vibesynergy/issues/7"}

    report = {
        "description": "The Sleep page is empty\nsince this morning",
        "page": "#/sleep",
        "errors": ["Could not load this page: 500"],
        "failed_requests": [{"method": "GET", "url": "api/sleep", "status": 500, "error": "boom"}],
        "browser": "Chrome",
        "screenshot": PNG,
    }
    kept = bugs.create(runtime.settings, report, post=post)
    assert kept["issue_url"] is None and "no GitHub account" in kept["send_error"] and not calls
    assert kept["screenshot"].endswith(".png")
    bugs.save_token(runtime.settings, " ghp_test ")
    sent = bugs.send(runtime.settings, kept["id"], post=post)
    assert sent["issue_url"].endswith("/issues/7") and sent["send_error"] is None
    url, token, body = calls[0]
    assert url.endswith("/repos/rvartanov/vibesynergy/issues") and token == "ghp_test"
    assert body["title"] == "Bug: The Sleep page is empty"
    assert "`GET api/sleep` → 500: boom" in body["body"] and "#/sleep" in body["body"]
    assert "Screenshot kept on this server" in body["body"]
    assert bugs.send(runtime.settings, kept["id"], post=post) == sent and len(calls) == 1
    assert [r["id"] for r in bugs.listing(runtime.settings)] == [kept["id"]]


def test_a_github_failure_keeps_the_report(runtime) -> None:
    bugs.save_token(runtime.settings, "t")

    def post(url, token, body):
        raise RuntimeError("GitHub said 401: Bad credentials")

    kept = bugs.create(runtime.settings, {"description": "x"}, post=post)
    assert kept["issue_url"] is None and "401" in kept["send_error"]
    with pytest.raises(ValueError):
        bugs.create(runtime.settings, {"description": "  "})
    with pytest.raises(ValueError):
        bugs.create(runtime.settings, {"description": "x", "screenshot": "data:text/html,<b>"})
    with pytest.raises(ValueError):
        bugs.load(runtime.settings, "../../etc/passwd")


def test_bug_report_api(runtime) -> None:
    settings = replace(runtime.settings, server={"public_url": f"https://{TAILNET}"})
    rt = Runtime.open(settings)
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    assert client.post("/api/bugs", json={"description": "x"}).status_code == 403
    reply = client.post("/api/bugs", json={"description": "Broken", "page": "#/x"}, headers=WRITE)
    assert reply.status_code == 200 and reply.json()["data"]["issue_url"] is None
    data = client.get("/api/bugs").json()["data"]
    assert data["token_set"] is False and data["reports"][0]["description"] == "Broken"
    assert client.post("/api/bugs/token", json={"value": "t"}, headers=WRITE).json()["data"][
        "token_set"
    ]
    assert client.post("/api/bugs/bug_000000000000/send", headers=WRITE).status_code == 400
    rt.close()


def test_the_repository_comes_from_config(runtime) -> None:
    calls = []
    settings = replace(runtime.settings, bugs={"repo": "someone/fork"})
    bugs.save_token(settings, "t")
    bugs.create(settings, {"description": "x"}, post=lambda url, *_: calls.append(url) or {})
    assert calls == ["https://api.github.com/repos/someone/fork/issues"]
    with pytest.raises(ValueError):
        bugs.repo(replace(runtime.settings, bugs={"repo": "../x"}))


def test_connect_a_github_account_with_the_device_flow(runtime) -> None:
    settings = replace(runtime.settings, bugs={"github_client_id": "Iv1.abc"})
    with pytest.raises(ValueError):
        bugs.start_connect(runtime.settings)  # no app set up for this install
    answers = iter(
        [
            {"device_code": "dev", "user_code": "ABCD-1234", "interval": 5, "expires_in": 900},
            {"error": "authorization_pending"},
            {"access_token": "ghu_user"},
        ]
    )
    sent = []

    def post(url, body):
        sent.append((url, body))
        return next(answers)

    start = bugs.start_connect(settings, post=post)
    assert start["user_code"] == "ABCD-1234" and sent[0][1] == {"client_id": "Iv1.abc"}
    assert bugs.poll_connect(settings, post=post) == {"status": "pending"}
    done = bugs.poll_connect(settings, post=post, get=lambda url, token: {"login": "reporter"})
    assert done == {"status": "done", "login": "reporter"}
    assert sent[-1][1]["device_code"] == "dev"
    assert bugs.connection(settings) == {"kind": "account", "login": "reporter"}
    assert bugs.poll_connect(settings, post=post) == {"status": "expired"}  # code spent

    def refused(url, token, body):
        assert token == "ghu_user"
        raise RuntimeError("GitHub said 401: Bad credentials")

    kept = bugs.create(settings, {"description": "x"}, post=refused)
    assert "connect your account again" in kept["send_error"]
    bugs.disconnect(settings)
    assert bugs.connection(settings) == {"kind": None, "login": None}


def test_a_refused_device_code(runtime) -> None:
    settings = replace(runtime.settings, bugs={"github_client_id": "Iv1.abc"})
    bugs.start_connect(
        settings, post=lambda url, body: {"device_code": "d", "user_code": "X", "interval": 5}
    )
    assert bugs.poll_connect(settings, post=lambda url, body: {"error": "access_denied"}) == {
        "status": "denied"
    }
