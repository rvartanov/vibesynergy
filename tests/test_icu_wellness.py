"""Check-in scores pushed to intervals.icu wellness. Mocked HTTP, no live calls."""

from __future__ import annotations

import json
import logging
from datetime import date

import pytest
from sqlalchemy import select

from vibesynergy.query import icu_plan, icu_wellness, journal
from vibesynergy.sources.intervals_icu.client import (
    API_KEY_ENV,
    ATHLETE_ID_ENV,
    IntervalsIcuClient,
    IntervalsIcuError,
)
from vibesynergy.store.schema import audit_log, coach_decision_log, icu_checkin_push

KEY = "icu-wellness-secret-key"
ATHLETE = "i000001"
DAY = date(2026, 11, 2)


def test_scale_direction_and_range() -> None:
    assert icu_wellness.TO_ICU == {1: 1, 2: 2, 3: 3, 4: 3, 5: 4}
    assert icu_wellness.TO_ICU_REVERSED == {1: 4, 2: 3, 3: 2, 4: 2, 5: 1}
    assert frozenset({"mood", "motivation", "sleepQuality"}) == icu_wellness.REVERSED_FIELDS
    for field in ("soreness", "fatigue", "stress"):
        got = [icu_wellness.to_icu(field, value) for value in range(1, 6)]
        assert got == [1, 2, 3, 3, 4]
    for field in icu_wellness.REVERSED_FIELDS:
        got = [icu_wellness.to_icu(field, value) for value in range(1, 6)]
        assert got == [4, 3, 2, 2, 1]
    # On intervals.icu, 1 is good for every subjective field. A better VibeSynergy mood
    # (5) is therefore 1, and a worse one (1) is 4. Soreness stays the other way round.
    assert icu_wellness.to_icu("mood", 5) == 1
    assert icu_wellness.to_icu("mood", 1) == 4
    assert icu_wellness.to_icu("soreness", 1) < icu_wellness.to_icu("soreness", 5)


def test_body_sends_only_checkin_fields() -> None:
    body = icu_wellness.wellness_fields(
        {
            "mood": 5,
            "soreness": 1,
            "stress": 2,
            "motivation": 4,
            "sleep_quality": 3,
            "note": "legs heavy",
            "hrv": 48,
            "restingHR": 46,
            "sleepSecs": 28000,
            "sleepScore": 80,
        }
    )
    assert body == {
        "mood": 1,
        "soreness": 1,
        "stress": 2,
        "motivation": 2,
        "sleepQuality": 2,
    }
    assert "fatigue" not in body
    assert icu_wellness.wellness_fields({"fatigue": 5}) == {"fatigue": 4}
    assert icu_wellness.FORBIDDEN_FIELDS.isdisjoint(body)


class WellnessApi:
    def __init__(self, current: dict | None = None) -> None:
        self.record = {} if current is None else dict(current)
        self.calls: list[tuple] = []

    def exchange(self, method, url, params, headers, body):
        self.calls.append((method, url, body, dict(headers)))
        assert KEY not in url
        assert "bulk" not in url
        if method == "GET":
            if not self.record:
                return 404, {}, b"missing"
            return 200, {}, json.dumps(self.record).encode()
        if method == "PUT":
            payload = json.loads(body)
            assert icu_wellness.FORBIDDEN_FIELDS.isdisjoint(payload)
            assert set(payload) <= icu_wellness.WELLNESS_FIELDS
            self.record.update(payload)
            stored = {
                "hrv": 47,
                "restingHR": 45,
                "sleepSecs": 27000,
                "sleepScore": 81,
                **self.record,
            }
            return 200, {}, json.dumps(stored).encode()
        raise AssertionError(method)


def _client(api: WellnessApi, **kwargs) -> IntervalsIcuClient:
    options = {"interval": 0, "write_interval": 0}
    options.update(kwargs)
    return IntervalsIcuClient(KEY, ATHLETE, exchange=api.exchange, **options)


def _save(runtime, monkeypatch, **scores):
    monkeypatch.setenv(API_KEY_ENV, KEY)
    monkeypatch.setenv(ATHLETE_ID_ENV, ATHLETE)
    monkeypatch.setattr(icu_wellness, "_start", lambda fn: None)
    return journal.set_checkin(runtime.engine, DAY, **scores)


def test_put_is_idempotent_per_date_and_audited(runtime, monkeypatch) -> None:
    _save(runtime, monkeypatch, mood=5, soreness=1, sleep_quality=3, stress=2, motivation=4)
    api = WellnessApi({"hrv": 47, "restingHR": 45, "sleepSecs": 27000, "mood": 2})
    client = _client(api)
    first = icu_wellness.push_checkin_wellness(runtime.engine, DAY, client)
    assert first is not None
    put = next(call for call in api.calls if call[0] == "PUT")
    assert put[1].endswith(f"/athlete/{ATHLETE}/wellness/{DAY.isoformat()}")
    sent = json.loads(put[2])
    assert set(sent) <= icu_wellness.WELLNESS_FIELDS
    assert "hrv" not in sent and "sleepScore" not in sent
    assert first["before"]["mood"] == 2
    assert first["before"]["sleepQuality"] == icu_wellness.CLEAR_FIELD
    assert first["sent"]["mood"] == 1
    assert first["sent"]["sleepQuality"] == 2
    detail = _audit(runtime, first["audit_id"])
    assert detail["action"] == "icu.wellness"
    assert detail["revertable"] is True
    assert detail["before"]["mood"] == 2
    assert "hrv" not in detail["before"] and "hrv" not in detail["after"]
    with runtime.engine.connect() as conn:
        feed = (
            conn.execute(
                select(coach_decision_log.c.text).where(
                    coach_decision_log.c.entry_type == "plan_change"
                )
            )
            .scalars()
            .all()
        )
    assert any("2-11-2026" in text for text in feed)

    monkeypatch.setattr(icu_wellness, "_start", lambda fn: None)
    journal.set_checkin(runtime.engine, DAY, mood=1)
    icu_wellness.push_checkin_wellness(runtime.engine, DAY, client)
    puts = [call for call in api.calls if call[0] == "PUT"]
    assert len(puts) == 2
    assert all(call[1] == puts[0][1] for call in puts)
    assert json.loads(puts[1][2])["mood"] == 4


def test_revert_restores_previous_wellness_fields(runtime, monkeypatch) -> None:
    _save(runtime, monkeypatch, mood=5, stress=5)
    api = WellnessApi({"mood": 2, "hrv": 50, "sleepScore": 70})
    client = _client(api)
    pushed = icu_wellness.push_checkin_wellness(runtime.engine, DAY, client)
    restored = icu_plan.revert(
        runtime.engine, client, audit_id=pushed["audit_id"], reason="Put the old check-in back"
    )
    last = [call for call in api.calls if call[0] == "PUT"][-1]
    body = json.loads(last[2])
    assert body["mood"] == 2
    assert body["stress"] == icu_wellness.CLEAR_FIELD
    assert body["stress"] == -1
    assert "hrv" not in body and "sleepScore" not in body
    assert restored["action"] == "icu.revert"
    again = icu_wellness.retry_pending(runtime.engine, runtime.settings, client)
    assert again == 0
    assert len([call for call in api.calls if call[0] == "PUT"]) == 2


def test_failure_does_not_block_the_checkin_and_retries_on_sync(
    runtime, monkeypatch, caplog
) -> None:
    monkeypatch.setenv(API_KEY_ENV, KEY)
    monkeypatch.setenv(ATHLETE_ID_ENV, ATHLETE)
    monkeypatch.setattr(icu_wellness, "_start", lambda fn: (_ for _ in ()).throw(RuntimeError(KEY)))
    # _start is replaced, so the exception happens inside schedule's caller only if _start raises.
    # The save itself must still return. schedule calls _start after marking pending.
    saved = journal.set_checkin(runtime.engine, DAY, mood=4)
    assert saved["mood"] == 4

    def exchange(method, url, params, headers, body):
        return 500, {}, f"nope {KEY} {headers['Authorization']}".encode()

    client = _client(WellnessApi())
    client._exchange = exchange
    with caplog.at_level(logging.WARNING):
        assert icu_wellness._push_quietly(runtime.engine, DAY, client) is False
    assert KEY not in caplog.text
    with runtime.engine.connect() as conn:
        error = conn.execute(select(icu_checkin_push.c.last_error)).scalar_one()
    assert error and KEY not in error
    assert "HTTP 500" in error or "IntervalsIcuError" in error

    ok = WellnessApi({"mood": 1})
    sent = icu_wellness.retry_pending(runtime.engine, runtime.settings, _client(ok))
    assert sent == 1
    assert any(call[0] == "PUT" for call in ok.calls)


def test_disabled_flag_sends_nothing(runtime, monkeypatch, tmp_path) -> None:
    config = runtime.settings.config_path
    config.write_text(config.read_text() + "\n[sources.intervals_icu]\ncheckin_wellness = false\n")
    monkeypatch.setenv(API_KEY_ENV, KEY)
    monkeypatch.setenv(ATHLETE_ID_ENV, ATHLETE)
    started = []
    monkeypatch.setattr(icu_wellness, "_start", lambda fn: started.append(fn))
    journal.set_checkin(runtime.engine, DAY, mood=3)
    assert started == []
    assert icu_wellness.retry_pending(runtime.engine, runtime.settings) == 0


def test_writes_keep_the_rate_limit() -> None:
    api = WellnessApi({"mood": 1})
    slept: list[float] = []
    clock = {"now": 5000.0}
    client = _client(api, write_interval=1.0, sleep=slept.append, clock=lambda: clock["now"])
    client.get_wellness(DAY)
    client.put_wellness(DAY, {"mood": 2})
    client.put_wellness(DAY, {"mood": 3})
    # The read does not start the write clock. The second write waits.
    assert slept == [1.0]
    assert KEY not in json.dumps([call[1] for call in api.calls])


def test_checkin_error_text_never_includes_the_key(monkeypatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, KEY)
    text = icu_wellness._redact(f"boom {KEY}")
    assert KEY not in text
    assert "[redacted]" in text
    client = IntervalsIcuClient(
        KEY,
        ATHLETE,
        exchange=lambda *a: (500, {}, f"x {KEY}".encode()),
        interval=0,
        write_interval=0,
    )
    with pytest.raises(IntervalsIcuError) as caught:
        client.put_wellness(DAY, {"mood": 1})
    assert KEY not in str(caught.value)


def _audit(runtime, audit_id: int) -> dict:
    with runtime.engine.connect() as conn:
        return (
            conn.execute(select(audit_log).where(audit_log.c.id == audit_id))
            .one()
            ._mapping["detail"]
        )
