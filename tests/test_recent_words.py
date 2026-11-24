"""#170: the athlete's own words of the last days in every advice run (``recent_words``)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from vibesynergy.coach_runner import context, recent_words, registry
from vibesynergy.derive import unify
from vibesynergy.model import Activity
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.query import journal, workout_texts

DAY = date(2026, 9, 21)  # "today" of the runs here


def _workout(rt, n: int, when: date, *, name: str, description: str | None = None) -> str:
    start = datetime(when.year, when.month, when.day, 10, tzinfo=UTC)
    record = Activity(
        str(n),
        start,
        when,
        "swimming",
        name=name,
        elapsed_s=1800,
        moving_s=1800,
        extra={"description": description} if description else {},
    )
    raw = rt.raw.put(
        source="strava", dataset="test", item_key=f"w{n}", payload=b"x", content_type="text/plain"
    )
    with rt.engine.begin() as conn:
        write_records(conn, [record], source="strava", raw_item_id=raw.id)
    unify.rebuild(rt.engine)
    return f"strava:{n}"


def _block(found: dict, name: str = "recent_words") -> str:
    text = found["text"]
    start = text.index(f'<context name="{name}"')
    return text[start : text.index("</context>", start)]


def _data(found: dict) -> dict:
    block = _block(found)
    return json.loads(block[block.index("{") :])


@pytest.fixture(autouse=True)
def _no_weather(monkeypatch):
    monkeypatch.setitem(
        context.BLOCKS, "weather", context.Block("weather", "Weer", 100, lambda *a: {"days": []})
    )


def test_every_advice_task_and_the_insight_get_the_block():
    for task in ("morning", "evening", "weekly", "injury", "checkin", "insight", "reply"):
        assert "recent_words" in registry.chain_for(task), task
    assert context.VERSION == 7


def test_a_note_on_yesterdays_workout_is_in_todays_morning_advice(runtime):
    group = _workout(runtime, 1, DAY - timedelta(days=1), name="Zwemmen")
    journal.add_note(
        runtime.engine,
        text="Schouder voelde stijf.",
        start=DAY - timedelta(days=1),
        activity_id=group,
        source="portal",
    )
    found = context.preload(runtime, "morning", DAY)
    data = _data(found)
    yesterday = next(d for d in data["days"] if d["date"] == (DAY - timedelta(1)).isoformat())
    [workout] = yesterday["workouts"]
    assert workout["name"] == "Zwemmen"
    # The linked note is the private text while VibeSynergy holds no version of its own.
    assert "Schouder voelde stijf." in json.dumps(workout, ensure_ascii=False)
    assert "samengevat" not in yesterday
    report = next(b for b in found["blocks"] if b["name"] == "recent_words")
    assert report["ok"] and report["chars"] <= recent_words.BUDGET


def test_the_window_is_nine_days_and_a_setting(runtime, monkeypatch):
    for back in (0, 8, 9):
        journal.set_checkin(runtime.engine, DAY - timedelta(days=back), note=f"dag -{back}")
    data = _data(context.preload(runtime, "morning", DAY))
    assert len(data["days"]) == 9
    assert data["days"][0]["date"] == DAY.isoformat()
    assert data["days"][-1]["date"] == (DAY - timedelta(days=8)).isoformat()
    notes = [d.get("checkin_note") for d in data["days"]]
    # Today's note is only here: the safety and today blocks point to it.
    assert "dag -0" in notes and "dag -8" in notes and "dag -9" not in notes
    found = context.preload(runtime, "morning", DAY)
    assert context.NOTE_IN_RECENT_WORDS in _block(found, "safety")
    assert '"dag -0"' not in _block(found, "today") and "dag -0" not in _block(found, "safety")

    def three(_rt):
        return 3

    monkeypatch.setattr(recent_words, "days_setting", three)
    assert len(_data(context.preload(runtime, "morning", DAY))["days"]) == 3


def test_the_setting_reads_the_config_and_stays_in_bounds():
    def rt(value):
        return SimpleNamespace(settings=SimpleNamespace(coach_runner={"recent_words_days": value}))

    assert recent_words.days_setting(SimpleNamespace(settings=SimpleNamespace())) == 9
    assert recent_words.days_setting(rt(5)) == 5
    assert recent_words.days_setting(rt(0)) == 1
    assert recent_words.days_setting(rt(400)) == recent_words.MAX_DAYS
    assert recent_words.days_setting(rt("7")) == 9


def test_the_private_text_goes_to_the_coach_not_to_the_insight(runtime):
    group = _workout(runtime, 1, DAY, name="Baantjes", description="Rustig gezwommen.")
    other = _workout(runtime, 2, DAY - timedelta(days=2), name="Open water", description="Koud!")
    workout_texts.set_texts(runtime.engine, other, private="Alleen voor mij: brilletje lekt.")
    journal.set_checkin(runtime.engine, DAY, note="Slecht geslapen.")
    journal.log_injury(
        runtime.engine,
        today=DAY,
        body_part="shoulder",
        side="left",
        pain=3,
        timing="after",
        note_text="Na het zwemmen.",
    )
    morning = _data(context.preload(runtime, "morning", DAY))
    text = json.dumps(morning, ensure_ascii=False)
    assert "Alleen voor mij: brilletje lekt." in text and morning["private_is"]
    assert "Slecht geslapen." in text and "Na het zwemmen." in text
    # The insight writes a public line: names and public texts only, and its own workout
    # is in block activity (given once).
    found = context.preload(runtime, "insight", DAY, activity_ids=[group])
    insight = _data(found)
    text = json.dumps(insight, ensure_ascii=False)
    assert "Koud!" in text and "Open water" in text
    for private in ("brilletje", "Slecht geslapen", "Na het zwemmen"):
        assert private not in text, private
    today = insight["days"][0]["workouts"]
    assert today == [{"id": group, "sport": "swimming", "name": "Baantjes", "see": "activity"}]
    assert "Rustig gezwommen." in _block(found, "activity")


def test_the_same_text_is_given_once(runtime):
    group = _workout(runtime, 1, DAY, name="Zwemmen", description="Lekker gezwommen.")
    # The Strava description the diary imported as a note, and a note of the athlete's own.
    journal.add_note(
        runtime.engine, text="Lekker gezwommen.", start=DAY, activity_id=group, source="strava"
    )
    journal.add_note(runtime.engine, text="Een AI-notitie.", start=DAY, source="ai")
    workout_texts.set_texts(runtime.engine, group, private="Eigen privétekst.")
    journal.add_note(
        runtime.engine, text="Eigen privétekst.\n\nNieuw.", start=DAY, activity_id=group
    )
    text = _block(context.preload(runtime, "morning", DAY))
    assert text.count("Lekker gezwommen.") == 1
    assert text.count("Eigen privétekst.") == 1 and "Nieuw." in text
    assert "Een AI-notitie." not in text


# --- the compression -----------------------------------------------------------------------


def _day(i: int, words: int) -> dict:
    day = (DAY - timedelta(days=i)).isoformat()
    return {
        "date": day,
        "checkin_note": f"notitie {i} " + "woord " * words,
        "complaints": [{"title": "Linkerschouder", "pain": 4, "timing": "after", "note": "Zeurt."}],
        "notes": [{"text": f"dagboek {i} " + "tekst " * words}],
        "workouts": [
            {
                "id": f"strava:{i}",
                "sport": "swimming",
                "name": f"Workout {i}",
                "public": "open " * words,
                "private": "privé " * words,
                "notes": ["noot " * words],
            }
        ],
    }


def test_too_much_text_is_compressed_without_losing_a_day():
    days = [_day(i, 80) for i in range(9)]
    block = recent_words.fit({"from": "x"}, days, recent_words.BUDGET)
    assert len(json.dumps(block, ensure_ascii=False)) <= recent_words.BUDGET
    assert [d["date"] for d in block["days"]] == [d["date"] for d in days]  # no day missing
    # Today and yesterday verbatim, the older days compressed and marked so.
    assert block["days"][:2] == days[:2]
    assert all(d.get("samengevat") is True for d in block["days"][2:])
    # A complaint stays verbatim, and each workout keeps its name.
    for got, given in zip(block["days"], days, strict=True):
        assert got["complaints"] == given["complaints"]
        assert got["workouts"][0]["name"] == given["workouts"][0]["name"]


def test_the_oldest_day_is_compressed_first_and_only_as_far_as_needed():
    days = [_day(i, 60) for i in range(9)]
    size = len(json.dumps({"days": days}, ensure_ascii=False))
    assert recent_words.fit({}, days, size + 10)["days"] == days  # it fits: nothing changes
    block = recent_words.fit({}, days, size - 100)
    assert block["days"][-1].get("samengevat") and not block["days"][-2].get("samengevat")


def test_recent_days_stay_verbatim_even_over_budget():
    days = [_day(i, 2000) for i in range(9)]
    block = recent_words.fit({}, days, recent_words.BUDGET)
    assert block["days"][:2] == days[:2]
    assert all(len(d) > 1 for d in block["days"])
    assert all("left_out" in d for d in block["days"][2:])


def test_shorten_cuts_at_a_word():
    assert recent_words.shorten("een  twee\ndrie", 50) == "een twee drie"
    assert recent_words.shorten("een twee drie vier", 12) == "een twee …"
    assert recent_words.shorten("een twee", 0) is None
    assert recent_words.shorten(None, 10) is None
