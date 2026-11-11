"""Strava push subscription (``vibesynergy webhooks subscribe strava``).

One subscription per app. Strava validates the callback with a GET carrying
``hub.challenge`` and the verify token, so ``vibesynergy serve`` must be running and reachable
through the Funnel when subscribing.
"""

from __future__ import annotations

import secrets as pysecrets

from vibesynergy.core.config import Settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources import oauth
from vibesynergy.sources.strava.client import API, StravaClient, StravaError

WEBHOOK = "webhook"


def subscribe(client: StravaClient, settings: Settings, store: SecretStore) -> dict:
    if not settings.public_url:
        raise ValueError("set [server] public_url first")
    app = oauth.load_app(store, "strava")
    verify_token = pysecrets.token_urlsafe(24)
    store.set(WEBHOOK, {**(store.get(WEBHOOK) or {}), "verify_token": verify_token})
    reply = client._http(
        "POST",
        f"{API}/push_subscriptions",
        {
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "callback_url": f"{settings.public_url}/vibesynergy/strava/webhook",
            "verify_token": verify_token,
        },
        {},
    )
    if reply.status not in (200, 201) or not isinstance(reply.body, dict):
        raise StravaError(reply.status, str(reply.body)[:300])
    store.set(WEBHOOK, {"verify_token": verify_token, "subscription_id": reply.body["id"]})
    return reply.body


def listing(client: StravaClient, store: SecretStore) -> list[dict]:
    app = oauth.load_app(store, "strava")
    reply = client._http(
        "GET",
        f"{API}/push_subscriptions",
        {"client_id": app.client_id, "client_secret": app.client_secret},
        {},
    )
    if reply.status != 200:
        raise StravaError(reply.status, str(reply.body)[:300])
    return reply.body or []


def unsubscribe(client: StravaClient, store: SecretStore) -> list[int]:
    app = oauth.load_app(store, "strava")
    removed = []
    for sub in listing(client, store):
        reply = client._http(
            "DELETE",
            f"{API}/push_subscriptions/{sub['id']}",
            {"client_id": app.client_id, "client_secret": app.client_secret},
            {},
        )
        if reply.status not in (200, 204):
            raise StravaError(reply.status, str(reply.body)[:300])
        removed.append(sub["id"])
    store.set(WEBHOOK, {})
    return removed
