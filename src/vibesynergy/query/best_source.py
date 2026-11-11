"""Best source per metric: which sources deliver each daily metric, the order in use, which
source wins now, and the order the athlete saved for one metric (Settings → System).

A saved order lives in one small JSON file in the data folder (``best-source.json``), so it
applies at the next derive without a restart. It beats the ``[best_source]`` prefix rules in
the config, which in turn beat the built-in defaults (``derive/unify.py``).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from vibesynergy.derive import unify
from vibesynergy.store.schema import daily_metric, daily_unified

FILE = "best-source.json"
METRIC = re.compile(r"[a-z0-9][a-z0-9_.]{0,99}")
SOURCE = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_SOURCES = 16

# Settings groups, in page order; a metric belongs to the first group whose domain matches.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sleep", ("sleep",)),
    ("hrv", ("hrv",)),
    ("body", ("body", "bp")),
    ("heart", ("hr", "spo2", "respiration", "stress")),
    ("activity", ("daily", "fitness", "training", "steps", "activity")),
)
OTHER = "other"


def group_of(metric: str) -> str:
    domain = metric.split(".", 1)[0]
    return next((gid for gid, domains in GROUPS if domain in domains), OTHER)


def path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE


def load(data_dir: Path) -> dict[str, list[str]]:
    """The saved per-metric orders; a damaged or missing file means none."""
    try:
        stored = json.loads(path(data_dir).read_text())
    except (OSError, ValueError):
        return {}
    metrics = stored.get("metrics") if isinstance(stored, dict) else None
    if not isinstance(metrics, dict):
        return {}
    out = {}
    for metric, order in metrics.items():
        if (
            isinstance(metric, str)
            and METRIC.fullmatch(metric)
            and isinstance(order, list)
            and order
            and all(isinstance(s, str) and SOURCE.fullmatch(s) for s in order)
        ):
            out[metric] = list(dict.fromkeys(order))
    return out


def save(data_dir: Path, metric: str, order: Sequence[str] | None) -> dict[str, list[str]]:
    """Save ``order`` (best first) for one metric; None or an empty list goes back to the
    default. Returns all saved orders."""
    if not isinstance(metric, str) or not METRIC.fullmatch(metric):
        raise ValueError("Unknown measurement.")
    saved = load(data_dir)
    if order:
        if (
            not isinstance(order, list | tuple)
            or len(order) > MAX_SOURCES
            or not all(isinstance(s, str) and SOURCE.fullmatch(s) for s in order)
        ):
            raise ValueError("Choose sources from the list.")
        if len(set(order)) != len(order):
            raise ValueError("Each source can appear only once.")
        saved[metric] = list(order)
    else:
        saved.pop(metric, None)
    target = path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target.parent, delete=False, suffix=".tmp") as tmp:
        json.dump({"metrics": dict(sorted(saved.items()))}, tmp, indent=2)
    os.replace(tmp.name, target)
    return saved


def rebuild(engine: Engine, settings) -> unify.UnifyStats:
    """Rebuild the unified tables with the config rules and the saved per-metric orders."""
    return unify.rebuild(engine, settings.best_source, exact=load(settings.data_dir))


def source_order(settings, metric: str) -> list[str]:
    """The order in use for ``metric``, best first (saved, config rule or default)."""
    return unify.source_order(metric, settings.best_source, load(settings.data_dir))


def _ranked(order: Sequence[str], delivering: set[str]) -> list[str]:
    """The delivering sources in the order derive ranks them: listed first, then by name."""
    listed = [s for s in order if s in delivering]
    return listed + sorted(delivering - set(listed))


def overview(engine: Engine, settings) -> dict[str, Any]:
    """Per daily metric: the sources that deliver it (days, last day), the order in use and
    whether it is the athlete's own, and the source the daily value comes from now."""
    saved = load(settings.data_dir)
    with engine.connect() as conn:
        delivered = conn.execute(
            select(
                daily_metric.c.metric,
                daily_metric.c.source,
                func.count().label("days"),
                func.max(daily_metric.c.date).label("last"),
            )
            .where(daily_metric.c.subject_id == 1)
            .group_by(daily_metric.c.metric, daily_metric.c.source)
        ).all()
        latest = (
            select(daily_unified.c.metric, func.max(daily_unified.c.date).label("date"))
            .where(daily_unified.c.subject_id == 1)
            .group_by(daily_unified.c.metric)
            .subquery()
        )
        winners = conn.execute(
            select(daily_unified.c.metric, daily_unified.c.source, daily_unified.c.date)
            .join(
                latest,
                (latest.c.metric == daily_unified.c.metric)
                & (latest.c.date == daily_unified.c.date),
            )
            .where(daily_unified.c.subject_id == 1)
        ).all()
    by_metric: dict[str, dict[str, dict[str, Any]]] = {}
    for metric, source, days, last in delivered:
        by_metric.setdefault(metric, {})[source] = {"id": source, "days": days, "last": str(last)}
    now = {m: {"source": s, "date": str(d)} for m, s, d in winners}
    metrics = []
    for metric in sorted(by_metric):
        sources = by_metric[metric]
        order = unify.source_order(metric, settings.best_source, saved)
        ranked = _ranked(order, set(sources))
        metrics.append(
            {
                "metric": metric,
                "group": group_of(metric),
                "order": ranked,
                "sources": [sources[s] for s in ranked],
                "custom": metric in saved,
                "now": now.get(metric),
            }
        )
    return {
        "groups": [gid for gid, _ in GROUPS] + [OTHER],
        "metrics": metrics,
        # Saved orders for metrics without data (kept, shown so they can be reset).
        "saved_without_data": sorted(set(saved) - set(by_metric)),
        "config_rules": {p: list(o) for p, o in (settings.best_source or {}).items()},
    }
