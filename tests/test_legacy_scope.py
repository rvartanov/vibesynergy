"""The retired 'vibesynergy' MCP scope: not advertised, not grantable, stored grants
rewritten once to the names it stood for."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_oauth import HOST, PASSPHRASE, REDIRECT_HTTPS, _pkce, _register

from vibesynergy.api import oauth
from vibesynergy.api.app import create_app
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.mcp.scopes import ADVERTISED_SCOPES, LEGACY_EXPANSION, LEGACY_SCOPE, SCOPES
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import audit_log, mcp_pat, oauth_client, oauth_grant


@pytest.fixture
def server(runtime):
    settings = replace(runtime.settings, server={"public_url": HOST})
    rt = Runtime.open(settings)
    oauth.set_passphrase(SecretStore(settings.secrets_dir, "mcp"), PASSPHRASE)
    with TestClient(create_app(rt), base_url=HOST, follow_redirects=False) as client:
        yield rt, client
    rt.close()


def _authorize(client, client_id: str, scope: str):
    _, challenge = _pkce()
    return client.get(
        "/vibesynergy/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_HTTPS,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-1",
            "scope": scope,
            "resource": f"{HOST}/vibesynergy/mcp",
        },
    )


def test_metadata_no_longer_advertises_the_old_scope(server) -> None:
    _, client = server
    assert LEGACY_SCOPE not in ADVERTISED_SCOPES
    assert tuple(ADVERTISED_SCOPES) == SCOPES
    meta = client.get("/vibesynergy/.well-known/oauth-authorization-server").json()
    assert LEGACY_SCOPE not in meta["scopes_supported"]
    resource = client.get("/vibesynergy/.well-known/oauth-protected-resource").json()
    assert LEGACY_SCOPE not in resource["scopes_supported"]


def test_registration_with_the_old_scope_is_refused(server) -> None:
    _, client = server
    reply = client.post(
        "/vibesynergy/register",
        json={
            "client_name": "Old app",
            "redirect_uris": [REDIRECT_HTTPS],
            "token_endpoint_auth_method": "none",
            "scope": "vibesynergy",
        },
    )
    assert reply.status_code == 400
    assert "vibesynergy" in reply.json()["error_description"]


def test_new_grant_cannot_ask_for_the_old_scope(server) -> None:
    rt, client = server
    client_id = _register(client)
    # An app registered before may still list the word; the request is refused anyway.
    with rt.engine.begin() as conn:
        info = conn.execute(
            select(oauth_client.c.info).where(oauth_client.c.client_id == client_id)
        ).scalar_one()
        conn.execute(
            oauth_client.update()
            .where(oauth_client.c.client_id == client_id)
            .values(info={**info, "scope": f"{info['scope']} vibesynergy"})
        )
    reply = _authorize(client, client_id, "vibesynergy")
    assert reply.status_code == 302
    query = parse_qs(urlparse(reply.headers["location"]).query)
    assert query["error"] == ["invalid_scope"]
    assert "retired" in query["error_description"][0]
    ok = _authorize(client, client_id, "read coach.write")
    assert ok.status_code == 302
    assert "/vibesynergy/approve?request=" in ok.headers["location"]


def test_stored_grants_are_expanded_once(server) -> None:
    rt, client = server
    client_id = _register(client)
    now = utc_now()
    with rt.engine.begin() as conn:
        conn.execute(
            mcp_pat.insert().values(
                id="old",
                name="hermes",
                token_hash="0" * 64,
                token_prefix="vpat_xxxx",
                created_at=now,
                expires_at=now + timedelta(days=30),
                scopes=["vibesynergy"],
                display_name="hermes",
            )
        )
        info = conn.execute(
            select(oauth_client.c.info).where(oauth_client.c.client_id == client_id)
        ).scalar_one()
        conn.execute(
            oauth_client.update()
            .where(oauth_client.c.client_id == client_id)
            .values(info={**info, "scope": "vibesynergy publish"})
        )
        conn.execute(
            oauth_grant.insert().values(
                token_hash="1" * 64,
                kind="access",
                client_id=client_id,
                data={"scopes": ["vibesynergy"], "resource": f"{HOST}/vibesynergy/mcp"},
                expires_at=now + timedelta(hours=1),
                created_at=now,
            )
        )

    assert oauth.expand_legacy_scope_grants(rt.engine) == {"tokens": 1, "apps": 1, "grants": 1}
    with rt.engine.connect() as conn:
        pat = conn.execute(select(mcp_pat.c.scopes).where(mcp_pat.c.id == "old")).scalar_one()
        app = conn.execute(
            select(oauth_client.c.info).where(oauth_client.c.client_id == client_id)
        ).scalar_one()
        grant = conn.execute(
            select(oauth_grant.c.data).where(oauth_grant.c.token_hash == "1" * 64)
        ).scalar_one()
        audits = conn.execute(
            select(audit_log.c.detail).where(audit_log.c.action == "oauth.legacy_scope.expanded")
        ).all()
    assert pat == list(LEGACY_EXPANSION)
    assert app["scope"].split() == [*LEGACY_EXPANSION, "publish"]
    assert grant["scopes"] == list(LEGACY_EXPANSION)
    assert grant["resource"] == f"{HOST}/vibesynergy/mcp"
    assert "sql" in pat and "publish" not in pat and "plan.write" not in pat
    assert len(audits) == 1
    # A second run (the next restart) finds nothing.
    assert oauth.expand_legacy_scope_grants(rt.engine) == {"tokens": 0, "apps": 0, "grants": 0}
