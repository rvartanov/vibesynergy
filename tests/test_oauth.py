"""OAuth 2.1 for the MCP endpoint, end to end, as an MCP client (e.g. Claude) performs it."""

from __future__ import annotations

import base64
import hashlib
import html as html_lib
import re
import secrets
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from vibesynergy.api import oauth
from vibesynergy.api.app import create_app
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.core.user_datetime import format_user_datetime
from vibesynergy.mcp.scopes import LEGACY_EXPANSION
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import mcp_pat, oauth_client

HOST = "https://server.example.ts.net"
REDIRECT_HTTPS = "https://claude.ai/api/mcp/auth_callback"
REDIRECT_LOOPBACK = "http://127.0.0.1:18789/vibesynergy/callback"
REDIRECT_CUSTOM = "cursor://vibesynergy/oauth/callback"
REDIRECT_CUSTOM_UNKNOWN = "my-custom-app://vibesynergy/oauth/callback"
PASSPHRASE = "correct horse battery staple"
_CODE_INPUT = re.compile(r'<label>Code<br><input readonly[^>]*value="([^"]+)"')
_CALLBACK_INPUT = re.compile(r'<label>Callback URL<br><input readonly[^>]*value="([^"]+)"')


def _assert_no_oauth_credentials(
    reply,
    *,
    case_name: str,
    forbidden_code: str,
    forbidden_callback: str,
) -> None:
    """Approval re-display must not leak code, callback, or auto-continue hints."""
    text = reply.text
    headers = {k.lower(): v for k, v in reply.headers.items()}
    blob = text + "\n" + "\n".join(headers.values())
    prefix = f"{case_name}: "
    assert _CODE_INPUT.search(text) is None, prefix + "code input widget present"
    assert _CALLBACK_INPUT.search(text) is None, prefix + "callback input widget present"
    assert forbidden_code not in blob, prefix + "authorization code leaked"
    assert forbidden_callback not in blob, prefix + "callback URL leaked"
    assert "Continue to" not in text, prefix + "auto-continue hint present"
    assert 'http-equiv="refresh"' not in text, prefix + "meta refresh present"
    assert "location" not in headers, prefix + "Location header present"
    assert "refresh" not in headers, prefix + "Refresh header present"
    assert "code=" not in blob.lower(), prefix + "code= present in body or headers"


def _assert_no_timed_handoff(reply) -> None:
    """No delayed navigation of any kind (WCAG 2.2.1 / axe meta-refresh): the user decides."""
    headers = {k.lower() for k in reply.headers}
    assert not re.search(r"http-equiv\s*=\s*[\"']?refresh", reply.text, re.IGNORECASE)
    assert "<script" not in reply.text.lower()
    assert "location" not in headers and "refresh" not in headers


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


@pytest.fixture
def setup(runtime):
    settings = replace(runtime.settings, server={"public_url": HOST})
    rt = Runtime.open(settings)
    oauth.set_passphrase(SecretStore(settings.secrets_dir, "mcp"), PASSPHRASE)
    with TestClient(create_app(rt), base_url=HOST, follow_redirects=False) as client:
        yield rt, client
    rt.close()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _register(client, name: str = "Claude", redirect_uri: str = REDIRECT_HTTPS) -> str:
    reply = client.post(
        "/vibesynergy/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert reply.status_code == 201, reply.text
    return reply.json()["client_id"]


def _authorize(client, client_id: str, challenge: str, redirect_uri: str = REDIRECT_HTTPS) -> str:
    reply = client.get(
        "/vibesynergy/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-1",
            "scope": " ".join(LEGACY_EXPANSION),
            "resource": f"{HOST}/vibesynergy/mcp",
        },
    )
    assert reply.status_code == 302, reply.text
    return reply.headers["location"]


def _request_id(location: str) -> str:
    assert location.startswith(f"{HOST}/vibesynergy/approve?request=")
    return parse_qs(urlparse(location).query)["request"][0]


def _approve_page(client, request_id: str) -> str:
    reply = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
    )
    assert reply.status_code == 200, reply.text
    assert "Approved" in reply.text
    csp = reply.headers.get("content-security-policy", "")
    assert "form-action" not in csp
    match = _CODE_INPUT.search(reply.text)
    assert match, reply.text
    return match.group(1)


def _login(client, redirect_uri: str = REDIRECT_HTTPS) -> dict:
    client_id = _register(client, redirect_uri=redirect_uri)
    verifier, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge, redirect_uri))
    code = _approve_page(client, request_id)
    token = client.post(
        "/vibesynergy/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    return {"client_id": client_id, "code": code, "verifier": verifier, **token.json()}


def test_discovery(setup) -> None:
    _, client = setup
    reply = client.post("/vibesynergy/mcp", json=INIT, headers=MCP_HEADERS)
    assert reply.status_code == 401
    header = reply.headers["www-authenticate"]
    assert f'resource_metadata="{HOST}/vibesynergy/.well-known/oauth-protected-resource"' in header
    resource = client.get("/vibesynergy/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == f"{HOST}/vibesynergy/mcp"
    assert resource["authorization_servers"] == [f"{HOST}/vibesynergy"]
    alias = client.get("/vibesynergy/.well-known/openid-configuration").json()
    assert alias["registration_endpoint"] == f"{HOST}/vibesynergy/register"
    assert alias["issuer"] == f"{HOST}/vibesynergy"
    for path in (
        "/vibesynergy/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/vibesynergy",
    ):
        meta = client.get(path).json()
        assert meta["issuer"] == f"{HOST}/vibesynergy"
        assert meta["authorization_endpoint"] == f"{HOST}/vibesynergy/authorize"
        assert meta["registration_endpoint"] == f"{HOST}/vibesynergy/register"
        assert meta["code_challenge_methods_supported"] == ["S256"]


def test_full_login_and_mcp_call(setup) -> None:
    _, client = setup
    tokens = _login(client)
    assert tokens["expires_in"] == 3600 and tokens["refresh_token"]
    auth = {**MCP_HEADERS, "authorization": f"Bearer {tokens['access_token']}"}
    assert client.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 200
    bad = {**MCP_HEADERS, "authorization": "Bearer not-a-token"}
    reply = client.post("/vibesynergy/mcp", json=INIT, headers=bad)
    assert reply.status_code == 401 and 'error="invalid_token"' in reply.headers["www-authenticate"]


def test_code_is_single_use_and_needs_pkce(setup) -> None:
    _, client = setup
    tokens = _login(client)
    again = client.post(
        "/vibesynergy/token",
        data={
            "grant_type": "authorization_code",
            "code": tokens["code"],
            "redirect_uri": REDIRECT_HTTPS,
            "client_id": tokens["client_id"],
            "code_verifier": tokens["verifier"],
        },
    )
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "redirect_uri",
    [REDIRECT_LOOPBACK, REDIRECT_HTTPS, REDIRECT_CUSTOM, REDIRECT_CUSTOM_UNKNOWN],
    ids=["loopback", "https", "allowlisted_custom_scheme", "other_custom_scheme"],
)
def test_approve_result_page_any_redirect(setup, redirect_uri: str) -> None:
    _, client = setup
    client_id = _register(client, name="MCP client", redirect_uri=redirect_uri)
    verifier, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge, redirect_uri))
    reply = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
    )
    assert reply.status_code == 200
    assert "Approved" in reply.text
    assert "form-action" not in reply.headers.get("content-security-policy", "")
    code = _CODE_INPUT.search(reply.text).group(1)
    callback = html_lib.unescape(_CALLBACK_INPUT.search(reply.text).group(1))
    assert code in callback
    assert callback.startswith(redirect_uri)
    _assert_no_timed_handoff(reply)
    assert "Only paste this into an app you started yourself." in reply.text
    assert reply.headers.get("cache-control") == "no-store"
    again = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
    )
    assert again.status_code == 200 and "Already approved" in again.text
    _assert_no_oauth_credentials(
        again,
        case_name="post_approve_retry",
        forbidden_code=code,
        forbidden_callback=callback,
    )
    token = client.post(
        "/vibesynergy/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text


def test_approve_unknown_request_expired(setup) -> None:
    _, client = setup
    reply = client.get("/vibesynergy/approve?request=not-a-real-request-id")
    assert reply.status_code == 200 and "expired" in reply.text.lower()


def test_personal_access_token_mcp(setup) -> None:
    rt, client = setup
    token = oauth.create_personal_access_token(rt.engine, "ci-agent")
    assert token.startswith(oauth.PAT_PREFIX)
    auth = {**MCP_HEADERS, "authorization": f"Bearer {token}"}
    assert client.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 200
    from sqlalchemy import select

    from vibesynergy.store.schema import audit_log, mcp_pat

    with rt.engine.connect() as conn:
        row = conn.execute(select(mcp_pat).where(mcp_pat.c.name == "ci-agent")).one()
        assert row.last_used_at is not None
        assert token not in conn.execute(select(mcp_pat.c.token_hash)).scalars().all()
        actions = conn.execute(select(audit_log.c.action)).scalars().all()
    assert "oauth.pat.create" in actions
    assert "oauth.pat.first_use" in actions
    listed = oauth.list_personal_access_tokens(rt.engine)
    assert all(t["prefix"] in token and token not in str(t) for t in listed)
    assert oauth.revoke_personal_access_token(rt.engine, "ci-agent")
    assert client.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 401


def test_refresh_rotates(setup) -> None:
    _, client = setup
    tokens = _login(client)
    refresh = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": tokens["client_id"],
    }
    first = client.post("/vibesynergy/token", data=refresh)
    assert first.status_code == 200 and first.json()["access_token"] != tokens["access_token"]
    assert client.post("/vibesynergy/token", data=refresh).status_code == 400  # old one is spent


def test_wrong_passphrase_and_lockout(setup) -> None:
    _, client = setup
    client_id = _register(client, name="<script>alert(1)</script>")
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    page = client.get(f"/vibesynergy/approve?request={request_id}")
    assert "&lt;script&gt;" in page.text and "<script>" not in page.text  # name escaped
    assert page.headers["x-frame-options"] == "DENY"
    for attempt in range(oauth.MAX_FAILURES):
        reply = client.post(
            "/vibesynergy/approve", data={"request": request_id, "passphrase": f"wrong {attempt}"}
        )
        assert reply.status_code == 200 and "Wrong passphrase" in reply.text
    locked = client.post(
        "/vibesynergy/approve", data={"request": request_id, "passphrase": PASSPHRASE}
    )
    assert locked.status_code == 200 and "Too many" in locked.text  # even the right one


def test_one_apps_lockout_does_not_block_another(setup) -> None:
    """a stranger's registration burning its attempts leaves the athlete's app usable."""
    _, client = setup
    stranger = _register(client, name="Stranger")
    _, challenge = _pkce()
    burnt = _request_id(_authorize(client, stranger, challenge))
    for attempt in range(oauth.MAX_FAILURES):
        client.post("/vibesynergy/approve", data={"request": burnt, "passphrase": f"no {attempt}"})
    locked = client.post("/vibesynergy/approve", data={"request": burnt, "passphrase": PASSPHRASE})
    assert "Too many" in locked.text

    mine = _register(client, name="Claude")
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, mine, challenge))
    approved = _approve_page(client, request_id)
    assert "Too many" not in approved


def test_passphrase_limiter_counts_per_key_with_a_ceiling() -> None:
    now = [0.0]
    limiter = oauth.PassphraseLimiter(per_key=2, ceiling=3, window=10, clock=lambda: now[0])
    assert limiter.fail("a") == 1
    assert limiter.fail("a") == 0
    assert limiter.locked("a") and not limiter.locked("b")
    assert limiter.fail("b") == 0  # the ceiling is reached: one left for b, none overall
    assert limiter.locked("b") and limiter.locked("c")
    now[0] = 11.0  # the window has passed
    assert not limiter.locked("a") and not limiter.locked("c")


def test_consent_page_shows_redirect_registration_and_first_connection(setup) -> None:
    rt, client = setup
    client_id = _register(client, name="My app", redirect_uri=REDIRECT_CUSTOM_UNKNOWN)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge, REDIRECT_CUSTOM_UNKNOWN))
    reply = client.get(f"/vibesynergy/approve?request={request_id}")
    assert reply.status_code == 200
    assert REDIRECT_CUSTOM_UNKNOWN in reply.text
    with rt.engine.connect() as conn:
        row = conn.execute(
            select(oauth_client.c.created_at, oauth_client.c.approved_at).where(
                oauth_client.c.client_id == client_id
            )
        ).one()
    assert row.approved_at is None
    assert format_user_datetime(row.created_at) in reply.text
    assert "First connection for this app." in reply.text
    csp = reply.headers.get("content-security-policy", "")
    assert "form-action 'self'" in csp
    assert "form-action 'self' https:" not in csp
    assert reply.headers.get("referrer-policy") == "no-referrer"
    assert reply.headers.get("x-content-type-options") == "nosniff"
    _approve_page(client, request_id)
    request_id_2 = _request_id(_authorize(client, client_id, challenge, REDIRECT_CUSTOM_UNKNOWN))
    second = client.get(f"/vibesynergy/approve?request={request_id_2}")
    assert second.status_code == 200
    assert "First connection for this app." not in second.text


def test_deny(setup) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    reply = client.post("/vibesynergy/approve", data={"request": request_id, "action": "deny"})
    assert reply.status_code == 200
    assert "Access denied" in reply.text
    assert "location" not in reply.headers
    assert reply.headers.get("cache-control") == "no-store"
    assert "form-action" not in reply.headers.get("content-security-policy", "")
    callback = html_lib.unescape(_CALLBACK_INPUT.search(reply.text).group(1))
    query = parse_qs(urlparse(callback).query)
    assert query["error"] == ["access_denied"] and query["state"] == ["st-1"]


def test_deny_loopback_shows_result_page(setup) -> None:
    _, client = setup
    client_id = _register(client, redirect_uri=REDIRECT_LOOPBACK)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge, REDIRECT_LOOPBACK))
    reply = client.post("/vibesynergy/approve", data={"request": request_id, "action": "deny"})
    assert reply.status_code == 200 and "Access denied" in reply.text
    _assert_no_timed_handoff(reply)
    callback = html_lib.unescape(_CALLBACK_INPUT.search(reply.text).group(1))
    assert callback.startswith(REDIRECT_LOOPBACK)
    assert parse_qs(urlparse(callback).query)["error"] == ["access_denied"]


def test_deny_custom_scheme_never_meta_refreshes(setup) -> None:
    _, client = setup
    client_id = _register(client, redirect_uri=REDIRECT_CUSTOM)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge, REDIRECT_CUSTOM))
    reply = client.post("/vibesynergy/approve", data={"request": request_id, "action": "deny"})
    assert reply.status_code == 200 and "Access denied" in reply.text
    _assert_no_timed_handoff(reply)


def test_approve_get_already_approved(setup) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    first = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
    )
    assert first.status_code == 200 and "Approved" in first.text
    code = _CODE_INPUT.search(first.text).group(1)
    callback = html_lib.unescape(_CALLBACK_INPUT.search(first.text).group(1))
    reply = client.get(f"/vibesynergy/approve?request={request_id}")
    assert reply.status_code == 200 and "Already approved" in reply.text
    assert reply.headers.get("cache-control") == "no-store"
    _assert_no_oauth_credentials(
        reply,
        case_name="get_already_approved",
        forbidden_code=code,
        forbidden_callback=callback,
    )


@pytest.mark.parametrize(
    "redirect_uri",
    [REDIRECT_HTTPS, REDIRECT_LOOPBACK, REDIRECT_CUSTOM],
    ids=["https", "loopback", "custom_scheme"],
)
def test_approved_request_never_redisplays_credentials(setup, redirect_uri: str) -> None:
    _, browser = setup
    initiator = TestClient(browser.app, base_url=HOST, follow_redirects=False)
    attacker = TestClient(browser.app, base_url=HOST, follow_redirects=False)
    try:
        client_id = _register(initiator, name="MCP client", redirect_uri=redirect_uri)
        verifier, challenge = _pkce()
        request_id = _request_id(_authorize(initiator, client_id, challenge, redirect_uri))
        first = browser.post(
            "/vibesynergy/approve",
            data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
        )
        assert first.status_code == 200 and "Approved" in first.text
        code = _CODE_INPUT.search(first.text).group(1)
        callback = html_lib.unescape(_CALLBACK_INPUT.search(first.text).group(1))
        assert code in callback and callback.startswith(redirect_uri)
        cases = [
            (
                "attacker_get_vibesynergy",
                lambda: attacker.get(f"/vibesynergy/approve?request={request_id}"),
            ),
            ("attacker_get_root", lambda: attacker.get(f"/approve?request={request_id}")),
            (
                "browser_get_vibesynergy",
                lambda: browser.get(f"/vibesynergy/approve?request={request_id}"),
            ),
            (
                "attacker_post_wrong_passphrase_vibesynergy",
                lambda: attacker.post(
                    "/vibesynergy/approve",
                    data={"request": request_id, "passphrase": "not-the-passphrase"},
                ),
            ),
            (
                "attacker_post_wrong_passphrase_root",
                lambda: attacker.post(
                    "/approve",
                    data={"request": request_id, "passphrase": "not-the-passphrase"},
                ),
            ),
            (
                "browser_post_correct_passphrase_vibesynergy",
                lambda: browser.post(
                    "/vibesynergy/approve",
                    data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
                ),
            ),
            (
                "browser_post_missing_passphrase_vibesynergy",
                lambda: browser.post(
                    "/vibesynergy/approve", data={"request": request_id, "action": "approve"}
                ),
            ),
            (
                "browser_post_missing_passphrase_root",
                lambda: browser.post("/approve", data={"request": request_id, "action": "approve"}),
            ),
            (
                "browser_post_correct_passphrase_root",
                lambda: browser.post(
                    "/approve",
                    data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
                ),
            ),
        ]
        for case_name, do_request in cases:
            assert not attacker.cookies, f"{case_name}: attacker unexpectedly has cookies"
            reply = do_request()
            assert reply.status_code == 200, f"{case_name}: {reply.text}"
            assert "Already approved" in reply.text, f"{case_name}: {reply.text[:200]}"
            _assert_no_oauth_credentials(
                reply,
                case_name=case_name,
                forbidden_code=code,
                forbidden_callback=callback,
            )
        token = initiator.post(
            "/vibesynergy/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
    finally:
        initiator.close()
        attacker.close()


def test_provider_approved_outcome_never_returns_credentials(setup) -> None:
    """Provider layer must not re-issue code/callback after first approval."""
    _, client = setup
    provider = client.app.state.oauth
    client_id = _register(client, redirect_uri=REDIRECT_CUSTOM)
    verifier, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge, REDIRECT_CUSTOM))
    first = provider.approve(request_id, PASSPHRASE)
    assert first.kind == "success"
    assert first.code and first.callback_url

    cached = provider.approved_outcome(request_id)
    assert cached is not None and cached.kind == "already"
    assert cached.code == "" and cached.callback_url == ""

    second = provider.approve(request_id, PASSPHRASE)
    assert second.kind == "already"
    assert second.code == "" and second.callback_url == ""

    token = client.post(
        "/vibesynergy/token",
        data={
            "grant_type": "authorization_code",
            "code": first.code,
            "redirect_uri": REDIRECT_CUSTOM,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text


def test_approve_after_code_exchange_not_already_approved(setup) -> None:
    _, client = setup
    client_id = _register(client)
    verifier, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    first = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
    )
    assert first.status_code == 200 and "Approved" in first.text
    code = _CODE_INPUT.search(first.text).group(1)
    exchange = client.post(
        "/vibesynergy/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_HTTPS,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert exchange.status_code == 200, exchange.text
    again = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
    )
    assert again.status_code == 200
    assert "Already approved" not in again.text
    assert "expired" in again.text.lower()


def test_revoked_app_loses_access(setup) -> None:
    rt, client = setup
    tokens = _login(client)
    names = [c["name"] for c in oauth.list_clients(rt.engine)]
    assert names == ["Claude"]
    assert oauth.revoke_client(rt.engine, tokens["client_id"])
    auth = {**MCP_HEADERS, "authorization": f"Bearer {tokens['access_token']}"}
    assert client.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 401
    refresh = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": tokens["client_id"],
    }
    assert client.post("/vibesynergy/token", data=refresh).status_code in (400, 401)


def test_no_passphrase_means_no_approval(runtime) -> None:
    settings = replace(runtime.settings, server={"public_url": HOST})
    rt = Runtime.open(settings)
    with TestClient(create_app(rt), base_url=HOST, follow_redirects=False) as client:
        client_id = _register(client)
        _, challenge = _pkce()
        location = _authorize(client, client_id, challenge)
        assert "error=temporarily_unavailable" in location and location.startswith(REDIRECT_HTTPS)
    rt.close()


def test_stored_secrets_are_hashes(setup) -> None:
    rt, client = setup
    tokens = _login(client)
    from sqlalchemy import select

    from vibesynergy.store.schema import oauth_grant

    with rt.engine.connect() as conn:
        stored = conn.execute(select(oauth_grant.c.token_hash)).scalars().all()
    assert tokens["access_token"] not in stored and tokens["refresh_token"] not in stored
    record = SecretStore(rt.settings.secrets_dir, "mcp").get("passphrase")
    assert PASSPHRASE not in str(record)


def test_revoke_personal_access_token_prefers_id_over_name(runtime) -> None:
    now = utc_now()
    with runtime.engine.begin() as conn:
        conn.execute(
            mcp_pat.insert().values(
                id="id-first-wins",
                name="shared",
                token_hash="a" * 64,
                token_prefix="vit_pat_a1111111",
                created_at=now,
                last_used_at=None,
                revoked_at=None,
            )
        )
        conn.execute(
            mcp_pat.insert().values(
                id="shared",
                name="other",
                token_hash="b" * 64,
                token_prefix="vit_pat_b2222222",
                created_at=now,
                last_used_at=None,
                revoked_at=None,
            )
        )
    assert oauth.revoke_personal_access_token(runtime.engine, "shared")
    with runtime.engine.connect() as conn:
        by_name = conn.execute(
            select(mcp_pat.c.revoked_at).where(mcp_pat.c.id == "id-first-wins")
        ).one()
        by_id = conn.execute(select(mcp_pat.c.revoked_at).where(mcp_pat.c.id == "shared")).one()
    assert by_name.revoked_at is None
    assert by_id.revoked_at is not None


def test_consent_grants_only_read(setup) -> None:
    _, client = setup
    client_id = _register(client, name="Read only")
    verifier, challenge = _pkce()
    reply = client.get(
        "/vibesynergy/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_HTTPS,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-1",
            "scope": "read coach.write diary.write plan.propose publish",
            "resource": f"{HOST}/vibesynergy/mcp",
        },
        follow_redirects=False,
    )
    assert reply.status_code == 302, reply.text
    request_id = _request_id(reply.headers["location"])
    page = client.get(f"/vibesynergy/approve?request={request_id}")
    assert "Choose what it may do" in page.text
    assert "See my training and health data" in page.text
    assert "Write messages to me" in page.text
    assert "Log check-ins, notes and pain for me" in page.text
    assert "Suggest changes to my plan" in page.text
    assert "Publish stats to Strava or Garmin" in page.text
    assert "wants to read your VibeSynergy data" not in page.text
    approved = client.post(
        "/vibesynergy/approve",
        data={
            "request": request_id,
            "passphrase": PASSPHRASE,
            "action": "approve",
            "scopes_shown": "1",
            "scope": "read",
        },
    )
    code = _CODE_INPUT.search(approved.text)
    assert code, approved.text
    token = client.post(
        "/vibesynergy/token",
        data={
            "grant_type": "authorization_code",
            "code": code.group(1),
            "redirect_uri": REDIRECT_HTTPS,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    assert set(token.json()["scope"].split()) == {"read"}


def test_pat_expires(setup) -> None:
    from sqlalchemy import update

    rt, client = setup
    token = oauth.create_personal_access_token(rt.engine, "week", scopes=["read"], days=30)
    auth = {**MCP_HEADERS, "authorization": f"Bearer {token}"}
    assert client.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 200
    listed = oauth.list_personal_access_tokens(rt.engine)
    assert listed[0]["last_used"] is not None
    assert listed[0]["expires_at"] is not None
    with rt.engine.begin() as conn:
        conn.execute(
            update(mcp_pat)
            .where(mcp_pat.c.name == "week")
            .values(expires_at=utc_now() - timedelta(minutes=1))
        )
    assert client.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 401
