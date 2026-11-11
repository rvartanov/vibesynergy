"""Garmin Connect session handling on top of ``garminconnect``.

OAuth tokens and account identity metadata are stored; the password is used once during
portal sign-in in Settings → Sources and never written. Tokens refreshed during a session
are saved back so the login survives restarts.
"""

from __future__ import annotations

from typing import Any

from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources.http_timeout import DEFAULT_HTTP_TIMEOUT_SECONDS

TOKENS = "tokens"


class GarminAuthError(Exception):
    pass


def _garmin_class():
    from garminconnect import Garmin  # imported lazily: pulls in curl_cffi

    return Garmin


def apply_request_timeout(api: Any, timeout_s: float) -> None:
    """Default ``timeout`` on garminconnect's ``Client._run_request`` (library default is 15s)."""
    client = api.client
    original = client._run_request

    def _run_request(method: str, path: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout_s)
        return original(method, path, **kwargs)

    client._run_request = _run_request  # type: ignore[method-assign]


def connect(secrets: SecretStore, *, request_timeout_s: float | None = None) -> Any:
    """A logged-in ``garminconnect.Garmin`` from stored tokens."""
    stored = secrets.get(TOKENS)
    if not stored or not stored.get("tokenstore"):
        raise GarminAuthError(
            "not logged in to Garmin Connect; connect Garmin in Settings → Sources"
        )
    timeout_s = request_timeout_s if request_timeout_s is not None else DEFAULT_HTTP_TIMEOUT_SECONDS
    api = _garmin_class()()
    api.login(tokenstore=stored["tokenstore"])
    apply_request_timeout(api, timeout_s)
    save_tokens(secrets, api)
    return api


def save_tokens(secrets: SecretStore, api: Any) -> None:
    dumped = api.client.dumps()
    stored = secrets.get(TOKENS) or {}
    if stored.get("tokenstore") != dumped:
        secrets.set(TOKENS, {"tokenstore": dumped})
