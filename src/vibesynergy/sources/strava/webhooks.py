"""Strava push subscription (``vibesynergy webhooks subscribe strava``).

One subscription per app. Strava does not sign its events and its ids are not secret, so
the callback URL carries a random token, like the Withings one:
``<public_url>/vibesynergy/strava/webhook/<token>``. Strava validates the callback with a
GET carrying ``hub.challenge`` and the verify token, so ``vibesynergy serve`` must be
running and reachable through the Funnel when subscribing.

An install that subscribed before the token existed keeps a record without ``token``; only
then does the server still accept the old ``/strava/webhook`` path. Subscribing again
replaces the subscription with the tokenised URL and closes the old path.
"""

from __future__ import annotations

import secrets as pysecrets

from vibesynergy.core.config import Settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources import oauth
from vibesynergy.sources.strava.client import API, StravaClient, StravaError

WEBHOOK = "webhook"


def callback_url(settings: Settings, token: str) -> str:
    if not settings.public_url:
        raise ValueError("set [server] public_url first")
    return f"{settings.public_url}/vibesynergy/strava/webhook/{token}"


def subscribe(client: StravaClient, settings: Settings, store: SecretStore) -> dict:
    """(Re-)create the subscription with a fresh tokenised callback URL.

    Strava allows one subscription per app, so an existing one (for example with the old URL
    without a token) is deleted first."""
    if not settings.public_url:
        raise ValueError("set [server] public_url first")
    app = oauth.load_app(store, "strava")
    _delete_all(client, app)
    token = pysecrets.token_urlsafe(32)
    verify_token = pysecrets.token_urlsafe(24)
    # Stored before the POST: Strava calls the new URL during it.
    store.set(WEBHOOK, {"verify_token": verify_token, "token": token})
    reply = client._http(
        "POST",
        f"{API}/push_subscriptions",
        {
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "callback_url": callback_url(settings, token),
            "verify_token": verify_token,
        },
        {},
    )
    if reply.status not in (200, 201) or not isinstance(reply.body, dict):
        raise StravaError(reply.status, str(reply.body)[:300])
    store.set(
        WEBHOOK, {"verify_token": verify_token, "token": token, "subscription_id": reply.body["id"]}
    )
    return reply.body


def listing(client: StravaClient, store: SecretStore) -> list[dict]:
    return _listing(client, oauth.load_app(store, "strava"))


def _listing(client: StravaClient, app: oauth.OAuthApp) -> list[dict]:
    reply = client._http(
        "GET",
        f"{API}/push_subscriptions",
        {"client_id": app.client_id, "client_secret": app.client_secret},
        {},
    )
    if reply.status != 200:
        raise StravaError(reply.status, str(reply.body)[:300])
    return reply.body or []


def _delete_all(client: StravaClient, app: oauth.OAuthApp) -> list[int]:
    removed = []
    for sub in _listing(client, app):
        reply = client._http(
            "DELETE",
            f"{API}/push_subscriptions/{int(sub['id'])}",
            {"client_id": app.client_id, "client_secret": app.client_secret},
            {},
        )
        if reply.status not in (200, 204):
            raise StravaError(reply.status, str(reply.body)[:300])
        removed.append(sub["id"])
    return removed


def unsubscribe(client: StravaClient, store: SecretStore) -> list[int]:
    removed = _delete_all(client, oauth.load_app(store, "strava"))
    store.set(WEBHOOK, {})
    return removed
