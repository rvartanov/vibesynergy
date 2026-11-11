from pathlib import Path

import pytest

from vibesynergy.core.config import ConfigError, load_settings, write_default_config


def test_defaults_without_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIBESYNERGY_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    s = load_settings(tmp_path / "missing.toml")
    assert s.timezone == "UTC"
    assert s.units == "metric"
    assert s.data_dir == tmp_path / "xdg" / "vibesynergy"
    assert s.db_path == s.data_dir / "db" / "vibesynergy.sqlite"


def test_env_data_dir_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[paths]\ndata_dir = "/nowhere"\n')
    monkeypatch.setenv("VIBESYNERGY_DATA_DIR", str(tmp_path / "d"))
    assert load_settings(cfg).data_dir == tmp_path / "d"


def test_sources_enabled(settings) -> None:
    assert settings.source_enabled("demo")
    assert not settings.source_enabled("garmin")


@pytest.mark.parametrize(
    "body, message",
    [
        ('[user]\ntimezone = "Mars/Olympus"\n', "timezone"),
        ('[user]\nunits = "furlongs"\n', "units"),
        ("[sources]\ndemo = 1\n", "must be a table"),
        ("not toml ===", "c.toml"),
    ],
)
def test_invalid_config(tmp_path: Path, body: str, message: str) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_settings(cfg)


def test_default_config_is_loadable(tmp_path: Path) -> None:
    cfg = tmp_path / "sub" / "config.toml"
    write_default_config(cfg, "Europe/Amsterdam")
    assert load_settings(cfg).timezone == "Europe/Amsterdam"


def test_retired_publication_keys_are_ignored_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from vibesynergy.core import config as config_module

    config_module._RETIRED_WARNED.clear()
    path = tmp_path / "config.toml"
    path.write_text(
        '[publication]\ninsight_enabled = false\nlabel_insight = "bad: label"\n'
        "quote_enabled = false\n"
    )
    with caplog.at_level("WARNING", logger="vibesynergy.core.config"):
        first = load_settings(path)
        load_settings(path)
    assert first.publication == {"quote_enabled": False}
    warnings = [r.getMessage() for r in caplog.records if "is ignored" in r.getMessage()]
    assert warnings == [
        "[publication] insight_enabled is ignored; nothing uses it any more",
        "[publication] label_insight is ignored; nothing uses it any more",
    ]
