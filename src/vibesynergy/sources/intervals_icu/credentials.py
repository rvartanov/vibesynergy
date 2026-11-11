"""Where the intervals.icu API key and athlete id live.

They are entered in Settings → Sources and kept in the encrypted secret store
(``secrets/intervals_icu/api.enc``). The page only ever learns whether a key is set and the
athlete id; the key is never sent back.

Older installs passed them as ``INTERVALS_ICU_API_KEY`` / ``INTERVALS_ICU_ATHLETE_ID``
through a systemd environment file. That is the legacy path: when the service starts,
``migrate_from_env`` copies them into the store once (with a warning to remove the file),
and until then a read still falls back to the environment, with a warning. The store wins
when both are there.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from vibesynergy.core.secrets import SecretStore

API_KEY_ENV = "INTERVALS_ICU_API_KEY"
ATHLETE_ID_ENV = "INTERVALS_ICU_ATHLETE_ID"
STORE = "intervals_icu"
DOCUMENT = "api"
ATHLETE = re.compile(r"[A-Za-z0-9_-]+")

log = logging.getLogger(__name__)
_warned: set[str] = set()


@dataclass(frozen=True)
class Credentials:
    api_key: str
    athlete_id: str
    origin: str  # "store" or "environment"

    def __repr__(self) -> str:  # never print the key
        return f"Credentials(athlete_id={self.athlete_id!r}, origin={self.origin!r})"


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


def _secrets_dir(secrets_dir: Path | None) -> Path | None:
    if secrets_dir is not None:
        return secrets_dir
    try:
        from vibesynergy.core.config import load_settings

        return load_settings().secrets_dir
    except Exception:  # no readable config: only the environment can answer
        return None


def _clean(api_key: str | None, athlete_id: str | None) -> tuple[str, str] | None:
    key = (api_key or "").strip()
    athlete = (athlete_id or "").strip()
    if not key or not athlete or not ATHLETE.fullmatch(athlete):
        return None
    return key, athlete


def _from_env() -> tuple[str, str] | None:
    return _clean(os.environ.get(API_KEY_ENV), os.environ.get(ATHLETE_ID_ENV))


def _from_store(secrets_dir: Path | None) -> tuple[str, str] | None:
    if secrets_dir is None:
        return None
    doc = SecretStore(secrets_dir, STORE).get(DOCUMENT) or {}
    if not isinstance(doc, dict):
        return None
    return _clean(doc.get("api_key"), doc.get("athlete_id"))


def load(secrets_dir: Path | None = None) -> Credentials | None:
    """The key and athlete id to use, or None when intervals.icu is not set up."""
    stored = _from_store(_secrets_dir(secrets_dir))
    env = _from_env()
    if stored:
        if env:
            _warn_once(
                "both",
                "intervals.icu: the key saved in Settings is used; "
                f"{API_KEY_ENV} in the environment is ignored and can be removed",
            )
        return Credentials(*stored, origin="store")
    if env:
        _warn_once(
            "env",
            f"intervals.icu: {API_KEY_ENV} from the environment is deprecated; "
            "it moves into the encrypted store when the service starts",
        )
        return Credentials(*env, origin="environment")
    return None


def save(secrets_dir: Path, api_key: str, athlete_id: str) -> None:
    """Store a new key (or a new athlete id with the stored key when ``api_key`` is empty)."""
    athlete = athlete_id.strip()
    if not ATHLETE.fullmatch(athlete):
        raise ValueError("Enter your intervals.icu athlete id, such as i12345.")
    key = api_key.strip()
    if not key:
        current = _from_store(secrets_dir)
        if current is None:
            raise ValueError("Enter your intervals.icu API key.")
        key = current[0]
    store = SecretStore(secrets_dir, STORE)
    with store.lock(DOCUMENT):
        store.set(DOCUMENT, {"api_key": key, "athlete_id": athlete})


def clear(secrets_dir: Path) -> None:
    store = SecretStore(secrets_dir, STORE)
    with store.lock(DOCUMENT):
        store.delete(DOCUMENT)


def status(secrets_dir: Path) -> dict:
    """What the page may know: whether a key is set, the athlete id and where it is from."""
    found = load(secrets_dir)
    return {
        "set": found is not None,
        "athlete_id": found.athlete_id if found else None,
        "origin": found.origin if found else None,
    }


def migrate_from_env(secrets_dir: Path) -> bool:
    """Copy the environment's key into the store once. True when something was copied."""
    if _from_store(secrets_dir) is not None:
        return False
    env = _from_env()
    if env is None:
        return False
    save(secrets_dir, *env)
    log.warning(
        "intervals.icu: copied %s and %s into the encrypted store. Remove them from the "
        "service environment (the 20-intervals-icu.conf drop-in and ~/.config/vibesynergy/env).",
        API_KEY_ENV,
        ATHLETE_ID_ENV,
    )
    return True
