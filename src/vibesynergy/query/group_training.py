"""Group trainings from RunSynergy as the workout for a "Training geven" session.

The athlete gives group trainings and runs along easy (Z1) the whole time. The plan holds a
"Training geven" session for each, with a generic Z1 workout. RunSynergy's group schedule has
the real content: warm-up and technique, the core on time (so he can blow the whistle) and
the cool-down. This module builds a workout from that schedule for the matching session and
lets him adopt it, after which the normal plan delivery puts it on the watch.

* **Match:** a coached run session (``coaching.is_coached_title``) and a RunSynergy group
  training (``soort`` groep, ``rol`` coach or assistent) on the same day. With more than one
  on a day, the club name in the session title decides.
* **Workout:** warm-up and technique, then cool-down, each until the lap button. The core is
  on time, read from ``kern_stappen`` when RunSynergy sends it, else from the core text
  (``7x 4' DL2 / 2' DL3``). The zone names are the step texts, so he knows what the runners
  do. His own target is Z1 HR throughout. A core that can't be read is one timed step with
  the core text.
* **Proposal:** shown while the session's workout differs from the one built here, so a
  change in RunSynergy makes a new one. Nothing is written until he adopts it.
* **Its day (#113):** each proposal says which day it is for (``when``: "do 20 nov, 19:30"),
  whether that moment has passed (``past``) and which other sessions the plan already has
  that day (``same_day``), so he can decide. It shows on Today, under Waiting for you, on
  Planning, as a notice (``alerts``) and in the MCP plan view.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from vibesynergy.core.user_datetime import format_user_short_day
from vibesynergy.query import coaching, workout_model
from vibesynergy.store.schema import plan_session, planned_item

SOURCE = "runsynergy"
COACH_ROLES = {"coach", "assistent", "assistent-coach", "assistant"}
REASON = "Groepstraining uit RunSynergy overgenomen"
Z1 = {"type": "hr_zone", "low": 1, "high": 1}
DEFAULT_WARMUP_MIN = 15
DEFAULT_COOLDOWN_MIN = 10
DEFAULT_CORE_MIN = 30
CUE_MAX = 160
# "7x 4' DL2 / 2' DL3", "5 x 4 min DL2 + 2 min DL3", "6x (3' DL3, 1' DL1)"
_MIN = r"(\d+(?:[.,]\d+)?)\s*(?:'|\u2019|\u2032|min\.?|minuten|m\b)"
_REPEAT = re.compile(
    rf"(?P<n>\d+)\s*[x\u00d7]\s*\(?\s*{_MIN}\s*(?P<z1>[A-Za-z][A-Za-z0-9]*)?\s*"
    rf"(?:/|\+|,|en|;)\s*{_MIN}\s*(?P<z2>[A-Za-z][A-Za-z0-9]*)?\s*\)?",
    re.IGNORECASE,
)


def _minutes(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if 0 < number < 600 else None


def _cue(text: str) -> str:
    """Step text that the step syntax reads back as text only: one line, no token that
    looks like a duration or distance (``20m``), no ``Press lap``."""
    words = []
    for word in " ".join(str(text or "").split()).split(" "):
        if not word:
            continue
        if workout_model._duration(word) is not None or workout_model._distance(word) is not None:
            word = re.sub(r"^(\d+(?:[.,]\d+)?)(.*)$", r"\1 \2", word)
        words.append(word)
    cue = " ".join(words)
    cue = re.sub(r"(?i)press\s+lap", "lap", cue)
    return cue[:CUE_MAX].strip(" ,;")


def _parts(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for part in data.get("onderdelen") or []:
        if isinstance(part, dict) and part.get("deel"):
            out.setdefault(str(part["deel"]), []).append(part)
    return out


def _text(parts: list[dict[str, Any]]) -> str:
    return " · ".join(str(p.get("tekst") or "").strip() for p in parts if p.get("tekst"))


def _sum_min(parts: list[dict[str, Any]], default: int) -> int:
    total = sum(m for p in parts if (m := _minutes(p.get("min"))))
    return round(total) or default


def _work(minutes: float, cue: str) -> dict[str, Any]:
    step: dict[str, Any] = {"kind": "work", "duration_s": round(minutes * 60), "target": dict(Z1)}
    if cue:
        step["cue"] = _cue(cue)
    return step


def _from_structured(blocks: Any) -> list[dict[str, Any]] | None:
    """``kern_stappen``: ``[{"herhaal": 7, "stappen": [{"min": 4, "zone": "DL2"}, …]}]`` or
    single steps ``{"min": 10, "zone": "DL1", "tekst": …}``. None when it doesn't read."""
    if not isinstance(blocks, list) or not blocks:
        return None
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            return None
        if "stappen" in block:
            inner = _from_structured(block.get("stappen"))
            times = int(block.get("herhaal") or 1)
            if not inner or any(s["kind"] == "repeat" for s in inner) or not 1 <= times <= 50:
                return None
            if times > 1:
                out.append({"kind": "repeat", "times": times, "steps": inner})
            else:
                out.extend(inner)
            continue
        minutes = _minutes(block.get("min"))
        if minutes is None:
            return None
        label = " ".join(str(block.get(k) or "").strip() for k in ("zone", "tekst")).strip()
        out.append(_work(minutes, label))
    return out


def _from_text(text: str) -> list[dict[str, Any]] | None:
    match = _REPEAT.search(text or "")
    if not match:
        return None
    times = int(match.group("n"))
    first, second = _minutes(match.group(2)), _minutes(match.group(4))
    if not first or not second or not 1 <= times <= 50:
        return None
    rest = text[match.end() :].strip(" ,;.-")
    cue_one = match.group("z1") or ""
    cue_two = ", ".join(x for x in (match.group("z2") or "", rest) if x)
    return [
        {"kind": "repeat", "times": times, "steps": [_work(first, cue_one), _work(second, cue_two)]}
    ]


def core_steps(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """The core as steps and how it was read: ``structured``, ``text`` or ``fallback``."""
    structured = _from_structured(data.get("kern_stappen"))
    if structured:
        return structured, "structured"
    core = _parts(data).get("kern") or []
    text = _text(core)
    parsed = _from_text(text)
    if parsed:
        return parsed, "text"
    return [_work(_sum_min(core, DEFAULT_CORE_MIN), text or "Kern volgens het groepsschema")], (
        "fallback"
    )


def build(data: dict[str, Any]) -> tuple[str, str]:
    """intervals.icu text for the session, and how the core was read."""
    parts = _parts(data)
    warm = parts.get("warming_up", []) + parts.get("techniek", [])
    cool = parts.get("cooling_down", [])
    core, how = core_steps(data)
    steps: list[dict[str, Any]] = [
        {
            "kind": "warmup",
            "duration_s": _sum_min(warm, DEFAULT_WARMUP_MIN) * 60,
            "target": dict(Z1),
            "until_lap": True,
            "cue": _cue(_text(warm) or "Inlopen en voorbereiding"),
        },
        *core,
        {
            "kind": "cooldown",
            "duration_s": _sum_min(cool, DEFAULT_COOLDOWN_MIN) * 60,
            "target": dict(Z1),
            "until_lap": True,
            "cue": _cue(_text(cool) or "Uitlopen"),
        },
    ]
    club = str(data.get("club") or "").strip()
    title = str(data.get("titel") or "").strip()
    head = ": ".join(x for x in (club, title) if x) or "Groepstraining"
    intro = (
        f"Training geven, {head}. Tijden volgens het groepsschema in RunSynergy, om te "
        "fluiten; de teksten zeggen wat de lopers doen. Zelf rustig mee in Z1. "
        "Warming-up en cooling-down: druk op Lap als je klaar bent."
    )
    intro = " ".join(intro.split())
    text = workout_model.to_intervals(workout_model.Workout(steps=steps, intro=intro))
    return text, how


def _norm(text: str | None) -> str:
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def _club_words(club: str) -> list[str]:
    return [w for w in re.split(r"\W+", club.casefold()) if len(w) > 2]


def _pick(session_title: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(candidates) == 1:
        return candidates[0]
    title = (session_title or "").casefold()
    scored = [
        (sum(word in title for word in _club_words(c["extra"].get("club") or "")), c)
        for c in candidates
    ]
    scored.sort(key=lambda pair: -pair[0])
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def trainings(engine: Engine, start: date, end: date) -> list[dict[str, Any]]:
    """RunSynergy group trainings the athlete coaches in the window."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(planned_item).where(
                planned_item.c.source == SOURCE,
                planned_item.c.kind == "group_training",
                planned_item.c.date.between(start, end),
            )
        ).mappings()
        out = []
        for row in rows:
            extra = dict(row["extra"] or {})
            if extra.get("soort") != "groep":
                continue
            if str(extra.get("rol") or "").casefold() not in COACH_ROLES:
                continue
            out.append({**dict(row), "extra": extra})
    return out


RUN_SPORTS = {"run", "running", "trailrun", "trail_running"}
# Waiting for you and Planning keep a proposal this many days after its day, marked as past.
PAST_DAYS = 2
AHEAD_DAYS = 14


def _planned(engine: Engine, start: date, end: date) -> list[dict[str, Any]]:
    """Every planned session of the current plan in the window."""
    from vibesynergy.query import plan_model

    plan_id = plan_model.current_plan_id(engine)
    if plan_id is None:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            select(plan_session).where(
                plan_session.c.plan_id == plan_id,
                plan_session.c.date.between(start, end),
                plan_session.c.status == "planned",
            )
        ).mappings()
        return [dict(r) for r in rows]


def _coached(session: dict[str, Any]) -> bool:
    """A coached run session ("Training geven"): the one a group training can fill."""
    return (session["sport"] or "").casefold() in RUN_SPORTS and coaching.is_coached_title(
        session["name"], "running"
    )


def is_past(day: date, start: str | None, now: datetime | None) -> bool:
    """Has the training's moment passed? The day before today; today once its start time
    (local ``HH:MM``) is reached. Unknown without ``now``."""
    if now is None:
        return False
    if day != now.date():
        return day < now.date()
    return bool(start) and str(start) <= now.strftime("%H:%M")


def when(proposal: dict[str, Any], language: str = "en") -> str:
    """The day it is for, with the start time when there is one: "do 20 nov, 19:30"."""
    day = format_user_short_day(date.fromisoformat(proposal["date"]), language)
    return f"{day}, {proposal['start']}" if proposal.get("start") else day


def _current_text(session: dict[str, Any]) -> str:
    workout = session.get("workout") or {}
    return workout_model.to_intervals(
        workout_model.Workout(
            steps=list(workout.get("steps") or []),
            intro=session.get("description") or "",
            garmin_block=workout.get("garmin_block") or "",
        )
    )


def proposals(
    engine: Engine, start: date, end: date, *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Per plan session id: the RunSynergy training it matches and the workout built from it.

    ``state`` is ``proposed`` (the session differs), ``adopted`` (it already is this
    workout) or ``cancelled`` (RunSynergy cancelled the training). ``past`` says whether
    its moment has passed by ``now`` (local, naive); ``same_day`` lists the plan's other
    planned sessions that day (#113)."""
    found = trainings(engine, start, end)
    if not found:
        return {}
    by_day: dict[date, list[dict[str, Any]]] = {}
    for item in found:
        by_day.setdefault(item["date"], []).append(item)
    planned = _planned(engine, start, end)
    out: dict[str, dict[str, Any]] = {}
    for session in (s for s in planned if _coached(s)):
        training = _pick(session["name"] or "", by_day.get(session["date"], []))
        if training is None:
            continue
        extra = training["extra"]
        built, how = build(extra)
        steps = workout_model.parse_intervals(built).steps
        current = _current_text(session)
        if extra.get("cancelled"):
            state = "cancelled"
        elif _norm(current) == _norm(built):
            state = "adopted"
        else:
            state = "proposed"
        out[session["id"]] = {
            "session_id": session["id"],
            "date": session["date"].isoformat(),
            "state": state,
            "club": extra.get("club"),
            "title": extra.get("titel") or training.get("title"),
            "start": extra.get("start"),
            "url": extra.get("url"),
            "core_read": how,
            "workout": built,
            "steps": steps,
            "current": current,
            "minutes": round(workout_model.total_seconds(steps) / 60),
            "past": is_past(session["date"], extra.get("start"), now),
            "same_day": [
                {"session_id": other["id"], "name": other["name"], "sport": other["sport"]}
                for other in planned
                if other["date"] == session["date"] and other["id"] != session["id"]
            ],
        }
    return out


def waiting(engine: Engine, today: date, now: datetime | None = None) -> list[dict[str, Any]]:
    """The proposals that wait for the athlete (not adopted), from ``PAST_DAYS`` back to
    ``AHEAD_DAYS`` ahead, by date: Waiting for you and Planning."""
    found = proposals(
        engine, today - timedelta(days=PAST_DAYS), today + timedelta(days=AHEAD_DAYS), now=now
    )
    return [
        {k: v for k, v in p.items() if k != "current"}
        for p in sorted(found.values(), key=lambda p: (p["date"], p.get("start") or ""))
        if p["state"] != "adopted"
    ]


def proposal_for(engine: Engine, session_id: str, around: date) -> dict[str, Any] | None:
    found = proposals(engine, around - timedelta(days=1), around + timedelta(days=1))
    return found.get(session_id)


def adopt(engine: Engine, session_id: str, *, author: str = "athlete") -> dict[str, Any]:
    """Write the RunSynergy workout into the session (a new plan version)."""
    from vibesynergy.query import plan_model

    with engine.connect() as conn:
        session = (
            conn.execute(select(plan_session).where(plan_session.c.id == session_id))
            .mappings()
            .first()
        )
    if session is None:
        raise ValueError("unknown plan session")
    proposal = proposal_for(engine, session_id, session["date"])
    if proposal is None:
        raise ValueError("no RunSynergy group training matches this session")
    if proposal["state"] == "cancelled":
        raise ValueError("RunSynergy cancelled this group training")
    out = plan_model.set_session(
        engine,
        session_id=session_id,
        day=session["date"],
        name=session["name"],
        sport=session["sport"],
        description=proposal["workout"],
        reason=REASON,
        author=author,
        plan_id=session["plan_id"],
        slot=session["slot"],
    )
    return {**out, "state": "adopted"}
