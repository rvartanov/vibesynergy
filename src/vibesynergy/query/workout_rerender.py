"""Planned sessions built from a template, and building them again after a method change.

A session in VibeSynergy's plan records the template that built it in ``workout.template``
(#142): ``{"id", "params", "lang", "method_version", "source"}``. It is recorded when the
session is written with ``template_id`` (``vibesynergy_plan_session_set``), or when its text
is exactly what a template gives under its name (``recognise``). A session whose text was
changed by hand no longer matches, so it is never rebuilt.

``changes`` lists, on request, the planned sessions from a day on whose text the current
method would build differently. Only sessions still ``planned`` and dated after today: a
session of today may be under way and a done session never changes. The caller writes each
one as a ``vibesynergy_plan_session_set`` with its ``session_id``, so the usual approval and
the mirror to intervals.icu, Zwift and Garmin apply.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from vibesynergy.query import workout_library, workout_method
from vibesynergy.query.workout_method import Method, Values

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _name_pattern(text: str) -> re.Pattern[str] | None:
    if not text:
        return None
    parts, last = [], 0
    for match in _PLACEHOLDER.finditer(text):
        parts.append(re.escape(text[last : match.start()]))
        parts.append(rf"(?P<{match.group(1)}>\d+(?:\.\d+)?)")
        last = match.end()
    parts.append(re.escape(text[last:]))
    return re.compile("^" + "".join(parts) + "$")


def _number(text: str) -> float | int:
    value = float(text)
    return int(value) if value.is_integer() else value


def _normalised(text: str) -> str:
    from vibesynergy.query import plan_model

    return plan_model._normalised(text)


def stamp(found: dict[str, Any], method: Method, lang: str) -> dict[str, Any]:
    """What a session records about the template and the method version that built it."""
    return {
        "id": found["template"],
        "params": dict(found["params"]),
        "lang": lang,
        "method_version": method.version,
        "source": method.source,
    }


def recognise(
    name: str | None, day: date, text: str, methods: list[Method]
) -> dict[str, Any] | None:
    """The template (and method version) whose workout is exactly this session: same name
    pattern, same text. None when the session was written or changed by hand."""
    if not name or not text:
        return None
    wanted = _normalised(text)
    for template in workout_library.TEMPLATES:
        for method in methods:
            v = Values(template.id, method.values(template.id))
            for lang in workout_method.LANGS:
                pattern = _name_pattern(v.text("name", lang))
                match = pattern.match(name.strip()) if pattern else None
                if match is None:
                    continue
                params = {k: _number(x) for k, x in match.groupdict().items()}
                if template.id == "swim_technique":
                    params["week"] = day.isocalendar()[1]
                try:
                    found = workout_library.render(template.id, lang, method, **params)
                except (ValueError, KeyError, TypeError):
                    continue
                if _normalised(found["text"]) == wanted:
                    return stamp(found, method, lang)
    return None


def _planned(engine: Engine, start: date) -> list[dict[str, Any]]:
    from vibesynergy.query import plan_model
    from vibesynergy.store.schema import plan_session

    plan_id = plan_model.current_plan_id(engine)
    if plan_id is None:
        raise ValueError("there is no plan yet: create one with `vibesynergy plan create`")
    with engine.connect() as conn:
        rows = conn.execute(
            select(plan_session)
            .where(
                plan_session.c.plan_id == plan_id,
                plan_session.c.status == "planned",
                plan_session.c.date >= start,
            )
            .order_by(plan_session.c.date, plan_session.c.id)
        ).mappings()
        return [dict(r) for r in rows]


def changes(engine: Engine, from_date: date, today: date) -> dict[str, Any]:
    """What building the planned sessions from ``from_date`` again with the current method
    would change. Nothing is written."""
    from vibesynergy.query import plan_model

    start = max(from_date, date.fromordinal(today.toordinal() + 1))
    current = workout_method.load(engine)
    older: list[Method] | None = None
    out: list[dict[str, Any]] = []
    same, by_hand = 0, 0
    for row in _planned(engine, start):
        text = plan_model.session_text(row)
        made = (row.get("workout") or {}).get("template")
        if not made:
            if older is None:
                older = workout_method.versions(engine)
            made = recognise(row["name"], row["date"], text, older)
        if not made or made.get("id") not in workout_library.BY_ID:
            by_hand += 1
            continue
        lang = made.get("lang") or "nl"
        params = dict(made.get("params") or {})
        if made["id"] == "swim_technique":
            params["week"] = row["date"].isocalendar()[1]
        try:
            fresh = workout_library.render(made["id"], lang, current, **params)
        except ValueError:
            by_hand += 1
            continue
        if older is None:
            older = workout_method.versions(engine)
        # A name the AI coach gave stays; a name the template gave follows the method.
        name = row["name"]
        if name == _template_name(older, made, lang, params):
            name = fresh["name"]
        if _normalised(fresh["text"]) == _normalised(text) and name == row["name"]:
            same += 1
            continue
        out.append(
            {
                "session_id": row["id"],
                "date": row["date"].isoformat(),
                "sport": row["sport"],
                "name": name,
                "before": {"name": row["name"], "text": text},
                "description": fresh["text"],
                "template": stamp(fresh, current, lang),
                "built_with": made.get("method_version"),
            }
        )
    return {
        "from": start.isoformat(),
        "method": {"slug": workout_method.SLUG, "version": current.version},
        "changes": out,
        "unchanged": same,
        "not_from_a_template": by_hand,
        "note": (
            "Only planned sessions after today; done sessions never change. A session whose "
            "text was written or changed by hand is not from a template and stays as it is."
        ),
    }


def _template_name(
    methods: list[Method], made: dict[str, Any], lang: str, params: dict[str, Any]
) -> str | None:
    """The name the method version that built the session gave it."""
    for method in methods:
        if method.version == made.get("method_version"):
            try:
                return workout_library.render(made["id"], lang, method, **params)["name"]
            except ValueError:
                return None
    return None
