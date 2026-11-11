"""MCP 2025-06-18 OAuth discovery and authorization contract, using synthetic clients."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_lib
import re
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
import pytest
from sqlalchemy import select, update
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from vibesynergy.api import oauth
from vibesynergy.api.app import create_app
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import oauth_grant

HOST = "https://server.example.ts.net"
ISSUER = f"{HOST}/vibesynergy"
RESOURCE = f"{ISSUER}/mcp"
REDIRECT = "https://client.example/callback"
PASSPHRASE = "synthetic passphrase for tests"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


@pytest.fixture
def app_client(runtime):
    oauth.set_passphrase(SecretStore(runtime.settings.secrets_dir, "mcp"), PASSPHRASE)
    provider = oauth.VibeSynergyOAuthProvider(
        runtime.engine, SecretStore(runtime.settings.secrets_dir, "mcp"), ISSUER, RESOURCE
    )

    async def mcp_app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    app = Starlette(
        routes=[
            *oauth.routes(provider),
            Route("/mcp", oauth.BearerGuard(mcp_app, provider, "/mcp"), methods=["POST"]),
        ]
    )

    class Client:
        def request(self, method, path, **kwargs):
            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=HOST, follow_redirects=False
                ) as client:
                    return await client.request(method, path, **kwargs)

            return asyncio.run(run())

        def get(self, path, **kwargs):
            return self.request("GET", path, **kwargs)

        def post(self, path, **kwargs):
            return self.request("POST", path, **kwargs)

    return runtime, Client()


def register(client, redirect=REDIRECT, **metadata):
    return client.post(
        "/register",
        json={
            "client_name": "Synthetic client",
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            **metadata,
        },
    )


def pkce():
    verifier = "synthetic-verifier-with-at-least-43-safe-characters-12345"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def authorize(client, client_id, **overrides):
    _, challenge = pkce()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-1",
    }
    params.update(overrides)
    params = {key: value for key, value in params.items() if value is not None}
    return client.get("/authorize", params=params)


_APPROVAL_CALLBACK = re.compile(r'<label>Callback URL<br><input readonly[^>]*value="([^"]+)"')


def code_from_approval(client, authorization):
    assert authorization.status_code == 302
    request_id = parse_qs(urlparse(authorization.headers["location"]).query)["request"][0]
    approval = client.post(
        "/approve", data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"}
    )
    assert approval.status_code == 200
    callback = _APPROVAL_CALLBACK.search(approval.text)
    assert callback, approval.text[:500]
    return parse_qs(urlparse(html_lib.unescape(callback.group(1))).query)["code"][0]


def exchange(client, client_id, code, **overrides):
    verifier, _ = pkce()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    data.update(overrides)
    return client.post("/token", data=data)


def refresh(client, client_id, token, **overrides):
    data = {"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id}
    data.update(overrides)
    return client.post("/token", data=data)


def test_metadata_and_no_oidc(app_client):
    _, client = app_client
    # Unknown suffixes are not discovery documents. The path-appended
    # openid-configuration URL is a separate alias; see the path-prefix test.
    for path in (
        "/.well-known/openid-configuration/anything",
        "/vibesynergy/.well-known/openid-configuration/anything",
    ):
        assert client.get(path).status_code == 404

    docs = [
        client.get(path)
        for path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/vibesynergy",
            "/vibesynergy/.well-known/oauth-authorization-server",
        )
    ]
    assert all(reply.status_code == 200 for reply in docs)
    assert docs[0].json() == docs[1].json() == docs[2].json()
    as_meta = docs[0].json()
    assert as_meta["issuer"] == ISSUER
    for field, path in (
        ("authorization_endpoint", "authorize"),
        ("token_endpoint", "token"),
        ("registration_endpoint", "register"),
        ("revocation_endpoint", "revoke"),
    ):
        assert as_meta[field] == f"{ISSUER}/{path}"
    assert as_meta["response_types_supported"] == ["code"]
    assert as_meta["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert set(as_meta["token_endpoint_auth_methods_supported"]) == {
        "none",
        "client_secret_post",
        "client_secret_basic",
    }
    assert as_meta["code_challenge_methods_supported"] == ["S256"]
    from vibesynergy.mcp.scopes import ADVERTISED_SCOPES

    assert as_meta["scopes_supported"] == list(ADVERTISED_SCOPES)
    assert "jwks_uri" not in as_meta and "id_token_signing_alg_values_supported" not in as_meta

    prm_paths = (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/vibesynergy/mcp",
        "/vibesynergy/.well-known/oauth-protected-resource",
    )
    prm_replies = [client.get(path) for path in prm_paths]
    assert all(reply.status_code == 200 for reply in prm_replies)
    assert all(reply.json() == prm_replies[0].json() for reply in prm_replies)
    prm = prm_replies[0].json()
    assert prm == {
        "resource": RESOURCE,
        "authorization_servers": [ISSUER],
        "scopes_supported": list(ADVERTISED_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "VibeSynergy",
    }


def test_well_known_under_mcp_url(app_client):
    """Sign-in metadata under the MCP address matches the issuer documents.

    Clients such as the Grok Bot connector probe ``{mcp}/.well-known/...`` instead
    of the issuer. Both the ``/vibesynergy`` path and the prefix-stripped path must
    answer, and the body must be the document already served at the issuer.
    """
    _, client = app_client
    documents = {
        "oauth-protected-resource": "/.well-known/oauth-protected-resource",
        "oauth-authorization-server": "/.well-known/oauth-authorization-server",
        "openid-configuration": "/.well-known/openid-configuration",
    }
    for name, canonical in documents.items():
        expected = client.get(canonical)
        assert expected.status_code == 200, canonical
        for prefix in ("/mcp", "/vibesynergy/mcp"):
            path = f"{prefix}/.well-known/{name}"
            reply = client.get(path)
            assert reply.status_code == 200, path
            assert reply.json() == expected.json()


def test_actual_app_mounts_discovery_and_bearer_guard(runtime):
    configured = replace(runtime, settings=replace(runtime.settings, server={"public_url": HOST}))
    app = create_app(configured)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=HOST
        ) as client:
            oidc = await client.get("/.well-known/openid-configuration")
            server = await client.get("/.well-known/oauth-authorization-server/vibesynergy")
            protected = await client.get("/.well-known/oauth-protected-resource/vibesynergy/mcp")
            under_mcp = [
                await client.get(f"{prefix}/.well-known/{name}")
                for prefix in ("/mcp", "/vibesynergy/mcp")
                for name in (
                    "oauth-protected-resource",
                    "oauth-authorization-server",
                    "openid-configuration",
                )
            ]
            challenge = await client.post("/mcp", json=INIT, headers=MCP_HEADERS)
            return oidc, server, protected, under_mcp, challenge

    oidc, server, protected, under_mcp, challenge = asyncio.run(run())
    assert oidc.status_code == 200
    assert oidc.json()["issuer"] == ISSUER
    assert oidc.json()["registration_endpoint"] == f"{ISSUER}/register"
    assert "openid" not in oidc.json()["scopes_supported"]
    assert server.status_code == 200 and server.json()["issuer"] == ISSUER
    assert protected.status_code == 200 and protected.json()["resource"] == RESOURCE
    expected = {
        "oauth-protected-resource": protected.json(),
        "oauth-authorization-server": server.json(),
        "openid-configuration": oidc.json(),
    }
    assert len(under_mcp) == 6
    for reply in under_mcp:
        assert reply.status_code == 200, reply.request.url
        name = reply.request.url.path.rsplit("/", 1)[-1]
        assert reply.json() == expected[name]
    assert challenge.status_code == 401
    assert (
        f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"'
        in challenge.headers["www-authenticate"]
    )


def test_bearer_challenge_and_audience(app_client):
    rt, client = app_client
    metadata_url = f"{ISSUER}/.well-known/oauth-protected-resource"
    for headers, invalid in (
        (MCP_HEADERS, False),
        ({**MCP_HEADERS, "authorization": "Bearer bad"}, True),
        ({**MCP_HEADERS, "authorization": "Bearer "}, True),
    ):
        reply = client.post("/mcp", json=INIT, headers=headers)
        assert reply.status_code == 401
        challenge = reply.headers["www-authenticate"]
        assert challenge.startswith(f'Bearer resource_metadata="{metadata_url}"')
        from vibesynergy.mcp.scopes import SCOPES

        assert f'scope="{" ".join(SCOPES)}"' in challenge
        assert ('error="invalid_token"' in challenge) is invalid

    client_id = register(client).json()["client_id"]
    tokens = exchange(
        client, client_id, code_from_approval(client, authorize(client, client_id))
    ).json()
    token = tokens["access_token"]
    access_headers = {**MCP_HEADERS, "authorization": f"Bearer {token}"}
    assert client.post("/mcp", json=INIT, headers=access_headers).status_code == 200
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with rt.engine.begin() as conn:
        row = conn.execute(
            select(oauth_grant.c.data).where(oauth_grant.c.token_hash == token_hash)
        ).scalar_one()
        assert row["resource"] == RESOURCE
        conn.execute(
            update(oauth_grant)
            .where(oauth_grant.c.token_hash == token_hash)
            .values(data={"scopes": ["vibesynergy"]})
        )
    assert client.post("/mcp", json=INIT, headers=access_headers).status_code == 200
    with rt.engine.begin() as conn:
        conn.execute(
            update(oauth_grant)
            .where(oauth_grant.c.token_hash == token_hash)
            .values(data={"scopes": ["vibesynergy"], "resource": "https://other.example/mcp"})
        )
    assert client.post("/mcp", json=INIT, headers=access_headers).status_code == 401
    with rt.engine.begin() as conn:
        conn.execute(
            update(oauth_grant)
            .where(oauth_grant.c.token_hash == token_hash)
            .values(data={"scopes": [], "resource": RESOURCE})
        )
    no_scope = client.post("/mcp", json=INIT, headers=access_headers)
    assert no_scope.status_code == 403
    assert 'error="insufficient_scope"' in no_scope.headers["www-authenticate"]

    with rt.engine.begin() as conn:
        conn.execute(
            update(oauth_grant)
            .where(oauth_grant.c.token_hash == token_hash)
            .values(
                data={"scopes": ["vibesynergy"], "resource": RESOURCE},
                expires_at=utc_now() - timedelta(seconds=1),
            )
        )
    expired = client.post("/mcp", json=INIT, headers=access_headers)
    assert expired.status_code == 401
    assert 'error="invalid_token"' in expired.headers["www-authenticate"]

    refresh_hash = hashlib.sha256(tokens["refresh_token"].encode()).hexdigest()
    with rt.engine.begin() as conn:
        conn.execute(
            update(oauth_grant)
            .where(oauth_grant.c.token_hash == refresh_hash)
            .values(data={"scopes": ["vibesynergy"]})
        )
    assert refresh(client, client_id, tokens["refresh_token"]).status_code == 200


@pytest.mark.parametrize(
    "redirect",
    [
        "http://client.example/callback",
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///tmp/callback",
        "http://192.0.2.1/callback",
        "https://bad host/callback",
    ],
)
def test_dcr_rejects_unsafe_redirect(app_client, redirect):
    _, client = app_client
    response = register(client, redirect)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_dcr_accepts_https_and_loopback(app_client):
    _, client = app_client
    for redirect in (
        REDIRECT,
        "http://127.0.0.1:9876/callback",
        "http://[::1]:9876/callback",
        "http://localhost:54546/callback",
        "cursor://anysphere.cursor-retrieval/oauth/user-vibesynergy/callback",
        "com.example.app:/oauth/callback",
    ):
        reply = register(client, redirect)
        assert reply.status_code == 201, reply.text
        info = reply.json()
        assert info["client_id"] and info["client_id_issued_at"] > 0
        assert info["redirect_uris"] == [redirect]
        assert info["client_name"] == "Synthetic client"
        assert info["token_endpoint_auth_method"] == "none"
        assert info["grant_types"] == ["authorization_code", "refresh_token"]


def test_loopback_redirect_allows_ephemeral_port_only(app_client):
    _, client = app_client
    info = register(client, "http://127.0.0.1:9876/callback").json()
    client_id = info["client_id"]
    success = authorize(client, client_id, redirect_uri="http://127.0.0.1:45678/callback")
    assert success.status_code == 302 and success.headers["location"].startswith(
        f"{ISSUER}/approve"
    )
    wrong_path = authorize(client, client_id, redirect_uri="http://127.0.0.1:45678/other")
    assert wrong_path.status_code == 400


def test_dcr_rejects_unsupported_metadata(app_client):
    _, client = app_client
    for metadata in (
        {"token_endpoint_auth_method": "private_key_jwt"},
        {"response_types": ["code", "token"]},
        {"grant_types": None},
    ):
        reply = register(client, **metadata)
        assert reply.status_code == 400 and reply.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
def test_advertised_client_secret_methods_work(app_client, method):
    _, client = app_client
    registration = register(client, token_endpoint_auth_method=method)
    assert registration.status_code == 201
    info = registration.json()
    code = code_from_approval(client, authorize(client, info["client_id"]))
    verifier, _ = pkce()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": info["client_id"],
        "code_verifier": verifier,
    }
    auth = None
    if method == "client_secret_post":
        data["client_secret"] = info["client_secret"]
    else:
        auth = (info["client_id"], info["client_secret"])
    tokens = client.post("/token", data=data, auth=auth)
    assert tokens.status_code == 200, tokens.text


def test_pkce_and_resource_errors(app_client):
    _, client = app_client
    client_id = register(client).json()["client_id"]
    for overrides in (
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
        {"code_challenge": None},
    ):
        reply = authorize(client, client_id, **overrides)
        assert reply.status_code == 302
        assert parse_qs(urlparse(reply.headers["location"]).query)["error"] == ["invalid_request"]
    invalid_redirect = authorize(
        client, client_id, redirect_uri="https://evil.example", code_challenge=None
    )
    assert invalid_redirect.status_code == 400

    bad_resource = authorize(client, client_id, resource="https://other.example/mcp")
    assert bad_resource.status_code == 302
    assert parse_qs(urlparse(bad_resource.headers["location"]).query)["error"] == ["invalid_target"]
    code = code_from_approval(client, authorize(client, client_id, resource=RESOURCE + "/"))
    wrong = exchange(client, client_id, code, code_verifier="wrong-verifier")
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    mismatch = exchange(client, client_id, code, resource="https://other.example/mcp")
    assert mismatch.status_code == 400 and mismatch.json()["error"] == "invalid_target"
    token = exchange(client, client_id, code, resource=RESOURCE)
    assert token.status_code == 200, token.text
    refresh_token = token.json()["refresh_token"]
    bad_refresh = refresh(client, client_id, refresh_token, resource="https://other.example/mcp")
    assert bad_refresh.status_code == 400 and bad_refresh.json()["error"] == "invalid_target"
    first_refresh = refresh(client, client_id, refresh_token)
    assert first_refresh.status_code == 200, first_refresh.text
    second_refresh = refresh(
        client, client_id, first_refresh.json()["refresh_token"], resource=RESOURCE + "/"
    )
    assert second_refresh.status_code == 200, second_refresh.text


def test_refresh_with_foreign_stored_resource_issues_for_mcp_resource(app_client):
    rt, client = app_client
    client_id = register(client).json()["client_id"]
    tokens = exchange(
        client, client_id, code_from_approval(client, authorize(client, client_id))
    ).json()
    refresh_hash = hashlib.sha256(tokens["refresh_token"].encode()).hexdigest()
    with rt.engine.begin() as conn:
        conn.execute(
            update(oauth_grant)
            .where(oauth_grant.c.token_hash == refresh_hash)
            .values(
                data={"scopes": ["vibesynergy"], "resource": f"{HOST}/vibesynergy/old-secret/mcp"}
            )
        )
    # A request resource that is not the MCP resource is still rejected, grant untouched.
    bad = refresh(client, client_id, tokens["refresh_token"], resource="https://other.example/mcp")
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_target"
    renewed = refresh(client, client_id, tokens["refresh_token"])
    assert renewed.status_code == 200, renewed.text
    new = renewed.json()
    headers = {**MCP_HEADERS, "authorization": f"Bearer {new['access_token']}"}
    assert client.post("/mcp", json=INIT, headers=headers).status_code == 200
    with rt.engine.begin() as conn:
        for token in (new["access_token"], new["refresh_token"]):
            stored = conn.execute(
                select(oauth_grant.c.data).where(
                    oauth_grant.c.token_hash == hashlib.sha256(token.encode()).hexdigest()
                )
            ).scalar_one()
            assert stored["resource"] == RESOURCE


@pytest.mark.parametrize(
    "redirect",
    [
        "cursor://anysphere.cursor-retrieval/oauth/callback",
        "vscode://ms-vscode.mcp/callback",
        "vscode-insiders://ms-vscode.mcp/callback",
        "windsurf://codeium.windsurf/callback",
        "claude://oauth/callback",
        "zed://mcp/oauth/callback",
        "raycast://extensions/oauth",
        "goose://oauth/callback",
        "com.example.app:/oauth/callback",
    ],
)
def test_dcr_accepts_private_use_schemes(app_client, redirect):
    _, client = app_client
    reply = register(client, redirect)
    assert reply.status_code == 201, reply.text
    assert reply.json()["redirect_uris"] == [redirect]


@pytest.mark.parametrize(
    "redirect",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html,hi",
        "file:///tmp/callback",
        "blob:https://client.example/uuid",
        "about:blank",
        "ftp://client.example/callback",
        "zed://mcp/callback#fragment",
        "zed://user:pw@mcp/callback",
        "callback",
        "/callback",
    ],
)
def test_dcr_rejects_dangerous_or_malformed_schemes(app_client, redirect):
    _, client = app_client
    response = register(client, redirect)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
    assert "private-use" in response.json()["error_description"]


def test_dcr_validation_edge_cases(app_client):
    _, client = app_client
    for redirect_uris in ("https://client.example/callback", {"a": 1}, None, []):
        reply = register(client, redirect_uris=redirect_uris)
        assert reply.status_code == 400
        assert reply.json()["error"] == "invalid_redirect_uri"
    reply = register(client, grant_types=[])
    assert reply.status_code == 400 and reply.json()["error"] == "invalid_client_metadata"


def test_localhost_loopback_port_variation_and_mismatch(app_client):
    _, client = app_client
    client_id = register(client, "http://localhost:9876/callback").json()["client_id"]
    ok = authorize(client, client_id, redirect_uri="http://localhost:45678/callback")
    assert ok.status_code == 302 and ok.headers["location"].startswith(f"{ISSUER}/approve")
    for redirect in (
        "http://127.0.0.1:9876/callback",  # host mismatch
        "https://localhost:9876/callback",  # scheme mismatch
        "http://localhost:9876/callback?x=1",  # query mismatch
        "http://localhost.evil.example:9876/callback",
    ):
        assert authorize(client, client_id, redirect_uri=redirect).status_code == 400, redirect


def test_authorize_post_form_branch(app_client):
    _, client = app_client
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    form = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-1",
    }
    ok = client.post("/authorize", data=form)
    assert ok.status_code == 302 and ok.headers["location"].startswith(f"{ISSUER}/approve")
    code = code_from_approval(client, ok)
    assert exchange(client, client_id, code).status_code == 200

    plain = client.post("/authorize", data=dict(form, code_challenge_method="plain"))
    assert plain.status_code == 302
    assert parse_qs(urlparse(plain.headers["location"]).query)["error"] == ["invalid_request"]
    wrong = client.post("/authorize", data=dict(form, resource="https://other.example/mcp"))
    assert wrong.status_code == 302
    query = parse_qs(urlparse(wrong.headers["location"]).query)
    assert query["error"] == ["invalid_target"] and query["state"] == ["state-1"]
    duplicate = client.post("/authorize", data=dict(form, resource=[RESOURCE, RESOURCE]))
    assert duplicate.status_code == 302
    assert parse_qs(urlparse(duplicate.headers["location"]).query)["error"] == ["invalid_target"]


def test_duplicate_resource_is_rejected(app_client):
    _, client = app_client
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    query = (
        f"response_type=code&client_id={client_id}&redirect_uri={REDIRECT}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
        f"&resource={RESOURCE}&resource={RESOURCE}"
    )
    reply = client.get("/authorize?" + query)
    assert reply.status_code == 302
    assert parse_qs(urlparse(reply.headers["location"]).query)["error"] == ["invalid_target"]

    code = code_from_approval(client, authorize(client, client_id))
    verifier, _ = pkce()
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": [RESOURCE, RESOURCE],
        },
    )
    assert token.status_code == 400 and token.json()["error"] == "invalid_target"
    # The rejected request did not consume the code.
    assert exchange(client, client_id, code).status_code == 200


def test_lowercase_bearer_scheme_is_accepted(app_client):
    _, client = app_client
    client_id = register(client).json()["client_id"]
    token = exchange(
        client, client_id, code_from_approval(client, authorize(client, client_id))
    ).json()["access_token"]
    for scheme in ("bearer", "BEARER"):
        headers = {**MCP_HEADERS, "authorization": f"{scheme} {token}"}
        assert client.post("/mcp", json=INIT, headers=headers).status_code == 200
    bad = client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": "bearer nope"})
    assert bad.status_code == 401 and 'error="invalid_token"' in bad.headers["www-authenticate"]


def test_protected_resource_metadata_layout_is_pinned(app_client):
    """Funnel: only /vibesynergy/* reaches this service; the host root is another service.

    The 401 advertises the /vibesynergy-prefixed document, whose `resource` is exactly the MCP URL.
    The path-inserted route must match the advertised resource; see docs/MCP.md.
    """
    _, client = app_client
    advertised = client.post("/mcp", json=INIT, headers=MCP_HEADERS).headers["www-authenticate"]
    assert f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"' in advertised
    path = "/vibesynergy/.well-known/oauth-protected-resource"
    assert client.get(path).json()["resource"] == RESOURCE
    for alias in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/vibesynergy/mcp",
    ):
        assert client.get(alias).json() == client.get(path).json()
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404


def _sdk_register_join(authorization_server_url: str) -> str:
    """MCP SDK ``registerClient`` fallback: ``new URL("/register", authorizationServerUrl)``."""
    parts = urlparse(authorization_server_url)
    return f"{parts.scheme}://{parts.netloc}/register"


def _sdk_origin_fallback(server_url: str) -> str:
    """MCP SDK when protected-resource discovery misses: ``String(new URL("/", serverUrl))``."""
    parts = urlparse(server_url)
    return f"{parts.scheme}://{parts.netloc}/"


def _sdk_discovery_paths(authorization_server_url: str) -> list[str]:
    """Paths from MCP SDK ``buildDiscoveryUrls`` for an issuer that has a path."""
    path = urlparse(authorization_server_url).path.rstrip("/")
    return [
        f"/.well-known/oauth-authorization-server{path}",
        f"/.well-known/openid-configuration{path}",
        f"{path}/.well-known/openid-configuration",
    ]


def test_path_prefix_registration_endpoint_and_dcr(app_client):
    """DCR uses the absolute /vibesynergy registration_endpoint, not the host-root join.

    ``new URL("/register", "https://host/vibesynergy")`` is ``https://host/register``.
    Metadata must not point there. The path-appended discovery URL is the probe
    the Funnel can deliver; registering at its ``registration_endpoint`` succeeds.
    Rediscovery that starts from the MCP resource URL keeps the /vibesynergy issuer.
    The SDK's bare-host fallback is a different URL.
    """
    _, client = app_client
    challenge = client.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert challenge.status_code == 401
    metadata_url = f"{ISSUER}/.well-known/oauth-protected-resource"
    assert f'resource_metadata="{metadata_url}"' in challenge.headers["www-authenticate"]

    prm = client.get(urlparse(metadata_url).path).json()
    origin = _sdk_origin_fallback(RESOURCE)
    assert origin == f"{HOST}/"
    assert prm["resource"] == RESOURCE
    assert prm["authorization_servers"] == [ISSUER]
    assert prm["authorization_servers"] != [origin.rstrip("/")]
    # Logout clears client state. The next login that reads this document again
    # still gets the path-prefixed issuer, not the bare host.
    assert client.get("/vibesynergy/.well-known/oauth-protected-resource").json() == prm

    bare_register = _sdk_register_join(ISSUER)
    assert bare_register == f"{HOST}/register"
    discovered = []
    for path in _sdk_discovery_paths(ISSUER):
        reply = client.get(path)
        assert reply.status_code == 200, path
        body = reply.json()
        assert body["issuer"] == ISSUER
        assert body["registration_endpoint"] == f"{ISSUER}/register"
        assert body["authorization_endpoint"] == f"{ISSUER}/authorize"
        assert body["token_endpoint"] == f"{ISSUER}/token"
        assert body["revocation_endpoint"] == f"{ISSUER}/revoke"
        assert body["registration_endpoint"] != bare_register
        assert urlparse(body["registration_endpoint"]).path == "/vibesynergy/register"
        discovered.append(body)

    assert "jwks_uri" not in discovered[0]
    assert "id_token_signing_alg_values_supported" not in discovered[0]
    alias = discovered[2]
    assert alias["jwks_uri"] == f"{ISSUER}/.well-known/jwks.json"
    assert alias["subject_types_supported"] == ["public"]
    assert alias["id_token_signing_alg_values_supported"] == ["RS256"]
    assert "openid" not in alias["scopes_supported"]
    assert client.get("/.well-known/jwks.json").json() == {"keys": []}
    assert client.get("/vibesynergy/.well-known/jwks.json").json() == {"keys": []}
    stripped = client.get("/.well-known/openid-configuration").json()
    assert stripped == alias

    registered = client.post(
        urlparse(alias["registration_endpoint"]).path,
        json={
            "client_name": "Path prefix client",
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    authorization = authorize(client, client_id)
    request_id = parse_qs(urlparse(authorization.headers["location"]).query)["request"][0]
    approval = client.post(
        "/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
    )
    assert approval.status_code == 200
    query = parse_qs(
        urlparse(html_lib.unescape(_APPROVAL_CALLBACK.search(approval.text).group(1))).query
    )
    assert query["iss"] == [ISSUER]
    token = exchange(client, client_id, query["code"][0])
    assert token.status_code == 200, token.text
    assert "id_token" not in token.json()
    denied = client.post(
        "/approve",
        data={
            "request": parse_qs(urlparse(authorize(client, client_id).headers["location"]).query)[
                "request"
            ][0],
            "action": "deny",
        },
    )
    assert denied.status_code == 200 and "Access denied" in denied.text
    deny_callback = html_lib.unescape(_APPROVAL_CALLBACK.search(denied.text).group(1))
    assert parse_qs(urlparse(deny_callback).query)["iss"] == [ISSUER]
