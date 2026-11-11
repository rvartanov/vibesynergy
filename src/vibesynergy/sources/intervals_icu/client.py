"""Read-only intervals.icu API client.

Authentication is HTTP Basic with username ``API_KEY`` and the athlete's API key. The key
and athlete id are entered in Settings → Sources and stored encrypted (``credentials``);
the old ``INTERVALS_ICU_API_KEY`` / ``INTERVALS_ICU_ATHLETE_ID`` environment variables are
only a migration path. The key is never written to a log, an exception message, or
a URL.

Calls covered here: calendar events (workouts, races, notes, weekly targets), the
workout library, and activities. Writes are one event at a time, by intervals.icu
event id. There is no bulk upsert.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from vibesynergy.core.errors import RateLimited
from vibesynergy.core.timeutil import utc_now
from vibesynergy.sources.http_timeout import DEFAULT_HTTP_TIMEOUT_SECONDS, requests_timeout_pair
from vibesynergy.sources.intervals_icu import credentials

API = "https://intervals.icu/api/v1"
USER_AGENT = "VibeSynergy/1 (intervals.icu)"
# Writes stay at about one a second, well under the per-IP limit of about 10/s.
WRITE_INTERVAL_SECONDS = 1.0
# Legacy names, kept for callers and tests; see ``credentials``.
API_KEY_ENV = credentials.API_KEY_ENV
ATHLETE_ID_ENV = credentials.ATHLETE_ID_ENV

_ATHLETE = re.compile(r"[A-Za-z0-9_-]+")
# The only activity fields VibeSynergy writes: RPE and Feel (see query/activity_effort.py).
ACTIVITY_WRITE_FIELDS = frozenset({"icu_rpe", "feel"})
# The only sport-settings fields VibeSynergy writes (Training → Zones & thresholds).
SPORT_SETTINGS_WRITE_FIELDS = frozenset({"ftp", "lthr", "max_hr", "hr_zones", "threshold_pace"})
DESCRIPTION_MAX = 20000
# url, query, headers -> status, response headers, body
Get = Callable[[str, Mapping[str, str], Mapping[str, str]], tuple[int, Mapping[str, str], bytes]]
# method, url, query, headers, body -> status, response headers, body
Exchange = Callable[
    [str, str, Mapping[str, str], Mapping[str, str], bytes | None],
    tuple[int, Mapping[str, str], bytes],
]


class IntervalsIcuError(Exception):
    """intervals.icu refused the call or the client is not set up."""


class IntervalsIcuRateLimited(IntervalsIcuError, RateLimited):
    def __init__(self, retry_at: datetime | None) -> None:
        IntervalsIcuError.__init__(self, "intervals.icu rate limit")
        self.retry_at = retry_at


class _RedactFilter(logging.Filter):
    """Drop the API key (and its Basic token) from log records."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:
            message = ""
        if any(secret in message for secret in self._secrets):
            record.msg = _redact(message, self._secrets)
            record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            text = _redact(str(exc), self._secrets)
            if text != str(exc):
                record.exc_info = (IntervalsIcuError, IntervalsIcuError(text), record.exc_info[2])
        if record.exc_text:
            record.exc_text = _redact(record.exc_text, self._secrets)
        return True


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _install_redaction(secrets: tuple[str, ...]) -> None:
    filt = _RedactFilter(secrets)
    root = logging.getLogger()
    _attach(root, filt)
    for handler in list(root.handlers):
        _attach(handler, filt)
    for name in (
        "vibesynergy.sources.intervals_icu",
        "vibesynergy.sync.engine",
        "vibesynergy.sync.worker",
    ):
        _attach(logging.getLogger(name), filt)


def _attach(target: Any, filt: _RedactFilter) -> None:
    current = getattr(target, "filters", ())
    if any(isinstance(item, _RedactFilter) and item._secrets == filt._secrets for item in current):
        return
    target.addFilter(filt)


def account_label() -> str | None:
    """The athlete id when intervals.icu is set up; never the API key."""
    found = credentials.load()
    return found.athlete_id if found else None


def _default_get(timeout: float) -> Get:
    def get(
        url: str, params: Mapping[str, str], headers: Mapping[str, str]
    ) -> tuple[int, Mapping[str, str], bytes]:
        import requests

        response = requests.get(
            url,
            params=dict(params) or None,
            headers=dict(headers),
            timeout=requests_timeout_pair(timeout),
        )
        return response.status_code, {k: v for k, v in response.headers.items()}, response.content

    return get


class IntervalsIcuClient:
    """HTTP client. ``get`` and ``exchange`` are injectable so tests never open a socket."""

    def __init__(
        self,
        api_key: str,
        athlete_id: str,
        *,
        timeout: float | None = None,
        get: Get | None = None,
        exchange: Exchange | None = None,
        interval: float = 0.2,
        write_interval: float = WRITE_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        key = api_key.strip()
        athlete = athlete_id.strip()
        if not key:
            raise IntervalsIcuError("intervals.icu API key is empty")
        if not _ATHLETE.fullmatch(athlete):
            raise IntervalsIcuError("intervals.icu athlete id is not valid")
        self._api_key = key
        self._athlete_id = athlete
        self._timeout = timeout if timeout is not None else DEFAULT_HTTP_TIMEOUT_SECONDS
        self._get = get or _default_get(self._timeout)
        self._exchange = exchange
        self._interval = interval
        self._write_interval = write_interval
        self._sleep = sleep
        self._clock = clock
        self._last = 0.0
        self._last_write = 0.0
        token = base64.b64encode(f"API_KEY:{key}".encode()).decode()
        self._secrets = (key, token)
        _install_redaction(self._secrets)

    def __repr__(self) -> str:
        return f"IntervalsIcuClient(athlete_id={self._athlete_id!r})"

    @classmethod
    def from_env(cls, **kwargs: Any) -> IntervalsIcuClient:
        """A client with the stored key (Settings → Sources), or the legacy environment's.

        The name stays for its many callers; it no longer means only the environment.
        """
        found = credentials.load()
        if found is None:
            raise IntervalsIcuError(
                "intervals.icu is not set up: enter the API key under Settings → Sources"
            )
        return cls(found.api_key, found.athlete_id, **kwargs)

    def events(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        """Calendar events in the inclusive local-date window."""
        return self._get_list(
            "events", {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        )

    def workouts(self) -> list[dict[str, Any]]:
        """Workout library. Not the calendar."""
        return self._get_list("workouts", {})

    def activities(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        return self._get_list(
            "activities", {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        )

    def wellness(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        """Wellness records in the inclusive window: intervals.icu's own fitness (``ctl``),
        fatigue (``atl``), ``rampRate`` and the day's load (``ctlLoad``), one per date."""
        return self._get_list(
            "wellness", {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        )

    def notes(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        return [event for event in self.events(oldest, newest) if event.get("category") == "NOTE"]

    def weekly_targets(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        """Weekly targets and weekly notes (``for_week``, or category ``TARGET``)."""
        return [
            event
            for event in self.events(oldest, newest)
            if event.get("for_week") or event.get("category") == "TARGET"
        ]

    def weather_forecast(self) -> dict[str, Any]:
        """The athlete's weather forecasts (supporter feature): daily and hourly per location."""
        payload = self._request(f"/athlete/{self._athlete_id}/weather-forecast", {})
        if not isinstance(payload, dict):
            raise IntervalsIcuError("intervals.icu weather-forecast was not an object")
        return payload

    def _event_path(self, event_id: str) -> str:
        # Single events live under the athlete; a bare /events/{id} answers 404.
        return f"/athlete/{self._athlete_id}/events/{_event_path_id(event_id)}"

    def get_event(self, event_id: str) -> dict[str, Any]:
        """One calendar event. Used to check the version before a write."""
        return self._send("GET", self._event_path(event_id), None, write=True)

    def create_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Create one event. Not a bulk upsert."""
        return self._send("POST", f"/athlete/{self._athlete_id}/events", dict(event), write=True)

    def update_event(self, event_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        """Replace one event by its intervals.icu id."""
        return self._send("PUT", self._event_path(event_id), dict(event), write=True)

    def delete_event(self, event_id: str) -> None:
        """Delete one event by its intervals.icu id."""
        self._send("DELETE", self._event_path(event_id), None, write=True)

    def get_wellness(self, day: date) -> dict[str, Any]:
        """The wellness record for one local date. Missing day is an empty record.

        This is a read, so it uses the read interval rather than the write clock.
        """
        try:
            payload = self._send(
                "GET",
                f"/athlete/{self._athlete_id}/wellness/{day.isoformat()}",
                None,
                write=False,
            )
        except IntervalsIcuError as exc:
            if "HTTP 404" in str(exc):
                return {}
            raise
        return payload if isinstance(payload, dict) else {}

    def put_wellness(self, day: date, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Update one day's wellness. Callers must send only the fields they mean to change."""
        payload = self._send(
            "PUT",
            f"/athlete/{self._athlete_id}/wellness/{day.isoformat()}",
            dict(fields),
            write=True,
        )
        return payload if isinstance(payload, dict) else dict(fields)

    def get_athlete(self) -> dict[str, Any]:
        """The athlete's settings, e.g. ``private_wellness_keys`` (read before a private write)."""
        payload = self._request(f"/athlete/{self._athlete_id}", {})
        if not isinstance(payload, dict):
            raise IntervalsIcuError("intervals.icu athlete was not an object")
        return payload

    def sport_settings(self) -> list[dict[str, Any]]:
        """Per-sport settings: FTP, LTHR, max HR, threshold pace and zones."""
        return self._get_list("sport-settings", {})

    def update_sport_settings(self, settings_id: str | int, fields: Mapping[str, Any]) -> Any:
        """Change FTP, LTHR, max HR, HR zones and/or threshold pace (m/s) of one sport.

        Only those fields are accepted; HR zones are sent as given, never recalculated.
        """
        body = dict(fields)
        if not body or set(body) - SPORT_SETTINGS_WRITE_FIELDS:
            raise IntervalsIcuError(
                "only ftp, lthr, max_hr, hr_zones and threshold_pace are written to sport settings"
            )
        return self._send(
            "PUT",
            f"/athlete/{self._athlete_id}/sport-settings/{_event_path_id(str(settings_id))}",
            body,
            params={"recalcHrZones": "false"},
            write=True,
        )

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        """One activity, read right before a write to check what is already there."""
        payload = self._send("GET", f"/activity/{_event_path_id(activity_id)}", None, write=False)
        if not isinstance(payload, dict):
            raise IntervalsIcuError("intervals.icu activity was not an object")
        return payload

    def update_activity(self, activity_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Set RPE (``icu_rpe``, 1-10) and/or Feel (``feel``, 1-5) on one activity.

        Only these two fields are accepted, so a partial PUT cannot change anything else.
        """
        body = dict(fields)
        if not body or set(body) - ACTIVITY_WRITE_FIELDS:
            raise IntervalsIcuError("only icu_rpe and feel are written to an activity")
        payload = self._send("PUT", f"/activity/{_event_path_id(activity_id)}", body, write=True)
        return payload if isinstance(payload, dict) else body

    def update_activity_description(self, activity_id: str, description: str) -> dict[str, Any]:
        """Set only the activity's description (the athlete's text plus VibeSynergy's published
        block, see ``query/publication_icu``); no other field is sent."""
        if not isinstance(description, str) or len(description) > DESCRIPTION_MAX:
            raise IntervalsIcuError("the activity description is text of at most 20000 characters")
        payload = self._send(
            "PUT",
            f"/activity/{_event_path_id(activity_id)}",
            {"description": description},
            write=True,
        )
        return payload if isinstance(payload, dict) else {"description": description}

    def _get_list(self, resource: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        payload = self._request(f"/athlete/{self._athlete_id}/{resource}", params)
        if not isinstance(payload, list):
            raise IntervalsIcuError(f"intervals.icu {resource} was not a list")
        return payload

    def _request(self, path: str, params: Mapping[str, str]) -> Any:
        return self._send("GET", path, None, params=params, write=False)

    def _send(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        params: Mapping[str, str] | None = None,
        write: bool,
    ) -> Any:
        _reject_bulk(path)
        if write:
            self._wait_write()
        else:
            self._wait()
        url = f"{API}{path}"
        headers = {
            "Authorization": "Basic " + self._secrets[1],
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        query = params or {}
        try:
            if self._exchange is not None:
                status, response_headers, raw = self._exchange(method, url, query, headers, payload)
            elif method == "GET" and payload is None:
                status, response_headers, raw = self._get(url, query, headers)
            else:
                status, response_headers, raw = _default_exchange(self._timeout)(
                    method, url, query, headers, payload
                )
        except IntervalsIcuError:
            raise
        except Exception as exc:
            raise IntervalsIcuError(self._safe(str(exc))) from None
        text = raw.decode("utf-8", errors="replace")
        if status == 429:
            raise IntervalsIcuRateLimited(_retry_at(response_headers))
        if status in {401, 403}:
            raise IntervalsIcuError("intervals.icu refused the API key")
        if status >= 400:
            raise IntervalsIcuError(self._safe(f"intervals.icu HTTP {status}: {text[:200]}"))
        if not text:
            return [] if method == "GET" else None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise IntervalsIcuError("intervals.icu returned non-JSON") from None

    def _safe(self, text: str) -> str:
        return _redact(text, self._secrets)

    def _wait(self) -> None:
        self._pace(self._interval, "_last")

    def _wait_write(self) -> None:
        self._pace(self._write_interval, "_last_write")

    def _pace(self, interval: float, attr: str) -> None:
        if interval <= 0:
            return
        now = self._clock()
        delay = interval - (now - getattr(self, attr))
        if delay > 0:
            self._sleep(delay)
        setattr(self, attr, self._clock())


def _event_path_id(event_id: str) -> str:
    text = str(event_id).strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise IntervalsIcuError("intervals.icu event id is not valid")
    return text


def _reject_bulk(path: str) -> None:
    if "bulk" in path or "upsert" in path:
        raise IntervalsIcuError("intervals.icu bulk upsert is not used")


def _default_exchange(timeout: float) -> Exchange:
    def exchange(
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        import requests

        response = requests.request(
            method,
            url,
            params=dict(params) or None,
            headers=dict(headers),
            data=body,
            timeout=requests_timeout_pair(timeout),
        )
        return response.status_code, {k: v for k, v in response.headers.items()}, response.content

    return exchange


def _retry_at(headers: Mapping[str, str]) -> datetime:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value.strip()
            break
    if raw.isdigit():
        return utc_now() + timedelta(seconds=int(raw))
    if raw:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return when
    return utc_now() + timedelta(seconds=60)
