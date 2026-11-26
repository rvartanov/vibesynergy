"""Request-body limit on what anyone can call: the public app and the portal's /auth/*."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from vibesynergy.api import oauth
from vibesynergy.api.app import create_app
from vibesynergy.api.body_limit import DEFAULT_LIMIT, BodyLimit
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import audit_log, oauth_client

HOST = "https://server.example.ts.net"
BIG = b"x" * (DEFAULT_LIMIT + 1)


@pytest.fixture
def public(runtime):
    settings = replace(runtime.settings, server={"public_url": HOST})
    rt = Runtime.open(settings)
    oauth.set_passphrase(SecretStore(settings.secrets_dir, "mcp"), "correct horse battery staple")
    # Valid webhook tokens, so the receivers get as far as reading the body.
    SecretStore(settings.secrets_dir, "withings").set("webhook", {"token": "tok"})
    SecretStore(settings.secrets_dir, "strava").set("webhook", {"token": "tok"})
    with TestClient(create_app(rt), base_url=HOST, follow_redirects=False) as client:
        yield rt, client
    rt.close()


def _chunks(data: bytes, size: int = 8192):
    for start in range(0, len(data), size):
        yield data[start : start + size]


@pytest.mark.parametrize(
    "path",
    [
        "/vibesynergy/register",
        "/vibesynergy/token",
        "/vibesynergy/approve",
        "/vibesynergy/withings/notify/tok",
        "/strava/webhook/tok",
    ],
)
def test_a_big_body_is_refused_with_413(public, path) -> None:
    _, client = public
    assert client.post(path, content=BIG).status_code == 413
    # Without Content-Length (chunked): the streamed bytes are counted.
    assert client.post(path, content=_chunks(BIG)).status_code == 413


def test_a_normal_registration_still_works(public) -> None:
    _, client = public
    reply = client.post(
        "/vibesynergy/register",
        content=_chunks(b'{"client_name": "App", "redirect_uris": ["https://app.example/cb"]}'),
        headers={"content-type": "application/json"},
    )
    assert reply.status_code == 201, reply.text


def test_registration_free_text_is_cut_before_it_is_stored(public) -> None:
    rt, client = public
    reply = client.post(
        "/vibesynergy/register",
        json={"client_name": "A" * 5000, "redirect_uris": ["https://app.example/cb"]},
    )
    assert reply.status_code == 201, reply.text
    with rt.engine.connect() as conn:
        info = conn.execute(select(oauth_client.c.info)).scalar_one()
        detail = conn.execute(
            select(audit_log.c.detail).where(audit_log.c.action == "oauth.register")
        ).scalar_one()
    assert info["client_name"] == "A" * oauth.MAX_CLIENT_TEXT
    assert detail["client_name"] == "A" * oauth.MAX_CLIENT_TEXT
    many = {"redirect_uris": [f"https://app.example/{i}" for i in range(50)]}
    assert client.post("/vibesynergy/register", json=many).status_code == 400


def test_anonymous_audit_rows_are_pruned(public) -> None:
    rt, client = public
    old = utc_now() - oauth.ANON_AUDIT_KEEP - timedelta(days=1)
    with rt.engine.begin() as conn:
        conn.execute(
            audit_log.insert(),
            [
                {"at": old, "actor": "oauth", "action": "oauth.register", "detail": {}},
                {"at": old, "actor": "oauth", "action": "oauth.approved", "detail": {}},
            ]
            + [
                {"at": utc_now(), "actor": "oauth", "action": "oauth.denied", "detail": {}}
                for _ in range(oauth.MAX_ANON_AUDIT_ROWS + 20)
            ],
        )
    client.post("/vibesynergy/register", json={"redirect_uris": ["https://app.example/cb"]})
    with rt.engine.connect() as conn:
        counts = dict(
            conn.execute(
                select(audit_log.c.action, func.count()).group_by(audit_log.c.action)
            ).all()
        )
    assert counts["oauth.denied"] == oauth.MAX_ANON_AUDIT_ROWS
    assert counts["oauth.register"] == 1  # the new one; the old one is gone
    assert counts["oauth.approved"] == 1  # what the owner did stays


def test_portal_auth_routes_are_limited(runtime) -> None:
    portal = TestClient(create_portal_app(runtime), base_url="http://localhost")
    headers = {"x-vibesynergy": "portal", "content-type": "application/json"}
    assert portal.post("/auth/login", content=BIG, headers=headers).status_code == 413
    assert portal.post("/auth/link", content=_chunks(BIG), headers=headers).status_code == 413


def test_larger_cap_and_unlimited_prefixes() -> None:
    seen: list[int] = []

    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        seen.append(len(body))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    def call(limiter, path: str, size: int) -> int:
        statuses: list[int] = []
        chunks = [b"x" * 1024] * (size // 1024)

        async def receive():
            body = chunks.pop() if chunks else b""
            return {"type": "http.request", "body": body, "more_body": bool(chunks)}

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        scope = {"type": "http", "path": path, "headers": []}
        asyncio.run(limiter(scope, receive, send))
        return statuses[0]

    limiter = BodyLimit(app, larger=[(r"/upload", 1024 * 1024)])
    assert call(limiter, "/upload", 512 * 1024) == 200
    assert call(limiter, "/other", 512 * 1024) == 413
    only_auth = BodyLimit(app, prefixes=("/auth/",))
    assert call(only_auth, "/api/files/upload", 512 * 1024) == 200
    assert call(only_auth, "/auth/login", 512 * 1024) == 413
