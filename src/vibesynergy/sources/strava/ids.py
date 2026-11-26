"""Strava ids as strings (#87).

From 1 June 2027 Strava activity and athlete ids may need all 64 bits. ``id`` stays a JSON
number, and ``id_str`` carries the same value as a string. Python reads both exactly, but a
browser's ``JSON.parse`` rounds a number above 2^53, so VibeSynergy keeps every Strava id
as a string from the moment it reads it: in storage, the portal's JSON and MCP output.
"""

from __future__ import annotations

from typing import Any

MAX = 2**64  # a Strava id is a positive 64-bit number


def strava_id(obj: Any, key: str = "id") -> str | None:
    """``obj[key + "_str"]`` when it is a plain id, else ``obj[key]`` as a string; None when
    neither is a positive whole number (a float has already lost digits, so it is refused).

    The result is digits only, so it can go into an API path as it is."""
    if not isinstance(obj, dict):
        return None
    for value in (obj.get(f"{key}_str"), obj.get(key)):
        text = _digits(value)
        if text is not None:
            return text
    return None


def same_id(a: Any, b: Any) -> bool:
    """Two ids (stored or received, number or string) name the same Strava object."""
    return a is not None and b is not None and _text(a) == _text(b)


def _text(value: Any) -> str:
    return _digits(value) or str(value)


def _digits(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        text = value.lstrip("0") or "0"
    else:
        return None
    return text if 0 < int(text) < MAX else None
