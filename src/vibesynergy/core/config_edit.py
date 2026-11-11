"""Change config.toml from the portal's Settings page.

Only known settings can be changed (profile, sources, sync intervals, best-source rules, the
tile cache); paths, the public address and the ports stay file-only, because a wrong value
there cuts off the portal or the webhooks. The file is edited with tomlkit, so comments and
layout survive; the result is validated with the normal loader before it replaces the file,
and the previous version is kept next to it.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import tomlkit

from vibesynergy.core.config import RETIRED_KEYS, ConfigError, Settings, load_settings

USER_KEYS = (
    "timezone",
    "units",
    "temperature",
    "wind",
    "sex",
    "max_hr",
    "resting_hr",
    "birth_date",
)
SOURCE_KEYS = ("enabled", "datasets", "calendars", "garmin_target_calendar", "mirror_enabled")
PORTAL_KEYS = ("tile_cache", "tile_max_age_days")
ALERT_KEYS = ("enabled", "email_to")
DROPFOLDER_KEYS = ("dir",)
AI_KEYS = (
    "provider",
    "model",
    "reasoning",
    "chat_provider",
    "chat_model",
    "chat_reasoning",
    "coach_provider",
    "coach_model",
    "local_url",
    "base_url",
)
PUBLICATION_KEYS = (
    "auto_publish",
    "auto_publish_since",
    "language",
    "quote_enabled",
    "weather_enabled",
    "garmin_mirror_enabled",
    "icu_description_enabled",
    "quote_themes",
    "quote_sources",
    "quote_providers",
    "opener",
    "closer",
    "label_stats",
    "label_quote",
)
MAIL_KEYS = (
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "username",
    "from",
    "imap_host",
    "imap_port",
    "sent_folder",
)
BACKUPS_KEPT = 10


def apply_changes(path: Path, changes: dict[str, Any]) -> Settings:
    """Apply ``changes`` to the config file and return the new settings.

    ``changes`` may hold ``user``, ``sources`` ({id: {enabled, datasets}}), ``schedule``
    ({id: minutes}), ``best_source`` ({prefix: [sources]}) and ``portal``. A value of None
    removes the setting (back to the default). Raises ConfigError and leaves the file as it
    was when a change is unknown or the result is invalid.
    """
    document = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    unknown = set(changes) - {
        "user",
        "sources",
        "schedule",
        "best_source",
        "portal",
        "alerts",
        "mail",
        "dropfolder",
        "ai",
        "publication",
    }
    if unknown:
        raise ConfigError(f"these settings can't be changed here: {', '.join(sorted(unknown))}")
    _merge(document, "user", changes.get("user"), USER_KEYS)
    _merge(document, "portal", changes.get("portal"), PORTAL_KEYS)
    _merge(document, "alerts", changes.get("alerts"), ALERT_KEYS)
    if changes.get("alerts"):
        _drop_retired(document, "alerts")
    _merge(document, "mail", changes.get("mail"), MAIL_KEYS)
    _merge(document, "dropfolder", changes.get("dropfolder"), DROPFOLDER_KEYS)
    _merge(document, "ai", changes.get("ai"), AI_KEYS)
    _merge(document, "publication", changes.get("publication"), PUBLICATION_KEYS)
    for source_id, values in (changes.get("sources") or {}).items():
        sources = _table(document, "sources")
        _merge(sources, source_id, values, SOURCE_KEYS)
    if changes.get("schedule"):
        _merge(document, "schedule", changes["schedule"], None, number=True)
    if changes.get("best_source") is not None:
        rules = _table(document, "best_source")
        for prefix, order in changes["best_source"].items():
            if order is None:
                rules.pop(prefix, None)
            elif not isinstance(order, list) or not all(isinstance(s, str) for s in order):
                raise ConfigError(f"[best_source] {prefix!r} must be a list of source ids")
            else:
                rules[prefix] = order

    return _write(path, document)


def set_portal_hosts(path: Path, hosts: list[str]) -> Settings:
    """Replace ``[portal] hosts`` (the extra names the portal answers on). The caller
    validates each name; an empty list removes the key."""
    document = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    portal = _table(document, "portal")
    if hosts:
        portal["hosts"] = list(hosts)
    else:
        portal.pop("hosts", None)
    return _write(path, document)


def _write(path: Path, document: Any) -> Settings:
    """Validate and replace the file, keeping the previous version next to it."""
    text = tomlkit.dumps(document)
    if path.exists() and text == path.read_text():
        return load_settings(path)  # nothing changed: no write, no backup
    candidate = path.with_name(f".{path.name}.new")
    candidate.write_text(text)
    try:
        load_settings(candidate)  # the same validation as at startup
    except ConfigError:
        candidate.unlink(missing_ok=True)
        raise
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
        _prune_backups(path)
    os.chmod(candidate, path.stat().st_mode if path.exists() else 0o600)
    os.replace(candidate, path)
    return load_settings(path)


def _table(parent: Any, key: str) -> Any:
    if key not in parent:
        parent[key] = tomlkit.table()
    return parent[key]


def _merge(
    parent: Any,
    key: str,
    values: dict[str, Any] | None,
    allowed: tuple[str, ...] | None,
    *,
    number: bool = False,
) -> None:
    if not values:
        return
    if not isinstance(values, dict):
        raise ConfigError(f"[{key}] must be a table")
    if allowed is not None:
        unknown = set(values) - set(allowed)
        if unknown:
            raise ConfigError(f"[{key}] can't set {', '.join(sorted(unknown))} here")
    table = _table(parent, key)
    for name, value in values.items():
        if value is None or (value == "" and key == "publication"):
            table.pop(name, None)
        elif number and (
            not isinstance(value, int | float) or isinstance(value, bool) or value < 0
        ):
            raise ConfigError(f"[{key}] {name} must be a number of minutes (0 = off)")
        else:
            table[name] = value


def _drop_retired(document: Any, section: str) -> None:
    """Saving a section also removes the keys nothing reads any more, so the warning ends."""
    table = document.get(section)
    if table is None:
        return
    for key in RETIRED_KEYS.get(section, ()):
        table.pop(key, None)


def _prune_backups(path: Path) -> None:
    backups = sorted(path.parent.glob(f"{path.name}.bak-*"))
    for old in backups[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
