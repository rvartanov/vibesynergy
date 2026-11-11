"""Diary notes from what was entered with activities: Strava text. Garmin feel/effort is
shown as RPE and Feel, not copied into the diary."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select

from vibesynergy.derive import unify
from vibesynergy.model import Activity
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import journal
from vibesynergy.store.schema import note

DAY = date(2026, 9, 19)
T = datetime(2026, 9, 19, 8, tzinfo=UTC)
GENERATED = (
    "Top race, legs heavy at the end.\n\n-Persoonlijke AI inzicht: steady.\n"
    "-Garmin stats:\n  RPE: 60/100\n\n-Quote: go."
)


def _put(rt, source, records, key):
    stored = rt.raw.put(
        source=source, dataset="test", item_key=key, payload=key.encode(), content_type="text/plain"
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)


def _notes(rt):
    with rt.engine.connect() as conn:
        return {(r.source, r.source_ref): r for r in conn.execute(select(note))}


def test_personal_text() -> None:
    assert journal.personal_text(GENERATED) == "Top race, legs heavy at the end."
    assert journal.personal_text("-Persoonlijke AI inzicht: only generated") is None
    assert (
        journal.personal_text("Recorded on Perform for iOS 🎧 Music powered by Perform.fm") is None
    )
    assert journal.personal_text(None) is None


def test_import_creates_updates_and_removes(runtime) -> None:
    _put(
        runtime,
        "garmin",
        [Activity("g1", T, DAY, "running", name="Long run", extra={"feel": 75.0, "rpe": 60.0})],
        "g",
    )
    _put(runtime, "strava", [Activity("s1", T, DAY, "Run", extra={"description": GENERATED})], "s")
    _put(
        runtime,
        "strava",
        [
            Activity(
                "s2", T, DAY, "Run", extra={"description": "Playlist synced with Perform (pr.fm)"}
            )
        ],
        "s2",
    )
    unify.rebuild(runtime.engine)
    assert journal.import_activity_notes(runtime.engine) == 1
    notes = _notes(runtime)
    assert ("garmin", "g1") not in notes  # RPE/Feel live on the activity, not in the diary
    assert notes[("strava", "s1")].text == "Top race, legs heavy at the end."
    assert notes[("strava", "s1")].activity_id and ("strava", "s2") not in notes
    assert journal.import_activity_notes(runtime.engine) == 0  # nothing changed

    # The description is edited to app text only: the imported note goes.
    _put(
        runtime,
        "strava",
        [Activity("s1", T, DAY, "Run", extra={"description": "Recorded on Perform for iOS"})],
        "s1b",
    )
    assert journal.import_activity_notes(runtime.engine) == 1
    assert ("strava", "s1") not in _notes(runtime)


def test_earlier_garmin_feel_notes_are_removed_by_the_import(runtime) -> None:
    """Notes the import wrote from Garmin feel/effort before go on the next run; the
    athlete's own notes and Strava text stay."""
    _put(
        runtime,
        "garmin",
        [Activity("g1", T, DAY, "running", name="Long run", extra={"feel": 75.0, "rpe": 60.0})],
        "g",
    )
    unify.rebuild(runtime.engine)
    journal.add_note(
        runtime.engine,
        text="Long run: felt strong, effort 6/10 (Garmin).",
        start=DAY,
        source="garmin",
        source_ref="g1",
    )
    own = journal.add_note(runtime.engine, text="Legs heavy", start=DAY, source="portal")
    assert journal.import_activity_notes(runtime.engine) == 1
    notes = _notes(runtime)
    assert ("garmin", "g1") not in notes
    assert any(row.id == own["id"] for row in notes.values())
    assert journal.import_activity_notes(runtime.engine) == 0


def test_coach_still_reads_garmin_rpe_and_feel_over_mcp(runtime) -> None:
    from vibesynergy.mcp.server import build_server

    _put(
        runtime,
        "garmin",
        [Activity("g1", T, DAY, "running", name="Long run", extra={"feel": 75.0, "rpe": 60.0})],
        "g",
    )
    unify.rebuild(runtime.engine)
    journal.import_activity_notes(runtime.engine)
    tool = build_server(runtime)._tool_manager.get_tool("vibesynergy_activity")
    effort = tool.fn("garmin:g1")["data"]["effort"]
    assert effort["rpe"]["value"] == 6
    assert effort["feel"]["value"] == 2  # Garmin 75/100 "strong" is Feel 2
