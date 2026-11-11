from vibesynergy.query import weather


class Fake:
    def __init__(self, payload):
        self.payload = payload

    def weather_forecast(self):
        return self.payload


def _day(day, rain=0.0, gust=3.0, low=8.0, snow=None):
    raw = {
        "id": day,
        "temp": {"min": low, "max": low + 5, "day": low + 3},
        "wind_speed": 2.0,
        "wind_gust": gust,
        "rain": rain,
        "weather": [{"description": "light rain"}],
    }
    if snow is not None:
        raw["snow"] = snow
    return raw


def test_forecast_flags_and_limits_days() -> None:
    payload = {
        "forecasts": [
            {
                "label": "Example town",
                "provider": "OPEN_WEATHER",
                "daily": [
                    _day("2026-11-01"),
                    _day("2026-11-02", rain=4.5, gust=14.0),
                    _day("2026-11-03", low=-1.0, snow=0.5),
                ],
            }
        ]
    }
    out = weather.forecast(Fake(payload), days=2)
    assert out["location"] == "Example town"
    assert [d["date"] for d in out["days"]] == ["2026-11-01", "2026-11-02"]
    assert out["days"][0]["flags"] == []
    assert out["days"][1]["flags"] == ["rain", "gusts"]
    third = weather.forecast(Fake(payload), days=14)["days"][2]
    assert third["flags"] == ["snow", "cold"]
    assert third["summary"] == "light rain"


def test_forecast_without_location() -> None:
    out = weather.forecast(Fake({"forecasts": []}))
    assert out["days"] == [] and out["location"] is None
