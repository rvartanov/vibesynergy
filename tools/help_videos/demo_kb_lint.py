"""Put an outdated working-method article in the demo, or take it out again (#120, #137).

The demo's knowledge base is clean, so Coach → Methods & rules shows no check of the texts.
``seed`` adds a working-method article (topic ``coach-loop``) with rules DEMO-1..3 whose
text refers to "DEMO-1..2", names the retired ``SendUserMessage`` and writes a threshold as
"FTP 250 W". Then VibeSynergy's daily fix runs (``knowledge.editorial_sweep``): it fixes the
rule range and the threshold without asking, and the tool of another app stays a warning.
Last, the AI coach proposes a content change, which waits as a diff. ``clear`` removes the
article, its rules, their versions and the proposals for them.

Only for the help-video demo home (``~/vs-help-home``), never for real data. Run with the
demo's XDG variables::

    XDG_CONFIG_HOME=~/vs-help-home/config XDG_DATA_HOME=~/vs-help-home/data \
        uv run python tools/help_videos/demo_kb_lint.py seed|clear
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import delete, or_, select

from vibesynergy.core.config import load_settings
from vibesynergy.query import knowledge
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import (
    kb_article,
    kb_article_revision,
    kb_proposal,
    kb_rule,
    kb_rule_revision,
)

DEMO_HOME = Path.home() / "vs-help-home"
SLUG = "demo-coach-loop"


def _guard() -> None:
    config = os.environ.get("XDG_CONFIG_HOME", "")
    if not config or not Path(config).expanduser().resolve().is_relative_to(DEMO_HOME):
        sys.exit("refused: set XDG_CONFIG_HOME/XDG_DATA_HOME to the demo home (~/vs-help-home)")


BODY = (
    "# Werkwijze (voorbeeld)\n\n"
    "Regels DEMO-1..2 zijn leidend.\n"
    "Lees elke ochtend eerst het herstel en de check-in.\n"
    "Stuur het bericht met SendUserMessage.\n"
    "Rijd de blokken op FTP 250 W.\n"
    "Plan een rustdag na twee zware dagen."
)


def clear(rt: Runtime) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            delete(kb_proposal).where(
                or_(kb_proposal.c.target == SLUG, kb_proposal.c.target.like("DEMO-%"))
            )
        )
        ids = conn.execute(select(kb_article.c.id).where(kb_article.c.slug == SLUG)).scalars()
        for article_id in list(ids):
            rules = conn.execute(
                select(kb_rule.c.id).where(kb_rule.c.article_id == article_id)
            ).scalars()
            rule_ids = list(rules)
            conn.execute(delete(kb_rule_revision).where(kb_rule_revision.c.rule_id.in_(rule_ids)))
            conn.execute(delete(kb_rule).where(kb_rule.c.id.in_(rule_ids)))
            conn.execute(
                delete(kb_article_revision).where(kb_article_revision.c.article_id == article_id)
            )
            conn.execute(delete(kb_article).where(kb_article.c.id == article_id))
        knowledge._rebuild(conn)  # the search index follows


def seed(rt: Runtime) -> None:
    clear(rt)
    knowledge.upsert_article(
        rt.engine,
        slug=SLUG,
        author="user",
        title="Werkwijze (voorbeeld)",
        body_md=BODY,
        topic="coach-loop",
        evidence_grade="C",
        status="active",
        rules=[{"id": f"DEMO-{n}", "text": f"Regel {n}.", "grade": "C"} for n in (1, 2, 3)],
    )
    knowledge.editorial_sweep(rt.engine)  # the daily fix: range and threshold, no approval
    fixed = knowledge.get_article(rt.engine, SLUG)["body_md"]
    knowledge.propose_change(
        rt.engine,
        kind="article",
        target=SLUG,
        rationale="Na twee zware dagen volgt een rustige hersteldag in plaats van een rustdag: zo "
        "blijft het weekvolume gelijk.",
        author="client:coach-runner",
        changes={"body_md": fixed.replace("een rustdag", "een rustige hersteldag")},
    )


def main() -> None:
    _guard()
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in {"seed", "clear"}:
        sys.exit("usage: demo_kb_lint.py seed|clear")
    rt = Runtime.open(load_settings())
    try:
        (seed if action == "seed" else clear)(rt)
    finally:
        rt.close()


if __name__ == "__main__":
    main()
