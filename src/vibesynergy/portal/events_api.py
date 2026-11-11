"""Settings → Sync between sources: webhook links for coach agents.

Secrets are write-only: the page sends one with ``secret`` and only ever gets ``has_secret``
back. Errors are plain English sentences the page translates; they never include the secret.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from vibesynergy.query import agent_events
from vibesynergy.runtime import Runtime


class Subscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None
    auth: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None
    daily_cap: int | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    only_missing_insight: bool | None = None
    secret: str | None = None


def build_events_router(rt: Runtime) -> APIRouter:
    router = APIRouter()

    def values(body: Subscription) -> tuple[dict[str, Any], str | None]:
        data = body.model_dump(exclude_none=True)
        secret = data.pop("secret", None)
        return data, (secret if secret else None)

    @router.get("/events/subscriptions")
    def list_subscriptions() -> dict[str, Any]:
        return {"data": agent_events.overview(rt)}

    @router.post("/events/subscriptions")
    def create_subscription(body: Subscription) -> dict[str, Any]:
        data, secret = values(body)
        agent_events.save_subscription(rt, data, secret=secret)
        return {"data": agent_events.overview(rt)}

    @router.post("/events/subscriptions/{sub_id}")
    def change_subscription(sub_id: str, body: Subscription) -> dict[str, Any]:
        data, secret = values(body)
        agent_events.save_subscription(rt, data, sub_id=sub_id, secret=secret)
        return {"data": agent_events.overview(rt)}

    @router.post("/events/subscriptions/{sub_id}/delete")
    def remove_subscription(sub_id: str) -> dict[str, Any]:
        agent_events.delete_subscription(rt, sub_id)
        return {"data": agent_events.overview(rt)}

    @router.post("/events/subscriptions/{sub_id}/test")
    def test_subscription(sub_id: str) -> dict[str, Any]:
        delivery = agent_events.send_test(rt, sub_id)
        return {"data": {"delivery": delivery, **agent_events.overview(rt)}}

    return router
