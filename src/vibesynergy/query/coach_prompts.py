"""The coach routines' prompts, kept as knowledge-base articles.

The knowledge base is the live source: the athlete edits a prompt under Coach → Methods &
rules, every edit is a version in the change log, and routines read it with
``vibesynergy_coach_prompt`` (or ``vibesynergy_kb_get`` by slug). ``docs/coach-prompts/*.md`` is
only the seed. Seeding is idempotent: it adds a missing prompt, updates one that still holds
an earlier seed, and leaves a prompt alone once someone else has edited it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from vibesynergy.query import knowledge

JOBS = (
    "morning",
    "evening",
    "weekly",
    "monthly",
    "block",
    "race",
    "test",
    "thresholds",
    "kb",
    "injury",
    "checkin",
    "insight",
)
TOPIC = knowledge.PROMPT_TOPIC
SEED_AUTHOR = "seed:docs/coach-prompts"
TITLES = {
    "morning": "Routine-opdracht ochtend",
    "evening": "Routine-opdracht avond",
    "weekly": "Routine-opdracht weekreview",
    "monthly": "Routine-opdracht maandrapport",
    "block": "Routine-opdracht blokreview",
    "race": "Routine-opdracht race-brief",
    "test": "Routine-opdracht testevaluatie",
    "thresholds": "Routine-opdracht drempelcheck",
    "kb": "Routine-opdracht onderhoud kennisbank",
    "injury": "Routine-opdracht blessurecheck",
    "checkin": "Routine-opdracht reactie op de check-in",
    "insight": "Routine-opdracht inzicht na training",
}
_LEADING_COMMENT = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)


def seed_dir() -> Path:
    """``docs/coach-prompts`` in the checkout the service runs from."""
    return Path(__file__).resolve().parents[3] / "docs" / "coach-prompts"


def slug_for(job: str) -> str:
    return f"coach-prompt-{_job(job)}"


def overlay_dir(settings: Any) -> Path:
    """An install's own seed prompts: ``<data_dir>/coach-prompts``. The repo holds
    generic seeds; a file here wins, so personal prompts never live in the code."""
    return Path(settings.data_dir) / "coach-prompts"


def seed_text(job: str, directory: Path | None = None, overlay: Path | None = None) -> str | None:
    """The seed file's prompt without its copy note, or None when the file is missing.
    ``directory`` alone when given; else the overlay first, then the repo's seed."""
    folders = [directory] if directory else [overlay, seed_dir()]
    for folder in folders:
        path = folder / f"{_job(job)}.md" if folder else None
        if path is not None and path.is_file():
            body = _LEADING_COMMENT.sub("", path.read_text(encoding="utf-8"), count=1).strip()
            return body or None
    return None


def seed(
    engine: Engine, directory: Path | None = None, overlay: Path | None = None
) -> list[dict[str, Any]]:
    """Put each seed prompt in the knowledge base. Safe to run again.

    ``action`` per job: ``created``, ``updated`` (the article still held an earlier seed),
    ``unchanged``, ``kept`` (edited since; the knowledge base wins) or ``missing`` (no file).
    """
    results = []
    for job in JOBS:
        slug = slug_for(job)
        body = seed_text(job, directory, overlay)
        if body is None:
            results.append({"job": job, "slug": slug, "action": "missing", "version": None})
            continue
        try:
            current = knowledge.get_article(engine, slug)
        except LookupError:
            current = None
        if current is None:
            action = "created"
        elif current["body_md"] == body:
            action = "unchanged"
        elif current["author"] == SEED_AUTHOR:
            action = "updated"
        else:
            action = "kept"
        if action in ("created", "updated"):
            current = knowledge.upsert_article(
                engine,
                slug=slug,
                author=SEED_AUTHOR,
                title=TITLES[job],
                body_md=body,
                topic=TOPIC,
                evidence_grade="C",
                status="active",
            )
        version = current["version"] if current else None
        results.append({"job": job, "slug": slug, "action": action, "version": version})
    return results


def prompt(
    engine: Engine, job: str, directory: Path | None = None, overlay: Path | None = None
) -> dict[str, Any]:
    """The prompt a routine should run: the knowledge-base article, else the seed file."""
    slug = slug_for(job)
    try:
        article = knowledge.get_article(engine, slug)
    except LookupError:
        article = None
    if article is not None and article["status"] == "active":
        return {
            "job": job,
            "slug": slug,
            "title": article["title"],
            "body_md": article["body_md"],
            "version": article["version"],
            "updated_at": article["updated_at"],
            "author": article["author"],
            "source": "knowledge_base",
        }
    body = seed_text(job, directory, overlay)
    if body is None:
        raise LookupError(f"no prompt for {job}")
    return {
        "job": job,
        "slug": slug,
        "title": TITLES[job],
        "body_md": body,
        "version": None,
        "updated_at": None,
        "author": SEED_AUTHOR,
        "source": "seed_file",
    }


def _job(job: str) -> str:
    if job not in JOBS:
        raise ValueError("job must be one of " + ", ".join(JOBS))
    return job
