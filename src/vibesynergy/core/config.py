"""Configuration: one TOML file plus environment overrides.

Lookup order for the config file: ``VIBESYNERGY_CONFIG``, then
``$XDG_CONFIG_HOME/vibesynergy/config.toml`` (default ``~/.config/vibesynergy/config.toml``).
The data directory comes from ``VIBESYNERGY_DATA_DIR``, then ``[paths] data_dir`` in the file,
then ``$XDG_DATA_HOME/vibesynergy`` (default ``~/.local/share/vibesynergy``).
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)
_AI_WARNED = False
_SECRET_URL_WARNED = False
# Sources that no longer exist. Old config sections, sync state rows and queued jobs that
# still name them are ignored rather than reported as problems.
RETIRED_SOURCES = frozenset({"icloud"})
_RETIRED_WARNED: set[tuple[str, str]] = set()

UNITS = ("metric", "imperial")
DEFAULT_CALENDAR_LOOKAHEAD_MONTHS = 12
MAX_CALENDAR_LOOKAHEAD_MONTHS = 60


class ConfigError(Exception):
    """The configuration file is missing values or contains invalid ones."""


@dataclass(frozen=True)
class Settings:
    config_path: Path
    data_dir: Path
    timezone: str = "UTC"
    units: str = "metric"
    # [user] sex / max_hr / resting_hr: for the HR training load (TRIMP); optional.
    athlete: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    server: dict[str, Any] = field(default_factory=dict)
    schedule: dict[str, Any] = field(default_factory=dict)
    portal: dict[str, Any] = field(default_factory=dict)
    best_source: dict[str, list[str]] = field(default_factory=dict)
    mail: dict[str, Any] = field(default_factory=dict)  # [mail]: smtp_*, imap_*, username
    alerts: dict[str, Any] = field(default_factory=dict)  # [alerts]: enabled, email_to
    dropfolder: dict[str, Any] = field(default_factory=dict)  # [dropfolder]: dir
    ai: dict[str, Any] = field(
        default_factory=dict
    )  # [ai]: provider/model, chat_provider/chat_model, coach_provider/coach_model, local_url
    publication: dict[str, Any] = field(default_factory=dict)
    calendar: dict[str, Any] = field(default_factory=dict)  # [calendar]: lookahead_months
    # [coach_runner]: defaults for the API coach (docs/COACH_RUNNER.md); the portal's
    # switch and cap override them. Off unless switched on in the portal.
    coach_runner: dict[str, Any] = field(default_factory=dict)

    @property
    def public_url(self) -> str | None:
        """Base URL of the public gateway (Tailscale Funnel), e.g. https://server.x.ts.net.
        OAuth redirects and webhooks are served under ``<public_url>/vibesynergy/``."""
        url = self.server.get("public_url")
        return str(url).rstrip("/") if url else None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "vibesynergy.sqlite"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def secrets_dir(self) -> Path:
        return self.config_path.parent / "secrets"

    def source_config(self, source_id: str) -> dict[str, Any]:
        return self.sources.get(source_id, {})

    def source_enabled(self, source_id: str) -> bool:
        return bool(self.source_config(source_id).get("enabled", False))


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def default_config_path() -> Path:
    env = os.environ.get("VIBESYNERGY_CONFIG")
    return (
        Path(env).expanduser()
        if env
        else _xdg("XDG_CONFIG_HOME", ".config") / "vibesynergy" / "config.toml"
    )


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or default_config_path()
    try:
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    paths = raw.get("paths", {})
    user = raw.get("user", {})
    data_dir = (
        os.environ.get("VIBESYNERGY_DATA_DIR")
        or paths.get("data_dir")
        or _xdg("XDG_DATA_HOME", ".local/share") / "vibesynergy"
    )
    sources, schedule = dict(raw.get("sources", {})), dict(raw.get("schedule", {}))
    # Retired sources (the calendar source that was "icloud" is now "caldav"): their old
    # sections are ignored, with one warning.
    for table, name in ((sources, "sources"), (schedule, "schedule")):
        for retired in RETIRED_SOURCES & table.keys():
            table.pop(retired)
            if (name, retired) not in _RETIRED_WARNED:
                _RETIRED_WARNED.add((name, retired))
                log.warning(
                    "[%s] %s is ignored; that source has been removed (calendars use caldav, "
                    "set up in the portal under Settings → Calendar)",
                    name,
                    retired,
                )
    if "ai" in raw or raw.get("portal", {}).get("legacy_ai"):
        global _AI_WARNED
        if not _AI_WARNED:
            _AI_WARNED = True
            log.warning("[ai] settings are ignored; the internal coach has been removed")
    if "mcp_secret_url" in raw.get("server", {}):
        global _SECRET_URL_WARNED
        if not _SECRET_URL_WARNED:
            _SECRET_URL_WARNED = True
            log.warning(
                "[server] mcp_secret_url is ignored; the secret-link MCP address has been "
                "removed (use OAuth or a personal access token at /vibesynergy/mcp)"
            )
    publication = _without_retired(dict(raw.get("publication", {})), "publication")
    _without_retired(dict(raw.get("coach", {})), "coach")  # nothing else lives in [coach]
    settings = Settings(
        config_path=path,
        data_dir=Path(data_dir).expanduser(),
        timezone=user.get("timezone", "UTC"),
        units=user.get("units", "metric"),
        athlete={
            k: user[k]
            for k in ("sex", "max_hr", "resting_hr", "birth_date", "load_source")
            if k in user
        },
        sources=sources,
        server=raw.get("server", {}),
        schedule=schedule,
        portal=raw.get("portal", {}),
        best_source=raw.get("best_source", {}),
        mail=raw.get("mail", {}),
        alerts=_without_retired(dict(raw.get("alerts", {})), "alerts"),
        dropfolder=raw.get("dropfolder", {}),
        ai=raw.get("ai", {}),
        publication=publication,
        calendar=raw.get("calendar") or {},
        coach_runner=raw.get("coach_runner") or {},
    )
    _validate(settings)
    return settings


# Keys nothing reads any more. An old config file still loads: each key is dropped with
# one warning per process.
RETIRED_KEYS = {
    # The AI insight section left publication with the internal coach.
    "publication": ("insight_enabled", "label_insight"),
    # The WhatsApp alert channel and its local bridge were removed on 31-10-2026.
    "alerts": ("whatsapp_phone", "whatsapp_bridge"),
    # VibeSynergy no longer adds approved workouts to Garmin.
    "coach": ("workout_push_enabled",),
}
_RETIRED_WARNED: set[str] = set()


def _without_retired(table: dict[str, Any], section: str) -> dict[str, Any]:
    for key in RETIRED_KEYS.get(section, ()):
        if key in table:
            table.pop(key)
            name = f"[{section}] {key}"
            if name not in _RETIRED_WARNED:
                _RETIRED_WARNED.add(name)
                log.warning("%s is ignored; nothing uses it any more", name)
    return table


def _validate(settings: Settings) -> None:
    if not isinstance(settings.publication.get("auto_publish", False), bool):
        raise ConfigError("[publication] auto_publish must be true or false")
    since = settings.publication.get("auto_publish_since")
    if since is not None:
        try:
            datetime.fromisoformat(str(since))
        except ValueError as exc:
            raise ConfigError("[publication] auto_publish_since must be an ISO time") from exc
    if settings.publication.get("language", "nl") not in ("en", "nl"):
        raise ConfigError("[publication] language must be en or nl")
    from vibesynergy.query.publication_plugins import THEMES

    for key in (
        "quote_enabled",
        "weather_enabled",
        "garmin_mirror_enabled",
        "icu_description_enabled",
    ):
        if not isinstance(settings.publication.get(key, True), bool):
            raise ConfigError(f"[publication] {key} must be true or false")
    themes = settings.publication.get("quote_themes", list(THEMES))
    if not isinstance(themes, list) or any(t not in THEMES for t in themes):
        raise ConfigError("[publication] quote_themes contains an unknown theme")
    from vibesynergy.query.publication_plugins import COLLECTIONS

    sources = settings.publication.get("quote_sources", list(COLLECTIONS))
    if not isinstance(sources, list) or any(x not in COLLECTIONS for x in sources):
        raise ConfigError("[publication] quote_sources contains an unknown source")
    from vibesynergy.query.publication_plugins import PROVIDERS

    providers = settings.publication.get("quote_providers", list(PROVIDERS))
    if not isinstance(providers, list) or any(x not in PROVIDERS for x in providers):
        raise ConfigError("[publication] quote_providers contains an unknown provider")
    from vibesynergy.query.publication_format import (
        validate_closer,
        validate_label,
        validate_opener,
    )

    if "opener" in settings.publication:
        validate_opener(settings.publication["opener"])
    if "closer" in settings.publication:
        validate_closer(settings.publication["closer"])
    for key in ("label_stats", "label_quote"):
        if key in settings.publication:
            validate_label(key, settings.publication[key])
    try:
        ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown timezone {settings.timezone!r}") from exc
    if settings.units not in UNITS:
        raise ConfigError(f"units must be one of {UNITS}, got {settings.units!r}")
    sex = settings.athlete.get("sex")
    if sex is not None and sex not in ("male", "female"):
        raise ConfigError(f"[user] sex must be male or female, got {sex!r}")
    for key in ("max_hr", "resting_hr"):
        value = settings.athlete.get(key)
        if value is not None and not (isinstance(value, int | float) and 25 <= value <= 250):
            raise ConfigError(f"[user] {key} must be a heart rate in bpm, got {value!r}")
    if "birth_date" in settings.athlete:
        settings.athlete["birth_date"] = _birth_date(settings.athlete["birth_date"])
    # Whose training load counts: intervals.icu's model or VibeSynergy's own.
    source = settings.athlete.get("load_source")
    if source is not None and source not in ("intervals", "vibesynergy"):
        raise ConfigError(f"[user] load_source must be intervals or vibesynergy, got {source!r}")
    security = settings.mail.get("smtp_security")
    if security is not None and security not in ("starttls", "ssl", "none"):
        raise ConfigError("[mail] smtp_security must be starttls, ssl or none")
    for key in ("smtp_port", "imap_port"):
        port = settings.mail.get(key)
        if port is not None and not (isinstance(port, int) and 0 < port < 65536):
            raise ConfigError(f"[mail] {key} must be a port number")
    url = settings.server.get("public_url")
    if url and not str(url).startswith("https://"):
        raise ConfigError("[server] public_url must start with https://")
    port = settings.portal.get("port", 8447)
    if not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError(f"[portal] port must be a port number, got {port!r}")
    for source_id, cfg in settings.sources.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"[sources.{source_id}] must be a table")
    caldav = settings.source_config("caldav")
    target = caldav.get("garmin_target_calendar")
    if target is not None:
        calendars = caldav.get("calendars", [])
        if (
            not isinstance(target, str)
            or not isinstance(calendars, list)
            or target not in {item.get("url") for item in calendars if isinstance(item, dict)}
        ):
            raise ConfigError("[sources.caldav] Garmin target must be a selected calendar")
    mirror = caldav.get("mirror_enabled", False)
    if not isinstance(mirror, bool):
        raise ConfigError("[sources.caldav] mirror_enabled must be true or false")
    if mirror and not target:
        raise ConfigError("[sources.caldav] copying Garmin workouts needs a selected calendar")
    if not isinstance(settings.calendar, dict):
        raise ConfigError("[calendar] must be a table")
    if not isinstance(settings.coach_runner, dict):
        raise ConfigError("[coach_runner] must be a table")
    for key in ("monthly_cap_usd", "max_turns", "max_output_tokens"):
        value = settings.coach_runner.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int | float) or value <= 0
        ):
            raise ConfigError(f"[coach_runner] {key} must be a positive number")
    calendar_lookahead_months(settings)


def calendar_lookahead_months(settings: Settings) -> int:
    """How many months ahead Garmin and CalDAV calendar sync read. Default is one year."""
    if "lookahead_months" not in settings.calendar:
        return DEFAULT_CALENDAR_LOOKAHEAD_MONTHS
    value = settings.calendar["lookahead_months"]
    if type(value) is not int or not 1 <= value <= MAX_CALENDAR_LOOKAHEAD_MONTHS:
        raise ConfigError(
            "[calendar] lookahead_months must be a whole number of months from 1 to "
            f"{MAX_CALENDAR_LOOKAHEAD_MONTHS}"
        )
    return value


def detect_timezone() -> str:
    """Best-effort IANA name of the host's time zone."""
    if tz := os.environ.get("TZ"):
        return tz.lstrip(":")
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    etc_tz = Path("/etc/timezone")
    if etc_tz.exists():
        return etc_tz.read_text().strip() or "UTC"
    return "UTC"


DEFAULT_CONFIG = """\
# VibeSynergy configuration.

[paths]
# data_dir = "~/.local/share/vibesynergy"

[user]
timezone = "{timezone}"
units = "metric"   # metric | imperial
# For the heart-rate training load (TRIMP), all optional: sex = "male" | "female" (Banister's
# weighting; male when unset), max_hr and resting_hr in bpm (default: the highest max HR in
# your heart-rate zones and your median resting HR).
# sex = "male"

[server]
# Public address of this machine (Tailscale Funnel); needed for Withings/Strava login
# and for instant updates (webhooks). Example: "https://server.example.ts.net"
# public_url = ""

# Sources are disabled until enabled here. Each source documents its own options.
# See docs/SOURCES.md. To try VibeSynergy without any account, enable the demo source
# (synthetic data) and run `vibesynergy sync`.
# [sources.demo]
# enabled = true
#
# [sources.garmin]
# enabled = true

# How far ahead the Garmin training calendar and selected CalDAV calendars are read.
# 12 months is one year. A race further out waits until it falls inside this horizon.
# [calendar]
# lookahead_months = 12
"""


def write_default_config(path: Path, timezone: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG.format(timezone=timezone))


def _birth_date(value: object) -> str:
    """[user] birth_date as ISO text. TOML may give a date or a string."""
    from datetime import date

    try:
        day = value if isinstance(value, date) else date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"[user] birth_date must be a date (YYYY-MM-DD), got {value!r}") from exc
    if not (date(1900, 1, 1) <= day <= date.today()):
        raise ConfigError(f"[user] birth_date must be a past date, got {value!r}")
    return day.isoformat()
