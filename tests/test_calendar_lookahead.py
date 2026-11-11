"""Calendar sync reads one year ahead, from one config value, for both feeds."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.core.timeutil import today_in
from vibesynergy.model import CalendarSpan, PlannedItem
from vibesynergy.normalize.normaliser import write_records
from vibesynergy.sources.registry import SourceRegistry
from vibesynergy.store.schema import planned_item


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _spec(settings, source_id: str, dataset: str):
    source = SourceRegistry(settings).get(source_id, require_enabled=False)
    return source.dataset(dataset)


def test_both_calendar_feeds_look_twelve_months_ahead(settings, runtime) -> None:
    today = today_in(settings.timezone)
    expected = _add_months(today, 12)
    for source_id, dataset in (("garmin", "scheduled_workouts"), ("caldav", "calendar")):
        window = runtime.sync.plan_window(source_id, _spec(settings, source_id, dataset))
        assert window.end == expected


def test_lookahead_months_is_one_config_value(env, runtime) -> None:
    env.write_text(env.read_text() + "\n[calendar]\nlookahead_months = 6\n")
    loaded = load_settings(env)
    today = today_in(loaded.timezone)
    caldav = runtime.sync.plan_window("caldav", _spec(loaded, "caldav", "calendar"))
    garmin = runtime.sync.plan_window("garmin", _spec(loaded, "garmin", "scheduled_workouts"))
    assert caldav.end == garmin.end == _add_months(today, 6)


@pytest.mark.parametrize(
    "body",
    ["lookahead_months = 0\n", "lookahead_months = true\n", 'lookahead_months = "12"\n'],
)
def test_lookahead_months_must_be_a_whole_number_of_months(env, body: str) -> None:
    env.write_text(env.read_text() + "\n[calendar]\n" + body)
    with pytest.raises(ConfigError, match="lookahead_months"):
        load_settings(env)


def test_resync_keeps_one_race_and_a_removed_race_inside_the_window_is_deleted(runtime) -> None:
    """A race inside the configured horizon is stored once. A later reply that omits it
    deletes it. A race past the horizon is left alone, as a short read always was."""
    today = today_in(runtime.settings.timezone)
    horizon = runtime.sync.plan_window(
        "garmin", _spec(runtime.settings, "garmin", "scheduled_workouts")
    ).end
    race_day = _add_months(today, 11)
    assert today + timedelta(days=183) < race_day <= horizon
    far_day = horizon + timedelta(days=40)
    race = PlannedItem("event:example", race_day, "event", title="Example triathlon", is_race=True)
    beyond = PlannedItem("event:later", far_day, "event", title="Later", is_race=True)

    def write(raw_id: str, records: list) -> None:
        with runtime.engine.begin() as conn:
            write_records(conn, records, source="garmin", raw_item_id=raw_id)

    keys = ("far", "far-again", "far-gone")
    stored = [
        runtime.raw.put(
            source="garmin",
            dataset="scheduled_workouts",
            item_key=key,
            payload=b"{}",
            content_type="application/json",
        )
        for key in keys
    ]
    write(stored[0].id, [race, beyond, CalendarSpan(today, horizon)])
    write(stored[1].id, [race, beyond, CalendarSpan(today, horizon)])
    titles = _titles(runtime)
    assert titles.count("Example triathlon") == 1 and "Later" in titles

    write(stored[2].id, [beyond, CalendarSpan(today, horizon)])
    titles = _titles(runtime)
    assert "Example triathlon" not in titles and "Later" in titles


def _titles(runtime) -> list[str]:
    with runtime.engine.connect() as conn:
        return list(conn.execute(select(planned_item.c.title)).scalars())
