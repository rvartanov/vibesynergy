"""Quotes fetched from free sources (Wikiquote and similar free APIs, topic and
sources chosen in Settings → Sync).

Providers: Wikiquote (its topic pages, each quote with author and work), Quotable (a free API
with topic tags and the author's dates) and ZenQuotes (free, no topics: a theme is matched on
words, and the line carries "via zenquotes.io" as its terms ask). Only authors who died more
than 70 years ago (public domain; Quotable's bio, else Wikidata) and nothing religious. The
pool lives in ``<data_dir>/quotes/pool.json``; ``refill`` tops a theme up when few unused
quotes are left, at most once every few hours per provider and theme. Every quote is used once
(``publication_plugins``).
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

PROVIDERS = ("wikiquote", "quotable", "zenquotes")
USER_AGENT = "VibeSynergy/1 (personal training archive; info@vibesynergy.nl)"
WIKIQUOTE_API = "https://en.wikiquote.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
QUOTABLE_API = "https://api.quotable.kurokeita.dev/api/quotes"
ZENQUOTES_API = "https://zenquotes.io/api/quotes"
LOW = 10  # unused quotes per theme below which a provider is asked again
RETRY = timedelta(hours=6)
MAX_CHARS = 220
PUBLIC_DOMAIN_YEARS = 70

# Our themes on each source; a missing one is not asked there. Quotable's tags are
# case-sensitive ("Perseverance", not "perseverance").
WIKIQUOTE_PAGES = {
    "nature": "Nature",
    "adventure": "Adventure",
    "curiosity": "Curiosity",
    "science": "Science",
    "creativity": "Creativity",
    "music": "Music",
    "literature": "Literature",
    "philosophy": "Philosophy",
    "humour": "Humor",
    "friendship": "Friendship",
    "kindness": "Kindness",
    "courage": "Courage",
    "patience": "Patience",
    "perseverance": "Perseverance",
    "balance": "Moderation",
    "change": "Change",
    "freedom": "Freedom",
    "simplicity": "Simplicity",
    "gratitude": "Gratitude",
    "time": "Time",
    "sport": "Sports",
}
QUOTABLE_TAGS = {
    "nature": "Nature",
    "science": "Science",
    "creativity": "Creativity",
    "literature": "Literature",
    "philosophy": "Philosophy",
    "humour": "Humorous",
    "friendship": "Friendship",
    "courage": "Courage",
    "perseverance": "Perseverance",
    "change": "Change",
    "freedom": "Freedom",
    "gratitude": "Gratitude",
    "time": "Time",
    "sport": "Sports",
    "curiosity": "Knowledge",
    "kindness": "Generosity",
}
# ZenQuotes has no topics: a quote joins the first theme one of its words points to.
THEME_WORDS = {
    "nature": ("nature", "tree", "forest", "mountain", "river", "sea", "earth", "sky"),
    "courage": ("courage", "brave", "fear", "dare"),
    "perseverance": ("persever", "persist", "never give up", "keep going", "effort"),
    "patience": ("patien", "wait"),
    "friendship": ("friend",),
    "kindness": ("kind",),
    "time": ("time", "today", "tomorrow", "moment"),
    "change": ("change",),
    "freedom": ("free",),
    "simplicity": ("simpl",),
    "gratitude": ("grateful", "gratitude", "thank"),
    "science": ("science", "knowledge"),
    "creativity": ("creat", "imagination"),
    "sport": ("run", "race", "win", "athlete", "game"),
}
RELIGIOUS = re.compile(
    r"\b(god|gods|lord|jesus|christ|bible|allah|prayer|pray|heaven|hell|church|faith|"
    r"divine|holy|sin|saint|scripture|psalm|gospel|prophet|buddha|religion|spiritual)\b",
    re.IGNORECASE,
)
RELIGIOUS_TAGS = {"religion", "faith", "spirituality"}


def _pool_path(data_dir: Path) -> Path:
    return Path(data_dir) / "quotes" / "pool.json"


def load(data_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_pool_path(data_dir).read_text())
    except (OSError, ValueError):
        data = {}
    return {
        "quotes": data.get("quotes") or {},
        "authors": data.get("authors") or {},
        "fetched": data.get("fetched") or {},
    }


def save(data_dir: Path, pool: dict[str, Any]) -> None:
    path = _pool_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pool, ensure_ascii=False, indent=1))
    tmp.replace(path)


def quote_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _get(url: str, params: dict[str, Any] | None = None, *, http=requests.get) -> Any:
    response = http(url, params=params, timeout=20, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return response.json()


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", text)  # [[link|label]] → label
    text = re.sub(r"\[https?://\S+ ([^\]]+)\]", r"\1", text)  # [url label] → label
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    return html.unescape(" ".join(text.split())).strip()


def _acceptable(text: str) -> bool:
    return (
        20 <= len(text) <= MAX_CHARS
        and not RELIGIOUS.search(text)
        and "..." not in text
        and "…" not in text
        and not text.startswith(("(", "["))
    )


# Sources ------------------------------------------------------------------------------------


def wikiquote(theme: str, *, http=requests.get) -> list[dict[str, Any]]:
    """The quotes on a Wikiquote topic page: text, author (with its page) and work."""
    page = WIKIQUOTE_PAGES.get(theme)
    if not page:
        return []
    data = _get(
        WIKIQUOTE_API,
        {"action": "parse", "page": page, "prop": "wikitext", "format": "json", "formatversion": 2},
        http=http,
    )
    wikitext = (data.get("parse") or {}).get("wikitext") or ""
    out = []
    lines = wikitext.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("*") or line.startswith("**"):
            continue
        attribution = lines[i + 1] if i + 1 < len(lines) and lines[i + 1].startswith("**") else ""
        author = re.search(r"\[\[(?:w:)?([^|\]]+)(?:\|([^\]]+))?\]\]", attribution)
        if not author:
            continue
        name = _clean(author.group(2) or author.group(1))
        work = re.search(r"''([^']+)''", attribution)
        text = _clean(line.lstrip("* "))
        if not _acceptable(text) or RELIGIOUS.search(attribution):
            continue
        out.append(
            {
                "text": text,
                "author": name,
                "work": _clean(work.group(1)) if work else "",
                "url": f"https://en.wikiquote.org/wiki/{page}",
                "provider": "wikiquote",
                "theme": theme,
            }
        )
    return out


def quotable(theme: str, *, http=requests.get) -> list[dict[str, Any]]:
    tag = QUOTABLE_TAGS.get(theme)
    if not tag:
        return []
    data = _get(QUOTABLE_API, {"tags": tag, "limit": 100}, http=http)
    rows = data.get("data") or data.get("results") or (data if isinstance(data, list) else [])
    out = []
    for row in rows:
        tags = {str(t.get("name", t)).lower() for t in row.get("tags") or []}
        author = row.get("author") or {}
        text = _clean(str(row.get("content") or ""))
        if tags & RELIGIOUS_TAGS or not _acceptable(text):
            continue
        out.append(
            {
                "text": text,
                "author": str(author.get("name") or row.get("authorName") or ""),
                "work": "",
                "url": str(author.get("link") or "https://github.com/lukePeavey/quotable"),
                "provider": "quotable",
                "theme": theme,
                "died": _died_from_bio(str(author.get("bio") or "")),
            }
        )
    return out


def zenquotes(*, http=requests.get) -> list[dict[str, Any]]:
    out = []
    for row in _get(ZENQUOTES_API, http=http) or []:
        text = _clean(str(row.get("q") or ""))
        if not _acceptable(text):
            continue
        low = text.lower()
        theme = next((t for t, words in THEME_WORDS.items() if any(w in low for w in words)), None)
        if theme:
            out.append(
                {
                    "text": text,
                    "author": str(row.get("a") or ""),
                    "work": "via zenquotes.io",
                    "url": "https://zenquotes.io/",
                    "provider": "zenquotes",
                    "theme": theme,
                }
            )
    return out


# Public domain ---------------------------------------------------------------------------


def _died_from_bio(bio: str) -> int | None:
    """Quotable's bio starts "Name (14 March 1879 - 18 April 1955) …": the second year."""
    match = re.search(r"\(([^)]*\d{3,4}[^)]*)\)", bio)
    years = re.findall(r"\b(\d{3,4})\b", match.group(1)) if match else []
    return int(years[1]) if len(years) >= 2 else None


def died(name: str, authors: dict[str, Any], *, http=requests.get) -> int | None:
    """The author's year of death from Wikidata (cached in the pool); None when unknown or
    alive."""
    if name in authors:
        return authors[name]
    year = None
    try:
        found = _get(
            WIKIDATA_API,
            {"action": "wbsearchentities", "search": name, "language": "en", "format": "json",
             "limit": 1, "type": "item"},
            http=http,
        )  # fmt: skip
        hits = found.get("search") or []
        if hits:
            entity = hits[0]["id"]
            data = _get(
                WIKIDATA_API,
                {"action": "wbgetentities", "ids": entity, "props": "claims", "format": "json"},
                http=http,
            )
            claims = data["entities"][entity].get("claims") or {}
            for claim in claims.get("P570") or []:
                value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
                match = re.match(r"[+-](\d{1,4})-", str(value.get("time") or ""))
                if match:
                    year = int(match.group(1))
                    break
    except (requests.RequestException, ValueError, KeyError):
        return None  # not cached: asked again next time
    authors[name] = year
    return year


def public_domain(year: int | None, today: date) -> bool:
    return year is not None and year <= today.year - PUBLIC_DOMAIN_YEARS - 1


# The pool -----------------------------------------------------------------------------------


def as_quotes(pool: dict[str, Any]) -> list[dict[str, Any]]:
    """The pool's quotes in the shape of ``publication_plugins.QUOTES``."""
    return [
        {
            "collection": q["provider"],
            "theme": q["theme"],
            "text": q["text"],
            "author": q["author"],
            "work": q.get("work") or "",
            "url": q["url"],
        }
        for q in pool["quotes"].values()
    ]


def _unused(pool: dict[str, Any], provider: str, theme: str, used: set[str]) -> int:
    return sum(
        1
        for ident, q in pool["quotes"].items()
        if q["provider"] == provider and q["theme"] == theme and ident not in used
    )


def due(data_dir: Path, themes: list[str], providers: list[str], used: set[str],
        now: datetime | None = None) -> list[tuple[str, str]]:  # fmt: skip
    """(provider, theme) pairs that are low and were not asked recently."""
    now = now or datetime.now(UTC)
    pool = load(data_dir)
    out = []
    for provider in providers:
        if provider not in PROVIDERS:
            continue
        for theme in themes:
            key = f"{provider}:{theme if provider != 'zenquotes' else '*'}"
            last = pool["fetched"].get(key)
            if last and now - datetime.fromisoformat(last) < RETRY:
                continue
            if _unused(pool, provider, theme, used) < LOW:
                out.append((provider, theme))
    return out


def refill(data_dir: Path, themes: list[str], providers: list[str], used: set[str],
           today: date, *, http=requests.get, now: datetime | None = None) -> int:  # fmt: skip
    """Ask the low (provider, theme) pairs for new quotes; returns how many were added."""
    now = now or datetime.now(UTC)
    pool = load(data_dir)
    added = 0
    asked_zen = False
    for provider, theme in due(data_dir, themes, providers, used, now):
        key = f"{provider}:{theme if provider != 'zenquotes' else '*'}"
        if provider == "zenquotes" and asked_zen:
            continue
        try:
            if provider == "wikiquote":
                found = wikiquote(theme, http=http)
            elif provider == "quotable":
                found = quotable(theme, http=http)
            else:
                found = zenquotes(http=http)
                asked_zen = True
        except (requests.RequestException, ValueError) as exc:
            log.warning("quotes: %s %s failed (%s)", provider, theme, exc)
            pool["fetched"][key] = now.isoformat()
            continue
        pool["fetched"][key] = now.isoformat()
        for q in found:
            ident = quote_id(q["text"])
            if ident in pool["quotes"] or not q["author"] or RELIGIOUS.search(q["author"]):
                continue
            year = q.pop("died", None)
            if year is None:
                year = died(q["author"], pool["authors"], http=http)
            if not public_domain(year, today):
                continue
            pool["quotes"][ident] = {**q, "died": year, "fetched_at": now.isoformat()}
            added += 1
    save(data_dir, pool)
    return added


# The service --------------------------------------------------------------------------------
JOB = "quote_refill"


def _wanted(rt) -> tuple[list[str], list[str]] | None:
    from vibesynergy.query import publication_plugins as plugins

    pub = rt.settings.publication or {}
    if not pub.get("quote_enabled", True):
        return None
    themes = list(pub.get("quote_themes", list(plugins.THEMES)))
    providers = [p for p in pub.get("quote_providers", list(plugins.PROVIDERS)) if p in PROVIDERS]
    return (themes, providers) if themes and providers else None


def due_service(rt) -> bool:
    """Asked by the scheduler: a chosen theme is low on a chosen source."""
    from vibesynergy.query import publication_plugins as plugins

    wanted = _wanted(rt)
    if wanted is None:
        return False
    try:
        return bool(due(rt.settings.data_dir, *wanted, plugins.used_quote_ids(rt)))
    except Exception:
        log.exception("quotes: checking the pool failed")
        return False


def run(rt) -> dict[str, Any]:
    from vibesynergy.core.timeutil import today_in
    from vibesynergy.query import publication_plugins as plugins

    wanted = _wanted(rt)
    if wanted is None:
        return {"skipped": "quotes off"}
    added = refill(
        rt.settings.data_dir,
        *wanted,
        plugins.used_quote_ids(rt),
        today_in(rt.settings.timezone),
    )
    return {"added": added}
