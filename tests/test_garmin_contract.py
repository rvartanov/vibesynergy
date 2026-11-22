"""Contract with the pinned ``garminconnect``: every method VibeSynergy calls exists.

The fake client in ``garmin_fake`` answers any name the real class has, and defines some
methods itself, so a method renamed or dropped in a library upgrade would only fail against
Garmin. This test reads the code instead: every attribute VibeSynergy takes from a Garmin
session (``api.…``, ``self.api.…``, ``source.api.…``, ``self.wrapped.…``) and from its HTTP
client (``api.client.…``) must exist on the pinned classes.
"""

from __future__ import annotations

import ast
from importlib.metadata import version
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "vibesynergy"
PINNED = "0.3.20"
# Modules that hold a Garmin session.
MODULES = [
    *sorted((SRC / "sources" / "garmin").glob("*.py")),
    SRC / "query" / "garmin_strength.py",
    SRC / "query" / "garmin_endurance.py",
    SRC / "query" / "source_removed.py",
]
_SESSION_NAMES = {"api"}
_SESSION_ATTRS = {"api", "wrapped"}  # self.api, source.api, self.wrapped
# Names used through a session that are not Garmin's: VibeSynergy's throttle proxy.
_OWN = {"wrapped"}


def _is_session(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _SESSION_NAMES
    return isinstance(node, ast.Attribute) and node.attr in _SESSION_ATTRS


def _used() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``({garmin attribute: modules}, {client attribute: modules})``."""
    on_garmin: dict[str, set[str]] = {}
    on_client: dict[str, set[str]] = {}
    for path in MODULES:
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            where = str(path.relative_to(SRC))
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "client"
                and _is_session(value.value)
            ):
                on_client.setdefault(node.attr, set()).add(where)
            elif _is_session(value) and node.attr not in _OWN:
                on_garmin.setdefault(node.attr, set()).add(where)
    return on_garmin, on_client


def test_the_pin_is_installed() -> None:
    assert version("garminconnect") == PINNED
    pyproject = (SRC.parents[1] / "pyproject.toml").read_text()
    assert f'"garminconnect=={PINNED}"' in pyproject


def test_every_garmin_method_vibesynergy_calls_exists() -> None:
    from garminconnect import Garmin

    on_garmin, _ = _used()
    # The scan must see the calls; an empty result would pass for the wrong reason.
    for expected in (
        "get_activities_by_date",
        "get_activity_splits",
        "get_activity_step_details",
        "get_activity_rounds",
        "download_activity",
        "upload_workout",
        "schedule_workout",
        "get_heart_rate_zones",
        "connectapi",
        "client",
    ):
        assert expected in on_garmin, expected
    session = Garmin()  # no login: only the attributes set in __init__
    missing = {n: sorted(w) for n, w in on_garmin.items() if not hasattr(session, n)}
    assert not missing, f"not on garminconnect {PINNED} Garmin: {missing}"


def test_every_client_method_vibesynergy_calls_exists() -> None:
    from garminconnect.client import Client

    _, on_client = _used()
    assert {"put", "post", "connectapi", "dumps"} <= set(on_client)
    missing = {n: sorted(w) for n, w in on_client.items() if not hasattr(Client, n)}
    assert not missing, f"not on garminconnect {PINNED} Client: {missing}"


def test_request_timeout_wraps_run_request() -> None:
    """apply_request_timeout replaces Client._run_request(method, path, **kwargs)."""
    import inspect

    from garminconnect import Garmin

    from vibesynergy.sources.garmin.client import apply_request_timeout

    params = list(inspect.signature(Garmin().client._run_request).parameters)
    assert params[:2] == ["method", "path"] and params[-1] == "kwargs"
    session = Garmin()
    seen: dict = {}
    session.client._run_request = lambda method, path, **kwargs: seen.update(kwargs) or "ok"
    apply_request_timeout(session, 42.0)
    assert session.client._run_request("GET", "/x") == "ok" and seen == {"timeout": 42.0}


@pytest.mark.parametrize(
    "method", ["get_activity_step_details", "get_activity_rounds", "get_activity_splits"]
)
def test_activity_detail_methods_take_the_activity_id(method: str) -> None:
    import inspect

    from garminconnect import Garmin

    assert list(inspect.signature(getattr(Garmin, method)).parameters) == ["self", "activity_id"]
