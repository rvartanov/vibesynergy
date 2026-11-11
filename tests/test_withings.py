"""Withings source against a fake API: login, token refresh, datasets, mappings."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import select
from withings_fake import FakeWithingsAPI

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import Window
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth
from vibesynergy.sources.withings.client import WithingsClient
from vibesynergy.store.schema import (
    activity,
    body_measurement,
    daily_metric,
    sleep_session,
    sleep_stage,
)

DAY = date(2026, 9, 20)


@pytest.fixture
def withings(runtime):
    settings = replace(
        runtime.settings,
        sources={"withings": {"enabled": True}},
        server={"public_url": "https://server.example.ts.net"},
    )
    rt = Runtime.open(settings)
    source = rt.registry.get("withings")
    fake = FakeWithingsAPI()
    store = source.ctx.secrets
    store.set(oauth.APP, {"client_id": "cid", "client_secret": "csecret"})
    oauth.save_tokens(store, {"access_token": "a0", "refresh_token": "r0", "expires_in": 3600})
    source._client = WithingsClient(store, post=fake, interval=0)
    yield rt, fake, store
    rt.close()


def _rows(rt, table, *where):
    with rt.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table).where(*where))]


def _metric(rt, metric, day=DAY):
    rows = _rows(
        rt,
        daily_metric,
        daily_metric.c.metric == metric,
        daily_metric.c.date == day,
        daily_metric.c.source == "withings",
    )
    return rows[0]["value"] if rows else None


def test_all_datasets_sync(withings) -> None:
    rt, _, _ = withings
    results = rt.sync.sync("withings", window=Window(DAY, DAY))
    failed = {r.dataset: r.error for r in results if r.error or r.normalise_errors}
    assert failed == {} and len(results) == 9


def test_measures_every_type_paged(withings) -> None:
    rt, fake, _ = withings
    rt.sync.sync("withings", ["measures"], Window(DAY, DAY))
    assert sum(1 for c in fake.calls if c[1] == "getmeas") == 2  # followed "more"
    rows = {(r["source_id"], r["metric"]): r["value"] for r in _rows(rt, body_measurement)}
    assert rows[("101", "body.weight")] == pytest.approx(74.25)
    assert rows[("101", "body.pwv")] == pytest.approx(8.12)
    assert rows[("101", "withings.meastype_999")] == 5  # unknown types are kept
    assert rows[("102", "bp.pulse")] == 61  # pulse with a BP reading
    assert ("104", "body.weight") not in rows  # category 2 = objective, not a measurement
    assert _metric(rt, "body.weight") == pytest.approx(73.9)  # last of the day
    assert _metric(rt, "bp.systolic") == 128


def test_activity_sleep_workouts_heart(withings) -> None:
    rt, _, _ = withings
    rt.sync.sync(
        "withings",
        ["activity", "sleep_summary", "sleep_series", "workouts", "heart", "intraday_activity"],
        Window(DAY, DAY),
    )
    assert _metric(rt, "daily.steps") == 8123
    assert _metric(rt, "daily.intensity_minutes.vigorous") == 10
    assert _metric(rt, "sleep.score") == 82
    assert _metric(rt, "sleep.efficiency") == pytest.approx(93)
    assert _metric(rt, "sleep.apnea_hypopnea_index") == 3
    session = _rows(rt, sleep_session)[0]
    assert session["duration_s"] == 26000 and session["local_date"] == DAY
    assert sorted(r["stage"] for r in _rows(rt, sleep_stage)) == ["awake", "deep"]

    run = _rows(rt, activity)[0]
    assert (run["source"], run["sport"], run["elapsed_s"]) == ("withings", "running", 1800)
    assert run["tz_offset_s"] == 7200

    afib = _rows(rt, body_measurement, body_measurement.c.metric == "heart.afib_ecg")
    assert afib[0]["extra"]["signalid"] == 42
    ecg = rt.raw.load(next(rt.raw.current_ids("withings", "heart"))).json()
    assert ecg["signals"]["42"]["sampling_frequency"] == 300  # raw ECG signal archived

    wellness = rt.settings.parquet_dir / "wellness" / "source=withings"
    assert {p.stem for p in wellness.glob("*.parquet")} == {
        "intraday-2026-09-20",
        "sleep-2026-09-20",
    }


def test_drop_a_dataset_and_bring_it_back(withings) -> None:
    rt, _, _ = withings
    rt.sync.sync("withings", ["measures", "workouts", "intraday_activity"], Window(DAY, DAY))
    wellness = rt.settings.parquet_dir / "wellness" / "source=withings"
    assert _rows(rt, activity) and list(wellness.glob("intraday-*"))
    weights = _rows(rt, body_measurement, body_measurement.c.metric == "body.weight")

    items, rows, files = rt.normaliser.drop("withings", "workouts")
    assert items >= 1 and rows >= 1 and files == 0 and _rows(rt, activity) == []
    assert rt.normaliser.drop("withings", "intraday_activity")[2] == 1
    assert not list(wellness.glob("intraday-*"))
    assert _rows(rt, body_measurement, body_measurement.c.metric == "body.weight") == weights

    rt.normaliser.rebuild("withings", "workouts")  # the raw archive was kept
    assert _rows(rt, activity)


def test_expired_token_is_refreshed_and_rotated(withings) -> None:
    rt, fake, store = withings
    tokens = store.get(oauth.TOKENS)
    store.set(oauth.TOKENS, {**tokens, "expires_at": int(time.time()) - 10})
    rt.sync.sync("withings", ["devices"])
    assert fake.token_requests[-1]["grant_type"] == "refresh_token"
    assert fake.token_requests[-1]["refresh_token"] == "r0"
    assert store.get(oauth.TOKENS)["refresh_token"] == "refresh-1"  # new one saved


def test_invalid_token_triggers_one_refresh(withings) -> None:
    rt, fake, _ = withings
    fake.expire_next_call = True
    result = rt.sync.sync("withings", ["goals"])[0]
    assert result.error is None and len(fake.token_requests) == 1


def test_login_paste_back_flow(withings, monkeypatch) -> None:
    rt, fake, store = withings
    source = rt.registry.get("withings")
    store.delete(oauth.TOKENS)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    def answers(prompt: str) -> str:
        if "stored" in prompt:
            return ""
        link = next(line for line in printed if "authorize2" in line)
        state = link.split("state=")[1].split("&")[0]
        return f"https://server.example.ts.net/vibesynergy/withings/callback?code=abc&state={state}"

    monkeypatch.setattr("builtins.input", answers)
    source.authenticate()
    request = fake.token_requests[-1]
    assert request["grant_type"] == "authorization_code" and request["code"] == "abc"
    assert request["redirect_uri"] == "https://server.example.ts.net/vibesynergy/withings/callback"
    assert store.get(oauth.TOKENS)["access_token"].startswith("access-")


def test_wrong_state_is_rejected(monkeypatch) -> None:
    with pytest.raises(oauth.AuthError, match="does not belong"):
        oauth.paste_back_code(
            "https://example.com/auth",
            {},
            ask=lambda _: "https://x/callback?code=abc&state=forged",
        )


def test_auth_needs_public_url(runtime) -> None:
    with pytest.raises(oauth.AuthError, match="public_url"):
        oauth.redirect_uri(runtime.settings, "withings")


def test_not_logged_in(runtime, tmp_path) -> None:
    client = WithingsClient(SecretStore(tmp_path, "withings"), post=FakeWithingsAPI())
    with pytest.raises(oauth.AuthError, match="vibesynergy auth withings"):
        client.call("/v2/user", "getdevice")
