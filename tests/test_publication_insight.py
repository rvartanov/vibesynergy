"""The coach's public insight in the VibeSynergy block; every provider is synthetic."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_coach_predictions import _call
from test_evidence import START, put
from test_publication import DEFAULT_OPENER, garmin_activity
from test_publication_auto import NOW, switch_on
from test_publication_mirror import FakeGarmin, FakeStrava

from vibesynergy.query import (
    coach_workouts,
    publication,
    publication_auto,
    publication_insight,
    publication_plugins,
)
from vibesynergy.store.schema import audit_log, publication_ledger

DAY = START.date()
PUBLIC = "Rustige duurrit precies volgens plan. Een goede start van het blok."
PRIVATE = "Knie deed weer pijn, slecht geslapen"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


@pytest.fixture
def fakes(runtime, monkeypatch):
    a = garmin_activity()
    put(runtime, "garmin", a)
    put(runtime, "strava", replace(a, source_id="2", name="Strava title"))
    strava, garmin = FakeStrava(), FakeGarmin()
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )
    return strava, garmin


def write(runtime, key, **kwargs):
    values = {
        "entry_type": "activity_assessment",
        "summary": "Private coach summary.",
        "details": PRIVATE,
        "idempotency_key": key,
        "for_date": DAY,
        "author": "client:synthetic",
    }
    return coach_workouts.coach_write(runtime.engine, **(values | kwargs))


def audits(runtime, action):
    with runtime.engine.connect() as conn:
        rows = conn.execute(select(audit_log).where(audit_log.c.action == action))
        return rows.mappings().all()


def test_no_insight_means_no_line(runtime, fakes):
    item = publication.preview(runtime, "garmin:1", "strava", language="nl")
    assert "AI inzicht" not in item["owned_block"]
    assert item["insight"] == {"state": "missing"}


def test_only_the_public_insight_is_published(runtime, fakes):
    entry = write(runtime, "a", activity_id="strava:2", public_insight=f"  {PUBLIC}\n")
    assert entry["meta"]["activity_id"] == "garmin:1"  # linked to the group, any id given
    assert entry["meta"]["public_insight"] == PUBLIC
    item = publication.preview(runtime, "garmin:1", "strava", language="nl")
    assert item["owned_block"].startswith(
        f"{DEFAULT_OPENER}\n-Persoonlijke AI inzicht: {PUBLIC}\n-Garmin stats:\n"
    )
    assert item["insight"] == {"state": "present", "entry_id": entry["id"]}
    for private in (PRIVATE, "Private coach summary"):
        assert private not in item["description"]


def test_insight_follows_the_activity_when_garmin_arrives_later(runtime, monkeypatch):
    """Written while only Strava had it; the group then becomes garmin:1 (1 Nov)."""
    monkeypatch.setattr(publication, "provider", lambda *_: FakeStrava())
    a = garmin_activity()
    put(runtime, "strava", replace(a, source_id="2", name="Strava title"))
    entry = write(runtime, "a", activity_id="strava:2", public_insight=PUBLIC)
    assert entry["meta"]["activity_id"] == "strava:2"
    put(runtime, "garmin", a)
    found = publication_insight.for_activity(runtime.engine, "garmin:1")
    assert found and found["entry_id"] == entry["id"]


def test_entry_without_public_insight_never_reaches_the_block(runtime, fakes):
    write(runtime, "a", evidence_refs=["garmin:1"])
    write(runtime, "b", activity_id="garmin:1")
    assert publication_insight.for_activity(runtime.engine, "garmin:1") is None


def test_newest_entry_wins_and_empty_withdraws(runtime, fakes):
    write(runtime, "a", activity_id="garmin:1", public_insight="Eerste versie.")
    second = write(runtime, "b", activity_id="garmin:1", public_insight=PUBLIC)
    assert publication_insight.for_activity(runtime.engine, "strava:2")["entry_id"] == second["id"]
    write(runtime, "c", activity_id="garmin:1", public_insight="")
    assert publication_insight.for_activity(runtime.engine, "garmin:1") is None


def test_marker_convention_uses_the_summary(runtime, fakes):
    """Entries written before the field existed: details start with public_insight."""
    entry = write(
        runtime,
        "a",
        summary=PUBLIC,
        details="public_insight (v2). HR 117/131; private numbers",
        evidence_refs=["garmin:1"],
    )
    found = publication_insight.for_activity(runtime.engine, "garmin:1")
    assert found["text"] == PUBLIC and found["entry_id"] == entry["id"]
    # Linked to more than one activity: ambiguous, so not used.
    put(runtime, "garmin", replace(garmin_activity(), source_id="9", start_utc=NOW))
    write(
        runtime,
        "b",
        summary="Brick.",
        details="public_insight.",
        evidence_refs=["garmin:1", "garmin:9"],
    )
    assert publication_insight.for_activity(runtime.engine, "garmin:1")["entry_id"] == entry["id"]
    # Not a marker: a word that only starts with it.
    write(
        runtime, "c", summary="Nope.", details="public_insights later", evidence_refs=["garmin:1"]
    )
    assert publication_insight.for_activity(runtime.engine, "garmin:1")["entry_id"] == entry["id"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"public_insight": PUBLIC}, "activity_id"),
        ({"activity_id": "garmin:1", "public_insight": "x" * 301}, "300"),
        ({"activity_id": "garmin:1", "public_insight": "[/*] trick"}, r"\[, \]"),
        ({"activity_id": "garmin:1", "public_insight": "end */"}, r"\*/"),
        ({"activity_id": "garmin:404", "public_insight": PUBLIC}, "no activity"),
        ({"entry_type": "evening", "activity_id": "garmin:1", "public_insight": PUBLIC}, "belong"),
    ],
)
def test_invalid_public_insight_is_refused(runtime, fakes, kwargs, match):
    with pytest.raises(ValueError, match=match):
        write(runtime, "bad", **kwargs)


def test_a_bad_marker_summary_is_left_out_not_shortened(runtime, fakes):
    write(runtime, "a", summary="y" * 400, details="public_insight", evidence_refs=["garmin:1"])
    assert publication_insight.for_activity(runtime.engine, "garmin:1") is None


def test_later_insight_updates_the_published_block_and_the_garmin_copy(runtime, fakes):
    strava, garmin = fakes
    switch_on(runtime, language="nl")
    assert publication_auto.run(runtime, now=NOW)["published"] == 1
    assert "AI inzicht" not in strava.text
    assert publication_auto.refresh_candidates(runtime, now=NOW) == []
    # The athlete adds own text above the block on Strava; the coach writes the insight.
    strava.text = "Eigen woorden van mij\n\n" + strava.text.split("\n\n", 1)[1]
    entry = write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    later = NOW + timedelta(hours=3)
    assert publication_auto.due(runtime, now=later)
    summary = publication_auto.run(runtime, now=later)
    assert summary["insight_updated"] == 1 and summary["published"] == 0
    assert strava.text.startswith(
        f"Eigen woorden van mij\n\n{DEFAULT_OPENER}\n-Persoonlijke AI inzicht: {PUBLIC}\n"
    )
    assert garmin.text.endswith(strava.text.split("\n\n", 1)[1])
    assert garmin.text.startswith("Eigen woorden van mij")
    with runtime.engine.connect() as conn:
        rows = conn.execute(select(publication_ledger)).mappings().all()
    assert {(r["source"], r["state"]) for r in rows} == {
        ("strava", "verified"),
        ("garmin", "verified"),
    }
    assert all(PUBLIC in r["owned_block"] for r in rows)
    [insight_audit] = audits(runtime, "publication.insight")
    assert insight_audit["outcome"] == "ok" and insight_audit["detail"]["entry_id"] == entry["id"]
    assert PUBLIC not in str(insight_audit["detail"])
    assert [r["target"] for r in audits(runtime, "publication.update")] == ["strava:2"]
    # Idempotent: nothing more to do, nothing written again.
    writes = len(strava.writes)
    assert not publication_auto.due(runtime, now=later + timedelta(minutes=5))
    assert publication_auto.run(runtime, now=later + timedelta(minutes=5))["insight_updated"] == 0
    assert len(strava.writes) == writes
    # Withdrawn: the line goes again.
    write(runtime, "b", activity_id="garmin:1", public_insight="")
    assert publication_auto.run(runtime, now=later + timedelta(minutes=10))["insight_updated"] == 1
    assert "AI inzicht" not in strava.text and "Eigen woorden van mij" in strava.text


def test_hand_edited_block_is_left_for_review(runtime, fakes):
    strava, _ = fakes
    switch_on(runtime)
    publication_auto.run(runtime, now=NOW)
    strava.text = strava.text.replace("Training load", "Mijn eigen load")
    write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    writes = len(strava.writes)
    summary = publication_auto.run(runtime, now=NOW + timedelta(hours=1))
    assert summary["review"] == 1 and summary["insight_updated"] == 0
    assert len(strava.writes) == writes and "Mijn eigen load" in strava.text
    assert audits(runtime, "publication.insight")[0]["outcome"] == "needs_review"
    # Not retried for the same insight.
    assert publication_auto.refresh_candidates(runtime, now=NOW + timedelta(days=1)) == []


def test_activity_list_shows_what_still_lacks_an_insight(runtime, fakes):
    listing = publication_insight.recent(runtime, days=2, today=DAY)
    assert listing["missing"] == ["garmin:1"]
    assert listing["activities"][0]["strava"] == "not_published"
    # Still private on Strava (the evening fills it anyway) unless Strava said otherwise.
    assert listing["activities"][0]["strava_public"] in (None, False)
    write(runtime, "a", activity_id="garmin:1", public_insight=PUBLIC)
    listing = publication_insight.recent(runtime, days=2, today=DAY)
    assert listing["missing"] == [] and listing["activities"][0]["insight"] == "present"
    assert publication_insight.recent(runtime, days=2, today=DAY + timedelta(days=5)) == {
        "activities": [],
        "missing": [],
    }
    with pytest.raises(ValueError):
        publication_insight.recent(runtime, days=30)


def test_mcp_write_and_list(runtime, fakes):
    written = _call(
        runtime,
        None,
        "vibesynergy_coach_write",
        {
            "entry_type": "activity_assessment",
            "summary": "Private summary.",
            "idempotency_key": "mcp-insight-1",
            "for_date": DAY.isoformat(),
            "activity_id": "strava:2",
            "public_insight": PUBLIC,
        },
    )
    assert not written.is_error
    assert written.structured_content["data"]["meta"]["public_insight"] == PUBLIC
    refused = _call(
        runtime,
        None,
        "vibesynergy_coach_write",
        {
            "entry_type": "activity_assessment",
            "summary": "x",
            "idempotency_key": "mcp-insight-2",
            "for_date": DAY.isoformat(),
            "public_insight": PUBLIC,
        },
    )
    assert refused.is_error
    listed = _call(runtime, None, "vibesynergy_activity_insights", {"days": 14})
    assert not listed.is_error
    # The synthetic activity is weeks ago; the shape is what matters here.
    assert set(listed.structured_content["data"]) >= {"activities", "missing"}
    assert _call(runtime, None, "vibesynergy_activity_insights", {"days": 30}).is_error
