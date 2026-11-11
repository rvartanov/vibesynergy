from __future__ import annotations

from pathlib import Path

import pytest

from vibesynergy.core.config import Settings, load_settings
from vibesynergy.runtime import Runtime


def write_config(path: Path, *, demo: bool = True, extra: str = "") -> Path:
    path.write_text(
        f"""
[user]
timezone = "Europe/Amsterdam"

[sources.demo]
enabled = {"true" if demo else "false"}
{extra}
"""
    )
    return path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated config and data locations for a test."""
    config = write_config(tmp_path / "config.toml")
    monkeypatch.setenv("VIBESYNERGY_CONFIG", str(config))
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "data"))
    return config


@pytest.fixture
def settings(env: Path) -> Settings:
    return load_settings()


@pytest.fixture
def runtime(settings: Settings):
    rt = Runtime.open(settings)
    rt.migrate()
    yield rt
    rt.close()


@pytest.fixture(autouse=True)
def _no_live_intervals_icu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages and tools that read intervals.icu sport settings never reach the network."""
    from vibesynergy.query import thresholds
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuError

    def offline():
        raise IntervalsIcuError("intervals.icu is not set up: tests")

    monkeypatch.setattr(thresholds, "icu_settings_live", offline)
