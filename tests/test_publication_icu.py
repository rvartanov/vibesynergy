"""Strava-only publication and the same block in intervals.icu (1 Nov 2026); all synthetic."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_activity_effort import ActivityApi
from test_evidence import START, put
from test_publication import DEFAULT_OPENER, garmin_activity
from test_publication_auto import NOW, switch_on
from test_publication_insight import PRIVATE, PUBLIC, write
from test_publication_mirror import FakeGarmin, FakeStrava

from vibesynergy.core import config_edit
from vibesynergy.query import publication, publication_auto, publication_icu, publication_plugins
from vibesynergy.sources.intervals_icu.client import API_KEY_ENV, ATHLETE_ID_ENV, IntervalsIcuError
from vibesynergy.store.schema import audit_log, publication_ledger

OWN_TEXT = "Mijn eigen notitie in intervals.icu"
CLOSER = "[ http://vibesynergy.nl/ */]"


class FakeIcu:
    """intervals.icu: one activity found in the live list, GET and description PUT."""

    def __init__(self, description=OWN_TEXT, *, listed=True, source="GARMIN_CONNECT"):
        self.record = {"id": "i9", "description": description, "source": source}
        self.listed = listed
        self.writes: list[str] = []

    def activities(self, oldest, newest):
        if not self.listed:
            return []
        return [{"id": "i9", "external_id": "1", "strava_id": "2", "source": self.record["source"],
                 "start_date": START.isoformat()}]  # fmt: skip

    def get_activity(self, activity_id):
        assert activity_id == "i9"
        return dict(self.record)

    def update_activity_description(self, activity_id, description):
        assert activity_id == "i9"
        self.writes.append(description)
        self.record["description"] = description
        return dict(self.record)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


@pytest.fixture
def icu(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "secret-key")
    monkeypatch.setenv(ATHLETE_ID_ENV, "i000001")
    fake = FakeIcu()
    monkeypatch.setattr(publication_icu, "_client", lambda client=None: client or fake)
    return fake


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
        rows = conn.execute(select(audit_log).where(audit_log.c.action == action))
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


# --- intervals.icu description ----------------------------------------------------------------


def test_published_block_lands_in_intervals_icu_below_the_athletes_text(runtime, both, icu):
    strava, _ = both
    switch_on(runtime, language="nl")
    summary = publication_auto.run(runtime, now=NOW)
    assert summary["published"] == 1
    owned = strava.text.split("\n\n", 1)[1]
    assert icu.writes == [f"{OWN_TEXT}\n\n{owned}"]
    (row,) = icu_ledger(runtime)
    assert row["source_id"] == "i9" and row["state"] == "verified" and row["owned_block"] == owned
    (audit,) = audits(runtime, "publication.icu")
    assert audit["outcome"] == "ok" and audit["target"] == "i9"
    assert audit["detail"]["had_text"] is True and audit["detail"]["replaced_block"] is False
    assert OWN_TEXT not in str(audit["detail"])  # no text in the audit log
    # Idempotent: more runs write nothing.
    for minutes in (5, 10):
        publication_auto.run(runtime, now=NOW + timedelta(minutes=minutes))
    assert len(icu.writes) == 1
    # The insight replaces only VibeSynergy's block there too; the athlete's text stays.
    write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    assert publication_auto.run(runtime, now=NOW + timedelta(minutes=15))["insight_updated"] == 1
    assert len(icu.writes) == 2
    final = icu.record["description"]
    assert final.startswith(f"{OWN_TEXT}\n\n{DEFAULT_OPENER}") and final.count(CLOSER) == 1
    assert PUBLIC in final and PRIVATE not in final
    assert final == f"{OWN_TEXT}\n\n{strava.text.split(chr(10) * 2, 1)[1]}"
    publication_auto.run(runtime, now=NOW + timedelta(minutes=20))
    assert len(icu.writes) == 2


def test_switch_off_leaves_intervals_icu_alone(runtime, both, icu):
    switch_on(runtime, icu_description_enabled=False)
    assert publication_auto.run(runtime, now=NOW)["published"] == 1
    assert icu.writes == [] and icu_ledger(runtime) == []


def test_strava_stub_and_review_are_not_written(runtime, both, icu):
    icu.record["source"] = "STRAVA"
    switch_on(runtime)
    publication_auto.run(runtime, now=NOW)
    assert icu.writes == []
    assert publication_auto.attempts(runtime)["icu:2"]["state"] == "not_available"
    assert (
        publication_icu.candidates(runtime, now=NOW, attempts=publication_auto.attempts(runtime))
        == []
    )
    # Two VibeSynergy blocks there: left for review, never retried.
    icu.record.update(
        source="GARMIN_CONNECT",
        description=f"{DEFAULT_OPENER}\na\n{CLOSER}\n\n{DEFAULT_OPENER}\nb\n{CLOSER}",
    )
    with pytest.raises(publication_icu.Review):
        publication_icu.sync(runtime, "garmin:1")
    assert icu.writes == []


def test_catches_up_when_intervals_icu_gets_the_activity_later(runtime, both, icu):
    icu.listed = False
    switch_on(runtime)
    publication_auto.run(runtime, now=NOW)
    assert icu.writes == [] and publication_auto.attempts(runtime)["icu:2"]["state"] == "waiting"
    assert not publication_auto.due(runtime, now=NOW + timedelta(minutes=5))  # waits to retry
    icu.listed = True
    later = NOW + timedelta(minutes=31)
    assert publication_auto.due(runtime, now=later)
    assert publication_auto.run(runtime, now=later)["icu_updated"] == 1
    assert len(icu.writes) == 1 and not publication_auto.due(runtime, now=later)


def test_removing_the_strava_block_removes_it_from_intervals_icu(runtime, both, icu):
    switch_on(runtime)
    publication_auto.run(runtime, now=NOW)
    item = publication.preview(runtime, "garmin:1", "strava", language="nl")
    result = publication.unpublish(
        runtime, "garmin:1", "strava", item["preview_version"], language="nl"
    )
    assert result["icu"] == {"state": "removed"}
    assert icu.record["description"] == OWN_TEXT and icu_ledger(runtime) == []


def test_empty_intervals_description_gets_only_the_block(runtime, both, icu):
    icu.record["description"] = None
    switch_on(runtime)
    publication_auto.run(runtime, now=NOW)
    assert icu.writes[0].startswith(DEFAULT_OPENER)


def test_client_sends_only_the_description():
    api = ActivityApi({"id": "i9", "description": "x", "name": "keep"})
    client = api.client()
    client.update_activity_description("i9", "new text")
    ((method, url, body),) = api.calls
    assert method == "PUT" and url.endswith("/activity/i9") and body == {"description": "new text"}
    with pytest.raises(IntervalsIcuError):
        client.update_activity_description("i9", "x" * 20001)


def test_setting_is_saved(runtime):
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"icu_description_enabled": False}}
    )
    from vibesynergy.core.config import ConfigError, load_settings

    assert (
        load_settings(runtime.settings.config_path).publication["icu_description_enabled"] is False
    )
    with pytest.raises(ConfigError):
        config_edit.apply_changes(
            runtime.settings.config_path, {"publication": {"icu_description_enabled": "yes"}}
        )
