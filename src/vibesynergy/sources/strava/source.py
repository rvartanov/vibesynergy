"""Strava source: every dataset of the design through the official API v3.

Configuration (``[sources.strava]``)::

    enabled = true
    activity_details = true      # per-activity detail, laps and zones (3 calls each)

Strava is mostly a second copy of Garmin activities. Its own value is activities recorded
elsewhere (Zwift, other apps), user-edited titles and descriptions, and segments. The
default budget is 200 requests per 15 minutes and 2,000 per day. The API is only for what
is new: a sync looks back from the last one with a small overlap (never further than
``RECENT_DAYS``), and the past comes from the account archive (Sources → Strava → import).
Webhook events fetch single activities first. Slowly changing datasets (profile, gear, …)
are left alone for a day, and an activity whose summary hasn't changed isn't fetched again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.core.timeutil import Window
from vibesynergy.sources import oauth
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Mapper, Source
from vibesynergy.sources.strava import mappers
from vibesynergy.sources.strava.client import (
    StravaClient,
    StravaError,
    StravaRateLimited,
)

STREAM_KEYS = (
    "time,latlng,distance,altitude,velocity_smooth,heartrate,cadence,watts,temp,moving,grade_smooth"
)

LIST_KEPT_S = 120  # how long one sync's activity list may be reused by the next dataset
RECENT_DAYS = 30  # the API never reaches back further than this
DAY_MINUTES = 24 * 60

DATASETS = [
    DatasetSpec("activities", DatasetKind.ENTITY,
                "Activities: detail (incl. description, device, gear), laps, zones, efforts",
                initial_days=7, refetch_days=2, max_days=RECENT_DAYS),
    DatasetSpec("activity_streams", DatasetKind.ENTITY,
                "Activity streams (all channels), fetched once", initial_days=7, refetch_days=2,
                max_days=RECENT_DAYS),
    DatasetSpec("profile", DatasetKind.SNAPSHOT, "Athlete profile, zones and totals",
                min_age_minutes=6 * 60),
    DatasetSpec("gear", DatasetKind.SNAPSHOT, "Bikes and shoes with distance",
                min_age_minutes=DAY_MINUTES),
    DatasetSpec("routes", DatasetKind.SNAPSHOT, "Routes", min_age_minutes=DAY_MINUTES),
    DatasetSpec("starred_segments", DatasetKind.SNAPSHOT, "Starred segments",
                min_age_minutes=DAY_MINUTES),
    DatasetSpec("clubs", DatasetKind.SNAPSHOT, "Clubs", min_age_minutes=DAY_MINUTES),
]  # fmt: skip

# What identifies an activity's state in both the list summary and the detail.
_REVISION_FIELDS = (
    "name", "sport_type", "start_date", "distance", "moving_time", "elapsed_time",
    "total_elevation_gain", "gear_id", "private", "commute", "manual", "trainer",
    "average_heartrate", "average_watts",
)  # fmt: skip


def revision(summary: dict) -> str:
    """A token for an activity's state; kudos and the like don't change it."""
    state = [summary.get(k) for k in _REVISION_FIELDS]
    return hashlib.sha256(json.dumps(state, default=str).encode()).hexdigest()[:16]


class StravaSource(Source):
    id = "strava"
    name = "Strava"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._client: StravaClient | None = None
        self._lists: dict[Window, tuple[float, list[dict]]] = {}

    @property
    def client(self) -> StravaClient:
        if self._client is None:
            from vibesynergy.sources.http_timeout import http_timeout_seconds

            self._client = StravaClient(
                self.ctx.secrets,
                timeout=http_timeout_seconds(self.ctx.settings, self.id),
            )
        return self._client

    def datasets(self) -> Sequence[DatasetSpec]:
        return DATASETS

    def mappers(self) -> Mapping[str, Mapper]:
        return {"activities": mappers.map_activity, "activity_streams": mappers.map_streams}

    def authenticate(self) -> None:
        raise oauth.AuthError("Connect Strava in Settings → Sources")

    def fetch(self, dataset: DatasetSpec, window: Window | None) -> Iterator[FetchedItem]:
        c = self.client
        match dataset.name:
            case "profile":
                athlete = c.get("/athlete")
                payload = {
                    "athlete": athlete,
                    "zones": self._optional(lambda: c.get("/athlete/zones")),
                    "stats": c.get(f"/athletes/{athlete['id']}/stats"),
                }
                yield FetchedItem("current", payload)
            case "gear":
                athlete = c.get("/athlete")
                ids = [g["id"] for g in (athlete.get("bikes") or []) + (athlete.get("shoes") or [])]
                yield FetchedItem("current", {gid: c.get(f"/gear/{gid}") for gid in ids})
            case "routes":
                athlete_id = c.get("/athlete")["id"]
                yield FetchedItem("current", c.paged(f"/athletes/{athlete_id}/routes", 100))
            case "starred_segments":
                yield FetchedItem("current", c.paged("/segments/starred", 100))
            case "clubs":
                yield FetchedItem("current", c.paged("/athlete/clubs", 100))
            case "activities":
                assert window is not None
                for summary in self._list(window):
                    yield self._activity_item(summary)
            case "activity_streams":
                assert window is not None
                for summary in self._list(window):
                    if not summary.get("manual"):
                        yield self._streams_item(summary)

    def fetch_one(self, dataset: str, key: str) -> Iterator[FetchedItem]:
        """A single activity (for webhook events); raises StravaError 404 if deleted.

        Uses the calls reserved when a scheduled sync has stopped short of the hard limit.
        The context stays active while the caller reads the yielded item, so laps, zones
        and streams from that item use the same reserve.
        """
        with self.client.priority_budget():
            detail = self.client.get(f"/activities/{key}", include_all_efforts="true")
            if dataset == "activities":
                yield self._activity_item(detail, detail=detail)
            elif dataset == "activity_streams" and not detail.get("manual"):
                yield self._streams_item(detail)

    # Helpers ----------------------------------------------------------------------------

    def _list(self, window: Window) -> list[dict]:
        # Shared by the datasets of one sync (activities, streams), but not by later syncs:
        # the service keeps this object, and a list kept all day would miss new activities.
        cached = self._lists.get(window)
        if cached is None or monotonic() - cached[0] > LIST_KEPT_S:
            tz = ZoneInfo(self.ctx.settings.timezone)
            after = datetime.combine(window.start, time(), tzinfo=tz) - timedelta(hours=14)
            before = datetime.combine(window.end + timedelta(days=1), time(), tzinfo=tz)
            found = self.client.paged(
                "/athlete/activities",
                after=int(after.timestamp()),
                before=int(before.timestamp()) + 14 * 3600,
            )
            # Wide query (time zones), then keep activities whose local date is in window.
            self._lists[window] = (
                monotonic(),
                [
                    a
                    for a in found
                    if window.start.isoformat()
                    <= str(a.get("start_date_local"))[:10]
                    <= window.end.isoformat()
                ],
            )
        return self._lists[window][1]

    def _activity_item(self, summary: dict, detail: dict | None = None) -> FetchedItem:
        activity_id = str(summary["id"])

        def load() -> dict[str, Any]:
            payload: dict[str, Any] = {"summary": summary}
            if self.ctx.config.get("activity_details", True):
                c = self.client
                errors: dict[str, str] = {}
                for key, call in (
                    (
                        "detail",
                        lambda: (
                            detail
                            or c.get(f"/activities/{activity_id}", include_all_efforts="true")
                        ),
                    ),
                    ("laps", lambda: c.get(f"/activities/{activity_id}/laps")),
                    ("zones", lambda: c.get(f"/activities/{activity_id}/zones")),
                ):
                    try:
                        payload[key] = call()
                    except StravaRateLimited:
                        raise
                    except StravaError as exc:
                        errors[key] = str(exc)  # e.g. zones need a subscription
                if errors:
                    payload["errors"] = errors
            return payload

        day = _local_day(summary)
        return FetchedItem(
            activity_id,
            load,
            request={
                "dataset": "activities",
                "activity_id": activity_id,
                "revision": revision(summary),
            },
            window=Window(day, day) if day else None,
            revision=revision(summary),
        )

    def _streams_item(self, summary: dict) -> FetchedItem:
        activity_id = str(summary["id"])

        def load() -> dict:
            streams = self.client.get(
                f"/activities/{activity_id}/streams", keys=STREAM_KEYS, key_by_type="true"
            )
            return {
                "activity_id": activity_id,
                "start_date": summary.get("start_date"),
                "streams": streams,
            }

        day = _local_day(summary)
        return FetchedItem(
            activity_id,
            load,
            request={"dataset": "activity_streams", "activity_id": activity_id},
            window=Window(day, day) if day else None,
            immutable=True,
        )

    @staticmethod
    def _optional(call) -> Any:
        try:
            return call()
        except StravaRateLimited:
            raise
        except StravaError as exc:
            return {"error": str(exc)}


def _local_day(summary: dict) -> date | None:
    local = summary.get("start_date_local")
    try:
        return date.fromisoformat(str(local)[:10]) if local else None
    except ValueError:
        return None
