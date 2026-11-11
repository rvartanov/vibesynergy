"""Worker lane threads started by ``vibesynergy serve``; exposed on /health."""

from __future__ import annotations

import threading

_lanes: dict[str, threading.Thread] = {}


def register(name: str, thread: threading.Thread) -> None:
    _lanes[name] = thread


def status() -> dict[str, bool]:
    return {name: thread.is_alive() for name, thread in sorted(_lanes.items())}


def all_alive() -> bool:
    return all(thread.is_alive() for thread in _lanes.values())
