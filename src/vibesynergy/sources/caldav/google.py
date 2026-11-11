"""Google sign-in for CalDAV: OAuth 2.0 authorization code flow with PKCE and ``state``.

The user enters their own Google OAuth client (id and secret) once in the portal; both are
stored encrypted, as are the tokens. This sign-in only reads calendars, so it asks for the
narrowest scope, ``calendar.readonly`` ("see and download any calendar you can access").
The Garmin mirror refuses this account rather than requesting a broader scope. Google's
CalDAV guide doesn't list scopes; if Google's CalDAV endpoint refuses this scope for an
account, the discovery error says so and the broader ``calendar`` scope is the fallback
(change SCOPE; that fallback is not used by the mirror).

CalDAV requests then use ``Authorization: Bearer`` against GOOGLE_CALDAV. Access tokens are
refreshed automatically shortly before they expire.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as pysecrets
import time
from urllib.parse import urlencode

import requests

from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources.caldav.discovery import CalDAVError
from vibesynergy.sources.http_timeout import DEFAULT_HTTP_TIMEOUT_SECONDS, requests_timeout_pair

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALDAV = "https://apidata.googleusercontent.com/caldav/v2/"
APP, TOKENS, LOGIN = "google_app", "tokens", "google_login"
LOGIN_TTL_S = 15 * 60
REFRESH_MARGIN_S = 60


def app_configured(store: SecretStore) -> bool:
    app = store.get(APP) or {}
    return bool(app.get("client_id") and app.get("client_secret"))


def save_app(store: SecretStore, client_id: str, client_secret: str) -> None:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id or not client_secret or client_id == client_secret:
        raise CalDAVError("bad_google_app")
    store.set(APP, {"client_id": client_id, "client_secret": client_secret})


def start(store: SecretStore, redirect_uri: str) -> str:
    """The Google consent address; remembers state and PKCE verifier (one login at a time)."""
    app = store.get(APP) or {}
    if not app.get("client_id"):
        raise CalDAVError("no_google_app")
    state = pysecrets.token_urlsafe(32)
    verifier = pysecrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    store.set(
        LOGIN,
        {
            "state": state,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "created": time.time(),
        },
    )
    query = urlencode(
        {
            "client_id": app["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTH_URL}?{query}"


def _token_request(
    data: dict,
    http,
    *,
    request_timeout: float | tuple[float, float] | None = None,
) -> dict:
    timeout = request_timeout or requests_timeout_pair(DEFAULT_HTTP_TIMEOUT_SECONDS)
    try:
        response = http.post(TOKEN_URL, data=data, timeout=timeout)
    except requests.RequestException:
        raise CalDAVError("network") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("access_token"):
        if body.get("error") == "invalid_grant":
            raise CalDAVError("reauth")
        raise CalDAVError("google_failed")
    return body


def _store_tokens(store: SecretStore, body: dict, previous: dict | None = None) -> dict:
    tokens = {
        "access_token": body["access_token"],
        # Refresh responses omit the refresh token: keep the one we have.
        "refresh_token": body.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": time.time() + float(body.get("expires_in", 3600)),
    }
    store.set(TOKENS, tokens)
    return tokens


def complete(
    store: SecretStore,
    redirect_uri: str,
    code: str,
    state: str,
    http=None,
    *,
    request_timeout: float | tuple[float, float] | None = None,
) -> None:
    """Callback: check state, exchange the code (with the PKCE verifier), keep the tokens."""
    http = http or requests
    pending = store.get(LOGIN)
    store.delete(LOGIN)  # one use, whatever happens next
    if (
        not pending
        or not state
        or not hmac.compare_digest(str(pending["state"]), state)
        or time.time() - pending["created"] > LOGIN_TTL_S
        or pending["redirect_uri"] != redirect_uri
    ):
        raise CalDAVError("state_invalid")
    app = store.get(APP) or {}
    body = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": app.get("client_id"),
            "client_secret": app.get("client_secret"),
            "code_verifier": pending["verifier"],
        },
        http,
        request_timeout=request_timeout,
    )
    if not body.get("refresh_token"):
        raise CalDAVError("google_failed")
    _store_tokens(store, body)


def access_token(
    store: SecretStore,
    http=None,
    *,
    request_timeout: float | tuple[float, float] | None = None,
) -> str:
    """A valid access token, refreshed (and saved) when it is about to expire."""
    http = http or requests
    with store.lock(TOKENS):
        tokens = store.get(TOKENS)
        if not tokens:
            raise CalDAVError("reauth")
        if tokens["expires_at"] - time.time() > REFRESH_MARGIN_S:
            return tokens["access_token"]
        app = store.get(APP) or {}
        if not tokens.get("refresh_token"):
            raise CalDAVError("reauth")
        body = _token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": app.get("client_id"),
                "client_secret": app.get("client_secret"),
            },
            http,
            request_timeout=request_timeout,
        )
        return _store_tokens(store, body, tokens)["access_token"]
