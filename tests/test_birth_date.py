"""Date of birth in the athlete profile: stored in [user], the coach sees the age."""

from __future__ import annotations

from datetime import date

import pytest

from vibesynergy.core import config_edit
from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.query import coach


def test_age_on() -> None:
    assert coach.age_on(None, date(2026, 1, 1)) is None
    assert coach.age_on("1980-01-01", date(2026, 1, 1)) == 46
    assert coach.age_on("1980-01-02", date(2026, 1, 1)) == 45


def test_birth_date_saves_reads_back_and_clears(env) -> None:
    config_edit.apply_changes(env, {"user": {"birth_date": "1980-01-01"}})
    assert load_settings(env).athlete["birth_date"] == "1980-01-01"
    config_edit.apply_changes(env, {"user": {"birth_date": None}})
    assert "birth_date" not in load_settings(env).athlete


@pytest.mark.parametrize("bad", ["01-01-1980", "2999-01-01", "1800-01-01", "soon"])
def test_birth_date_must_be_a_past_iso_date(env, bad) -> None:
    env.write_text(env.read_text().replace("[user]", f'[user]\nbirth_date = "{bad}"'))
    with pytest.raises(ConfigError):
        load_settings(env)


def test_toml_date_literal_is_accepted(env) -> None:
    env.write_text(env.read_text().replace("[user]", "[user]\nbirth_date = 1980-01-01"))
    assert load_settings(env).athlete["birth_date"] == "1980-01-01"


def test_coach_gets_the_age_not_the_date(runtime, monkeypatch) -> None:
    from dataclasses import replace

    settings = replace(
        runtime.settings, athlete={**runtime.settings.athlete, "birth_date": "1980-01-01"}
    )
    profile = coach.athlete_for_coach(runtime.engine, settings)["profile"]
    assert isinstance(profile["age"], int) and profile["age"] >= 46
    assert "birth_date" not in profile
