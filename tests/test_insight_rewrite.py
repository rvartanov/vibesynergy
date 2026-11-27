"""The AI-coach insight follows the workout's texts in VibeSynergy (#114, phase 4).

Publish tick → first insight once; a text changed after the insight → one debounced
rewrite, within the daily limit; the athlete's correction → stored privately, in the
insight's context, rewritten at once. Synthetic data; the model, Strava and Garmin are
fakes, nothing leaves the test."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from test_coach_runner import FakeModel, done, tool_use
from test_evidence import START
from test_workout_page import WRITE, _client, _fake, _workout

from vibesynergy.coach_runner import (
    context,
    contract,
    insight_rewrite,
    prompts,
    registry,
    runner,
    schedule,
    tools,
)
from vibesynergy.coach_runner import settings as runner_settings
from vibesynergy.query import (
    coach_workouts,
    publication,
    publication_auto,
    publication_insight,
    publication_plugins,
    workout_texts,
)
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import coach_run, job

KEY = "sk-ant-api03-SYNTHETIC-insight-rewrite-key"
SECRET = "Linkerkuit iets stijf, niet aan de groep vertellen."
FIX = "Ik was niet met de groep, ik liep alleen vandaag."
FIRST = "Duurloop met de groep, netjes in Z2."
DAY = START.date().isoformat()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: None)


def enable(rt):
    runner_settings.set_api_key(rt, KEY)
    runner_settings.save(rt, {"enabled": True})


def insight_jobs(rt):
    with rt.engine.connect() as conn:
        rows = conn.execute(select(job).where(job.c.kind == schedule.JOB)).mappings().all()
    return [r for r in rows if r["payload"]["job"] == "insight"]


def write_insight(rt, group, text=FIRST, key="insight-garmin:1"):
    return coach_workouts.coach_write(
        rt.engine,
        summary="Prive samenvatting.",
        entry_type="activity_assessment",
        idempotency_key=key,
        for_date=START.date(),
        activity_id=group,
        public_insight=text,
    )


def at(monkeypatch, when):
    """The clock of the text versions."""
    monkeypatch.setattr(workout_texts, "utc_now", lambda: when)


def insight_model(monkeypatch, *steps):
    fake = FakeModel(*steps)
    monkeypatch.setattr(runner, "AnthropicClient", lambda key: fake)
    return fake


def entry(text, key):
    return {
        "entry_type": "activity_assessment",
        "summary": "Samenvatting.",
        "idempotency_key": key,
        "for_date": DAY,
        "activity_id": "garmin:1",
        "public_insight": text,
    }


# --- the Publish tick --------------------------------------------------------------------------


def test_the_publish_tick_queues_the_first_insight_once(runtime, monkeypatch):
    strava = _fake(runtime, monkeypatch)
    group = _workout(runtime)
    enable(runtime)
    client = _client(runtime)
    workout_texts.set_texts(runtime.engine, group, public="Lekker gelopen in het bos.")
    tick = client.post("/api/activity-publish", json={"id": group, "publish": True}, headers=WRITE)
    assert tick.json()["data"]["publish"]["state"] == "published"
    (queued,) = insight_jobs(runtime)
    payload = queued["payload"]
    assert payload["run_key"] == f"publish-{group}" and payload["trigger"] == "publish"
    assert payload["payload"]["activity_ids"] == [group]
    assert payload["payload"]["event_type"] == "workout.published"
    assert "redo" not in payload["payload"]
    page = client.get("/api/activity-texts", params={"id": group}).json()["data"]
    assert page["public"]["rewrite"]["state"] == "writing"
    # Ticking again, or Strava's own "made public" event, queues no second run.
    client.post("/api/activity-publish", json={"id": group, "publish": True}, headers=WRITE)
    event = {
        "type": "activity.published",
        "id": "evt-1",
        "data": {"activities": [{"id": group, "local_date": DAY, "public_insight_missing": True}]},
    }
    with runtime.engine.begin() as conn:
        assert schedule.on_agent_event(runtime, conn, event) is None
    assert len(insight_jobs(runtime)) == 1
    # The run writes the insight; the published section gets the line by itself, although
    # new workouts are "only me" (the automatic publishing is off).
    insight_model(
        monkeypatch, tool_use("vibesynergy_coach_write", entry(FIRST, "insight-garmin:1")), done()
    )
    assert runtime.worker(lane="coach").run_once() is True
    assert runner.get_run(runtime, "insight", f"publish-{group}")["status"] == "done"
    assert not publication_auto.enabled(runtime)
    assert publication_auto.due(runtime)
    assert publication_auto.run(runtime)["insight_updated"] == 1
    assert FIRST in strava.text and strava.text.startswith("Strava words\n\n")
    # Published again with an insight in place: nothing new is queued.
    client.post("/api/activity-publish", json={"id": group, "publish": False}, headers=WRITE)
    client.post("/api/activity-publish", json={"id": group, "publish": True}, headers=WRITE)
    assert len(insight_jobs(runtime)) == 1


def test_the_tick_queues_nothing_while_the_coach_is_off(runtime, monkeypatch):
    _fake(runtime, monkeypatch)
    group = _workout(runtime)
    client = _client(runtime)
    client.post("/api/activity-publish", json={"id": group, "publish": True}, headers=WRITE)
    assert insight_jobs(runtime) == []


# --- a text changed after the insight ------------------------------------------------------------


def test_a_text_change_after_the_insight_gives_one_debounced_rewrite(runtime, monkeypatch):
    group = _workout(runtime)
    enable(runtime)
    written = write_insight(runtime, group)
    t0 = datetime.now(UTC) + timedelta(minutes=10)
    at(monkeypatch, t0)
    workout_texts.set_texts(runtime.engine, group, public="Eerste versie van mijn tekst.")
    at(monkeypatch, t0 + timedelta(minutes=3))
    workout_texts.set_texts(runtime.engine, group, public="Tweede versie van mijn tekst.")
    cfg = runner_settings.load(runtime)
    # Still typing: it waits until the texts are quiet for a few minutes.
    assert insight_rewrite.due(runtime, cfg, t0 + timedelta(minutes=6)) == []
    status = insight_rewrite.status(runtime, group, t0 + timedelta(minutes=6))
    assert status["state"] == "waiting" and status["fields"] == ["public"]
    (item,) = insight_rewrite.due(runtime, cfg, t0 + timedelta(minutes=9))
    newest = workout_texts.history(runtime.engine, group, "public")[0]["version"]
    assert item["run_key"] == f"text-{group}-{newest}" and item["trigger"] == "text"
    assert item["payload"]["redo"] is True and item["payload"]["replaces"] == written["id"]
    assert item["payload"]["activity_ids"] == [group]
    # Through the scheduler: queued once.
    later = t0 + timedelta(minutes=9)
    monkeypatch.setattr(schedule, "utc_now", lambda: later)
    queued = schedule.enqueue_due(runtime, later)
    assert len([j for j in insight_jobs(runtime) if j["id"] in queued]) == 1
    assert insight_rewrite.due(runtime, cfg, later) == []  # busy
    schedule.enqueue_due(runtime, later + timedelta(minutes=1))
    assert len(insight_jobs(runtime)) == 1
    assert insight_rewrite.status(runtime, group, later)["state"] == "rewriting"
    # The rewrite runs: a redo with its own key; the same state never runs again. Other
    # tasks the scheduler found due at this hour (the evening review after 21:00) are no
    # part of this test: they would run first.
    mine = {j["id"] for j in insight_jobs(runtime)}
    with runtime.engine.begin() as conn:
        conn.execute(delete(job).where(job.c.kind == schedule.JOB, job.c.id.not_in(mine)))
    key = runner.insight_key(group, item["run_key"], redo=True)
    insight_model(
        monkeypatch,
        tool_use("vibesynergy_coach_write", entry("Rustige duurloop, alleen gelopen.", key)),
        done(),
    )
    assert runtime.worker(lane="coach").run_once() is True
    row = runner.get_run(runtime, "insight", item["run_key"])
    assert row["status"] == "done" and row["trigger"] == "text"
    found = publication_insight.for_activity(runtime.engine, group)
    assert found["text"] == "Rustige duurloop, alleen gelopen."
    assert insight_rewrite.due(runtime, cfg, later + timedelta(minutes=10)) == []
    versions = publication_insight.versions(runtime.engine, group)
    assert [v["text"] for v in versions] == ["Rustige duurloop, alleen gelopen.", FIRST]


def test_seeds_and_texts_from_before_the_insight_start_nothing(runtime, monkeypatch):
    group = _workout(runtime)
    enable(runtime)
    at(monkeypatch, datetime.now(UTC) - timedelta(hours=1))
    workout_texts.set_texts(runtime.engine, group, public="Geschreven voor het inzicht.")
    write_insight(runtime, group)
    cfg = runner_settings.load(runtime)
    assert insight_rewrite.due(runtime, cfg, datetime.now(UTC) + timedelta(hours=1)) == []
    assert insight_rewrite.status(runtime, group)["state"] is None


def test_a_change_from_strava_through_the_sync_rewrites_too(runtime, monkeypatch):
    group = _workout(runtime)
    enable(runtime)
    write_insight(runtime, group)
    t0 = datetime.now(UTC) + timedelta(minutes=1)
    at(monkeypatch, t0)
    workout_texts.take_over(runtime.engine, group, "name", "Bosloop met Jan", source="strava")
    (item,) = insight_rewrite.due(runtime, runner_settings.load(runtime), t0 + timedelta(minutes=6))
    assert item["payload"]["changed"] == ["name"]


def test_the_daily_limit_is_respected(runtime, monkeypatch):
    group = _workout(runtime)
    enable(runtime)
    write_insight(runtime, group)
    now = datetime.now(UTC) + timedelta(minutes=10)
    at(monkeypatch, now - timedelta(minutes=8))
    workout_texts.set_texts(runtime.engine, group, private=SECRET)
    with runtime.engine.begin() as conn:
        for n in range(insight_rewrite.DEFAULT_PER_DAY):
            conn.execute(
                coach_run.insert().values(
                    job="insight", run_key=f"text-{group}-x{n}", for_date=START.date(),
                    trigger="text", status="done", attempts=1, model="m", started_at=now,
                    updated_at=now, turns=1, input_tokens=0, output_tokens=0,
                    cache_write_tokens=0, cache_read_tokens=0, cost_usd=0.0,
                )
            )  # fmt: skip
    cfg = runner_settings.load(runtime)
    assert insight_rewrite.due(runtime, cfg, now) == []
    assert insight_rewrite.status(runtime, group, now)["state"] == "limit"
    assert insight_rewrite.queue_now(runtime, group, now) is None


def test_the_limit_is_a_setting():
    def rt(value):
        return SimpleNamespace(settings=SimpleNamespace(coach_runner=value))

    assert insight_rewrite.per_day(rt({})) == insight_rewrite.DEFAULT_PER_DAY
    assert insight_rewrite.per_day(rt({"insight_rewrites_per_day": 1})) == 1
    assert insight_rewrite.per_day(rt({"insight_rewrites_per_day": 0})) == 0
    assert insight_rewrite.per_day(rt({"insight_rewrites_per_day": "x"})) == 3
    assert insight_rewrite.per_day(rt({"insight_rewrites_per_day": 99})) == 24


def test_zero_switches_the_rewrites_off(runtime, monkeypatch):
    settings = replace(runtime.settings, coach_runner={"insight_rewrites_per_day": 0})
    rt = Runtime.open(settings)
    try:
        group = _workout(rt)
        enable(rt)
        write_insight(rt, group)
        at(monkeypatch, datetime.now(UTC) + timedelta(minutes=1))
        workout_texts.set_texts(rt.engine, group, public="Nieuw.")
        later = datetime.now(UTC) + timedelta(hours=1)
        assert insight_rewrite.due(rt, runner_settings.load(rt), later) == []
        assert insight_rewrite.status(rt, group, later)["state"] is None
    finally:
        rt.close()


# --- the athlete's correction ------------------------------------------------------------------


def test_a_correction_is_private_rewrites_at_once_and_reaches_the_context(runtime, monkeypatch):
    _fake(runtime, monkeypatch)
    group = _workout(runtime)
    enable(runtime)
    client = _client(runtime)
    refused = client.post(
        "/api/activity-insight-correction", json={"id": group, "text": FIX}, headers=WRITE
    )
    assert refused.status_code == 409 and refused.json()["code"] == "no_insight"
    write_insight(runtime, group)
    workout_texts.set_texts(runtime.engine, group, private=SECRET)
    assert (
        client.post("/api/activity-insight-correction", json={"id": group, "text": FIX}).status_code
        == 403
    )  # the portal header, as every write
    reply = client.post(
        "/api/activity-insight-correction", json={"id": group, "text": f"  {FIX}\n"},
        headers=WRITE,
    ).json()["data"]  # fmt: skip
    assert reply["changed"] == ["correction"] and reply["queued"] is True
    assert reply["public"]["rewrite"]["state"] == "rewriting"
    assert [c["text"] for c in reply["private"]["corrections"]] == [FIX]
    (queued,) = insight_jobs(runtime)
    assert queued["payload"]["trigger"] == "correction"
    assert queued["payload"]["payload"]["event_type"] == "insight.correction"
    stored = workout_texts.history(runtime.engine, group, "correction")
    assert stored[0]["text"] == FIX and stored[0]["source"] == "vs"
    # Private: not in the public part, the activity, the publication or a text field.
    assert FIX not in json.dumps(reply["public"]) and FIX not in json.dumps(reply["texts"])
    assert FIX not in json.dumps(reply["history"])
    assert FIX not in client.get("/api/activity", params={"id": group}).text
    item = publication.preview(runtime, group, "strava", language="nl")
    assert FIX not in item["description"] and SECRET not in item["description"]
    # The rewrite's context: the current insight and the correction, marked private.
    found = context.preload(runtime, "insight", START.date(), activity_ids=[group])
    text = found["text"]
    block = text[text.index('<context name="insight_rewrite"') :]
    block = block[: block.index("</context>")]
    assert FIRST in block and FIX in block and "privé" in block
    assert '"texts_changed_after_it": ["private"]' in block
    assert '"new": true' in block and "never quote it" in block
    activity = text[text.index('<context name="activity"') :]
    activity = activity[: activity.index("</context>")]
    assert SECRET in activity and "context only" in activity
    # The run: a line that quotes the correction or the private text is refused.
    run_key = queued["payload"]["run_key"]
    key = runner.insight_key(group, run_key, redo=True)
    fake = insight_model(
        monkeypatch,
        tool_use("vibesynergy_coach_write", entry(f"Zei hij: {FIX}", key), n=1),
        tool_use("vibesynergy_coach_write", entry(f"Over de kuit: {SECRET}", key), n=2),
        tool_use("vibesynergy_coach_write", entry("Rustige solo duurloop in Z2.", key), n=3),
        done(),
    )
    assert runtime.worker(lane="coach").run_once() is True
    for i in (1, 2):
        result = fake.requests[i]["messages"][-1]["content"][0]
        assert result["is_error"] and "private text or correction" in result["content"]
    assert publication_insight.for_activity(runtime.engine, group)["text"] == (
        "Rustige solo duurloop in Z2."
    )
    assert runner.get_run(runtime, "insight", run_key)["trigger"] == "correction"
    # Nothing more to do; the same correction again changes nothing.
    again = client.post(
        "/api/activity-insight-correction", json={"id": group, "text": FIX}, headers=WRITE
    ).json()["data"]
    assert again["changed"] == [] and again["queued"] is False
    assert len(insight_jobs(runtime)) == 1


def test_a_share_link_cannot_correct_the_insight(runtime):
    from vibesynergy.portal import share

    group = _workout(runtime)
    link = share.Share("x", "t", (share.ALL,), True)
    assert not link.allows("POST", "/activity-insight-correction")
    assert workout_texts.CORRECTION not in workout_texts.FIELDS
    assert workout_texts.texts(runtime.engine, group).keys() >= {"name", "public", "private"}


def test_the_guard_never_lets_a_private_sentence_into_the_public_line(runtime):
    group = _workout(runtime)
    workout_texts.set_texts(runtime.engine, group, public="Mooie route.", private=SECRET)
    workout_texts.set_correction(runtime.engine, group, FIX)
    check = tools.insight_guard(runtime.engine, [group])
    base = {"entry_type": "activity_assessment", "activity_id": group}
    for line in (SECRET, FIX.upper(), f"Vandaag: {FIX}"):
        assert "private text" in check("vibesynergy_coach_write", base | {"public_insight": line})
    assert check("vibesynergy_coach_write", base | {"public_insight": "Solo, rustig."}) is None


# --- the harness -------------------------------------------------------------------------------


def test_the_context_change_is_stamped(runtime):
    assert context.VERSION == 7
    assert "insight_rewrite" in registry.chain_for("insight")
    assert prompts.harness_stamp(runtime.settings, "insight").endswith(".c7")
    assert contract.stamp("p", "r", 7).endswith("c7")
    triggers = {t.detail for t in registry.BY_ID["insight"].triggers}
    assert {"activity.published", "workout.published", "workout.texts_changed"} <= triggers


def test_a_first_insight_gets_no_rewrite_block(runtime):
    group = _workout(runtime)
    found = context.preload(runtime, "insight", START.date(), activity_ids=[group])
    report = next(b for b in found["blocks"] if b["name"] == "insight_rewrite")
    assert report["ok"]
    block = found["text"][found["text"].index('<context name="insight_rewrite"') :]
    assert block[: block.index("</context>")].rstrip().endswith("geen")


def test_with_the_coach_off_the_page_says_so_and_nothing_is_queued(runtime, monkeypatch):
    group = _workout(runtime)
    write_insight(runtime, group)
    at(monkeypatch, datetime.now(UTC) + timedelta(minutes=1))
    workout_texts.set_texts(runtime.engine, group, public="Nieuw.")
    later = datetime.now(UTC) + timedelta(hours=1)
    assert insight_rewrite.status(runtime, group, later)["state"] == "off"
    assert insight_rewrite.queue_now(runtime, group, later) is None
    assert schedule.due_runs(runtime, later) == []
