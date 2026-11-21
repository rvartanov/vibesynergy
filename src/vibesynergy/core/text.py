"""Plain-text helpers for texts people read in a notice, a push or a mail subject."""

from __future__ import annotations

import re

ELLIPSIS = "…"
# The end of a sentence: . ! or ? followed by a space.
_SENTENCE_END = re.compile(r"[.!?](?=\s)")


def clip(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` characters, cut cleanly (#138): after the last whole
    sentence that fits when that keeps at least half the room, else after the last whole
    word that fits, and then "…". A text that fits comes back whole (whitespace folded).
    Only a single word longer than ``limit`` is cut inside."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    # Room for the text before " …" (sentence) or "…" (word).
    ends = [m.end() for m in _SENTENCE_END.finditer(text) if m.end() <= limit - 2]
    if ends and ends[-1] >= limit // 2:
        return f"{text[: ends[-1]]} {ELLIPSIS}"
    cut = text.rfind(" ", 0, limit)  # the space right after the last word that fits
    if cut <= 0:
        return text[: limit - 1] + ELLIPSIS
    return text[:cut].rstrip(" ,;:\u2013\u2014-") + ELLIPSIS
