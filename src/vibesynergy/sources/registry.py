"""Discovers source plugins and instantiates the ones enabled in the configuration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from importlib.metadata import entry_points

from vibesynergy.core.config import ConfigError, Settings, load_settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.sources.base import Source, SourceContext
from vibesynergy.store.raw import RawStore

ENTRY_POINT_GROUP = "vibesynergy.sources"


class SourceError(Exception):
    pass


def discover() -> dict[str, type[Source]]:
    found: dict[str, type[Source]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        cls = ep.load()
        if not (isinstance(cls, type) and issubclass(cls, Source)):
            raise SourceError(f"entry point {ep.name!r} is not a Source subclass")
        found[cls.id] = cls
    return found


class SourceRegistry:
    def __init__(
        self,
        settings: Settings,
        classes: Mapping[str, type[Source]] | None = None,
        *,
        raw: RawStore | None = None,
    ):
        self._raw = raw
        self._settings = settings
        self._classes = dict(discover() if classes is None else classes)
        self._instances: dict[str, Source] = {}

    def available(self) -> dict[str, type[Source]]:
        return dict(sorted(self._classes.items()))

    def _live(self, source_id: str) -> Settings:
        """The settings that decide whether a source runs. Sources set up in the portal
        (``live_config``) are read from the config file, so saving or disconnecting there
        applies at once (once the file has a section for it); the others keep the settings
        the service started with."""
        cls = self._classes.get(source_id)
        path = self._settings.config_path
        if cls is None or not cls.live_config or not path.exists():
            return self._settings
        try:
            on_disk = load_settings(path)
        except (ConfigError, OSError):
            return self._settings
        return on_disk if source_id in on_disk.sources else self._settings

    def source_enabled(self, source_id: str) -> bool:
        return self._live(source_id).source_enabled(source_id)

    def enabled_ids(self) -> list[str]:
        return [sid for sid in self.available() if self.source_enabled(sid)]

    def get(self, source_id: str, *, require_enabled: bool = True) -> Source:
        if source_id not in self._classes:
            raise SourceError(
                f"unknown source {source_id!r}; available: {', '.join(self._classes)}"
            )
        if require_enabled and not self.source_enabled(source_id):
            raise SourceError(
                f"source {source_id!r} is switched off; turn it on in the portal under "
                "Settings → Sources"
            )
        if source_id not in self._instances:
            ctx = SourceContext(
                settings=self._settings,
                raw=self._raw,
                config=self._settings.source_config(source_id),
                secrets=SecretStore(self._settings.secrets_dir, source_id),
                log=logging.getLogger(f"vibesynergy.sources.{source_id}"),
            )
            self._instances[source_id] = self._classes[source_id](ctx)
        return self._instances[source_id]
