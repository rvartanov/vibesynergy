"""Context block ``recent_words``: the athlete's own words of the last days (#170).

The AI coach works as in one uninterrupted session with a moving context: what the athlete
wrote in the last ``[coach_runner] recent_words_days`` days (default 9) is in every advice
run, newest day first (``query.recent_words``).

The block has a fixed budget. When the words do not fit, the newest days (today and
yesterday) stay verbatim and older days are compressed, oldest first and in steps: each text
shortened to a line, then a shorter line, then left out with a count. A complaint (what hurt,
how much, when, the athlete's note on it) always stays verbatim, and no day in the window is
ever left out. A compressed day says so (``samengevat``) and where the full text is. The
compression is deterministic (no model call), so the same words always give the same block
and nothing needs caching.

No text is given twice in one run: the run's own workouts are in block ``activity`` (the
insight), and today's check-in note is only here (the ``safety`` and ``today`` blocks point
to it).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

DEFAULT_DAYS = 9  # Robert, 23-11 (#170): a setting, this is its default
MAX_DAYS = 31
VERBATIM_DAYS = 2  # today and yesterday always go in verbatim
BUDGET = 9000  # characters of JSON for the whole block
# The compression steps for an older day: the longest a text may stay, in characters; 0
# leaves the text out (the day keeps its complaints, its workouts' names and a count).
LEVELS = (240, 120, 60, 0)
HOW = (
    "nieuwste dag eerst; vandaag en gisteren letterlijk. Een dag met samengevat=true is "
    "ingekort (klachten blijven letterlijk); de volledige tekst geven vibesynergy_notes "
    "(start en end = die datum), vibesynergy_injuries en vibesynergy_activity_texts (id)"
)
PRIVATE_IS = "private texts and notes: context only, never quote them in a public text"
INSIGHT_IS = (
    "alleen namen en openbare teksten van de andere workouts van deze dagen: deze taak "
    "schrijft een openbare regel"
)


def days_setting(rt) -> int:
    """How many days the block goes back (``[coach_runner] recent_words_days``)."""
    raw = (getattr(rt.settings, "coach_runner", None) or {}).get("recent_words_days", DEFAULT_DAYS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_DAYS
    return max(1, min(MAX_DAYS, raw))


def _size(data: Any) -> int:
    return len(json.dumps(data, ensure_ascii=False, default=str))


def shorten(text: str | None, limit: int) -> str | None:
    """``text`` on one line, at most ``limit`` characters, cut at a word with " …"."""
    if not text:
        return None
    line = " ".join(text.split())
    if limit <= 0:
        return None
    if len(line) <= limit:
        return line
    cut = line[: max(1, limit - 2)]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:") + " …"


def compress_day(day: dict[str, Any], limit: int) -> dict[str, Any]:
    """One older day with each free text at most ``limit`` characters (0: left out). A
    day with nothing to shorten at this step comes back as it is, unmarked."""
    out: dict[str, Any] = {"date": day["date"], "samengevat": True}
    dropped = 0
    changed = False

    def short(text: Any) -> str | None:
        nonlocal dropped, changed
        if not text:
            return None
        found = shorten(text, limit)
        if found is None:
            dropped += 1
            return None
        if len(" ".join(str(text).split())) > limit:
            changed = True
            return found
        return text  # short enough: verbatim

    if note := short(day.get("checkin_note")):
        out["checkin_note"] = note
    if day.get("complaints"):
        out["complaints"] = day["complaints"]  # verbatim, always
    notes = []
    for n in day.get("notes") or []:
        if text := short(n.get("text")):
            notes.append({**n, "text": text})
    if notes:
        out["notes"] = notes
    workouts = []
    for w in day.get("workouts") or []:
        row = {k: w[k] for k in ("id", "sport", "name", "see") if k in w}
        for field in ("public", "private"):
            if text := short(w.get(field)):
                row[field] = text
        linked = [t for t in (short(t) for t in w.get("notes") or []) if t]
        if linked:
            row["notes"] = linked
        workouts.append(row)
    if workouts:
        out["workouts"] = workouts
    if not changed and not dropped:
        return dict(day)
    if dropped:
        out["left_out"] = dropped
    return out


def _has_words(day: dict[str, Any]) -> bool:
    return len(day) > 1


def fit(head: dict[str, Any], days: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    """``head`` plus ``days`` (newest first) within ``budget`` characters of JSON.

    The first ``VERBATIM_DAYS`` stay as they are. The older days are compressed, oldest
    first, one step of ``LEVELS`` at a time, until the block fits. No day is left out, and
    a complaint is never shortened, so a window full of complaints can stay over budget."""
    out = [dict(d) for d in days]
    block = {**head, "days": out}
    older = [i for i in range(VERBATIM_DAYS, len(days)) if _has_words(days[i])]
    for limit in LEVELS:
        if _size(block) <= budget:
            break
        for i in reversed(older):  # the oldest day first
            out[i] = compress_day(days[i], limit)
            if _size(block) <= budget:
                break
    return block


def _run_groups(rt, cache: dict[str, Any]) -> set[str]:
    """The run's own workouts (the insight's ``activity_ids``), as group ids."""
    from vibesynergy.query import views

    out: set[str] = set()
    for ident in cache.get("activity_ids") or []:
        try:
            out.add(views.resolve_activity(rt.engine, str(ident)))
        except (LookupError, ValueError):
            out.add(str(ident))
    return out


def build(rt, day: date, cache: dict[str, Any]) -> dict[str, Any]:
    from vibesynergy.query import recent_words

    span = days_setting(rt)
    start = day - timedelta(days=span - 1)
    private = cache.get("task") != "insight"
    days = recent_words.gather(rt.engine, start, day, private=private)
    shown = _run_groups(rt, cache) if "activity" in _chain(cache) else set()
    for d in days:
        for w in d.get("workouts") or []:
            if w["id"] in shown:
                # Already in block ``activity`` with its own words: given once.
                for field in ("public", "private", "notes"):
                    w.pop(field, None)
                w["see"] = "activity"
    head: dict[str, Any] = {
        "from": start.isoformat(),
        "to": day.isoformat(),
        "days_back": span,
        "how": HOW,
    }
    if private:
        head["private_is"] = PRIVATE_IS
    else:
        head["only"] = INSIGHT_IS
    return fit(head, days, BUDGET)


def _chain(cache: dict[str, Any]) -> tuple[str, ...]:
    from vibesynergy.coach_runner import registry

    task = str(cache.get("task") or "")
    return registry.chain_for(task) if task in registry.BY_ID else ()


def in_chain(cache: dict[str, Any]) -> bool:
    """Whether this run's chain has the block (then today's check-in note is only here)."""
    return "recent_words" in _chain(cache)
