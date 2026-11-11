"""intervals.icu read-only source.

The API key and athlete id come from the environment (``INTERVALS_ICU_API_KEY``,
``INTERVALS_ICU_ATHLETE_ID``). Turn the source on in Settings → Sources like the others.
This plugin only reads: events (including weekly targets and notes), the workout
library, activities and wellness (its fitness, fatigue and ramp rate per day). Calendar
events are cached in ``icu_event``. Workouts, activities and wellness stay in the raw
archive; ``query.icu_fitness`` reads activities and wellness from there.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import timedelta

from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Mapper, Source
from vibesynergy.sources.http_timeout import http_timeout_seconds
from vibesynergy.sources.intervals_icu import client, mappers
from vibesynergy.sources.registry import SourceError

# Spec window: 14 days back through 300 days ahead. initial_days includes today,
# so 15 reaches today-14. max_days stops a later sync from walking further back.
_LOOKBACK_DAYS = 14
_LOOKAHEAD_DAYS = 300
_WELLNESS_DAYS = 366


def _window_spec(name: str, description: str, *, lookahead_days: int = 0) -> DatasetSpec:
    return DatasetSpec(
        name,
        DatasetKind.RANGE,
        description,
        initial_days=_LOOKBACK_DAYS + 1,
        refetch_days=_LOOKBACK_DAYS,
        lookahead_days=lookahead_days,
        max_days=_LOOKBACK_DAYS,
    )


def _plan_window(timezone: str, window: Window) -> Window:
    """The requested window widened to the full plan span (lookback to lookahead)."""
    today = today_in(timezone)
    start = min(window.start, today - timedelta(days=_LOOKBACK_DAYS))
    end = max(window.end, today + timedelta(days=_LOOKAHEAD_DAYS))
    return Window(start, end)


DATASETS = [
    _window_spec(
        "events",
        "Calendar events: workouts, races, notes and weekly targets",
        lookahead_days=_LOOKAHEAD_DAYS,
    ),
    DatasetSpec("workouts", DatasetKind.SNAPSHOT, "Workout library"),
    _window_spec("activities", "Activities recorded on intervals.icu"),
    # intervals.icu's fitness, fatigue and ramp rate per day, so the Training page
    # shows its numbers. A year on the first sync; one payload per month like activities.
    DatasetSpec(
        "wellness",
        DatasetKind.RANGE,
        "Fitness, fatigue and ramp rate per day as intervals.icu computes them",
        initial_days=_WELLNESS_DAYS,
        refetch_days=_LOOKBACK_DAYS,
    ),
]


class IntervalsIcuSource(Source):
    id = "intervals_icu"
    name = "intervals.icu"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._client: client.IntervalsIcuClient | None = None

    def datasets(self) -> Sequence[DatasetSpec]:
        return DATASETS

    def configured(self) -> bool:
        return client.account_label() is not None

    def authenticate(self) -> None:
        raise SourceError(
            "Enter the intervals.icu API key and athlete id under Settings → Sources."
        )

    @property
    def client(self) -> client.IntervalsIcuClient:
        if self._client is None:
            self._client = client.IntervalsIcuClient.from_env(
                timeout=http_timeout_seconds(self.ctx.settings, self.id),
                interval=float(self.ctx.config.get("request_interval", 0.2)),
            )
        return self._client

    def fetch(self, dataset: DatasetSpec, window: Window | None) -> Iterator[FetchedItem]:
        if dataset.name == "events":
            assert window is not None
            # One raw item ("calendar") holds the whole plan, and each new version replaces the
            # previous one. A narrow window (Refresh today, a one-day sync) would therefore drop
            # every planned day outside it (1 Nov 2026), so always ask for the full span too.
            window = _plan_window(self.ctx.settings.timezone, window)
            yield FetchedItem(
                "calendar",
                self.client.events(window.start, window.end),
                request={
                    "dataset": "events",
                    "oldest": window.start.isoformat(),
                    "newest": window.end.isoformat(),
                },
                window=window,
            )
            return
        if dataset.name == "workouts":
            yield FetchedItem("current", self.client.workouts(), request={"dataset": "workouts"})
            return
        if dataset.name in ("activities", "wellness"):
            assert window is not None
            read = self.client.activities if dataset.name == "activities" else self.client.wellness
            # One raw item per month, and each new version replaces the previous one, so
            # always fetch the whole month (up to today): a 14-day refetch that starts mid-month
            # would otherwise drop the month's earlier days from the archive.
            today = today_in(self.ctx.settings.timezone)
            for month in window.months():
                start = month.start
                end = min(month.end, max(today, window.end))
                yield FetchedItem(
                    month.start.strftime("%Y-%m"),
                    read(start, end),
                    request={
                        "dataset": dataset.name,
                        "oldest": start.isoformat(),
                        "newest": end.isoformat(),
                    },
                    window=Window(start, end),
                )
            return
        raise KeyError(dataset.name)

    def mappers(self) -> Mapping[str, Mapper]:
        return {"events": mappers.map_events}
