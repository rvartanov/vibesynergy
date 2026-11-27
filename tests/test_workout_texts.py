"""A workout's own texts in VibeSynergy with their history (#114, phase 1). Synthetic data."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, inspect, select
from test_evidence import START, activity, put

from vibesynergy.query import journal, views, workout_texts
from vibesynergy.store import db
from vibesynergy.store.schema import workout_text

BLOCK = (
    "[/*]\n-Persoonlijke AI inzicht: oude regel.\n-Stats:\n  📊 Training load: 177\n"
    "[Generated with VibeSynergy */]"
)
MINE = "Prachtig weer en lekker gelopen."
SECRET = "Linkerkuit iets stijf, niet aan de groep vertellen."


def _workout(rt, *, strava_text=f"{MINE}\n\n{BLOCK}", garmin_text=BLOCK, notes=()):
    garmin = replace(activity(), name="Ochtendloop", extra={"description": garmin_text})
    put(rt, "garmin", garmin)
    strava = replace(
        activity(), source_id="2", name="Eigen titel", fit_id=None,
        extra={"description": strava_text},
    )  # fmt: skip
    put(rt, "strava", strava)
    for text, source in notes:
        journal.add_note(rt.engine, text=text, start=START.date(), activity_id="garmin:1",
                         source=source, source_ref=f"ref-{len(text)}")  # fmt: skip
    return "garmin:1"


def _rows(rt):
    with rt.engine.connect() as conn:
        return conn.execute(select(workout_text).order_by(workout_text.c.id)).mappings().all()


# --- migration ------------------------------------------------------------------------------


def test_migration_0062_adds_the_table_and_goes_back(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    db.upgrade(engine, "0062")
    columns = {c["name"] for c in inspect(engine).get_columns("workout_text")}
    assert {"group_id", "field", "text", "old_text", "source", "kind", "restored_from"} <= columns
    from alembic import command

    cfg = db._alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0061")
    assert "workout_text" not in inspect(engine).get_table_names()


# --- derived texts, dedupe --------------------------------------------------------------------


def test_texts_follow_from_the_description_and_notes_without_the_block_or_doubles(runtime):
    # The diary imported the Strava description as a note: it is given once, as public.
    group = _workout(runtime, notes=[(MINE, "strava"), (SECRET, "portal")])
    found = workout_texts.texts(runtime.engine, group)
    assert found["public"]["text"] == MINE and found["public"]["source"] == "strava"
    assert found["private"]["text"] == SECRET and found["private"]["source"] == "vs"
    assert found["name"]["text"] and found["name"]["stored"] is False
    assert all(found[f]["version"] is None for f in workout_texts.FIELDS)
    assert "[/*]" not in json.dumps(found)
    assert _rows(runtime) == []  # reading stores nothing


def test_an_imported_note_with_the_description_and_a_private_note_is_split(runtime):
    group = _workout(runtime, notes=[(f"{MINE}\n\nPrivate note: {SECRET}", "strava")])
    found = workout_texts.texts(runtime.engine, group)
    assert found["public"]["text"] == MINE
    assert found["private"]["text"] == f"Private note: {SECRET}"


def test_garmin_text_that_repeats_strava_is_given_once(runtime):
    group = _workout(runtime, garmin_text=f"{MINE}\n\n{BLOCK}")
    assert workout_texts.texts(runtime.engine, group)["public"]["text"] == MINE


# --- versions and restore ------------------------------------------------------------------------


def test_the_first_write_keeps_what_was_there_and_every_change_is_a_version(runtime):
    group = _workout(runtime, notes=[(SECRET, "portal")])
    out = workout_texts.set_texts(runtime.engine, group, public="Nieuwe tekst", author="client:x")
    assert out["changed"] == ["public"]
    rows = _rows(runtime)
    seeds = [r for r in rows if r["kind"] == "seed"]
    assert {r["field"] for r in seeds} == {"name", "public", "private"}
    edit = rows[-1]
    assert (edit["field"], edit["text"], edit["old_text"]) == ("public", "Nieuwe tekst", MINE)
    assert (edit["source"], edit["kind"], edit["author"]) == ("vs", "edit", "client:x")
    # The same text again: no new version. Another change: one more, old ones untouched.
    assert workout_texts.set_texts(runtime.engine, group, public="Nieuwe tekst")["changed"] == []
    workout_texts.set_texts(runtime.engine, group, public="Derde", private="")
    after = _rows(runtime)
    assert [dict(r) for r in after[: len(rows)]] == [dict(r) for r in rows]
    current = workout_texts.texts(runtime.engine, group)
    assert current["public"]["text"] == "Derde" and current["private"]["text"] == ""
    assert all(current[f]["stored"] for f in workout_texts.FIELDS)


def test_restore_adds_a_version_that_copies_the_old_one(runtime):
    group = _workout(runtime)
    workout_texts.set_texts(runtime.engine, group, public="Tweede")
    workout_texts.set_texts(runtime.engine, group, public="Derde")
    first = next(
        v for v in workout_texts.history(runtime.engine, group, "public") if v["kind"] == "seed"
    )
    before = len(_rows(runtime))
    out = workout_texts.restore(runtime.engine, first["version"], activity_id="strava:2")
    assert out["changed"] == ["public"] and out["texts"]["public"]["text"] == MINE
    rows = _rows(runtime)
    assert len(rows) == before + 1
    last = rows[-1]
    assert (last["kind"], last["source"], last["restored_from"]) == (
        "restore",
        "vs",
        first["version"],
    )
    assert last["old_text"] == "Derde"
    history = workout_texts.history(runtime.engine, group, "public")
    assert [v["text"] for v in history] == [MINE, "Derde", "Tweede", MINE]


def test_restore_refuses_a_version_of_another_workout(runtime):
    group = _workout(runtime)
    workout_texts.set_texts(runtime.engine, group, name="Mijn naam")
    other = replace(activity(), source_id="9", start_utc=START.replace(hour=18), fit_id=None)
    put(runtime, "garmin", other)
    version = workout_texts.history(runtime.engine, group, "name")[0]["version"]
    with pytest.raises(ValueError, match="another workout"):
        workout_texts.restore(runtime.engine, version, activity_id="garmin:9")


def test_input_is_checked(runtime):
    group = _workout(runtime)
    with pytest.raises(ValueError, match="give a name"):
        workout_texts.set_texts(runtime.engine, group)
    with pytest.raises(ValueError, match="block markers"):
        workout_texts.set_texts(runtime.engine, group, public=f"mijn tekst\n\n{BLOCK}")
    with pytest.raises(ValueError, match="source"):
        workout_texts.set_texts(runtime.engine, group, name="x", source="intervals_icu")
    with pytest.raises(ValueError, match="field"):
        workout_texts.history(runtime.engine, group, "secret")
    assert (
        workout_texts.set_texts(runtime.engine, group, name=" Twee\nregels ")["texts"]["name"][
            "text"
        ]
        == "Twee regels"
    )


# --- private never public ---------------------------------------------------------------------


def test_the_private_text_never_reaches_anything_public(runtime):
    group = _workout(runtime)
    workout_texts.set_texts(runtime.engine, group, private=SECRET, public="Voor iedereen")
    # Not in the activity view (which share links and vibesynergy_activity show).
    detail = views.activity_detail(runtime.engine, runtime.parquet, group)
    assert SECRET not in json.dumps(detail, default=str)


def test_the_private_text_is_not_in_the_published_description(runtime, monkeypatch):
    from test_publication_targets import FakeStrava

    from vibesynergy.query import publication, publication_plugins

    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)
    strava = FakeStrava()
    monkeypatch.setattr(publication, "provider", lambda _rt, _dest: strava)
    from test_publication import garmin_activity

    a = garmin_activity()
    put(runtime, "garmin", a)
    put(runtime, "strava", replace(a, source_id="2", name="Strava title"))
    group = "garmin:1"
    workout_texts.set_texts(runtime.engine, group, private=SECRET, public="Strava words")
    item = publication.preview(runtime, group, "strava", language="nl")
    assert item["description"].startswith("Strava words") and SECRET not in item["description"]


# --- MCP tools ------------------------------------------------------------------------------------


def _call(rt, tool, **args):
    import anyio
    from mcp import Client

    from vibesynergy.mcp.server import build_server

    async def go():
        async with Client(build_server(rt)) as client:
            return await client.call_tool(tool, args)

    result = anyio.run(go)
    text = result.content[0].text if result.content else ""
    if result.is_error:
        raise ValueError(text)
    return result.structured_content or json.loads(text)


def test_mcp_tools_read_write_and_restore(runtime):
    from vibesynergy.mcp import roles
    from vibesynergy.mcp.server import build_server

    scopes = build_server(runtime).vibesynergy_tool_scopes  # type: ignore[attr-defined]
    assert scopes["vibesynergy_activity_texts"] == "read"
    assert scopes["vibesynergy_activity_set_texts"] == "diary.write"
    assert scopes["vibesynergy_activity_restore_text"] == "diary.write"
    assert roles.WRITE_POLICY["vibesynergy_activity_set_texts"] == roles.OPEN

    group = _workout(runtime)
    read = _call(runtime, "vibesynergy_activity_texts", id="strava:2")["data"]
    assert read["public"]["text"] == MINE and read["history"] == []
    saved = _call(runtime, "vibesynergy_activity_set_texts", id=group, name="Bosloop",
                  private=SECRET)["data"]  # fmt: skip
    assert set(saved["changed"]) == {"name", "private"}
    assert saved["texts"]["name"]["source"] == "vs"
    history = _call(runtime, "vibesynergy_activity_texts", id=group, field="name")["data"]
    seed = next(v for v in history["history"] if v["kind"] == "seed")
    back = _call(runtime, "vibesynergy_activity_restore_text", id=group,
                 version=seed["version"])["data"]  # fmt: skip
    assert back["texts"]["name"]["text"] == seed["text"]
    with pytest.raises(ValueError, match="no text version"):
        _call(runtime, "vibesynergy_activity_restore_text", id=group, version=999)


# --- the AI coach's context ---------------------------------------------------------------------


def test_the_coach_context_reads_the_vs_texts_when_there_are_any(runtime):
    from vibesynergy.coach_runner import context

    group = _workout(runtime, notes=[(MINE, "strava")])
    before = context.activity_view(runtime, group)["own_words"]
    assert "sources" in before and "name" not in before  # the earlier behaviour
    workout_texts.set_texts(runtime.engine, group, name="Bosloop", private=SECRET)
    journal.add_note(runtime.engine, text="Later nog een notitie.", start=START.date(),
                     activity_id=group)  # fmt: skip
    own = context.activity_view(runtime, group)["own_words"]
    assert own["name"] == "Bosloop" and own["public"] == MINE and own["private"] == SECRET
    assert own["notes"] == ["Later nog een notitie."]  # MINE is not given twice
    assert "private_is" in own and "sources" not in own
