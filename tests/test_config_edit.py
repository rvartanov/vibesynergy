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


# --- publication settings from before #175 ---------------------------------------------------

OLD_PUBLICATION = """
[publication]
garmin_mirror_enabled = false  # the old "Copy the Strava text to Garmin too"
text_sync_enabled = true
icu_description_enabled = true
quote_enabled = true
"""


@pytest.mark.parametrize(
    ("mirror", "targets"), [("true", ["strava", "garmin"]), ("false", ["strava"])]
)
def test_the_old_garmin_copy_switch_loads_as_the_garmin_target(tmp_path, mirror, targets):
    from vibesynergy.core.config import load_settings, publication_targets

    path = tmp_path / "config.toml"
    path.write_text(f"[publication]\ngarmin_mirror_enabled = {mirror}\n")
    pub = load_settings(path).publication
    assert pub == {"targets": targets} and publication_targets(pub) == targets


def test_without_any_setting_both_places_are_targets(config):
    from vibesynergy.core.config import load_settings, publication_targets

    assert publication_targets(load_settings(config).publication) == ["strava", "garmin"]


def test_saving_writes_an_old_publication_section_the_new_way(config):
    config.write_text(CONFIG + OLD_PUBLICATION)
    saved = config_edit.apply_changes(config, {"publication": {"quote_enabled": False}})
    text = config.read_text()
    assert "garmin_mirror_enabled" not in text and "icu_description_enabled" not in text
    assert 'targets = ["strava"]' in text
    assert saved.publication["targets"] == ["strava"]
    assert saved.publication["text_sync_enabled"] is True  # keeps its value
    assert saved.publication["quote_enabled"] is False
    assert "# VibeSynergy configuration." in text  # the rest of the file stays


def test_a_chosen_target_wins_over_the_old_switch(config):
    config.write_text(CONFIG + OLD_PUBLICATION)
    saved = config_edit.apply_changes(config, {"publication": {"targets": ["garmin"]}})
    assert saved.publication["targets"] == ["garmin"]
    assert "garmin_mirror_enabled" not in config.read_text()


@pytest.mark.parametrize("bad", [[], ["intervals_icu"], ["strava", "strava"], "strava"])
def test_targets_name_strava_and_or_garmin(config, bad):
    with pytest.raises(ConfigError, match="targets"):
        config_edit.apply_changes(config, {"publication": {"targets": bad}})


def test_the_old_switches_can_no_longer_be_saved(config):
    for key in ("garmin_mirror_enabled", "icu_description_enabled"):
        with pytest.raises(ConfigError, match=key):
            config_edit.apply_changes(config, {"publication": {key: False}})
