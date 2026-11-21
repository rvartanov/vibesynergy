"""The workout method: every coaching value of the workout library, from the knowledge base.

Robert, 21-11-2026: "Dit soort dingen moeten überhaupt niet in de code vastzitten, maar uit
een methode/instructie komen" (#142). The library's code knows the structure of a workout
(which steps, repeats, how it is rendered for intervals.icu, Zwift and Garmin); the values
(cadence bands, cues, % FTP, zones, warm-up and cool-down minutes, distances, rests,
exercises, names and descriptions) are in the knowledge-base article ``workout-method``.

The article holds one fenced ``toml`` block, one table per template (see
``docs/workout-method.md`` for the form). It is read each time a workout is built, so an
approved change applies to every new workout at once: no code, no deploy. An AI coach
changes a value with ``vibesynergy_kb_propose_change``; the athlete approves it.

The seed is ``docs/workout-method.md`` (packaged into the wheel as ``_docs/``). Migration 0058
copies it into the knowledge base once. It is also the fallback: a value the article lacks or
holds in the wrong form comes from the seed, and ``fallback`` names it (the render's
``method`` stamp, ``vibesynergy_explain`` with metric ``workout:<template>``, and a lint
warning on the article).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

SLUG = "workout-method"
TOPIC = "workout-method"
TITLE = "Trainingsmethode: waarden van de workoutbibliotheek"
SEED_DOC = "docs/workout-method.md"
SEED_AUTHOR = "seed:" + SEED_DOC
PACKAGED_DOC = Path(__file__).resolve().parents[1] / "_docs" / "workout-method.md"
LINT_CHECK = "workout_value"

_BLOCK = re.compile(r"^```toml[ \t]*\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)
_LEADING_COMMENT = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)

TARGETS = ("ftp_pct", "lthr_pct", "hr_zone", "pace_zone")
# The workout model's name of each target (workout_model: pct_ftp, pct_lthr, hr_zone, pace_zone).
MODEL_TARGET = {
    "ftp_pct": "pct_ftp",
    "lthr_pct": "pct_lthr",
    "hr_zone": "hr_zone",
    "pace_zone": "pace_zone",
}
LANGS = ("nl", "en")
LOADS = ("easy", "moderate", "hard")


# --- the form of the block ------------------------------------------------------------------


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _bounds(value: Any) -> bool:
    return isinstance(value, list) and 1 <= len(value) <= 2 and all(_number(v) for v in value)


def _cadence(value: Any) -> bool:
    return isinstance(value, list) and len(value) <= 2 and all(_number(v) for v in value)


def _text(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(value.get(lang), str) for lang in LANGS)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _count_or(param: str):
    def check(value: Any) -> bool:
        return value == param or (isinstance(value, int) and not isinstance(value, bool))

    return check


STEP_FIELDS = {
    "minutes": _number,
    "seconds": _number,
    "metres": _number,
    "min_minutes": _number,
    **dict.fromkeys(TARGETS, _bounds),
    "target": lambda v: v in TARGETS,
    "cadence": _cadence,
    "cue": lambda v: isinstance(v, str),
    "cue_repeat": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
}
EXERCISE_FIELDS = {
    "code": lambda v: isinstance(v, str) and bool(v.strip()),
    "sets": _count_or("sets"),
    "reps": _count_or("reps"),
    "reps_max": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1,
    "time": lambda v: isinstance(v, str) and bool(v.strip()),
    "side": lambda v: isinstance(v, str) and bool(v.strip()),
    "rest_s": _number,
    "label": lambda v: isinstance(v, str),
}
TEMPLATE_FIELDS = {
    "name": _text,
    "purpose": _text,
    "cue": _text,
    "phases": _strings,
    "load": lambda v: v in LOADS,
    "params": lambda v: isinstance(v, dict) and all(_number(x) for x in v.values()),
    "easier": lambda v: isinstance(v, str),
    "kb_rules": _strings,
    # steps and exercises are checked per item
}


def _exercises_ok(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict) or set(item) - set(EXERCISE_FIELDS):
            return False
        if not all(EXERCISE_FIELDS[k](v) for k, v in item.items()):
            return False
        if "code" not in item or "sets" not in item or ("reps" in item) == ("time" in item):
            return False
    return True


def parse(body_md: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    """The block's tables, or ``None`` with what is wrong (no block, not TOML)."""
    found = _BLOCK.search(body_md or "")
    if found is None:
        return None, ["the article has no ```toml block"]
    try:
        return tomllib.loads(found.group(1)), []
    except tomllib.TOMLDecodeError as exc:
        return None, [f"the toml block does not read: {exc}"]


# --- the seed --------------------------------------------------------------------------------


def seed_path() -> Path | None:
    """The seed the wheel carries; in a checkout (an editable install, the tests) the
    repository's own file."""
    if PACKAGED_DOC.is_file():
        return PACKAGED_DOC
    for parent in Path(__file__).resolve().parents:
        candidate = parent / SEED_DOC
        if candidate.is_file():
            return candidate
    return None


def seed_body() -> str | None:
    """The seed article's text, without its copy note."""
    path = seed_path()
    if path is None:
        return None
    return _LEADING_COMMENT.sub("", path.read_text(encoding="utf-8"), count=1).strip() or None


@cache
def _seed_tables() -> dict[str, Any]:
    data, _errors = parse(seed_body())
    return data or {}


def seed_tables() -> dict[str, Any]:
    """The seed's tables (a copy, so a caller cannot change the cached one)."""
    import copy

    return copy.deepcopy(_seed_tables())


# --- the method in use -----------------------------------------------------------------------


@dataclass
class Method:
    """The values every template reads, with where they came from."""

    templates: dict[str, dict[str, Any]]
    source: str  # knowledge_base | seed
    version: int | None  # the article's version; None when the seed is used
    fallback: dict[str, list[str]] = field(default_factory=dict)  # template -> seed keys
    problems: list[str] = field(default_factory=list)  # what the article got wrong

    def values(self, template_id: str) -> dict[str, Any]:
        return self.templates.get(template_id) or {}

    def stamp(self, template_id: str) -> dict[str, Any]:
        """What a rendered workout records: which method version built it."""
        return {
            "slug": SLUG,
            "version": self.version,
            "source": self.source,
            "fallback": list(self.fallback.get(template_id, [])),
        }


def _merge_steps(
    tid: str, mine: Any, seed: dict[str, Any], fallback: list[str], problems: list[str]
) -> dict[str, Any]:
    if mine is None:
        mine = {}
    if not isinstance(mine, dict):
        problems.append(f"{tid}.steps is not a table")
        fallback.append("steps")
        return dict(seed)
    out: dict[str, Any] = {}
    for name in sorted(set(seed) | set(mine), key=lambda n: (n not in seed, n)):
        base = seed.get(name)
        given = mine.get(name)
        if given is None:
            if base is not None:
                fallback.append(f"steps.{name}")
                out[name] = dict(base)
            continue
        if not isinstance(given, dict):
            problems.append(f"{tid}.steps.{name} is not a table")
            fallback.append(f"steps.{name}")
            out[name] = dict(base or {})
            continue
        if base is None:
            problems.append(f"{tid}.steps.{name} is not a step of this template")
            continue
        step: dict[str, Any] = {}
        for key in list(base) + [k for k in given if k not in base]:
            if key not in STEP_FIELDS:
                problems.append(f"{tid}.steps.{name}.{key} is not a step value")
                continue
            if key in given:
                if STEP_FIELDS[key](given[key]):
                    step[key] = given[key]
                    continue
                problems.append(f"{tid}.steps.{name}.{key} has the wrong form")
            if key in base:
                fallback.append(f"steps.{name}.{key}")
                step[key] = base[key]
        out[name] = step
    return out


def merge(data: dict[str, Any] | None, seed: dict[str, Any]) -> tuple[dict, dict, list[str]]:
    """The article's tables over the seed's: (templates, fallback per template, problems)."""
    data = data or {}
    templates: dict[str, dict[str, Any]] = {}
    fallbacks: dict[str, list[str]] = {}
    problems: list[str] = [f"{tid} is not a template" for tid in data if tid not in seed]
    for tid, base in seed.items():
        mine = data.get(tid)
        fallback: list[str] = []
        if not isinstance(mine, dict):
            if tid in data:
                problems.append(f"{tid} is not a table")
            templates[tid] = dict(base)
            fallbacks[tid] = ["*"]
            continue
        out: dict[str, Any] = {}
        for key in list(base) + [k for k in mine if k not in base]:
            if key == "steps":
                out["steps"] = _merge_steps(
                    tid, mine.get("steps"), base.get("steps") or {}, fallback, problems
                )
                continue
            check = _exercises_ok if key == "exercises" else TEMPLATE_FIELDS.get(key)
            if check is None:
                problems.append(f"{tid}.{key} is not a template value")
                continue
            if key in mine:
                if check(mine[key]):
                    out[key] = mine[key]
                    continue
                problems.append(f"{tid}.{key} has the wrong form")
            if key in base:
                fallback.append(key)
                out[key] = base[key]
        if "params" in out and set(out["params"]) != set(base.get("params") or {}):
            problems.append(f"{tid}.params must name {', '.join(base.get('params') or {})}")
            fallback.append("params")
            out["params"] = base.get("params") or {}
        templates[tid] = out
        if fallback:
            fallbacks[tid] = fallback
    return templates, fallbacks, problems


def from_body(body_md: str | None, *, version: int | None) -> Method:
    """The method an article text gives, over the seed."""
    seed = _seed_tables()
    data, problems = parse(body_md)
    templates, fallbacks, more = merge(data, seed)
    if data is None:
        fallbacks = {tid: ["*"] for tid in seed}
    return Method(templates, "knowledge_base", version, fallbacks, problems + more)


def seed_method() -> Method:
    templates, _f, _p = merge(_seed_tables(), _seed_tables())
    return Method(templates, "seed", None)


def article(engine: Engine | None) -> dict[str, Any] | None:
    """The active ``workout-method`` article (slug, version, body), or None."""
    if engine is None:
        return None
    from vibesynergy.store.schema import kb_article

    with engine.connect() as conn:
        row = conn.execute(select(kb_article).where(kb_article.c.slug == SLUG)).mappings().first()
    if row is None or row["status"] != "active":
        return None
    return dict(row)


def load(engine: Engine | None) -> Method:
    """The method in use now: the active article over the seed, else the seed."""
    found = article(engine)
    if found is None:
        return seed_method()
    return from_body(found["body_md"], version=found["version"])


def versions(engine: Engine | None) -> list[Method]:
    """Every stored version of the article, newest first, then the seed: what a workout in
    the plan may have been built from."""
    out: list[Method] = []
    if engine is not None:
        from vibesynergy.store.schema import kb_article, kb_article_revision

        with engine.connect() as conn:
            rows = conn.execute(
                select(kb_article_revision.c.version, kb_article_revision.c.body_md)
                .join(kb_article, kb_article.c.id == kb_article_revision.c.article_id)
                .where(kb_article.c.slug == SLUG)
                .order_by(kb_article_revision.c.version.desc())
            ).all()
        out = [from_body(r.body_md, version=r.version) for r in rows]
    out.append(seed_method())
    return out


# --- lint and explain ------------------------------------------------------------------------


def lint_body(body_md: str | None) -> list[str]:
    """The values the article lacks, holds in the wrong form or does not know, as dotted keys
    (``ride_endurance.steps.work.cadence``); the library takes the first two from the seed.
    ``toml`` when the block is missing or does not read."""
    data, _errors = parse(body_md)
    if data is None:
        return ["toml"]
    method = from_body(body_md, version=None)
    out: list[str] = []
    for tid, keys in method.fallback.items():
        out += [tid if key == "*" else f"{tid}.{key}" for key in keys]
    out += [problem.split(" ", 1)[0] for problem in method.problems]
    return list(dict.fromkeys(out))


def explain(engine: Engine | None, template_id: str | None = None) -> dict[str, Any]:
    """Where the workout values come from: the article (or the seed), its version, and per
    template the values in use and the ones taken from the seed."""
    method = load(engine)
    ids = [template_id] if template_id else list(method.templates)
    unknown = [tid for tid in ids if tid not in method.templates]
    if unknown:
        raise ValueError(f"unknown template {unknown[0]!r}; see vibesynergy_workout_library")
    return {
        "article": SLUG,
        "source": method.source,
        "version": method.version,
        "how_to_change": (
            "Propose the whole new article text with vibesynergy_kb_propose_change (kind "
            f"article, target {SLUG}); the athlete approves it. New workouts use it at once; "
            "planned ones change with vibesynergy_workout_rerender."
        ),
        "problems": method.problems,
        "templates": {
            tid: {"values": method.values(tid), "from_seed": method.fallback.get(tid, [])}
            for tid in ids
        },
    }


# --- the step a template asks for ------------------------------------------------------------


class Values:
    """One template's values, as the library's structure code reads them."""

    def __init__(self, template_id: str, values: dict[str, Any]) -> None:
        self.id = template_id
        self.data = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def text(self, key: str, lang: str) -> str:
        found = self.data.get(key) or {}
        return found.get(lang, "") if isinstance(found, dict) else ""

    def entry(self, name: str) -> dict[str, Any]:
        return (self.data.get("steps") or {}).get(name) or {}

    def minutes(self, name: str) -> float:
        """A step's length in minutes (``minutes`` or ``seconds``). The method must give it."""
        entry = self.entry(name)
        if "minutes" in entry:
            return entry["minutes"]
        if "seconds" in entry:
            return entry["seconds"] / 60
        raise ValueError(f"the workout method gives no length for {self.id}.steps.{name}")

    def floor(self, name: str) -> float:
        """The shortest a step may be, in minutes (``min_minutes``; none is no floor)."""
        return self.entry(name).get("min_minutes", 0)

    def metres(self, name: str) -> int:
        entry = self.entry(name)
        if "metres" not in entry:
            raise ValueError(f"the workout method gives no distance for {self.id}.steps.{name}")
        return int(entry["metres"])

    def target(self, name: str, bounds: tuple[float, float] | None = None) -> dict | None:
        """The step's target in the workout model's form; ``bounds`` (from parameters) with
        the kind of target the method names in ``target``."""
        entry = self.entry(name)
        if bounds is not None:
            kind = entry.get("target") or next((k for k in TARGETS if k in entry), None)
            if kind is None:
                return None
            return {"type": MODEL_TARGET[kind], "low": bounds[0], "high": bounds[1]}
        for kind in TARGETS:
            if kind in entry:
                low, *rest = entry[kind]
                return {"type": MODEL_TARGET[kind], "low": low, "high": rest[0] if rest else low}
        return None

    def step(
        self,
        name: str,
        kind: str | None = None,
        *,
        minutes: float | None = None,
        metres: int | None = None,
        bounds: tuple[float, float] | None = None,
        cue: str | None = None,
    ) -> dict[str, Any]:
        """One step: its length from the structure (``minutes``/``metres``) or the method,
        the target, cadence and cue from the method."""
        entry = self.entry(name)
        step: dict[str, Any] = {"kind": kind or name}
        if metres is not None:
            step["distance_m"] = metres
        elif minutes is not None:
            step["duration_s"] = round(minutes * 60)
        elif "metres" in entry:
            step["distance_m"] = int(entry["metres"])
        else:
            step["duration_s"] = round(self.minutes(name) * 60)
        target = self.target(name, bounds)
        if target:
            step["target"] = target
        if entry.get("cadence"):
            step["cadence"] = list(entry["cadence"])
        text = cue if cue is not None else entry.get("cue")
        if text:
            step["cue"] = text
        return step
