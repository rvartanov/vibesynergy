"""Thin Strava API v3 client (docs: developers.strava.com/docs/reference).

Raw JSON is what VibeSynergy archives, so this calls the REST API directly instead of going
through a model library. Rate limits are read from the ``X-RateLimit-*`` and
``X-ReadRateLimit-*`` headers: a nearly spent budget defers subsequent requests to the next
window. Successful replies are returned and the worker can process other jobs while the
limited sync waits.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vibesynergy.core.errors import RateLimited
from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources import oauth
from vibesynergy.sources.http_timeout import DEFAULT_HTTP_TIMEOUT_SECONDS

log = logging.getLogger(__name__)

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
SCOPES = "read,read_all,profile:read_all,activity:read_all,activity:write"
_WINDOW = 15 * 60
_DAY = 24 * 3600  # the daily budget resets at midnight UTC
_HEADROOM = 5  # scheduled syncs stop this many calls early; webhook fetches may use them


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any


Http = Callable[[str, str, dict[str, Any], dict[str, str]], Response]


class StravaError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Strava HTTP {status}: {message}")
        self.status = status


class StravaRateLimited(StravaError, RateLimited):
    def __init__(self, status: int, message: str, retry_at: datetime | None = None) -> None:
        super().__init__(status, message)
        self.retry_at = retry_at


def _requests_http(
    method: str,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: float,
) -> Response:
    import requests

    if method == "PUT":
        r = requests.put(url, data=params, headers=headers, timeout=timeout)
    elif method == "POST":
        r = requests.post(url, data=params, headers=headers, timeout=timeout)
    elif method == "DELETE":
        r = requests.delete(url, params=params, headers=headers, timeout=timeout)
    else:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return Response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, body)


def _default_http(timeout: float) -> Http:
    return lambda method, url, params, headers: _requests_http(
        method, url, params, headers, timeout=timeout
    )


class StravaClient:
    def __init__(
        self,
        store: SecretStore,
        *,
        timeout: float | None = None,
        http: Http | None = None,
        interval: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._timeout = timeout if timeout is not None else DEFAULT_HTTP_TIMEOUT_SECONDS
        self._http = http or _default_http(self._timeout)
        self._interval = interval
        self._sleep = sleep
        self._clock = clock
        self._last = 0.0
        self._call_lock = threading.RLock()
        self._local = threading.local()
        self._limited_until: tuple[datetime, str] | None = None
        self._hard_until: datetime | None = None
        self._hard_message = ""

    # Login ------------------------------------------------------------------------------

    def exchange_code(self, app: oauth.OAuthApp, code: str, scope: str | None = None) -> dict:
        with self._store.lock(oauth.TOKENS):
            result = self._token_request(
                {
                    "client_id": app.client_id,
                    "client_secret": app.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                }
            )
            if scope is not None:
                result = {**result, "scope": scope}
                self._store.set(oauth.TOKENS, result)
            return result

    def _refresh(self, *, rejected_access: str | None = None) -> dict:
        # Reread after locking: another process may already have rotated the token.
        with self._store.lock(oauth.TOKENS):
            tokens = self._store.get(oauth.TOKENS) or {}
            if not tokens.get("refresh_token"):
                raise oauth.AuthError(
                    "not logged in to Strava; connect Strava in Settings → Sources"
                )
            if (
                not oauth.needs_refresh(tokens)
                and tokens.get("access_token")
                and (rejected_access is None or tokens["access_token"] != rejected_access)
            ):
                return tokens
            app = oauth.load_app(self._store, "strava")
            return self._token_request(
                {
                    "client_id": app.client_id,
                    "client_secret": app.client_secret,
                    "refresh_token": tokens["refresh_token"],
                    "grant_type": "refresh_token",
                }
            )

    def _token_request(self, params: dict[str, Any]) -> dict:
        reply = self._http("POST", TOKEN_URL, params, {})
        if reply.status != 200 or not isinstance(reply.body, dict):
            raise oauth.AuthError(
                f"Strava token request failed ({reply.status}); no credentials saved"
            )
        body = dict(reply.body)
        athlete = body.pop("athlete", None)
        if not all(
            isinstance(body.get(key), str) and body[key]
            for key in ("access_token", "refresh_token")
        ):
            raise oauth.AuthError(
                "Strava returned incomplete tokens; previous credentials retained"
            )
        try:
            expiry = int(body.get("expires_at", self._clock() + int(body.get("expires_in", 0))))
        except (ValueError, TypeError):
            raise oauth.AuthError("Strava returned invalid token expiry") from None
        if expiry <= self._clock() + 120:
            raise oauth.AuthError("Strava returned an expired token; previous credentials retained")
        existing = self._store.get("athlete") or {}
        if athlete and existing.get("id") and str(existing["id"]) != str(athlete.get("id")):
            raise oauth.AuthError("Strava approved a different athlete; previous account retained")
        # Refresh replies may omit granted scopes; keep the verified callback grant.
        previous = self._store.get(oauth.TOKENS) or {}
        if previous.get("scope") and not body.get("scope"):
            body["scope"] = previous["scope"]
        record = oauth.save_tokens(self._store, body)
        if athlete:  # returned on first login only
            self._store.set("athlete", {"id": athlete.get("id")})
        return record

    def _access_token(self) -> str:
        tokens = self._store.get(oauth.TOKENS)
        if not tokens:
            raise oauth.AuthError("not logged in to Strava; connect Strava in Settings → Sources")
        if oauth.needs_refresh(tokens):
            tokens = self._refresh()
        return tokens["access_token"]

    # Calls ------------------------------------------------------------------------------

    @contextmanager
    def priority_budget(self) -> Iterator[None]:
        """Let a webhook fetch use the calls a scheduled sync left before the hard limit."""
        previous = getattr(self._local, "priority", False)
        self._local.priority = True
        try:
            yield
        finally:
            self._local.priority = previous

    def get(self, path: str, **params: Any) -> Any:
        with self._call_lock:
            return self._get(path, **params)

    def _priority(self) -> bool:
        return bool(getattr(self._local, "priority", False))

    def _get(self, path: str, **params: Any) -> Any:
        if blocked := self._blocked():
            raise StravaRateLimited(429, blocked[1], blocked[0])
        for attempt in (1, 2):
            wait = self._last + self._interval - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            access = self._access_token()
            headers = {"Authorization": f"Bearer {access}"}
            reply = self._http("GET", f"{API}{path}", params, headers)
            self._last = time.monotonic()
            if reply.status == 401 and attempt == 1:
                self._refresh(rejected_access=access)
                continue
            if reply.status == 429:
                daily = _daily_spent(reply.headers)
                until = self._reset_at(daily=daily)
                message = (
                    "daily request budget used; continuing tomorrow"
                    if daily
                    else "rate limit exceeded; the sync resumes later"
                )
                self._save_limit(until, message, soft=False)
                raise StravaRateLimited(429, message, until)
            if reply.status >= 400:
                raise StravaError(reply.status, str(reply.body)[:300])
            self._respect_limits(reply.headers)
            return reply.body
        raise AssertionError("unreachable")

    def update_description(self, activity_id: str, description: str) -> Any:
        """One guarded provider call; the publisher owns checkpoints and read-back."""
        with self._call_lock:
            return self._update_description(activity_id, description)

    def _update_description(self, activity_id: str, description: str) -> Any:
        if blocked := self._blocked():
            raise StravaRateLimited(429, blocked[1], blocked[0])
        for attempt in (1, 2):
            access = self._access_token()
            reply = self._http(
                "PUT",
                f"{API}/activities/{activity_id}",
                {"description": description},
                {"Authorization": f"Bearer {access}"},
            )
            if reply.status == 401 and attempt == 1:
                self._refresh(rejected_access=access)
                continue
            break
        if reply.status == 429:
            until = self._reset_at(daily=_daily_spent(reply.headers))
            self._save_limit(until, "Strava request budget used", soft=False)
            raise StravaRateLimited(429, "Strava request budget used", until)
        if reply.status >= 400:
            raise StravaError(reply.status, str(reply.body)[:300])
        self._respect_limits(reply.headers)
        return reply.body

    def _blocked(self) -> tuple[datetime, str] | None:
        """Deferral that applies to this call.

        Scheduled syncs wait out both the early headroom stop and a hard limit. A webhook
        fetch ignores headroom and still waits when the budget is actually used up.
        """
        self._absorb_store()
        now = self._clock()
        if self._hard_until is not None and now >= self._hard_until.timestamp():
            self._hard_until = None
            self._hard_message = ""
        if self._limited_until is not None and now >= self._limited_until[0].timestamp():
            self._limited_until = None
        if self._priority():
            if self._hard_until is not None:
                return self._hard_until, self._hard_message
            return None
        return self._limited_until

    def _absorb_store(self) -> None:
        saved = self._read_budget()
        if saved:
            self._apply_budget(combine_budget(self._memory_record(), saved))

    def _read_budget(self) -> dict[str, Any]:
        """The persisted deferral, or nothing when it is absent or unreadable."""
        try:
            return _valid_budget(self._store.get("request_budget"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            log.warning("Ignoring unreadable Strava request budget checkpoint")
            return {}

    def _memory_record(self) -> dict[str, Any]:
        if self._limited_until is None and self._hard_until is None:
            return {}
        hard_ts = self._hard_until.timestamp() if self._hard_until else None
        soft_ts = None
        soft_msg = ""
        if self._limited_until is not None:
            until_ts = self._limited_until[0].timestamp()
            if hard_ts is None or until_ts > hard_ts:
                soft_ts, soft_msg = until_ts, self._limited_until[1]
        return _pack(hard_ts, self._hard_message, soft_ts, soft_msg)

    def _apply_budget(self, record: dict[str, Any]) -> None:
        if not record or record.get("until") is None:
            return
        hard_ts, hard_msg, _, _ = _parts(record)
        self._limited_until = (
            datetime.fromtimestamp(float(record["until"]), UTC),
            str(record.get("message") or ""),
        )
        if hard_ts is None:
            self._hard_until = None
            self._hard_message = ""
        else:
            self._hard_until = datetime.fromtimestamp(hard_ts, UTC)
            self._hard_message = hard_msg

    def paged(self, path: str, per_page: int = 200, **params: Any) -> list:
        items: list = []
        for page in range(1, 1000):
            batch = self.get(path, page=page, per_page=per_page, **params) or []
            items.extend(batch)
            if len(batch) < per_page:
                break
        return items

    def _reset_at(self, *, daily: bool) -> datetime:
        """When the spent budget is available again: the next 15-minute window, or UTC midnight."""
        now = self._clock()
        period = _DAY if daily else _WINDOW
        return datetime.fromtimestamp(now - now % period + period + 60, UTC)

    def _respect_limits(self, headers: dict[str, str]) -> None:
        # The response just received is valid. Preserve it, but defer the next request
        # rather than sleeping inside the shared worker (up to fifteen minutes before).
        limits = []
        for prefix in ("x-ratelimit", "x-readratelimit"):
            limit, usage = headers.get(f"{prefix}-limit"), headers.get(f"{prefix}-usage")
            if not limit or not usage:
                continue
            (short_limit, daily_limit), (short_used, daily_used) = (
                [int(x) for x in limit.split(",")[:2]],
                [int(x) for x in usage.split(",")[:2]],
            )
            if daily_used >= daily_limit - _HEADROOM:
                limits.append(
                    (
                        self._reset_at(daily=True),
                        "daily request budget used; continuing tomorrow",
                        daily_used < daily_limit,
                    )
                )
            elif short_used >= short_limit - _HEADROOM:
                limits.append(
                    (
                        self._reset_at(daily=False),
                        "short request budget used; continuing next window",
                        short_used < short_limit,
                    )
                )
        for until, message, soft in limits:
            self._save_limit(until, message, soft=soft)

    def _save_limit(self, until: datetime, message: str, soft: bool = False) -> None:
        # Share provider deferral across restarts and CLI/portal processes. This contains
        # only the reset deadline and a fixed local message, never tokens or activity data.
        # A later soft stop must not erase an earlier hard stop: webhook fetches still
        # wait for the hard one, while scheduled syncs wait for whichever is later.
        try:
            with self._store.lock("request_budget"):
                try:
                    current = _valid_budget(self._store.get("request_budget"))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    current = {}
                record = combine_budget(
                    merge_budget(current, until, message, soft=soft), self._memory_record()
                )
                self._store.set("request_budget", record)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            record = merge_budget(self._memory_record(), until, message, soft=soft)
            # A successful provider response must still be archived/processed. Retain the
            # in-process deadline even if the optional cross-process checkpoint fails.
            log.warning(
                "Could not persist Strava request budget; this process still defers requests"
            )
        self._apply_budget(record)


def _valid_budget(saved: Any) -> dict[str, Any]:
    """A readable checkpoint. Incomplete or foreign JSON is an error, not a deferral."""
    if saved is None:
        return {}
    if not isinstance(saved, dict):
        raise TypeError("request budget checkpoint is not an object")
    if not saved:
        return {}
    if "until" not in saved or "message" not in saved:
        raise KeyError("request budget checkpoint is incomplete")
    float(saved["until"])
    if saved.get("hard_until") is not None:
        float(saved["hard_until"])
    return saved


def merge_budget(current: dict | None, until: datetime, message: str, *, soft: bool) -> dict:
    """Keep the later soft and hard deadlines. A later soft stop does not hide a hard one."""
    return combine_budget(current, _deadline_record(until, message, soft))


def combine_budget(left: dict | None, right: dict | None) -> dict[str, Any]:
    hard_ts: float | None = None
    hard_msg = ""
    soft_ts: float | None = None
    soft_msg = ""
    for record in (left or {}, right or {}):
        record_hard, record_hard_msg, record_soft, record_soft_msg = _parts(record)
        if record_hard is not None and (hard_ts is None or record_hard > hard_ts):
            hard_ts, hard_msg = record_hard, record_hard_msg
        if record_soft is not None and (soft_ts is None or record_soft > soft_ts):
            soft_ts, soft_msg = record_soft, record_soft_msg
    return _pack(hard_ts, hard_msg, soft_ts, soft_msg)


def _deadline_record(until: datetime, message: str, soft: bool) -> dict[str, Any]:
    timestamp = until.timestamp()
    if soft:
        return _pack(None, "", timestamp, message)
    return _pack(timestamp, message, None, "")


def _parts(record: dict) -> tuple[float | None, str, float | None, str]:
    """Hard deadline, its message, then a later headroom deadline and its message."""
    if not record or record.get("until") is None:
        return None, "", None, ""
    until = float(record["until"])
    message = str(record.get("message") or "")
    if record.get("hard_until") is not None:
        hard_ts = float(record["hard_until"])
        hard_msg = str(record.get("hard_message") or message)
    elif not record.get("soft"):
        hard_ts, hard_msg = until, message
    else:
        hard_ts, hard_msg = None, ""
    if hard_ts is None:
        return None, "", until, message
    if until > hard_ts:
        return hard_ts, hard_msg, until, message
    return hard_ts, hard_msg, None, ""


def _pack(
    hard_ts: float | None, hard_msg: str, soft_ts: float | None, soft_msg: str
) -> dict[str, Any]:
    if hard_ts is None and soft_ts is None:
        return {}
    if soft_ts is not None and (hard_ts is None or soft_ts >= hard_ts):
        until, message = soft_ts, soft_msg
    else:
        until, message = hard_ts, hard_msg
    record: dict[str, Any] = {"until": until, "message": message, "soft": hard_ts is None}
    if hard_ts is not None:
        record["hard_until"] = hard_ts
        record["hard_message"] = hard_msg
    return record


def budget_wait(store: SecretStore, now: float | None = None) -> dict[str, Any] | None:
    """Whether a new activity has to wait: ``{"kind": "daily" | "window", "until": iso}``,
    or None. Headroom left for a webhook fetch is not a wait. Reads the deadline the
    client persisted; never calls Strava."""
    try:
        saved = _valid_budget(store.get("request_budget"))
        if saved.get("hard_until") is not None:
            until = float(saved["hard_until"])
            message = str(saved.get("hard_message") or saved.get("message") or "")
        elif saved.get("soft"):
            return None
        elif saved.get("until") is not None:
            # Checkpoints written before headroom was marked still mean a hard stop.
            until = float(saved["until"])
            message = str(saved.get("message") or "")
        else:
            return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if (time.time() if now is None else now) >= until:
        return None
    return {
        "kind": "daily" if "daily" in message else "window",
        "until": datetime.fromtimestamp(until, UTC).isoformat(),
    }


def _daily_spent(headers: dict[str, str]) -> bool:
    """Whether a 429 is for the daily budget (else the 15-minute one)."""
    for prefix in ("x-ratelimit", "x-readratelimit"):
        limit, usage = headers.get(f"{prefix}-limit"), headers.get(f"{prefix}-usage")
        if limit and usage:
            try:
                daily_limit = int(limit.split(",")[1])
                daily_used = int(usage.split(",")[1])
            except (IndexError, ValueError):
                continue
            if daily_used >= daily_limit:
                return True
    return False
