"""Thin Withings Public API client (docs: developer.withings.com/api-reference).

Every call is a form POST ``action=…`` with a bearer token; responses are
``{"status": 0, "body": {…}}``, where a non-zero status is an error. Paginated endpoints
return ``more`` and ``offset``. Rate limit: 120 requests per minute per app.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from vibesynergy.core.errors import RateLimited
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.sources import oauth
from vibesynergy.sources.http_timeout import DEFAULT_HTTP_TIMEOUT_SECONDS

API = "https://wbsapi.withings.net"
AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
SCOPES = "user.info,user.metrics,user.activity,user.sleepevents"

# Status codes (https://developer.withings.com/api-reference/#section/Response-status)
_INVALID_TOKEN = {401}
_RATE_LIMITED = {601}

Post = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


class WithingsError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Withings status {status}: {message}")
        self.status = status


class WithingsRateLimited(WithingsError, RateLimited):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(status, message)
        self.retry_at = utc_now() + timedelta(seconds=65)  # the limit is per minute


def _requests_post(
    url: str, data: dict[str, Any], headers: dict[str, str], *, timeout: float
) -> dict[str, Any]:
    import requests

    response = requests.post(url, data=data, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _default_post(timeout: float) -> Post:
    return lambda url, data, headers: _requests_post(url, data, headers, timeout=timeout)


class WithingsClient:
    def __init__(
        self,
        store: SecretStore,
        *,
        timeout: float | None = None,
        post: Post | None = None,
        interval: float = 0.6,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._timeout = timeout if timeout is not None else DEFAULT_HTTP_TIMEOUT_SECONDS
        self._post = post or _default_post(self._timeout)
        self._interval = interval
        self._sleep = sleep
        self._last = 0.0

    # Login ------------------------------------------------------------------------------

    def exchange_code(self, app: oauth.OAuthApp, code: str, redirect_uri: str) -> dict:
        body = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": app.client_id,
                "client_secret": app.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )
        return oauth.save_tokens(self._store, body)

    def _refresh(self) -> dict:
        tokens = self._store.get(oauth.TOKENS) or {}
        if not tokens.get("refresh_token"):
            raise oauth.AuthError("not logged in to Withings; run `vibesynergy auth withings`")
        app = oauth.load_app(self._store, "withings")
        body = self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": app.client_id,
                "client_secret": app.client_secret,
                "refresh_token": tokens["refresh_token"],
            }
        )
        return oauth.save_tokens(self._store, body)

    def _token_request(self, params: dict[str, Any]) -> dict:
        reply = self._post(f"{API}/v2/oauth2", {"action": "requesttoken", **params}, {})
        if reply.get("status") != 0:
            raise oauth.AuthError(f"Withings token request failed: {reply}")
        return reply["body"]

    def _access_token(self) -> str:
        tokens = self._store.get(oauth.TOKENS)
        if not tokens:
            raise oauth.AuthError("not logged in to Withings; run `vibesynergy auth withings`")
        if oauth.needs_refresh(tokens):
            tokens = self._refresh()
        return tokens["access_token"]

    # Calls ------------------------------------------------------------------------------

    def call(self, path: str, action: str, **params: Any) -> dict[str, Any]:
        """One API call; returns ``body``. Refreshes an expired token once."""
        for attempt in (1, 2):
            wait = self._last + self._interval - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            data = {"action": action, **{k: v for k, v in params.items() if v is not None}}
            reply = self._post(f"{API}{path}", data, headers)
            self._last = time.monotonic()
            status = int(reply.get("status", -1))
            if status == 0:
                return reply.get("body") or {}
            if status in _INVALID_TOKEN and attempt == 1:
                self._refresh()
                continue
            error = reply.get("error", "")
            if status in _RATE_LIMITED:
                raise WithingsRateLimited(status, error or "too many requests")
            raise WithingsError(status, error)
        raise AssertionError("unreachable")

    def paged(self, path: str, action: str, key: str, **params: Any) -> dict[str, Any]:
        """Follow ``more``/``offset``; returns the first body with ``key`` holding all items."""
        first: dict[str, Any] | None = None
        items: list[Any] = []
        offset = None
        for _ in range(1000):
            body = self.call(path, action, offset=offset, **params)
            first = first or body
            items.extend(body.get(key) or [])
            if not body.get("more"):
                break
            offset = body.get("offset")
        result = dict(first or {})
        result[key] = items
        result.pop("more", None)
        result.pop("offset", None)
        return result
