"""Cost of a model response, from the usage the API returns.

List prices per model (USD per million tokens); Sonnet 5 and Haiku 4.5 since 8-11 for the model
choice in Coach → Settings. The runner adds these up per run
and per calendar month to enforce the monthly cap. Prices are the API list prices; the
console bill is the real figure, this is the runner's own estimate to stop in time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PER_MTOK = {
    "input": 4.00,
    "output": 20.00,
    "cache_write": 5.00,  # 5-minute cache write
    "cache_read": 0.20,
}
BY_MODEL = {
    "claude-opus-5-5": PER_MTOK,
    "claude-sonnet-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5-20251001": {
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
}


def tokens(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """The four counters from an API ``usage`` object (missing ones are 0)."""
    usage = usage or {}

    def count(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "input": count("input_tokens"),
        "output": count("output_tokens"),
        "cache_write": count("cache_creation_input_tokens"),
        "cache_read": count("cache_read_input_tokens"),
    }


def cost_usd(counts: Mapping[str, int], model: str | None = None) -> float:
    prices = BY_MODEL.get(model or "", PER_MTOK)  # unknown: the dearest, to stop in time
    return sum(counts.get(key, 0) * price for key, price in prices.items()) / 1_000_000
