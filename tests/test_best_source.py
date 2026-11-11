"""Best source per metric: what delivers each metric, the order in use, the winner now,
and an order saved for one metric that the next derive applies without a restart."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient

from vibesynergy.core.config import load_settings
from vibesynergy.model import DailyMetric
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import best_source, views
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import daily_unified

TAILNET = "server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": f"https://{TAILNET}:8447"}
DAY = date(2026, 10, 20)
_ids = count()


def _put(rt, source: str, *records) -> None:
    stored = rt.raw.put(
        source=source,
        dataset="test",
        item_key=f"bs-{next(_ids)}",
        payload=str(next(_ids)).encode(),
        content_type="text/plain",
    )
    with rt.engine.begin() as conn:
        write_records(conn, records, source=source, raw_item_id=stored.id)


def _winner(rt, metric: str) -> str:
    with rt.engine.connect() as conn:
        rows = conn.execute(
            daily_unified.select().where(
                daily_unified.c.metric == metric, daily_unified.c.date == DAY
            )
        ).mappings()
        (row,) = rows
        return row["source"]


def _seed(rt) -> None:
    _put(
        rt,
        "garmin",
        DailyMetric(DAY, "sleep.score", 80.0, "score"),
        DailyMetric(DAY - timedelta(days=1), "sleep.score", 78.0, "score"),
        DailyMetric(DAY, "body.weight", 80.0, "kg"),
        DailyMetric(DAY, "hrv.night_avg", 50.0, "ms"),
    )
    _put(
        rt,
        "withings",
        DailyMetric(DAY, "sleep.score", 85.0, "score"),
        DailyMetric(DAY, "body.weight", 79.5, "kg"),
    )


def test_group_of_puts_metrics_in_plain_groups() -> None:
    assert best_source.group_of("sleep.score") == "sleep"
    assert best_source.group_of("hrv.night_avg") == "hrv"
    assert best_source.group_of("bp.systolic") == "body"
    assert best_source.group_of("hr.resting") == "heart"
    assert best_source.group_of("daily.steps") == "activity"
    assert best_source.group_of("weather.temp") == "other"


def test_save_and_load_round_trip(tmp_path) -> None:
    assert best_source.load(tmp_path) == {}
    assert best_source.save(tmp_path, "sleep.score", ["withings", "garmin"]) == {
        "sleep.score": ["withings", "garmin"]
    }
    assert best_source.load(tmp_path) == {"sleep.score": ["withings", "garmin"]}
    assert best_source.save(tmp_path, "sleep.score", None) == {}  # back to the default
    assert best_source.load(tmp_path) == {}


@pytest.mark.parametrize(
    ("metric", "order"),
    [
        ("Sleep score", ["garmin"]),
        ("sleep.score", ["garmin", "garmin"]),
        ("sleep.score", ["Garmin!"]),
        ("sleep.score", ["s"] * 17),
    ],
)
def test_save_refuses_bad_input(tmp_path, metric, order) -> None:
    with pytest.raises(ValueError):
        best_source.save(tmp_path, metric, order)
    assert best_source.load(tmp_path) == {}


def test_damaged_file_means_no_saved_orders(tmp_path) -> None:
    best_source.path(tmp_path).write_text("{not json")
    assert best_source.load(tmp_path) == {}
    best_source.path(tmp_path).write_text('{"metrics": {"sleep.score": "garmin", "x": []}}')
    assert best_source.load(tmp_path) == {}


def test_exact_rule_beats_prefix_and_only_its_metric(runtime) -> None:
    _seed(runtime)
    _put(runtime, "withings", DailyMetric(DAY, "sleep.scored", 1.0, "x"))
    _put(runtime, "garmin", DailyMetric(DAY, "sleep.scored", 2.0, "x"))
    best_source.rebuild(runtime.engine, runtime.settings)
    assert _winner(runtime, "sleep.score") == "garmin"  # the built-in sleep order

    best_source.save(runtime.settings.data_dir, "sleep.score", ["withings", "garmin"])
    best_source.rebuild(runtime.engine, runtime.settings)
    assert _winner(runtime, "sleep.score") == "withings"
    assert _winner(runtime, "sleep.scored") == "garmin"  # not a prefix match
    assert _winner(runtime, "body.weight") == "withings"  # other metrics unchanged


def test_derive_applies_saved_order_without_restart(runtime) -> None:
    _seed(runtime)
    best_source.save(runtime.settings.data_dir, "body.weight", ["garmin"])
    runtime.derive()  # what a sync with new data runs
    assert _winner(runtime, "body.weight") == "garmin"


def test_overview_lists_sources_order_and_winner(runtime) -> None:
    _seed(runtime)
    best_source.rebuild(runtime.engine, runtime.settings)
    data = best_source.overview(runtime.engine, runtime.settings)
    assert data["groups"] == ["sleep", "hrv", "body", "heart", "activity", "other"]
    rows = {m["metric"]: m for m in data["metrics"]}
    sleep = rows["sleep.score"]
    assert sleep["group"] == "sleep" and sleep["order"] == ["garmin", "withings"]
    assert sleep["sources"][0] == {"id": "garmin", "days": 2, "last": str(DAY)}
    assert sleep["custom"] is False
    assert sleep["now"] == {"source": "garmin", "date": str(DAY)}
    assert rows["hrv.night_avg"]["order"] == ["garmin"]  # one source: nothing to choose

    best_source.save(runtime.settings.data_dir, "sleep.score", ["withings"])
    best_source.save(runtime.settings.data_dir, "sleep.gone", ["garmin"])
    data = best_source.overview(runtime.engine, runtime.settings)
    sleep = next(m for m in data["metrics"] if m["metric"] == "sleep.score")
    # The saved order comes first; a delivering source it leaves out still ranks after it.
    assert sleep["order"] == ["withings", "garmin"] and sleep["custom"] is True
    assert sleep["now"]["source"] == "garmin"  # until the next derive
    assert data["saved_without_data"] == ["sleep.gone"]


def test_explain_names_the_saved_order(runtime) -> None:
    _seed(runtime)
    exact = {"body.weight": ["garmin", "withings"]}
    best_source.save(runtime.settings.data_dir, "body.weight", exact["body.weight"])
    best_source.rebuild(runtime.engine, runtime.settings)
    result = views.explain(runtime.engine, "body.weight", DAY, None, exact)
    assert result["best_source_rule"] == {"prefix": "body.weight", "order": ["garmin", "withings"]}
    assert result["shown"]["source"] == "garmin"


def _client(runtime, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        f'[user]\ntimezone = "Europe/Amsterdam"\n\n[server]\npublic_url = "https://{TAILNET}"\n'
    )
    settings = replace(load_settings(config), data_dir=runtime.settings.data_dir)
    rt = Runtime.open(settings)
    _seed(rt)
    best_source.rebuild(rt.engine, rt.settings)
    return rt, TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")


def test_api_reads_and_saves_one_metric(runtime, tmp_path) -> None:
    rt, client = _client(runtime, tmp_path)
    data = client.get("/api/settings/best_source").json()["data"]
    assert any(m["metric"] == "sleep.score" for m in data["metrics"])

    url = "/api/settings/best_source/metric"
    body = {"metric": "sleep.score", "order": ["withings", "garmin"]}
    assert client.post(url, json=body).status_code == 403  # no portal header
    reply = client.post(url, json=body, headers=WRITE)
    assert reply.status_code == 200
    sleep = next(m for m in reply.json()["data"]["metrics"] if m["metric"] == "sleep.score")
    assert sleep["custom"] is True and sleep["order"] == ["withings", "garmin"]
    assert best_source.load(rt.settings.data_dir) == {"sleep.score": ["withings", "garmin"]}
    # No restart: the settings page shows no restart notice for it.
    assert client.get("/api/settings").json()["data"]["restart_needed"] is False

    bad = client.post(url, json={"metric": "sleep.score", "order": ["x", "x"]}, headers=WRITE)
    assert bad.status_code == 400

    back = client.post(url, json={"metric": "sleep.score", "order": None}, headers=WRITE)
    sleep = next(m for m in back.json()["data"]["metrics"] if m["metric"] == "sleep.score")
    assert sleep["custom"] is False and sleep["order"] == ["garmin", "withings"]
    assert best_source.load(rt.settings.data_dir) == {}
