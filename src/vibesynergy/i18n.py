"""Texts the server writes for people (the Dashboard's verdict and advice, what needs
attention, trends, injury flags, alerts) in English or Dutch.

The same catalogue as the portal (portal/static/i18n/nl.json): the English text is the key,
values go in as {name} placeholders. English is the fallback. The portal passes its
language (``lang``); alerts follow the language chosen in the portal's preferences. The MCP
tools stay in English (they are read by a model, which answers in the user's language).
"""

from __future__ import annotations

import json
import math
import re
from functools import cache
from pathlib import Path
from typing import Any

CATALOGUE = Path(__file__).parent / "portal" / "static" / "i18n" / "nl.json"
LANGUAGES = ("en", "nl")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@cache
def _dutch() -> dict[str, str]:
    try:
        return json.loads(CATALOGUE.read_text())
    except (OSError, ValueError):
        return {}


class Translator:
    def __init__(self, lang: str = "en") -> None:
        self.lang = lang if lang in LANGUAGES else "en"

    def __call__(self, text: str, **values: Any) -> str:
        template = _dutch().get(text, text) if self.lang == "nl" else text
        return _PLACEHOLDER.sub(
            lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template
        )


ENGLISH = Translator("en")


def translator(lang: str | None) -> Translator:
    return Translator(lang or "en")


def negotiate_language(accept_language: str | None) -> str:
    """Pick ``en`` or ``nl`` from an ``Accept-Language`` header; English when absent or unknown."""
    if not accept_language:
        return "en"
    ranked: list[tuple[str, float]] = []
    for part in accept_language.split(","):
        piece = part.strip()
        if not piece:
            continue
        lang_piece, *params = [p.strip() for p in piece.split(";")]
        quality = 1.0
        q_seen = False
        for param in params:
            if not param.lower().startswith("q="):
                continue
            q_seen = True
            try:
                quality = float(param[2:].strip())
            except ValueError:
                quality = -1.0
                break
            if not math.isfinite(quality):
                quality = -1.0
                break
            if quality < 0 or quality > 1:
                quality = -1.0
                break
        if q_seen and quality <= 0:
            continue
        if quality < 0:
            continue
        code = lang_piece.lower().split("-", 1)[0]
        if code in LANGUAGES:
            ranked.append((code, quality))
    if not ranked:
        return "en"
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[0][0]


COOKIE = "vibesynergy-language"


def request_language(request: Any) -> str:
    """Cookie from Settings → Appearance first, then ``Accept-Language``, then English."""
    cookie = ""
    accept = None
    if request is not None:
        cookies = getattr(request, "cookies", None)
        if cookies is not None:
            cookie = cookies.get(COOKIE) or ""
        headers = getattr(request, "headers", None)
        if headers is not None:
            accept = headers.get("accept-language")
    if cookie in LANGUAGES:
        return cookie
    return negotiate_language(accept)


def for_settings(settings: Any) -> Translator:
    """The language chosen in the portal (Settings -> Appearance), for alerts; English when
    it follows the browser, which the server can't know."""
    from vibesynergy.portal import prefs

    return translator(prefs.load(settings.data_dir).get("language"))
