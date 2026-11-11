"""Small helpers shared by tests."""

from __future__ import annotations

from pathlib import Path


def add_public_url(config: Path, url: str = "https://server.example.ts.net") -> None:
    config.write_text(config.read_text() + f'\n[server]\npublic_url = "{url}"\n')


def free_port() -> int:
    """An unused local TCP port, so browser tests on parallel CI runners don't collide."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def midday_timezone(now=None) -> str:
    """A fixed-offset IANA zone where it is about noon right now.

    Browser tests seed data for "today" and then read pages for minutes. Run near local
    midnight, the day flips under them. In a zone where it is about 12:00 there is
    half a day of margin either way. ``Etc/GMT-5`` means UTC+5 (the sign is inverted).
    """
    from datetime import UTC, datetime

    hour = (now or datetime.now(UTC)).astimezone(UTC).hour
    offset = 12 - hour  # -11 … +12, all valid Etc/GMT zones
    if offset == 0:
        return "Etc/GMT"
    return f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"
