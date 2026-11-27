"""The daily job that fixes the knowledge-base texts itself (#137).

Once a day VibeSynergy runs ``knowledge.editorial_sweep``: every lint finding with a
mechanical fix (an old article name, the old app name, a rule range from 1 that ends too
early, a fixed threshold on a plain instruction line) is fixed through the same path as an
AI coach's proposal, so the server's check (``knowledge._editorial_check``) decides. What it
fixed shows afterwards on Coach → Methods & rules and as one line under Notices, each with
an undo; nothing is pushed or mailed.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import job

log = logging.getLogger(__name__)

JOB = "kb_editorial"
EVERY = timedelta(days=1)


def due(rt) -> bool:
    """Asked by the scheduler: the last run finished a day ago or longer (or never ran)."""
    try:
        with rt.engine.connect() as conn:
            last = conn.execute(
                select(job.c.finished_at)
                .where(job.c.kind == JOB, job.c.status == "done")
                .order_by(job.c.finished_at.desc())
                .limit(1)
            ).scalar()
    except Exception:
        log.exception("kb_editorial: reading the last run failed")
        return False
    return last is None or utc_now() - last >= EVERY


def run(rt) -> dict[str, Any]:
    from vibesynergy.query import knowledge

    result = knowledge.editorial_sweep(rt.engine)
    if result["applied"]:
        log.info("kb_editorial: fixed %d text(s)", len(result["applied"]))
    return result
