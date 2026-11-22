"""Garmin Connect source: every dataset of the design, via ``garminconnect``.

Configuration (``[sources.garmin]``)::

    enabled = true
    extra_datasets = ["nutrition_food_log"]   # off-by-default datasets to add
    # datasets = ["sleep", "hrv"]              # or: only these
    activity_details = true                   # per-activity detail calls (splits, zones, …)
    request_interval = 1.0                    # minimum seconds between API calls

Connect Garmin in the portal under Settings → Sources; OAuth tokens are stored.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from vibesynergy.core.config import calendar_lookahead_months
from vibesynergy.core.timeutil import Window, today_in
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Mapper, Source
from vibesynergy.sources.caldav.account import garmin_target
from vibesynergy.sources.garmin import client, mappers
from vibesynergy.sources.garmin.datasets import BY_NAME, DATASETS, _is_fatal

# Wellness FIT files for days this far back are complete and are not downloaded again.
_WELLNESS_SETTLED_DAYS = 3
_LOGIN_RETRY_SECONDS = 15 * 60
# Sports whose activities can have rounds (HIIT, boxing and similar interval classes).
_ROUNDS_SPORTS = ("hiit", "boxing", "martial", "cardio")
# Summary fields that change when an activity is edited, cut or merged on Garmin Connect.
_ACTIVITY_SHAPE = ("elapsedDuration", "distance", "lapCount", "workoutId", "startTimeGMT")
# garminconnect's error for a final HTTP answer ("API Error 500 - …"; Garmin answers 500
# for an activity without that part): asking again gives the same answer. Gateway errors
# (502-504), timeouts and lost connections are asked again at the next sync.
_API_ANSWER = re.compile(r"API Error (?:4\d\d|500)\b")


class GarminSource(Source):
    id = "garmin"
    name = "Garmin Connect"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._api: Any = None
        self._activity_lists: dict[Window, list[dict]] = {}
        self._login_error: Exception | None = None
        self._login_failed_at = 0.0

    def datasets(self) -> Sequence[DatasetSpec]:
        months = calendar_lookahead_months(self.ctx.settings)
        specs = []
        for item in DATASETS:
            spec = item.spec
            if spec.name == "scheduled_workouts":
                spec = replace(spec, lookahead_months=months)
            specs.append(spec)
        return specs

    def authenticate(self) -> None:
        raise client.GarminAuthError("Connect Garmin in Settings → Sources")

    @property
    def api(self) -> Any:
        """The logged-in session. A failed login is not retried for a while: every dataset
        of a sync would otherwise log in again, which gets accounts throttled or locked."""
        if self._api is None:
            now = time.monotonic()
            if self._login_error and now - self._login_failed_at < _LOGIN_RETRY_SECONDS:
                raise self._login_error
            from vibesynergy.sources.http_timeout import http_timeout_seconds

            timeout_s = http_timeout_seconds(self.ctx.settings, self.id)
            try:
                with self.ctx.secrets.lock("tokens"):
                    session = client.connect(self.ctx.secrets, request_timeout_s=timeout_s)
            except Exception as exc:
                self._login_error, self._login_failed_at = exc, now
                raise
            self._login_error = None
            interval = float(self.ctx.config.get("request_interval", 1.0))
            self._api = Throttled(session, interval, self.ctx.secrets, request_timeout_s=timeout_s)
        return self._api

    def fetch(self, dataset: DatasetSpec, window: Window | None) -> Iterator[FetchedItem]:
        match dataset.kind:
            case DatasetKind.SNAPSHOT:
                yield self._snapshot(dataset)
            case DatasetKind.DAILY:
                assert window is not None
                yield from self._daily(dataset, window)
            case DatasetKind.RANGE:
                assert window is not None
                yield from self._range(dataset, window)
            case DatasetKind.ENTITY:
                assert window is not None
                if dataset.name == "activities":
                    yield from self._activities(window)
                else:
                    yield from self._activity_files(window)
        if self._api is not None:
            with self.ctx.secrets.lock("tokens"):
                client.save_tokens(self.ctx.secrets, self._api.wrapped)

    def mappers(self) -> Mapping[str, Mapper]:
        return mappers.build(self.ctx.settings.timezone)

    # Fetchers per kind -------------------------------------------------------------

    def _snapshot(self, spec: DatasetSpec) -> FetchedItem:
        payload = _or_empty(BY_NAME[spec.name].call, self.api)
        return FetchedItem("current", payload, request={"dataset": spec.name})

    def _daily(self, spec: DatasetSpec, window: Window) -> Iterator[FetchedItem]:
        call = BY_NAME[spec.name].call
        today = today_in(self.ctx.settings.timezone)
        settled = today - timedelta(days=_WELLNESS_SETTLED_DAYS)
        for day in window.days():
            iso = day.isoformat()
            if spec.name == "wellness_files":
                if day >= today:  # still growing; today's intraday data comes from the JSON
                    continue
                yield FetchedItem(
                    iso,
                    _bound(_or_empty, call, self.api, iso, empty=b""),
                    request={"dataset": spec.name, "date": iso},
                    window=Window(day, day),
                    content_type="application/zip",
                    immutable=day <= settled,
                )
            else:
                yield FetchedItem(
                    iso,
                    _or_empty(call, self.api, iso),
                    request={"dataset": spec.name, "date": iso},
                    window=Window(day, day),
                )

    def _range(self, spec: DatasetSpec, window: Window) -> Iterator[FetchedItem]:
        """One item per calendar month, so item keys stay stable as the month fills."""
        call = BY_NAME[spec.name].call
        for month in window.months():
            end = min(month.end, window.end)
            request = {"dataset": spec.name, "start": str(month.start), "end": str(end)}
            if spec.name == "scheduled_workouts":
                request["garmin_target_calendar"] = garmin_target(self.ctx.settings)
            payload = _or_empty(call, self.api, month.start.isoformat(), end.isoformat())
            if spec.name == "scheduled_workouts":
                mappers.validate_calendar(payload)
            yield FetchedItem(
                month.start.strftime("%Y-%m"),
                payload,
                request=request,
                window=Window(month.start, end),
            )

    def _list_activities(self, window: Window, *, fresh: bool = False) -> list[dict]:
        """The activity list for ``window``. ``activities`` asks Garmin again every sync
        (``fresh``); ``activity_files`` right after it reuses that list. The source lives as
        long as the service, so a cached list would otherwise hide new workouts until the
        next restart or day change."""
        if fresh or window not in self._activity_lists:
            found = self.api.get_activities_by_date(
                window.start.isoformat(), window.end.isoformat(), None, "asc"
            )
            self._activity_lists[window] = list(found or [])
        return self._activity_lists[window]

    def listed(self, window: Window) -> set[str] | None:
        """The activity ids in the list this sync fetched for ``window`` (None: not
        fetched). The removal check of #151 compares it with the stored copies."""
        found = self._activity_lists.get(window)
        if found is None:
            return None
        return {str(a["activityId"]) for a in found if a.get("activityId") is not None}

    def _activities(self, window: Window) -> Iterator[FetchedItem]:
        details = bool(self.ctx.config.get("activity_details", True))
        for summary in self._list_activities(window, fresh=True):
            activity_id = str(summary["activityId"])
            payload = {"summary": summary}
            if details:
                payload.update(self._activity_details(activity_id, summary))
            day = _activity_day(summary)
            yield FetchedItem(
                activity_id,
                payload,
                request={"dataset": "activities", "activity_id": activity_id},
                window=Window(day, day) if day else None,
            )

    def _activity_details(self, activity_id: str, summary: dict) -> dict[str, Any]:
        api = self.api
        calls: dict[str, Callable[[], Any]] = {
            "activity": lambda: api.get_activity(activity_id),
            "splits": lambda: api.get_activity_splits(activity_id),
            "typed_splits": lambda: api.get_activity_typed_splits(activity_id),
            "split_summaries": lambda: api.get_activity_split_summaries(activity_id),
            "hr_zones": lambda: api.get_activity_hr_in_timezones(activity_id),
            "power_zones": lambda: api.get_activity_power_in_timezones(activity_id),
            "weather": lambda: api.get_activity_weather(activity_id),
            "gear": lambda: api.get_activity_gear(activity_id),
        }
        type_key = (summary.get("activityType") or {}).get("typeKey", "")
        if "strength" in type_key or "training" in type_key:
            calls["exercise_sets"] = lambda: api.get_activity_exercise_sets(activity_id)
        details: dict[str, Any] = {}
        errors: dict[str, str] = {}
        # Workout steps and rounds do not change once the activity is saved: they are asked
        # once per new or changed activity and copied from the archived version otherwise.
        once: dict[str, Callable[[], Any]] = {}
        if summary.get("workoutId"):  # a structured workout; others answer HTTP 500
            once["workout_steps"] = lambda: api.get_activity_step_details(activity_id)
        if any(word in type_key for word in _ROUNDS_SPORTS):
            once["rounds"] = lambda: api.get_activity_rounds(activity_id)
        if once:
            kept, kept_errors = self._archived_parts(activity_id, summary, once)
            details.update(kept)
            errors.update(kept_errors)
            calls.update({k: v for k, v in once.items() if k not in kept and k not in kept_errors})
        for key, call in calls.items():
            try:
                details[key] = call()
            except Exception as exc:
                if _is_fatal(exc):
                    raise
                errors[key] = f"{type(exc).__name__}: {exc}"
        if errors:
            details["errors"] = errors  # e.g. no weather for indoor rides
        return details

    def _archived_parts(
        self, activity_id: str, summary: dict, parts: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Parts of ``parts`` the archived version of this activity already has, with the
        errors Garmin answered for them. Nothing when the activity is new or its summary
        changed shape (length, distance, laps, workout). A failed request that never got
        an answer (timeout, connection) is asked again."""
        raw = self.ctx.raw
        raw_id = raw.current_id(self.id, "activities", activity_id) if raw else None
        if raw is None or raw_id is None:
            return {}, {}
        try:
            stored = raw.load(raw_id).json()
        except (ValueError, OSError):
            return {}, {}
        if not isinstance(stored, dict):
            return {}, {}
        before = stored.get("summary") or {}
        if any(before.get(k) != summary.get(k) for k in _ACTIVITY_SHAPE):
            return {}, {}
        stored_errors = stored.get("errors") or {}
        kept = {k: stored[k] for k in parts if k in stored}
        answered = {
            k: str(stored_errors[k])
            for k in parts
            if k not in kept and _API_ANSWER.search(str(stored_errors.get(k) or ""))
        }
        return kept, answered

    def _activity_files(self, window: Window) -> Iterator[FetchedItem]:
        from garminconnect import Garmin

        original = Garmin.ActivityDownloadFormat.ORIGINAL
        for summary in self._list_activities(window):
            activity_id = str(summary["activityId"])
            if summary.get("manualActivity"):
                continue  # manual entries have no file
            day = _activity_day(summary)
            yield FetchedItem(
                activity_id,
                _bound(_or_empty, self.api.download_activity, activity_id, original, empty=b""),
                request={"dataset": "activity_files", "activity_id": activity_id},
                window=Window(day, day) if day else None,
                content_type="application/zip",
                immutable=True,
            )


class Throttled:
    """Proxy that spaces method calls on the wrapped API at least ``interval`` apart.

    Garmin throttles and eventually blocks clients that call too fast; a multi-year
    backfill makes tens of thousands of calls.
    """

    def __init__(
        self,
        wrapped: Any,
        interval: float,
        secrets=None,
        *,
        request_timeout_s: float | None = None,
    ) -> None:
        self.wrapped = wrapped
        self._interval = interval
        self._last = 0.0
        self._secrets = secrets
        self._request_timeout_s = request_timeout_s

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.wrapped, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            method = attr
            wait = self._last + self._interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                if self._secrets is None:
                    return method(*args, **kwargs)
                with self._secrets.lock("tokens"):
                    latest = (self._secrets.get("tokens") or {}).get("tokenstore")
                    if latest and latest != self.wrapped.client.dumps():
                        self.wrapped = client.connect(
                            self._secrets, request_timeout_s=self._request_timeout_s
                        )
                        method = getattr(self.wrapped, name)
                    result = method(*args, **kwargs)
                    client.save_tokens(self._secrets, self.wrapped)
                    return result
            finally:
                self._last = time.monotonic()

        return call


def _bound(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[[], Any]:
    return lambda: fn(*args, **kwargs)


def _or_empty(fn: Callable[..., Any], *args: Any, empty: Any = None) -> Any:
    """Call the API; Garmin answers 404 when a day or item simply has no data (a day without
    wellness data, an activity without an original file). That is stored as an empty item,
    which records that the day was checked, instead of failing the whole dataset."""
    from garminconnect import GarminConnectNotFoundError

    try:
        return fn(*args)
    except GarminConnectNotFoundError:
        return empty


def _activity_day(summary: dict) -> date | None:
    local = summary.get("startTimeLocal")
    try:
        return date.fromisoformat(str(local)[:10]) if local else None
    except ValueError:
        return None
