"""Prefixed, time-sortable identifiers such as ``raw_01J8…`` or ``act_01J8…``."""

from ulid import ULID


def new_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"
