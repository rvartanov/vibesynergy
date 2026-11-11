"""How weather is shown: the temperature scale (``[user] temperature``: c, f) and the wind
(``[user] wind``: kmh, ms, kn, bft). Stored and sent values stay SI (°C, m/s); this only
formats them. The portal has the same rules in ``static/js/format.js``.
"""

from __future__ import annotations

TEMPERATURES = ("c", "f")
WINDS = ("kmh", "ms", "kn", "bft")

# Upper bounds (m/s) of Beaufort 0..11; above the last is 12.
BEAUFORT_MS = (0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7)


def default_temperature(units: str) -> str:
    return "f" if units == "imperial" else "c"


def beaufort(ms: float) -> int:
    for force, limit in enumerate(BEAUFORT_MS):
        if ms < limit:
            return force
    return 12


def temperature(celsius: float, unit: str = "c") -> str:
    """'11°C' or '52°F', whole degrees."""
    if unit == "f":
        return f"{round(celsius * 9 / 5 + 32):d}°F"
    return f"{round(celsius):d}°C"


def degrees(celsius: float, unit: str = "c") -> int:
    """The bare number on the chosen scale (for ranges such as '3-9°C')."""
    return round(celsius * 9 / 5 + 32) if unit == "f" else round(celsius)


def wind(ms: float, unit: str = "kmh") -> str:
    """'18 km/h', '5 m/s', '10 kn' or '3 Bft'."""
    if unit == "ms":
        return f"{round(ms):d} m/s"
    if unit == "kn":
        return f"{round(ms * 3600 / 1852):d} kn"
    if unit == "bft":
        return f"{beaufort(ms)} Bft"
    return f"{round(ms * 3.6):d} km/h"
