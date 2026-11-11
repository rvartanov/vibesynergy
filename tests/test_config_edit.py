"""Changing config.toml from the Settings page."""

from __future__ import annotations

import pytest

from vibesynergy.core import config_edit
from vibesynergy.core.config import ConfigError
from vibesynergy.portal.settings_api import systemd_unit

CONFIG = """# VibeSynergy configuration.

[user]
timezone = "Europe/Amsterdam"
units = "metric"   # metric | imperial

[sources.withings]
enabled = true

[schedule]            # minutes between automatic syncs (0 = off)
garmin = 30
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


def test_changes_keep_comments_and_a_backup(config) -> None:
    settings = config_edit.apply_changes(
        config,
        {
            "user": {"sex": "male", "max_hr": 180},
            "sources": {"withings": {"datasets": ["measures", "heart"]}},
            "schedule": {"garmin": 15},
            "best_source": {"hr.": ["withings", "garmin"]},
        },
    )
    text = config.read_text()
    assert "# minutes between automatic syncs (0 = off)" in text and "# metric | imperial" in text
    assert settings.athlete == {"sex": "male", "max_hr": 180}
    assert settings.source_config("withings")["datasets"] == ["measures", "heart"]
    assert settings.schedule["garmin"] == 15 and settings.best_source["hr."] == [
        "withings",
        "garmin",
    ]
    assert [p.name for p in config.parent.glob("config.toml.bak-*")]

    settings = config_edit.apply_changes(
        config, {"user": {"max_hr": None}, "best_source": {"hr.": None}}
    )
    assert "max_hr" not in settings.athlete and "hr." not in settings.best_source


def test_invalid_changes_leave_the_file_alone(config) -> None:
    for bad in (
        {"user": {"timezone": "Mars/Olympus"}},
        {"user": {"sex": "other"}},
        {"user": {"max_hr": 900}},
        {"server": {"public_url": "https://evil.example"}},
        {"user": {"data_dir": "/tmp"}},
        {"schedule": {"garmin": -5}},
        {"best_source": {"hr.": "garmin"}},
    ):
        with pytest.raises(ConfigError):
            config_edit.apply_changes(config, bad)
    assert config.read_text() == CONFIG
    assert not list(config.parent.glob(".config.toml.new"))


def test_no_change_writes_nothing(config) -> None:
    config_edit.apply_changes(config, {"user": {"units": "metric"}})
    assert not list(config.parent.glob("config.toml.bak-*"))


def test_old_backups_are_pruned(config) -> None:
    for i in range(config_edit.BACKUPS_KEPT + 3):
        (config.parent / f"config.toml.bak-20260101-0000{i:02d}").write_text("")
    config_edit.apply_changes(config, {"user": {"sex": "female"}})
    assert len(list(config.parent.glob("config.toml.bak-*"))) == config_edit.BACKUPS_KEPT


def test_systemd_unit(tmp_path) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/vibesynergy.service\n"
    )
    assert systemd_unit(cgroup) == "vibesynergy.service"
    cgroup.write_text("0::/user.slice/user-1000.slice/session-2.scope\n")
    assert systemd_unit(cgroup) is None
    cgroup.write_text("0::/user.slice/user@1000.service/app.slice/x;rm -rf.service\n")
    assert systemd_unit(cgroup) is None
    assert systemd_unit(tmp_path / "missing") is None
