"""The plan builder's settings: defaults from the plan, choices checked, saved in prefs."""

from __future__ import annotations

import pytest

from vibesynergy.query import plan_builder


def test_sport_guess_from_the_race_title() -> None:
    assert plan_builder._guess_sport("Hometown hele afstand") == {
        "sport": "triathlon",
        "distance": "full",
        "type": "road",
    }
    assert plan_builder._guess_sport("Cross triathlon")["type"] == "cross"
    assert plan_builder._guess_sport("Trail Ardennen")["distance"] == "ultra"
    assert plan_builder._guess_sport("Rotterdam")["sport"] == "run"
    # A name that says nothing: the race's distance decides.
    km = {"unit": "kilometer", "unitType": "distance", "value": 226.34}
    assert plan_builder._guess_sport("Hometown 2027", km)["distance"] == "full"
    half = {"unit": "meter", "unitType": "distance", "value": 113000}
    assert plan_builder._guess_sport("Hometown 2027", half)["distance"] == "middle"
    trail = {"unit": "kilometer", "unitType": "distance", "value": 110}
    assert plan_builder._guess_sport("Hometown trail", trail)["sport"] == "run"


def test_validate_takes_every_sport_distance_and_type() -> None:
    for sport, spec in plan_builder.SPORTS.items():
        for distance in spec["distances"]:
            for kind in spec["types"]:
                assert plan_builder.validate({"sport": sport, "distance": distance, "type": kind})
    value = plan_builder.validate(
        {
            "goal": {"new": {"name": " Kustmarathon ", "date": "2027-10-02", "priority": "B"}},
            "distance": "custom",
            "sport": "run",
            "custom": {"run_km": "42.2", "bike_km": 0},
            "hours_min": "5",
            "taper_weeks": "3",
        }
    )
    assert value["goal"] == {"new": {"name": "Kustmarathon", "date": "2027-10-02", "priority": "B"}}
    assert value["custom"] == {"run_km": 42.2}
    assert value["hours_min"] == 5.0 and value["taper_weeks"] == 3


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"sport": "swim"},
        {"sport": "run", "distance": "olympic"},
        {"sport": "triathlon", "type": "trail"},
        {"method": "block"},
        {"hours_max": 60},
        {"rhythm": "4:1"},
        {"taper_weeks": 0},
        {"goal": {"new": {"name": "", "date": "2027-01-01"}}},
        {"goal": {"new": {"name": "X", "date": "soon"}}},
        {"colour": "red"},
    ],
)
def test_validate_refuses_bad_settings(bad) -> None:
    with pytest.raises(ValueError):
        plan_builder.validate(bad)


def test_draft_phases_and_hours() -> None:
    weeks = plan_builder._phases(12, 2, "3:1")
    assert weeks[-1] == ("race", "race") and weeks[-3:-1] == [("taper", "taper")] * 2
    assert weeks[-5:-3] == [("peak", "load")] * 2
    assert ("base", "recovery") in weeks  # the 3:1 rhythm puts in a recovery week
    hours = plan_builder._hours(weeks, 6.0, 10.0, 2)
    loads = [h for (p, k), h in zip(weeks, hours, strict=True) if k == "load" and p != "peak"]
    assert loads[0] == 6.0 and loads[-1] == 10.0 and loads == sorted(loads)
    assert hours[-1] == 4.0  # race week: 40 % of the maximum


def test_draft_for_a_new_race(runtime) -> None:
    from datetime import date

    from vibesynergy.portal import prefs

    prefs.save(
        runtime.settings.data_dir,
        {
            "plan_builder": {
                "goal": {"new": {"name": "Test 70.3", "date": "2027-03-07", "priority": "A"}},
                "sport": "triathlon",
                "distance": "middle",
                "type": "road",
                "hours_min": 6,
                "hours_max": 10,
                "rhythm": "3:1",
                "taper_weeks": 2,
                "strength": True,
                "tests": True,
            }
        },
    )
    out = plan_builder.draft(runtime.engine, runtime.settings.data_dir, date(2026, 11, 9))
    assert out["start"] == "2026-11-16" and out["race_date"] == "2027-03-07"
    weeks = out["weeks"]
    assert len(weeks) == 16 and weeks[-1]["phase"] == "race"
    assert all(w["sessions"] for w in weeks)
    assert any(s["template"] == "run_brick" for w in weeks for s in w["sessions"])
    assert any(s["template"].endswith("_test") for w in weeks for s in w["sessions"])
    assert set(weeks[0]["sports"]) == {"swim", "ride", "run"}


def test_rest_days_and_fixed_moments_shape_the_draft(runtime) -> None:
    """when you train is part of building the plan, and editable later."""
    from datetime import date

    from vibesynergy.portal import prefs

    group = {"day": 1, "sport": "run", "name": "Loopgroep Stad", "minutes": 90}
    prefs.save(
        runtime.settings.data_dir,
        {
            "plan_builder": {
                "goal": {"new": {"name": "Test 70.3", "date": "2027-03-07", "priority": "A"}},
                "sport": "triathlon",
                "distance": "middle",
                "type": "road",
                "rest_days": [5],
                "fixed": [group],
            }
        },
    )
    weeks = plan_builder.draft(runtime.engine, runtime.settings.data_dir, date(2026, 11, 9))[
        "weeks"
    ]
    for w in weeks:
        monday = date.fromisoformat(w["start"])
        friday = monday.toordinal() + 4
        assert all(date.fromisoformat(s["date"]).toordinal() != friday for s in w["sessions"])
        tuesday_runs = [
            s
            for s in w["sessions"]
            if s["sport"] == "run" and date.fromisoformat(s["date"]).weekday() == 1
        ]
        if w["kind"] == "race":
            assert not any(s.get("fixed") for s in w["sessions"])
        else:
            assert [s["name"] for s in tuesday_runs] == ["Loopgroep Stad"]
    assert any(s.get("moved") for w in weeks for s in w["sessions"])


@pytest.mark.parametrize(
    "bad",
    [
        {"rest_days": [7]},
        {"rest_days": [0, 1, 2, 3, 4, 5]},
        {"fixed": [{"day": 1, "sport": "yoga", "name": "x", "minutes": 60}]},
        {"fixed": [{"day": 1, "sport": "run", "name": "", "minutes": 60}]},
        {"fixed": [{"day": 1, "sport": "run", "name": "x", "minutes": 5}]},
    ],
)
def test_when_you_train_is_checked(bad) -> None:
    with pytest.raises(ValueError):
        plan_builder.validate(bad)


def test_draft_weeks_show_the_current_plan_to_compare(runtime, monkeypatch) -> None:
    from datetime import date

    from vibesynergy.portal import prefs
    from vibesynergy.query import plan_model

    prefs.save(
        runtime.settings.data_dir,
        {
            "plan_builder": {
                "goal": {"new": {"name": "Test 70.3", "date": "2027-03-07", "priority": "A"}},
                "sport": "triathlon",
                "distance": "middle",
                "type": "road",
            }
        },
    )
    weeks = [
        {
            "monday": "2026-11-16",
            "phase": "Base 1",
            "kind": "load",
            "targets": [{"time_s": 3600 * 5}, {"time_s": 3600 * 3}],
        }
    ]
    monkeypatch.setattr(plan_model, "view", lambda engine, start, end: {"weeks": weeks})
    out = plan_builder.draft(runtime.engine, runtime.settings.data_dir, date(2026, 11, 9))
    first, second = out["weeks"][:2]
    assert first["current"] == {"phase": "Base 1", "kind": "load", "hours": 8.0}
    assert second["current"] is None
