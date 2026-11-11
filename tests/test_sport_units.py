"""Synthetic Garmin type keys and SI values, with one shared presentation contract."""

import pytest

from vibesynergy.query.units import family, presentation, speed_text


@pytest.mark.parametrize(
    ("sport", "kind", "speed", "expected"),
    [
        ("running", "run", 1000 / 300, "5:00/km"),
        ("trail_running", "run", 1000 / 360, "6:00/km"),
        ("walking", "run", 1000 / 600, "10:00/km"),
        ("hiking", "run", 1000 / 900, "15:00/km"),
        ("lap_swimming", "pool_swim", 100 / 120, "2:00/100m"),
        ("open_water_swimming", "open_water_swim", 100 / 110, "1:50/100m"),
        ("cycling", "cycle", 8, "28.8 km/h"),
        ("virtual_ride", "cycle", 10, "36.0 km/h"),
        ("rowing", "row", 500 / 125, "2:05/500m"),
        ("strength_training", "time", 2, None),
        ("yoga", "time", 2, None),
        ("pilates", "time", 2, None),
        ("breathwork", "time", 2, None),
        ("unknown", "other", 2, None),
    ],
)
def test_sport_family_units(sport, kind, speed, expected):
    assert family(sport) == kind
    assert speed_text(sport, speed) == expected


def test_garmin_fields_are_only_shown_when_present():
    swim = presentation(
        "lap_swimming",
        {
            "avg_speed": 1,
            "extra": {
                "numberOfActiveLengths": 40,
                "poolLength": 25,
                "averageSwolf": 34,
                "averageStrokeRate": 27,
                "restDuration": 45,
            },
        },
    )
    assert swim == {
        "family": "pool_swim",
        "pace_or_speed": "1:40/100m",
        "lengths": 40,
        "pool_length_m": 25,
        "swolf": 34,
        "stroke_rate": 27,
        "rest_s": 45,
    }
    assert presentation("cycling", {"avg_power": 210}, weight_kg=70)["power_per_kg"] == "3.00 W/kg"
    assert presentation("strength_training", {"extra": {"sets": 3, "repetitions": 12}}) == {
        "family": "time",
        "sets": 3,
        "reps": 12,
    }
    assert presentation("unknown", {"avg_speed": 10}) == {"family": "other"}
    assert presentation("unknown", {"elapsed_s": 3661, "distance_m": 12345}) == {
        "family": "other",
        "duration": "1:01:01",
        "distance": "12.35 km",
    }
    assert presentation("lap_swimming", {"distance_m": 1000})["distance"] == "1000 m"
    assert (
        presentation("lap_swimming", {"elapsed_s": 45, "extra": {"intensityType": "REST"}})[
            "rest_s"
        ]
        == 45
    )
    nested = presentation(
        "lap_swimming",
        {
            "extra": {
                "averageSWOLF": 43,
                "averageSwimCadence": 28,
                "lengthDTOs": [
                    {"lengthIndex": 1, "duration": 30},
                    {"lengthIndex": 2, "duration": 31},
                    {"lengthIndex": 3, "duration": 20, "intensityType": "REST"},
                ],
            }
        },
    )
    assert (nested["lengths"], nested["swolf"], nested["stroke_rate"], nested["rest_s"]) == (
        2,
        43,
        28,
        20,
    )


def test_sport_units_do_not_change_the_assessment_input_version():
    from vibesynergy.query import assessments

    base = {
        "coach": {"documents": {}},
        "comparison": {"matches": []},
        "evidence": {"facts_version": "f"},
        "laps": [{"lap_index": 0, "distance_m": 100}],
    }
    shown = {
        **base,
        "sport_units": {"family": "pool_swim"},
        "laps": [{"lap_index": 0, "distance_m": 100, "sport_units": {"pace": "1:50/100m"}}],
    }
    assert assessments._semantic(base) == assessments._semantic(shown)
