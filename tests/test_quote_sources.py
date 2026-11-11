"""Quotes from Wikiquote, Quotable and ZenQuotes, with fake responses."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from vibesynergy.query import publication_plugins, quote_sources

WIKITEXT = """intro
* Victory belongs to the most persevering.
** [[Napoleon I of France]], as quoted in ''Treasury of Thought'' (1884)
* Pray without ceasing, for God hears.
** [[Somebody Holy]], ''Sermons''
* A modern line about grit and stubborn effort.
** [[Living Author]], ''Blog''
"""


class _Reply:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


def fake_http(url, params=None, timeout=None, headers=None):
    if "wikiquote" in url:
        return _Reply({"parse": {"wikitext": WIKITEXT}})
    if "quotable" in url:
        return _Reply(
            {
                "data": [
                    {
                        "content": "Results! Why, man, I have gotten a lot of results.",
                        "tags": [{"name": "Perseverance"}],
                        "author": {"name": "Thomas Edison", "bio": "Thomas (1847 - 1931) ..."},
                    },
                    {
                        "content": "Faith moves the mountains of the heart, always.",
                        "tags": [{"name": "Faith"}],
                        "author": {"name": "Old Saint", "bio": "(1100 - 1180)"},
                    },
                ]
            }
        )
    if "zenquotes" in url:
        return _Reply([{"q": "Never give up on the long run you love.", "a": "Old Runner"}])
    if params and params.get("action") == "wbsearchentities":
        return _Reply({"search": [{"id": "Q1"}]})
    if params and params.get("action") == "wbgetentities":
        year = 1821
        return _Reply(
            {
                "entities": {
                    "Q1": {
                        "claims": {
                            "P570": [
                                {"mainsnak": {"datavalue": {"value": {"time": f"+{year}-05-05"}}}}
                            ]
                        }
                    }
                }
            }
        )
    raise AssertionError(url)


def test_wikiquote_page_is_parsed_and_filtered() -> None:
    rows = quote_sources.wikiquote("perseverance", http=fake_http)
    texts = [r["text"] for r in rows]
    assert "Victory belongs to the most persevering." in texts
    assert not any("God" in t for t in texts)  # nothing religious
    row = rows[0]
    assert row["author"] == "Napoleon I of France" and row["work"] == "Treasury of Thought"


def test_refill_keeps_public_domain_and_tops_up_low_themes(tmp_path) -> None:
    today = date(2026, 11, 9)
    now = datetime(2026, 11, 9, 8, tzinfo=UTC)
    added = quote_sources.refill(
        tmp_path, ["perseverance"], ["wikiquote", "quotable", "zenquotes"], set(), today,
        http=fake_http, now=now,
    )  # fmt: skip
    pool = quote_sources.load(tmp_path)
    authors = {q["author"] for q in pool["quotes"].values()}
    assert "Thomas Edison" in authors and "Old Saint" not in authors
    assert added == len(pool["quotes"]) >= 2
    # Asked recently: not again within the retry window, even when still low.
    assert quote_sources.due(tmp_path, ["perseverance"], ["wikiquote"], set(), now) == []
    later = now + timedelta(hours=7)
    assert quote_sources.due(tmp_path, ["perseverance"], ["wikiquote"], set(), later)


def test_fetched_quotes_are_used_once_and_zen_is_named(runtime, monkeypatch) -> None:
    pool = {
        "quotes": {
            quote_sources.quote_id("Never give up on the long run you love."): {
                "text": "Never give up on the long run you love.",
                "author": "Old Runner",
                "work": "via zenquotes.io",
                "url": "https://zenquotes.io/",
                "provider": "zenquotes",
                "theme": "perseverance",
            }
        },
        "authors": {},
        "fetched": {},
    }
    quote_sources.save(runtime.settings.data_dir, pool)
    line, provenance = publication_plugins._quote(
        runtime, "a-1", "en", ["perseverance"], [], ["zenquotes"]
    )
    assert line.endswith("— Old Runner (via zenquotes.io)")
    assert provenance["collection"] == "zenquotes"
    publication_plugins._save(runtime, "a-1", "quote", line, provenance, "t")
    left = publication_plugins.quotes_left(runtime, ["perseverance"], [], ["zenquotes"])
    assert left["zenquotes"] == 0
    assert (
        publication_plugins._quote(runtime, "a-2", "en", ["perseverance"], [], ["zenquotes"])
        is None
    )
