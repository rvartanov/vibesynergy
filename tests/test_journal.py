"""The diary: notes, check-ins, the injury log and its warning rules."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from vibesynergy.core.config import Settings
from vibesynergy.core.timeutil import Window
from vibesynergy.query import journal, views
from vibesynergy.runtime import Runtime

TODAY = date(2026, 9, 25)


@pytest.fixture
def engine(tmp_path):
    settings = Settings(
        config_path=tmp_path / "config.toml",
        data_dir=tmp_path / "data",
        timezone="Europe/Amsterdam",
        sources={},
    )
    rt = Runtime.open(settings)
    rt.migrate()
    yield rt.engine
    rt.close()


def test_notes_overlap_tags_and_update(engine) -> None:
    trip = journal.add_note(
        engine,
        text="Conference in Lisbon",
        start=date(2026, 9, 10),
        end=date(2026, 9, 14),
        tags=["Travel", "work", "travel"],
        source="claude",
    )
    assert trip["tags"] == ["travel", "work"] and trip["id"].startswith("note_")
    journal.add_note(engine, text="Cold", start=date(2026, 9, 13), tags=["sick"])

    found = journal.notes(engine, Window(date(2026, 9, 12), date(2026, 9, 13)))
    assert [n["text"] for n in found] == ["Conference in Lisbon", "Cold"]
    assert [n["text"] for n in journal.notes(engine, Window(TODAY, TODAY))] == []
    assert len(journal.notes(engine, Window(date(2026, 9, 1), TODAY), tag="sick")) == 1
    assert journal.tagged_days(engine, Window(date(2026, 9, 1), TODAY), ["travel"]) == {
        date(2026, 9, d) for d in range(10, 15)
    }

    edited = journal.edit_note(engine, trip["id"], end=date(2026, 9, 15), tags=["travel"])
    assert edited["end_date"] == "2026-09-15" and edited["tags"] == ["travel"]
    journal.delete_note(engine, trip["id"])
    with pytest.raises(ValueError, match="no note"):
        journal.get_note(engine, trip["id"])


def test_notes_for_activity(engine) -> None:
    tied = journal.add_note(
        engine, text="Felt strong", start=TODAY, activity_id="grp_1", source="strava"
    )
    journal.add_note(engine, text="Unrelated", start=TODAY)
    assert [n["id"] for n in journal.notes_for_activity(engine, "grp_1")] == [tied["id"]]
    assert journal.notes_for_activity(engine, "grp_2") == []


def test_imported_notes_update_in_place(engine) -> None:
    first = journal.add_note(engine, text="Easy", start=TODAY, source="strava", source_ref="act_1")
    again = journal.add_note(
        engine, text="Easy, windy", start=TODAY, source="strava", source_ref="act_1"
    )
    assert first["id"] == again["id"] and again["text"] == "Easy, windy"
    assert len(journal.notes(engine, Window(TODAY, TODAY))) == 1


def test_note_validation(engine) -> None:
    with pytest.raises(ValueError, match="empty"):
        journal.add_note(engine, text="  ", start=TODAY)
    with pytest.raises(ValueError, match="before start"):
        journal.add_note(engine, text="x", start=TODAY, end=TODAY - timedelta(days=1))


def test_checkin_partial_updates(engine) -> None:
    journal.set_checkin(engine, TODAY, sleep_quality=4, mood=3)
    out = journal.set_checkin(engine, TODAY, soreness=2, note="legs heavy")
    assert (out["sleep_quality"], out["mood"], out["soreness"]) == (4, 3, 2)
    assert out["note"] == "legs heavy"
    with pytest.raises(ValueError, match="1 to 5"):
        journal.set_checkin(engine, TODAY, mood=7)
    with pytest.raises(ValueError, match="at least one"):
        journal.set_checkin(engine, TODAY)


def test_empty_checkin_rejected(engine) -> None:
    with pytest.raises(ValueError, match="at least one"):
        journal.set_checkin(engine, TODAY, note="")
    assert journal.checkins(engine, Window(TODAY, TODAY)) == []
    with pytest.raises(ValueError, match="at least one"):
        journal.set_checkin(engine, TODAY, note="   ")
    assert journal.checkins(engine, Window(TODAY, TODAY)) == []
    stored = journal.set_checkin(engine, TODAY, mood=3, note="legs heavy")
    assert stored["note"] == "legs heavy"
    cleared = journal.set_checkin(engine, TODAY, note="")
    assert cleared["mood"] == 3 and cleared["note"] is None
    assert len(journal.checkins(engine, Window(TODAY, TODAY))) == 1


def test_injury_is_found_by_place_and_logged_per_day(engine) -> None:
    start = TODAY - timedelta(days=2)
    first = journal.log_injury(
        engine, today=TODAY, body_part="Calf", side="left", pain=2, timing="after", when=start
    )
    assert first["title"] == "Left calf" and first["status"] == "active"
    assert first["kind"] == "niggle" and first["onset"] == str(start)

    # Same place: the open injury is reused, not a new one.
    again = journal.log_injury(engine, today=TODAY, body_part="calf", side="left", pain=3)
    assert again["id"] == first["id"] and len(again["pain_log"]) == 2
    # Logging the same day again replaces that day's entry (worst pain of the day).
    again = journal.log_injury(engine, today=TODAY, injury_id=first["id"], pain=4)
    assert [e["pain"] for e in again["pain_log"]] == [2, 4]

    other = journal.log_injury(engine, today=TODAY, body_part="calf", side="right", pain=1)
    assert other["id"] != first["id"]
    assert len(journal.injuries(engine, TODAY)) == 2

    done = journal.log_injury(
        engine, today=TODAY, injury_id=first["id"], status="resolved", notes_text="Physio: fine"
    )
    assert done["status"] == "resolved" and done["resolved_on"] == str(TODAY)
    assert "Physio: fine" in done["notes"] and done["flags"] == []
    assert [i["id"] for i in journal.injuries(engine, TODAY)] == [other["id"]]
    assert len(journal.injuries(engine, TODAY, include_resolved=True)) == 2


def test_injury_validation(engine) -> None:
    with pytest.raises(ValueError, match="body_part must be one of"):
        journal.log_injury(engine, today=TODAY, body_part="pinky", side="left")
    with pytest.raises(ValueError, match="side must be one of"):
        journal.log_injury(engine, today=TODAY, body_part="knee", side="middle")
    with pytest.raises(ValueError, match="0 \\(none\\) to 10"):
        journal.log_injury(engine, today=TODAY, body_part="knee", side="left", pain=11)
    with pytest.raises(ValueError, match="future"):
        journal.log_injury(
            engine, today=TODAY, body_part="knee", side="left", when=TODAY + timedelta(days=1)
        )
    with pytest.raises(ValueError, match="no open injury"):
        journal.log_injury(engine, today=TODAY, body_part="hip", side="left", status="resolved")


def _rules(item: dict) -> set[str]:
    return {f["rule"] for f in item["flags"]}


def test_warning_rules(engine) -> None:
    for i, pain in enumerate([2, 3, 5]):
        out = journal.log_injury(
            engine,
            today=TODAY,
            body_part="achilles",
            side="right",
            pain=pain,
            when=TODAY - timedelta(days=2 - i),
        )
    assert _rules(out) == {"pain_above_3", "worsening"}
    pain = next(flag for flag in out["flags"] if flag["rule"] == "pain_above_3")
    assert "25-9-2026" in pain["message"]
    assert "2026-09-25" not in pain["message"]

    out = journal.log_injury(
        engine, today=TODAY, injury_id=out["id"], pain=5, affects_movement=True
    )
    assert "affects_movement" in _rules(out)
    assert next(f for f in out["flags"] if f["rule"] == "affects_movement")["level"] == "stop"

    # Not rising any more: no 'worsening'.
    out = journal.log_injury(engine, today=TODAY, injury_id=out["id"], pain=2)
    assert _rules(out) == set()

    # Resolved, then back at the same place: 'recurring'.
    journal.log_injury(engine, today=TODAY, injury_id=out["id"], status="resolved")
    later = TODAY + timedelta(days=30)
    back = journal.log_injury(
        engine, today=later, body_part="achilles", side="right", pain=1, when=later
    )
    assert back["id"] != out["id"] and _rules(back) == {"recurring"}
    recurring = next(flag for flag in back["flags"] if flag["rule"] == "recurring")
    assert "25-9-2026" in recurring["message"]
    assert "2026-09-25" not in recurring["message"]

    # No update for more than a week.
    assert "no_update" in _rules(journal.injuries(engine, later + timedelta(days=8))[0])


def test_day_view_includes_the_journal(engine) -> None:
    journal.add_note(engine, text="Cold", start=TODAY, tags=["sick"])
    journal.set_checkin(engine, TODAY, mood=2)
    journal.log_injury(engine, today=TODAY, body_part="knee", side="left", pain=3)
    day = views.day(engine, TODAY)["journal"]
    assert day["notes"][0]["text"] == "Cold"
    assert day["checkin"]["mood"] == 2
    assert day["injuries"][0]["title"] == "Left knee" and day["injuries"][0]["pain"]["pain"] == 3
    assert views.day(engine, TODAY - timedelta(days=1))["journal"]["injuries"] == []


def test_diary_tables_are_queryable_with_sql(engine) -> None:
    from pathlib import Path

    from vibesynergy.query import sql

    journal.add_note(engine, text="Cold", start=TODAY, tags=["sick"])
    journal.log_injury(engine, today=TODAY, body_part="knee", side="left", pain=3)
    out = sql.run(
        Path(engine.url.database),
        "select n.text, l.pain from note n, injury_log l where l.date = n.start_date",
    )
    assert out["rows"] == [["Cold", 3]]


# Part 4: the diary in analyses ------------------------------------------------------------


def _hrv(engine, values: dict[date, float]) -> None:
    from vibesynergy.store.schema import daily_unified

    with engine.begin() as conn:
        conn.execute(
            daily_unified.insert(),
            [
                {
                    "subject_id": 1,
                    "date": d,
                    "metric": "hrv.night_avg",
                    "value": v,
                    "unit": "ms",
                    "source": "garmin",
                }
                for d, v in values.items()
            ],
        )


def test_diary_metrics_are_series(engine) -> None:
    from vibesynergy.query import catalog
    from vibesynergy.query import series as qs

    journal.set_checkin(engine, TODAY, mood=2, soreness=4)
    journal.log_injury(engine, today=TODAY, body_part="knee", side="left", pain=3)
    journal.log_injury(engine, today=TODAY, body_part="calf", side="right", pain=6)
    catalog.check_metrics(engine, ["checkin.mood", "pain.max"])
    values = qs.daily_values(
        engine, ["checkin.mood", "checkin.soreness", "pain.max"], Window(TODAY, TODAY)
    )
    assert values == {
        "checkin.mood": {TODAY: 2.0},
        "checkin.soreness": {TODAY: 4.0},
        "pain.max": {TODAY: 6.0},  # worst over all injuries
    }
    coverage = catalog.catalogue(engine)["pain.max"]["coverage"]
    assert coverage["days_with_data"] == 1 and coverage["sources"] == ["diary"]
    assert qs.first_date(engine, ["pain.max"]) == TODAY


def test_sick_days_are_left_out_of_baselines(engine) -> None:
    from vibesynergy.query import series as qs

    # 30 days around 50 ms, with a cold (20 ms) on 5 of them, then today at 50.
    days = {TODAY - timedelta(days=i): 50.0 + (i % 3) for i in range(1, 31)}
    cold = [TODAY - timedelta(days=i) for i in range(10, 15)]
    for d in cold:
        days[d] = 20.0
    days[TODAY] = 50.0
    _hrv(engine, days)
    with_cold = qs.baseline(engine, "hrv.night_avg", TODAY, exclude_tags=[])
    journal.add_note(engine, text="Cold", start=cold[-1], end=cold[0], tags=["sick"])
    healthy = qs.baseline(engine, "hrv.night_avg", TODAY)  # default leaves sick days out
    assert with_cold["n"] == 30 and healthy["n"] == 25
    assert healthy["baseline_mean"] > with_cold["baseline_mean"]
    assert healthy["baseline_std"] < with_cold["baseline_std"]

    stats = qs.stats(engine, ["hrv.night_avg"], Window(TODAY - timedelta(days=30), TODAY))
    kept = qs.stats(
        engine,
        ["hrv.night_avg"],
        Window(TODAY - timedelta(days=30), TODAY),
        exclude_tags=["sick"],
    )
    assert stats["hrv.night_avg"]["n"] == 31 and kept["hrv.night_avg"]["n"] == 26


def test_anomalies_skip_sick_history_and_carry_the_diary(engine) -> None:
    from vibesynergy.query import analysis

    days = {TODAY - timedelta(days=i): 50.0 + (i % 3) for i in range(1, 40)}
    days[TODAY] = 30.0
    _hrv(engine, days)
    journal.add_note(engine, text="Cold, fever", start=TODAY, tags=["sick"])
    journal.set_checkin(engine, TODAY, sleep_quality=1)
    found = analysis.anomalies(engine, ["hrv.night_avg"], Window(TODAY, TODAY))
    assert len(found) == 1 and found[0]["direction"] == "low"
    diary = found[0]["diary"]
    assert diary["notes"] == [{"text": "Cold, fever", "tags": ["sick"]}]
    assert diary["checkin"]["sleep_quality"] == 1
    # A day with nothing in the diary has no context.
    assert journal.context(engine, TODAY - timedelta(days=1)) is None


def test_day_journal_shows_own_words_and_links_a_source_activity_id(runtime) -> None:
    """coach/agent logs stay out of the diary; a note on 'strava:…' finds its workout."""
    from datetime import UTC, datetime

    from vibesynergy.derive import unify
    from vibesynergy.model import Activity
    from vibesynergy.normalize.normaliser import write_records

    day = date(2026, 11, 3)
    stored = runtime.raw.put(
        source="strava",
        dataset="activities",
        item_key="t",
        payload=b"{}",
        content_type="application/json",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [Activity("555", datetime(2026, 11, 3, 7, tzinfo=UTC), day, "cycling", moving_s=60.0)],
            source="strava",
            raw_item_id=stored.id,
        )
    unify.rebuild(runtime.engine)
    journal.add_note(runtime.engine, text="Zwaar maar goed", start=day, source="portal")
    journal.add_note(
        runtime.engine, text="Coachplatform coach note", start=day, source="client:abc"
    )
    journal.add_note(runtime.engine, text="[Coachplatform] horizon", start=day, source="claude")
    journal.add_note(
        runtime.engine,
        text="Rit",
        start=day,
        source="strava",
        source_ref="555",
        activity_id="strava:555",
    )
    notes = journal.day_journal(runtime.engine, day)["notes"]
    assert [n["text"] for n in notes] == ["Zwaar maar goed", "Rit"]
    assert notes[1]["activity"] is not None


@pytest.mark.parametrize(
    "description",
    [
        "🗺️ Col du Galibier, Lautaret in Video",
        "Zwift video, waar de framerate aangepast wordt aan je snelheid",
        "[/*]\n-Stats:\n  Aerobe trainingseffect: 0.8\n[Generated with VibeSynergy */]",
    ],
)
def test_app_and_vibesynergy_texts_are_not_personal(description: str) -> None:
    """Zwift's route text and VibeSynergy's own block are no diary note."""
    assert journal.personal_text(description) is None


def test_own_text_before_vibesynergy_block_stays() -> None:
    text = "Benen zwaar na gisteren\n\n[/*]\n-Stats:\n  x\n[Generated with VibeSynergy */]"
    assert journal.personal_text(text) == "Benen zwaar na gisteren"
