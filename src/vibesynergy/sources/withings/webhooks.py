"""Withings notification subscriptions (``vibesynergy webhooks subscribe withings``).

The callback URL carries a random token, because Withings does not sign notifications:
``<public_url>/vibesynergy/withings/notify/<token>``. Withings calls the URL when subscribing,
so ``vibesynergy serve`` must be running and reachable through the Funnel.
"""

from __future__ import annotations

import secrets as pysecrets

from vibesynergy.core.config import Settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources.withings.client import WithingsClient, WithingsError

WEBHOOK = "webhook"
APPLIS = {1: "weight", 4: "blood pressure / SpO2", 16: "activity", 44: "sleep", 54: "ECG"}


def callback_url(settings: Settings, store: SecretStore) -> str:
    hook = store.get(WEBHOOK) or {}
    if not hook.get("token"):
        hook = {"token": pysecrets.token_urlsafe(32)}
        store.set(WEBHOOK, hook)
    if not settings.public_url:
        raise ValueError("set [server] public_url first")
    return f"{settings.public_url}/vibesynergy/withings/notify/{hook['token']}"


def subscribe(client: WithingsClient, settings: Settings, store: SecretStore) -> dict[str, str]:
    url = callback_url(settings, store)
    outcome = {}
    for appli, label in APPLIS.items():
        try:
            client.call("/notify", "subscribe", callbackurl=url, appli=appli, comment="VibeSynergy")
            outcome[label] = "subscribed"
        except WithingsError as exc:
            outcome[label] = str(exc)
    return outcome


def unsubscribe(client: WithingsClient, settings: Settings, store: SecretStore) -> dict[str, str]:
    url = callback_url(settings, store)
    outcome = {}
    for appli, label in APPLIS.items():
        try:
            client.call("/notify", "revoke", callbackurl=url, appli=appli)
            outcome[label] = "removed"
        except WithingsError as exc:
            outcome[label] = str(exc)
    return outcome


def listing(client: WithingsClient) -> list[dict]:
    return client.call("/notify", "list").get("profiles") or []
