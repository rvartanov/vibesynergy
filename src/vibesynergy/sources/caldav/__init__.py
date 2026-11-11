"""Opt-in read-only ingestion of explicitly selected iCal / CalDAV calendars (any provider).

Setup happens in the portal (Settings → Calendar): sign-in, calendar discovery and selection
are verified server-side and the credentials are kept encrypted in the secret store.
"""

from datetime import datetime, timedelta

from vibesynergy.core.config import calendar_lookahead_months, load_settings
from vibesynergy.core.timeutil import utc_now
from vibesynergy.query.evidence import digest
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Source
from vibesynergy.sources.caldav import account, google
from vibesynergy.sources.caldav.calendar import map_calendars, parse_object
from vibesynergy.sources.caldav.client import IncrementalSnapshotInvalid, calendar_url


class CalDAVSource(Source):
    id = "caldav"
    name = "iCal / CalDAV calendar"
    live_config = True  # Settings → Calendar enables it and picks calendars while running

    def __init__(self, ctx):
        super().__init__(ctx)
        self._client = None

    def datasets(self):
        return [
            DatasetSpec(
                "calendar",
                DatasetKind.RANGE,
                "Selected personal calendars and mirror identities",
                initial_days=30,
                refetch_days=30,
                lookahead_months=calendar_lookahead_months(self.ctx.settings),
                keep_history=True,
            )
        ]

    def configured(self):
        """At least one calendar selected and a login stored (password or Google tokens)."""
        stored = self.ctx.secrets.get(account.ACCOUNT) or {}
        signed_in = bool(stored.get("password")) or (
            stored.get("kind") == "oauth" and bool(self.ctx.secrets.get(google.TOKENS))
        )
        return bool(self.selected() and signed_in)

    @property
    def client(self):
        if self._client is not None:  # injected (tests)
            return self._client
        # Built per sync, so a new sign-in applies without restarting the service.
        stored = self.ctx.secrets.get(account.ACCOUNT) or {}
        if not stored or (stored.get("kind") != "oauth" and not stored.get("password")):
            raise ValueError("CalDAV account missing; sign in under Settings → Calendar")
        from vibesynergy.sources.http_timeout import http_timeout_seconds, requests_timeout_pair

        timeout = requests_timeout_pair(http_timeout_seconds(self.ctx.settings, self.id))
        return account.client_for(self.ctx.secrets, stored, request_timeout=timeout)

    def selected(self) -> list[dict]:
        """The calendars chosen in the portal: read from the config file, so a selection
        saved there applies at the next sync."""
        path = self.ctx.settings.config_path
        on_disk = load_settings(path).sources if path.exists() else {}
        config = on_disk.get(self.id, self.ctx.config)
        return list(config.get("calendars", []))

    def fetch(self, dataset, window):
        selected = self.selected()
        if not selected or window is None:
            raise ValueError("Select at least one calendar (Settings → Calendar) and a window")
        snapshots = []
        stored = self.ctx.secrets.get(account.ACCOUNT) or {}
        identity = digest(
            {key: stored.get(key) for key in ("provider", "username", "url", "discovered_at")}
        )
        previous = {}
        full_read_at = utc_now()
        if self.ctx.raw:
            raw_id = self.ctx.raw.current_id(self.id, "calendar", "selected-calendars")
            if raw_id and not self.ctx.raw.needs_normalising(raw_id):
                prior = self.ctx.raw.load(raw_id).json()
                try:
                    stamp = datetime.fromisoformat(prior["full_read_at"])
                    if (
                        prior.get("account_id") == identity
                        and prior.get("complete") is True
                        and timedelta(0) <= utc_now() - stamp < timedelta(days=1)
                    ):
                        previous = {c["url"]: c["objects"] for c in prior["calendars"]}
                        full_read_at = stamp
                except (KeyError, TypeError, ValueError):
                    pass
        seen = set()
        client = self.client
        for entry in selected:
            url = calendar_url(entry["url"])
            if url in seen:
                raise ValueError("Duplicate selected calendar")
            seen.add(url)
            if url in previous:
                try:
                    objects = client.incremental_snapshot(url, previous[url])
                except IncrementalSnapshotInvalid:
                    previous.pop(url, None)
                    full_read_at = utc_now()
                    objects = client.snapshot(url)
            else:
                objects = client.snapshot(url)
            # Validate every collection before emitting any snapshot or deletion span.
            for obj in objects:
                parse_object(obj, url)
            snapshots.append(
                {
                    "url": url,
                    "name": entry.get("name"),
                    "objects": objects,
                    "classification": entry.get("classification", "unclassified"),
                }
            )
        yield FetchedItem(
            "selected-calendars",
            {
                "complete": True,
                "account_id": identity,
                "full_read_at": full_read_at.isoformat(),
                "window": f"{window.start}..{window.end}",
                "timezone": self.ctx.settings.timezone,
                "calendars": snapshots,
            },
            window=window,
        )

    def mappers(self):
        return {"calendar": map_calendars}

    def authenticate(self):
        raise ValueError(
            "iCal / CalDAV calendars are set up in the portal: Settings → Calendar. "
            "Nothing is entered in the shell."
        )
