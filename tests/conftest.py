from __future__ import annotations

from pathlib import Path

import pytest
from tests_helpers import contact_window_off, no_live_intervals_icu, portal_login_off

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
    no_live_intervals_icu(monkeypatch)


@pytest.fixture(autouse=True)
def _contact_window_off(monkeypatch: pytest.MonkeyPatch) -> None:
    contact_window_off(monkeypatch)


@pytest.fixture(autouse=True)
def _portal_login_off(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if not request.node.get_closest_marker("auth"):
        portal_login_off(monkeypatch)


def drop_workout_method(rt) -> None:
    """Remove the workout method that migration 0058 seeds into every new database (#142),
    for a test that counts the knowledge-base articles it writes itself."""
    from sqlalchemy import text

    with rt.engine.begin() as conn:
        ids = conn.execute(text("SELECT id FROM kb_article WHERE slug = 'workout-method'"))
        for (article_id,) in ids.all():
            conn.execute(
                text("DELETE FROM kb_article_revision WHERE article_id = :i"), {"i": article_id}
            )
            conn.execute(text("DELETE FROM kb_article WHERE id = :i"), {"i": article_id})
        conn.execute(text("DROP TABLE IF EXISTS kb_fts"))
