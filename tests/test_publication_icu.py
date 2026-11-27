"""Strava-only publication (1 Nov 2026), and nothing written to intervals.icu (#114, #160);
all synthetic."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_evidence import START, put
from test_publication import DEFAULT_OPENER, garmin_activity
from test_publication_auto import NOW, switch_on
from test_publication_insight import PRIVATE, PUBLIC, write
from test_publication_targets import FakeGarmin, FakeStrava

from vibesynergy.core import config_edit
from vibesynergy.query import publication, publication_auto, publication_plugins
from vibesynergy.sources.intervals_icu.client import API_KEY_ENV, ATHLETE_ID_ENV
from vibesynergy.store.schema import audit_log, publication_ledger

OWN_TEXT = "Mijn eigen notitie in intervals.icu"
CLOSER = "[ http://vibesynergy.nl/ */]"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


def providers(monkeypatch, strava, garmin=None):
    garmin = garmin or FakeGarmin()
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )
    return strava, garmin


@pytest.fixture
def both(runtime, monkeypatch):
    a = garmin_activity()
    put(runtime, "garmin", a)
    put(runtime, "strava", replace(a, source_id="2", name="Strava title"))
    return providers(monkeypatch, FakeStrava())


def audits(runtime, action):
    with runtime.engine.connect() as conn:
        rows = conn.execute(
            select(audit_log).where(audit_log.c.action == action).order_by(audit_log.c.id)
        )
        return rows.mappings().all()


def icu_ledger(runtime):
    with runtime.engine.connect() as conn:
        return (
            conn.execute(
                select(publication_ledger).where(publication_ledger.c.source == "intervals_icu")
            )
            .mappings()
            .all()
        )


# --- Strava-only: the same block, filled from Strava's own figures --------------------------------


def strava_only(**extra):
    return replace(
        garmin_activity(),
        source_id="2",
        name="MyWhoosh ride",
        sport="VirtualRide",
        elapsed_s=3600,
        aerobic_te=None,
        anaerobic_te=None,
        training_load=None,
        fit_id=None,
        extra={"suffer_score": 42, "kilojoules": 655.4, "trainer": True, **extra},
    )


def test_strava_only_is_published_with_strava_stats_after_waiting_for_garmin(runtime, monkeypatch):
    strava, _ = providers(monkeypatch, FakeStrava())
    put(runtime, "strava", strava_only())
    switch_on(runtime, language="nl")
    ended = START + timedelta(hours=1)
    # Still waiting for a Garmin copy that may come.
    assert publication_auto.candidates(runtime, now=ended + timedelta(minutes=10)) == []
    later = ended + timedelta(minutes=31)
    assert publication_auto.run(runtime, now=later)["published"] == 1
    text = strava.text
    assert text.startswith(f"Strava words\n\n{DEFAULT_OPENER}")
    assert "Strava stats" not in text  # one block and label, whatever the figures' source
    assert "Relatieve inspanning: 42" in text and "Arbeid: 655 kJ" in text
    # No Garmin lines that do not exist, and no RPE or feel without one (Zwift asks none).
    assert "Aerobe" not in text and "RPE" not in text and "feel" not in text
    # The coach's insight arrives later and is added through the same path.
    write(runtime, "a", activity_id="strava:2", public_insight=PUBLIC)
    summary = publication_auto.run(runtime, now=later + timedelta(minutes=5))
    assert summary["insight_updated"] == 1
    assert f"-Persoonlijke AI inzicht: {PUBLIC}" in strava.text
    assert PRIVATE not in strava.text and strava.text.count(CLOSER) == 1


def test_strava_only_with_rpe_from_intervals_icu(runtime, monkeypatch):
    from vibesynergy.query import activity_effort

    providers(monkeypatch, FakeStrava())
    put(runtime, "strava", strava_only())
    monkeypatch.setattr(
        activity_effort,
        "for_activity",
        lambda *_a, **_k: {"rpe": {"value": 6}, "feel": {"value": 2}},
    )
    from vibesynergy.query import publication_format

    lines = publication_format.strava_stats_lines(
        publication_format.strava_fields(runtime, "strava:2"), "en"
    )
    assert lines == [
        "❤️ Relative Effort: 42",
        "⚙️ Work: 655 kJ",
        "😌 Workout feel: Good",
        "😄 RPE: 6/10 (Difficult)",
    ]


# --- intervals.icu is out of the text sync (#114, #160) ------------------------------------------


class Recorder:
    """Every intervals.icu request VibeSynergy would send, instead of the network."""

    def __init__(self):
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(self, client, method, path, body, params=None, write=False):
        self.calls.append((method, path, write))
        if method == "GET" and "/activities" in path:
            return []
        if method == "GET":
            return {"id": "i9", "description": OWN_TEXT, "source": "GARMIN_CONNECT"}
        raise AssertionError(f"intervals.icu write: {method} {path}")


@pytest.fixture
def icu(monkeypatch):
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    monkeypatch.setenv(API_KEY_ENV, "secret-key")
    monkeypatch.setenv(ATHLETE_ID_ENV, "i000001")
    recorder = Recorder()
    monkeypatch.setattr(IntervalsIcuClient, "_send", recorder)
    return recorder


def icu_writes(recorder):
    return [c for c in recorder.calls if c[2] or c[0] != "GET"]


def test_publish_insight_and_unpublish_write_nothing_to_intervals_icu(runtime, both, icu):
    switch_on(runtime, language="nl")
    path = runtime.settings.config_path  # the retired switch, still on in an old config
    path.write_text(
        path.read_text().replace(
            "[publication]\n", "[publication]\nicu_description_enabled = true\n", 1
        )
    )
    assert publication_auto.run(runtime, now=NOW)["published"] == 1
    write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    assert publication_auto.run(runtime, now=NOW + timedelta(minutes=15))["insight_updated"] == 1
    assert not publication_auto.due(runtime, now=NOW + timedelta(minutes=40))
    item = publication.preview(runtime, "garmin:1", "strava", language="nl")
    result = publication.unpublish(
        runtime, "garmin:1", "strava", item["preview_version"], language="nl"
    )
    assert "icu" not in result
    assert icu_writes(icu) == []
    assert icu_ledger(runtime) == [] and audits(runtime, "publication.icu") == []


def test_the_client_has_no_description_write():
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    assert not hasattr(IntervalsIcuClient, "update_activity_description")


def test_the_retired_switch_is_not_saved_any_more(runtime):
    from vibesynergy.core.config import ConfigError, load_settings

    with pytest.raises(ConfigError, match="icu_description_enabled"):
        config_edit.apply_changes(
            runtime.settings.config_path, {"publication": {"icu_description_enabled": False}}
        )
    # An old file that still has it: ignored on load, dropped on the next save (#175).
    path = runtime.settings.config_path
    path.write_text(path.read_text() + "\n[publication]\nicu_description_enabled = true\n")
    assert "icu_description_enabled" not in load_settings(path).publication
    config_edit.apply_changes(path, {"publication": {"quote_enabled": False}})
    assert "icu_description_enabled" not in path.read_text()


def test_insight_update_keeps_text_typed_on_strava_after_the_first_block(runtime, both, icu):
    """22 Nov: block published, the athlete typed his text on Strava, the insight update
    went to Strava with the text and then to intervals.icu without it, which wrote that
    copy back to Strava. intervals.icu is no longer written, so the text stays on Strava,
    and Garmin keeps its own text with the same new block (#175)."""
    strava, garmin = both
    strava.text = ""
    switch_on(runtime, language="nl")
    assert publication_auto.run(runtime, now=NOW)["published"] == 1
    first_block = strava.text
    assert first_block.startswith(DEFAULT_OPENER)
    mine = "Toch nog een beetje trail… \nPrachtig weer en lekker gelopen."
    strava.text = f"{mine}\n\n{first_block}"  # typed on Strava, after the first publish
    write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    summary = publication_auto.run(runtime, now=NOW + timedelta(minutes=15))
    assert summary["insight_updated"] == 1 and summary["review"] == 0
    assert strava.text.startswith(f"{mine}\n\n{DEFAULT_OPENER}")
    assert PUBLIC in strava.text and strava.text.count(CLOSER) == 1
    block = strava.text.split("\n\n", 1)[1]
    assert garmin.text == f"Garmin words\n\n{block}"
    assert icu_writes(icu) == []
    writes = (len(strava.writes), len(garmin.writes))
    publication_auto.run(runtime, now=NOW + timedelta(minutes=20))
    assert (len(strava.writes), len(garmin.writes)) == writes
