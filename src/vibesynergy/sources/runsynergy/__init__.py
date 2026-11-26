"""RunSynergy: the group trainings from the running-group app, read from a calendar link.

Set up in Settings → Sources → RunSynergy by pasting the personal calendar link RunSynergy
gives under *Mijn gegevens*. Read only: nothing goes back to RunSynergy. Each event with
contract-1 data becomes a ``group_training`` planned item. Those are never counted as
planned training themselves; ``query.group_training`` turns the ones the athlete coaches into a
workout proposal for the plan's "Training geven" session that day.
"""

from __future__ import annotations

from datetime import timedelta

from vibesynergy.core.timeutil import Window, today_in, utc_now
from vibesynergy.model import CalendarSpan, PlannedItem
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Source
from vibesynergy.sources.runsynergy import feed

ITEM_KEY = "calendar-link"
PAST_DAYS = 14
AHEAD_DAYS = 366


class RunSynergySource(Source):
    id = "runsynergy"
    name = "RunSynergy"
    live_config = True  # the link is pasted in the portal, which switches the source on

    def datasets(self):
        return [
            DatasetSpec(
                "trainings",
                DatasetKind.SNAPSHOT,
                "Group trainings from your RunSynergy calendar link",
            )
        ]

    def configured(self) -> bool:
        return feed.link(self.ctx.settings.secrets_dir) is not None

    def _previous(self) -> dict:
        if not self.ctx.raw:
            return {}
        raw_id = self.ctx.raw.current_id(self.id, "trainings", ITEM_KEY)
        if not raw_id:
            return {}
        try:
            return self.ctx.raw.load(raw_id).json() or {}
        except (OSError, ValueError):
            return {}

    def fetch(self, dataset, window):
        import requests

        from vibesynergy.sources.http_timeout import http_timeout_seconds, requests_timeout_pair

        url = feed.link(self.ctx.settings.secrets_dir)
        if not url:
            raise ValueError("Paste your RunSynergy calendar link under Settings → Sources")
        previous = self._previous()
        headers = {"Accept": "text/calendar", "User-Agent": "VibeSynergy"}
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        timeout = requests_timeout_pair(http_timeout_seconds(self.ctx.settings, self.id))
        reply = requests.get(url, headers=headers, timeout=timeout)
        if reply.status_code == 304:
            return  # unchanged since the archived copy
        if reply.status_code == 404:
            raise ValueError(
                "RunSynergy does not know this calendar link (stopped or renewed): "
                "paste the current one under Settings → Sources"
            )
        reply.raise_for_status()
        text = reply.content.decode("utf-8", errors="replace")
        feed.parse(text, self.ctx.settings.timezone)  # refuse anything that is not a calendar
        today = today_in(self.ctx.settings.timezone)
        span = Window(today - timedelta(days=PAST_DAYS), today + timedelta(days=AHEAD_DAYS))
        yield FetchedItem(
            ITEM_KEY,
            {
                "ics": text,
                "etag": reply.headers.get("ETag"),
                "timezone": self.ctx.settings.timezone,
                "fetched_at": utc_now().isoformat(),
                "window": f"{span.start}..{span.end}",
            },
            window=span,
        )

    def mappers(self):
        return {"trainings": map_trainings}

    def authenticate(self):
        raise ValueError(
            "RunSynergy is set up in the portal: paste your calendar link under "
            "Settings → Sources → RunSynergy."
        )


KEEP = (
    "club",
    "club_id",
    "groep",
    "rol",
    "soort",
    "titel",
    "type",
    "onderdelen",
    "kern_stappen",
    "url",
)


def map_trainings(raw):
    snapshot = raw.json()
    span = Window.parse(snapshot["window"])
    for item in feed.parse(snapshot["ics"], snapshot["timezone"]):
        if not span.start <= item.day <= span.end:
            continue
        yield PlannedItem(
            source_id=item.uid,
            date=item.day,
            kind="group_training",
            title=item.summary or item.data.get("titel"),
            sport="running",
            extra={
                **{key: item.data[key] for key in KEEP if key in item.data},
                "start": item.start,
                "sequence": item.sequence,
                "cancelled": item.cancelled,
            },
        )
    yield CalendarSpan(span.start, span.end)
