"""Zwift and multisport in the two-way text sync (#114, phase 7b). Every platform is fake.

Zwift: Zwift's upload and the watch are two Garmin copies of one ride with one Strava copy;
the texts go to the one Strava and the one Garmin copy publication writes to, the Zwift title
gives way to the AI coach's name, and nothing echoes. Multisport: Garmin gets the text once,
on the multisport activity; every Strava part gets the public text under a part line and the
name with the part, and a name changed on one part locks only that part.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_evidence import START, put
from test_publication import garmin_activity
from test_text_sync import Fake

from vibesynergy.core import config_edit
from vibesynergy.query import (
    auto_names,
    multisport,
    publication,
    publication_format,
    publication_plugins,
    source_removed,
    text_sync,
    workout_texts,
)
from vibesynergy.store.schema import activity_unified, publication_ledger
from vibesynergy.store.schema import text_sync as text_sync_table


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


class Router:
    """One platform with several copies: each call goes to the copy it names."""

    def __init__(self, *copies: Fake):
        self.copies = {c.source_id: c for c in copies}

    def read(self, source_id):
        return self.copies[source_id].read(source_id)

    def write(self, source_id, description):
        self.copies[source_id].write(source_id, description)

    def write_title(self, source_id, title):
        self.copies[source_id].write_title(source_id, title)


def _providers(monkeypatch, strava: Router, garmin: Router) -> None:
    monkeypatch.setattr(
        publication, "provider", lambda _rt, dest: strava if dest == "strava" else garmin
    )


def _settings(rt, **more):
    config_edit.apply_changes(
        rt.settings.config_path,
        {"publication": {"text_sync_enabled": True, "language": "nl", **more}},
    )


def _later(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _sent(*fakes: Fake) -> list[tuple[int, int]]:
    return [(len(f.writes), len(f.titles)) for f in fakes]


# --- Zwift ---------------------------------------------------------------------------------


@pytest.fixture
def zwift(runtime, monkeypatch):
    """As on 21-11-2026: Zwift's own upload to Garmin (1), the watch's indoor ride (3, 37 s
    later) and Zwift's upload to Strava (2), all with the Zwift title."""
    ride = replace(
        garmin_activity(sport="virtual_ride", name="Zwift - Lange rit", fit_id=None),
        elapsed_s=4989, moving_s=4672, training_load=66.0,
    )  # fmt: skip
    watch = replace(
        ride, source_id="3", sport="indoor_cycling", name="Indoorcycling",
        start_utc=START + timedelta(seconds=37), training_load=59.0,
    )  # fmt: skip
    put(runtime, "garmin", ride, watch)
    put(runtime, "strava", replace(ride, source_id="2", sport="VirtualRide"))
    fakes = (
        Fake("strava", "2", "Zwift - Lange rit", ""),
        Fake("garmin", "1", "Zwift - Lange rit", ""),
        Fake("garmin", "3", "Indoorcycling", ""),
    )
    _providers(monkeypatch, Router(fakes[0]), Router(fakes[1], fakes[2]))
    _settings(runtime, auto_name_since=(START - timedelta(days=1)).isoformat())
    return fakes


def _publish_zwift(rt):
    item = publication.preview(rt, "strava:2", "strava")
    return publication.publish(rt, "strava:2", "strava", item["preview_version"])


def test_zwift_copies_are_one_workout_synced_with_one_strava_and_one_garmin_copy(runtime, zwift):
    _strava, _upload, watch = zwift
    with runtime.engine.connect() as conn:
        sources = conn.scalar(
            select(activity_unified.c.sources).where(activity_unified.c.group_id == "garmin:1")
        )
    assert sorted(sources) == ["garmin:1", "garmin:3", "strava:2"]
    assert text_sync.targets(runtime, "garmin:1") == {"strava": "2", "garmin": "1"}
    result = _publish_zwift(runtime)
    assert result["texts"]["state"] == "checked"
    # The watch copy is never read nor written: no second Garmin write, nothing to echo.
    assert watch.reads == 0 and watch.writes == watch.titles == []
    # The Zwift title is the name it came in with: no change of yours, so not locked.
    name = workout_texts.texts(runtime.engine, "garmin:1")["name"]
    assert name["text"] == "Zwift - Lange rit" and name["lock"] is None


def test_the_ai_name_replaces_the_zwift_title_once_everywhere(runtime, zwift):
    strava, upload, watch = zwift
    _publish_zwift(runtime)
    before = _sent(strava, upload)
    assert auto_names.from_insight(runtime, "garmin:1", "Rustige duurrit op Zwift") == "named"
    text_sync.run(runtime, now=_later(6))
    assert strava.name == upload.name == "Rustige duurrit op Zwift"
    assert [t - b for (_, t), (_, b) in zip(_sent(strava, upload), before, strict=True)] == [1, 1]
    assert watch.writes == watch.titles == []
    # The next checks find VibeSynergy's own write: no echo, nothing written again.
    sent = _sent(strava, upload)
    text_sync.run(runtime, now=_later(3 * 60))
    text_sync.run(runtime, now=_later(9 * 60))
    assert _sent(strava, upload) == sent
    assert workout_texts.texts(runtime.engine, "garmin:1")["name"]["lock"] is None


def test_a_new_garmin_copy_starts_fresh_and_locks_nothing(runtime, zwift):
    """Zwift's Garmin upload is removed there (as on 21-11): the watch copy takes over. Its own
    name is no outside change: the agreed name goes to it, the name stays unlocked."""
    strava, upload, watch = zwift
    _publish_zwift(runtime)
    auto_names.from_insight(runtime, "garmin:1", "Rustige duurrit op Zwift")
    text_sync.run(runtime, now=_later(6))
    source_removed.mark(runtime.engine, "garmin", "1", reason="sync")
    assert text_sync.targets(runtime, "garmin:1") == {"strava": "2", "garmin": "3"}
    watch.text = publication_format.wrap_owned("-Garmin stats: x", {})
    _verified(runtime, "garmin", "3", watch.text)
    text_sync.run(runtime, now=_later(3 * 60))
    assert watch.name == "Rustige duurrit op Zwift" and strava.name == watch.name
    texts = workout_texts.texts(runtime.engine, "garmin:1")
    assert texts["name"]["text"] == "Rustige duurrit op Zwift" and texts["name"]["lock"] is None
    assert upload.titles == ["Rustige duurrit op Zwift"]  # the removed copy is left alone


# --- multisport ----------------------------------------------------------------------------


def _verified(rt, source: str, source_id: str, block: str) -> None:
    now = datetime.now(UTC)
    with rt.engine.begin() as conn:
        conn.execute(
            publication_ledger.insert().values(
                source=source, source_id=source_id, assessment_id="a", preview_version="v",
                owned_block=block, base_description="", description=block, state="verified",
                updated_at=now,
            )
        )  # fmt: skip


@pytest.fixture
def brick(runtime, monkeypatch):
    """A Garmin multisport (10) with its parts on Strava as activities of their own: swim (21),
    ride (22) and run (23)."""
    holder = replace(
        garmin_activity(sport="multi_sport", name="Example sim", fit_id=None),
        source_id="10", elapsed_s=3 * 3600, moving_s=3 * 3600,
    )  # fmt: skip
    put(runtime, "garmin", holder)
    legs = [("21", "Swim", 0, "Ochtendzwemmen"), ("22", "Ride", 40, "Ochtendrit")]
    legs.append(("23", "Run", 150, "Ochtendloop"))
    put(
        runtime,
        "strava",
        *[
            replace(
                garmin_activity(sport=sport, name=name, fit_id=None),
                source_id=sid,
                start_utc=START + timedelta(minutes=minute),
                elapsed_s=1800,
                moving_s=1800,
                distance_m=1000.0 + int(sid),
            )
            for sid, sport, minute, name in legs
        ],
    )
    block = publication_format.wrap_owned("-Garmin stats: x", {})
    garmin = Fake("garmin", "10", "Example sim", block)
    parts = [Fake("strava", sid, name, "") for sid, _, _, name in legs]
    _providers(monkeypatch, Router(*parts), Router(garmin))
    _settings(runtime)
    _verified(runtime, "garmin", "10", block)
    workout_texts.set_texts(runtime.engine, "garmin:10", public="Mooie generale.")
    return garmin, parts, block


def _group(rt, ref: str) -> str:
    return workout_texts.texts(rt.engine, ref)["activity_id"]


def test_the_parts_are_found_and_synced_with_strava_only(runtime, brick):
    swim = _group(runtime, "strava:21")
    part = multisport.part_of(runtime.engine, swim)
    assert part == {"holder": "garmin:10", "index": 1, "count": 3, "sport": "Swim"}
    assert text_sync.targets(runtime, "garmin:10") == {"garmin": "10"}
    assert text_sync.targets(runtime, swim) == {"strava": "21"}


def test_every_strava_part_gets_the_text_and_the_name_with_its_part(runtime, brick):
    garmin, (swim, ride, run), block = brick
    summary = text_sync.run(runtime, now=_later(6))
    assert summary["checked"] == 4 and summary["review"] == summary["failed"] == 0
    assert swim.name == "Example sim \u2013 zwemmen" and swim.text == (
        "Deel 1/3: zwemmen\n\nMooie generale."
    )
    assert ride.name == "Example sim \u2013 fietsen"
    assert ride.text == "Deel 2/3: fietsen\n\nMooie generale."
    assert run.name == "Example sim \u2013 lopen"
    assert run.text == "Deel 3/3: lopen\n\nMooie generale."
    # Garmin gets the text once, on the multisport activity, above VibeSynergy's block.
    assert garmin.text == f"Mooie generale.\n\n{block}" and garmin.titles == []
    # Checked again: everything reads back as written, nothing goes out twice.
    sent = _sent(garmin, swim, ride, run)
    text_sync.run(runtime, now=_later(3 * 60))
    text_sync.run(runtime, now=_later(9 * 60))
    assert _sent(garmin, swim, ride, run) == sent


def test_the_part_words_follow_the_publication_language(runtime, brick):
    _garmin, (swim, ride, _run), _ = brick
    _settings(runtime, language="en")
    text_sync.run(runtime, now=_later(6))
    assert swim.name == "Example sim \u2013 swimming"
    assert ride.text == "Part 2/3: cycling\n\nMooie generale."


def test_a_name_changed_on_one_part_locks_that_part_only(runtime, brick):
    garmin, (swim, ride, run), _ = brick
    text_sync.run(runtime, now=_later(6))
    run.name = "Laatste loopje"  # typed on Strava
    text_sync.run(runtime, now=_later(3 * 60))
    run_group = _group(runtime, "strava:23")
    assert workout_texts.texts(runtime.engine, run_group)["name"]["lock"]["source"] == "strava"
    assert workout_texts.texts(runtime.engine, "garmin:10")["name"]["lock"] is None
    # The workout is renamed: the other parts and Garmin follow, the locked part keeps its name.
    workout_texts.set_texts(runtime.engine, "garmin:10", name="Example")
    text_sync.run(runtime, now=_later(4 * 60))
    assert garmin.name == "Example"
    assert swim.name == "Example \u2013 zwemmen" and ride.name == "Example \u2013 fietsen"
    assert run.name == "Laatste loopje"
    assert workout_texts.texts(runtime.engine, _group(runtime, "strava:21"))["name"]["lock"] is None


def test_a_text_changed_on_a_part_reaches_garmin_and_the_other_parts_once(runtime, brick):
    garmin, (swim, ride, run), block = brick
    text_sync.run(runtime, now=_later(6))
    ride.text = "Deel 2/3: fietsen\n\nWind tegen op de dijk."  # typed on Strava
    text_sync.run(runtime, now=_later(3 * 60))
    holder = workout_texts.texts(runtime.engine, "garmin:10")["public"]
    assert holder["text"] == "Wind tegen op de dijk." and holder["source"] == "strava"
    text_sync.run(runtime, now=_later(4 * 60))  # after the wait: the others follow
    assert garmin.text == f"Wind tegen op de dijk.\n\n{block}"
    assert swim.text == "Deel 1/3: zwemmen\n\nWind tegen op de dijk."
    assert run.text == "Deel 3/3: lopen\n\nWind tegen op de dijk."
    assert ride.writes[-1] == "Deel 2/3: fietsen\n\nMooie generale."  # not written back
    # No echo: the next checks write nothing.
    sent = _sent(garmin, swim, ride, run)
    text_sync.run(runtime, now=_later(8 * 60))
    text_sync.run(runtime, now=_later(14 * 60))
    assert _sent(garmin, swim, ride, run) == sent
    with runtime.engine.connect() as conn:
        states = set(conn.scalars(select(text_sync_table.c.state)))
    assert states == {"active"}


def test_a_parts_own_text_is_kept_on_the_first_sync(runtime, brick):
    _garmin, (swim, _ride, _run), _ = brick
    swim.text = "Koud water, 16 graden."
    text_sync.run(runtime, now=_later(6))
    holder = workout_texts.texts(runtime.engine, "garmin:10")["public"]["text"]
    assert holder == "Mooie generale.\n\nKoud water, 16 graden."
    assert swim.text == f"Deel 1/3: zwemmen\n\n{holder}"


def test_the_part_line_comes_off_in_either_language():
    assert multisport.without_part_line("Deel 2/3: fietsen\n\nWind.") == "Wind."
    assert multisport.without_part_line("Part 1/4: swimming\n\nCold.") == "Cold."
    assert multisport.without_part_line("Deel 3/3: lopen") == ""
    assert multisport.without_part_line("Gewoon tekst.\n\nMeer.") == "Gewoon tekst.\n\nMeer."
    assert multisport.part_kind("transition_v2") == multisport.part_kind("Workout")
    assert multisport.part_kind("open_water_swimming") == "swim"
    assert multisport.part_kind("VirtualRide") == "bike"
