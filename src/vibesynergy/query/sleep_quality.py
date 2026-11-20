"""The sleep check (#100): is last night's measurement plausible?

Garmin's sleep tracking is not always right, and it is the only source that measures sleep.
On 17-11-2026 the AI coach, not VibeSynergy, saw that the HRV covered only half the night and
that there was no REM. This module checks every night automatically, read-only on the raw
history VibeSynergy keeps (the night's sleep payload and the all-day heart rate), and marks it:

- ``ok``: nothing found;
- ``suspect``: the measurement may be off (asked, still counted);
- ``incomplete``: part of the night was not measured;
- ``bad``: the night was recorded badly (the stages cannot be right).

Every rule has a threshold in ``THRESHOLDS`` and names the metrics it touches (``sleep``,
``hrv``, ``readiness``, ``resting_hr``). The rules:

- **watch worn** (``hr_gap``): heart-rate samples during the sleep window. A gap longer than
  ``hr_gap_s`` or under ``hr_coverage`` of the window measured: incomplete.
- **window** (``window_off``, ``hr_runs_on``): the night against the median bedtime and wake
  time of the ``usual_days`` before it; one that starts or ends ``window_off_s`` or more
  away from them is suspect. A night under ``short_sleep_s`` while the heart rate stays at
  a sleeping level for ``runs_on_s`` or more after the recorded end is suspect too.
- **stages** (``no_rem``, ``stage_spread``, ``one_stage``): over ``stages_min_s`` of sleep
  with no REM, an implausible share of one stage, or one stage without a change for
  ``one_stage_s``: bad.
- **HRV night** (``hrv_partial``): HRV over under ``hrv_coverage`` of the sleep window:
  incomplete for HRV.
- **updated later** (``updated``, ``readiness_partial``): from ``query.measurement`` (#88). A
  night that grew after its first reading is said (it does not change the mark by itself);
  a readiness computed on the partial night is incomplete for readiness.
- **check-in** (``checkin``): the check-in rates sleep 4-5 but under 5 h was measured (#53):
  suspect.

The outcome is a derived result, stored in ``sleep_check`` with the time of the check and a
fingerprint of its inputs: a new version of the night from Garmin (or a new check-in score)
changes the fingerprint and the night is checked again on the next read. Nothing is written
by hand, and Garmin's record is never changed.

The athlete decides last (``checkin.sleep_incomplete``): ``incomplete`` ("yes, incomplete")
or ``ok`` ("no, it's right") beats the check. A metric that is ``bad`` or ``incomplete`` does
not count for the safety floor (``short_sleep``, ``low_readiness``, ``low_hrv``) and is left
out of the personal baselines; a ``suspect`` one still counts, and the advice names it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.i18n import ENGLISH, Translator
from vibesynergy.query import measurement
from vibesynergy.store.schema import checkin, raw_item, sleep_check, sleep_session

log = logging.getLogger(__name__)

SUBJECT = 1
SOURCE = "garmin"
# Bump when a rule or a threshold changes: every stored night is checked again.
VERSION = 1
# A read checks again at most this many of the latest nights it asks for (a long period on
# the sleep page shows the stored checks of older nights).
FRESH_DAYS = 120
STATES = ("ok", "suspect", "incomplete", "bad")
_RANK = {state: i for i, state in enumerate(STATES)}
# Metrics a night's measurement feeds, and the daily metrics behind each.
METRICS = ("sleep", "hrv", "readiness", "resting_hr")
_METRIC_PREFIXES = {
    "sleep": ("sleep.",),
    "hrv": ("hrv.",),
    "readiness": ("recovery.readiness",),
    "resting_hr": ("hr.resting",),
}
# What does not count: these states drop the metric out of the floor and the baselines.
NOT_COUNTED = frozenset({"incomplete", "bad"})
# Robert's "yes, incomplete" on a night the check found nothing on: as #53.
OVERRIDE_DEFAULT = ("sleep", "readiness")

THRESHOLDS: dict[str, float] = {
    "hr_gap_s": 30 * 60,  # one gap in the heart rate longer than this: incomplete
    "hr_coverage": 0.8,  # or less of the window measured than this
    "hr_slack_s": 5 * 60,  # a step between samples up to this is no gap (2-min samples)
    "usual_days": 28,  # the usual bedtime and wake time: the median of these nights
    "usual_min_nights": 7,
    "window_off_s": 4 * 3600,  # a start or an end this far from the usual one: suspect
    "short_sleep_s": 6 * 3600,  # under this, heart rate that runs on is a cut-off night
    "runs_on_s": 90 * 60,  # heart rate at a sleeping level this long after the end
    "runs_on_bpm": 5,  # "sleeping level": at most this much above the night's average
    "stages_min_s": 3 * 3600,  # the stage rules apply to nights longer than this
    "deep_share_max": 0.5,
    "light_share_max": 0.9,
    "rem_share_max": 0.45,
    "awake_share_max": 0.5,  # of the window
    "one_stage_s": 4 * 3600,  # one stage without a change this long: bad
    "hrv_coverage": 0.7,  # HRV over less of the window than this: incomplete for HRV
    "hrv_slack_s": 10 * 60,  # HRV samples come every 5 minutes
    "checkin_quality": 4,  # #53: a check-in this good …
    "checkin_sleep_s": 5 * 3600,  # … with less sleep measured than this
}
_LEVELS = {0.0: "deep", 1.0: "light", 2.0: "rem", 3.0: "awake"}

# The sentence per rule (the English text is the i18n key; values in {}).
TEXTS = {
    "hr_gap": "no heart rate between {from} and {to}",
    "hr_coverage": "heart rate measured for only {pct}% of the night",
    "hr_none": "no heart rate during the night",
    "window_off": "the night ran from {start} to {end}, far from your usual {bed} to {wake}",
    "hr_runs_on": "only {time} of sleep recorded, but your heart rate stayed at a sleeping "
    "level for {more} after it",
    "no_rem": "no REM sleep recorded in a night of {time}",
    "stage_spread": "{pct}% {stage} sleep, more than is plausible",
    "one_stage": "{time} of {stage} sleep without a single change",
    "hrv_partial": "HRV measured only from {from} to {to}",
    "updated": "the night was updated after its first reading: {first} became {now}",
    "readiness_partial": "readiness was computed on the partial night",
    "checkin": "your check-in says you slept well, but only {time} was measured",
}
STAGE_NAMES = {"deep": "deep", "light": "light", "rem": "REM", "awake": "awake"}
HEADLINES = {
    "bad": "Sleep may be badly recorded",
    "incomplete": "Sleep was measured incompletely",
    "suspect": "Sleep measurement may be off",
}
METRIC_NAMES = {
    "sleep": "sleep",
    "hrv": "HRV",
    "readiness": "readiness",
    "resting_hr": "resting heart rate",
}


# --- reading the payloads ---------------------------------------------------------------------


def _seconds(value: Any) -> float | None:
    """Epoch seconds from Garmin's epoch milliseconds or a GMT ISO text without offset."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000
    if isinstance(value, str) and value:
        try:
            found = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (found if found.tzinfo else found.replace(tzinfo=UTC)).timestamp()
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def heart_samples(payloads: list[Any]) -> list[tuple[float, float]]:
    """(epoch seconds, bpm) from Garmin's all-day heart-rate payloads; a sample without a
    value (the watch not worn) is left out."""
    out = []
    for payload in payloads:
        values = payload.get("heartRateValues") if isinstance(payload, dict) else None
        for row in values or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                when, bpm = _seconds(row[0]), _num(row[1])
                if when is not None and bpm:
                    out.append((when, bpm))
    return sorted(set(out))


def _stretches(points: list[float], start: float, end: float) -> list[tuple[float, float]]:
    """The steps between consecutive points, from the start of the window to its end."""
    inside = [start, *sorted(p for p in points if start <= p <= end), end]
    return list(pairwise(inside))


def _coverage(points: list[float], start: float, end: float, slack: float):
    """(share of the window measured, the longest step) for samples ``points``."""
    steps = _stretches(points, start, end)
    window = end - start
    uncovered = sum(max(0.0, b - a - slack) for a, b in steps)
    longest = max(steps, key=lambda s: s[1] - s[0])
    return max(0.0, 1 - uncovered / window), longest


def _minutes_from_noon(moment: float, zone: ZoneInfo) -> float:
    local = datetime.fromtimestamp(moment, zone)
    return (local.hour * 60 + local.minute - 12 * 60) % (24 * 60)


def _clock(moment: float, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(moment, zone).strftime("%H:%M")


def _clock_from_noon(minutes: float) -> str:
    total = round(minutes + 12 * 60) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def worst(states) -> str:
    found = "ok"
    for state in states:
        if _RANK[state] > _RANK[found]:
            found = state
    return found


# --- the rules --------------------------------------------------------------------------------


def assess(
    sleep: Any,
    heart: list[tuple[float, float]] | None,
    *,
    timezone: str,
    usual: dict[str, Any] | None = None,
    sleep_quality: int | None = None,
    gap: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The check of one night from its Garmin sleep payload ``sleep``, the all-day heart rate
    around it (``heart_samples``; ``None`` when that was never fetched), the ``usual`` night
    (``usual_window``), the check-in's sleep score and ``query.measurement.night``'s ``gap``.
    ``None`` when there is no night to check (no sleep window recorded)."""
    t = THRESHOLDS
    daily = sleep.get("dailySleepDTO") if isinstance(sleep, dict) else None
    if not isinstance(daily, dict):
        return None
    start = _seconds(daily.get("sleepStartTimestampGMT"))
    end = _seconds(daily.get("sleepEndTimestampGMT"))
    if start is None or end is None or end <= start:
        return None
    zone = ZoneInfo(timezone)
    window = end - start
    asleep = _num(daily.get("sleepTimeSeconds"))
    reasons: list[dict[str, Any]] = []

    def add(rule: str, state: str, metrics: tuple[str, ...], key: str, **values: Any) -> None:
        reasons.append(
            {"rule": rule, "state": state, "metrics": list(metrics), "key": key, "values": values}
        )

    # Watch worn: the all-day heart rate and the heart rate in the sleep payload together.
    night_hr = [
        s
        for s in (
            _seconds(r.get("startGMT"))
            for r in (sleep.get("sleepHeartRate") or [])
            if isinstance(r, dict) and _num(r.get("value"))
        )
        if s is not None
    ]
    points = [when for when, _ in heart or []] + night_hr
    if heart is not None or night_hr:
        inside = [p for p in points if start <= p <= end]
        if not inside:
            add("hr_gap", "incomplete", METRICS, TEXTS["hr_none"])
        else:
            share, (a, b) = _coverage(points, start, end, t["hr_slack_s"])
            if b - a > t["hr_gap_s"]:
                add(
                    "hr_gap",
                    "incomplete",
                    METRICS,
                    TEXTS["hr_gap"],
                    **{"from": _clock(a, zone), "to": _clock(b, zone)},
                )
            elif share < t["hr_coverage"]:
                add("hr_gap", "incomplete", METRICS, TEXTS["hr_coverage"], pct=round(share * 100))

    # Window: against the usual night.
    if usual and usual.get("nights", 0) >= t["usual_min_nights"]:
        bed_off = _circular(_minutes_from_noon(start, zone) - usual["bed"])
        wake_off = _circular(_minutes_from_noon(end, zone) - usual["wake"])
        if max(bed_off, wake_off) * 3600 >= t["window_off_s"]:
            add(
                "window_off",
                "suspect",
                ("sleep", "readiness"),
                TEXTS["window_off"],
                start=_clock(start, zone),
                end=_clock(end, zone),
                bed=_clock_from_noon(usual["bed"]),
                wake=_clock_from_noon(usual["wake"]),
            )
    if asleep is not None and asleep < t["short_sleep_s"] and heart:
        level = _num(daily.get("avgHeartRate"))
        if level is None:
            during = [bpm for when, bpm in heart if start <= when <= end]
            level = statistics.median(during) if during else None
        more = _runs_on(heart, end, level) if level is not None else 0.0
        if more >= t["runs_on_s"]:
            add(
                "hr_runs_on",
                "suspect",
                ("sleep", "readiness", "hrv"),
                TEXTS["hr_runs_on"],
                seconds={"time": asleep, "more": more},
            )

    # Stages.
    stages = {
        name: _num(daily.get(f"{name}SleepSeconds")) for name in ("deep", "light", "rem", "awake")
    }
    if asleep is not None and asleep > t["stages_min_s"]:
        rem_capable = daily.get("deviceRemCapable") is not False
        if rem_capable and stages["rem"] == 0:
            add("no_rem", "bad", ("sleep", "readiness"), TEXTS["no_rem"], seconds={"time": asleep})
        for name, limit, base in (
            ("deep", t["deep_share_max"], asleep),
            ("light", t["light_share_max"], asleep),
            ("rem", t["rem_share_max"], asleep),
            ("awake", t["awake_share_max"], window),
        ):
            value = stages[name]
            if value is not None and base and value / base > limit:
                add(
                    "stage_spread",
                    "bad",
                    ("sleep", "readiness"),
                    TEXTS["stage_spread"],
                    pct=round(100 * value / base),
                    stage=STAGE_NAMES[name],
                )
        longest = _longest_level(sleep.get("sleepLevels"))
        if longest and longest[0] >= t["one_stage_s"]:
            add(
                "one_stage",
                "bad",
                ("sleep", "readiness"),
                TEXTS["one_stage"],
                stage=STAGE_NAMES[longest[1]],
                seconds={"time": longest[0]},
            )

    # HRV night.
    hrv = [
        s
        for s in (
            _seconds(r.get("startGMT"))
            for r in (sleep.get("hrvData") or [])
            if isinstance(r, dict) and _num(r.get("value"))
        )
        if s is not None
    ]
    inside_hrv = sorted(p for p in hrv if start <= p <= end)
    if inside_hrv:
        share, _ = _coverage(inside_hrv, start, end, t["hrv_slack_s"])
        if share < t["hrv_coverage"]:
            add(
                "hrv_partial",
                "incomplete",
                ("hrv",),
                TEXTS["hrv_partial"],
                **{
                    "from": _clock(inside_hrv[0], zone),
                    "to": _clock(min(end, inside_hrv[-1] + 300), zone),
                },
            )

    # Updated later (#88).
    grown = (gap or {}).get("sleep")
    if grown:
        add(
            "updated",
            "ok",
            (),
            TEXTS["updated"],
            seconds={"first": grown["first_s"], "now": grown["now_s"]},
        )
    if ((gap or {}).get("readiness") or {}).get("partial_night"):
        add("readiness_partial", "incomplete", ("readiness",), TEXTS["readiness_partial"])

    # The check-in (#53).
    if (
        isinstance(sleep_quality, int)
        and sleep_quality >= t["checkin_quality"]
        and asleep is not None
        and asleep < t["checkin_sleep_s"]
    ):
        add(
            "checkin",
            "suspect",
            ("sleep", "readiness"),
            TEXTS["checkin"],
            seconds={"time": asleep},
        )

    metrics: dict[str, str] = {}
    for reason in reasons:
        for name in reason["metrics"]:
            metrics[name] = worst([metrics.get(name, "ok"), reason["state"]])
    return {
        "state": worst(r["state"] for r in reasons),
        "metrics": {m: metrics[m] for m in METRICS if m in metrics},
        "reasons": reasons,
        "window": {
            "start": datetime.fromtimestamp(start, zone).isoformat(),
            "end": datetime.fromtimestamp(end, zone).isoformat(),
        },
        "sleep_s": asleep,
    }


def _circular(minutes: float) -> float:
    """Minutes between two clock times, the short way round."""
    found = abs(minutes) % (24 * 60)
    return min(found, 24 * 60 - found) / 60  # hours


def _runs_on(heart: list[tuple[float, float]], end: float, level: float) -> float:
    """How long after ``end`` the heart rate stays at a sleeping level: samples close
    together (no gap over ``hr_slack_s``) at most ``runs_on_bpm`` above ``level``, judged
    per quarter hour so one restless minute does not end it."""
    after = [(when, bpm) for when, bpm in heart if end < when <= end + 6 * 3600]
    limit = level + THRESHOLDS["runs_on_bpm"]
    reached = end
    block: list[float] = []
    block_start = end
    last = end
    for when, bpm in after:
        if when - last > THRESHOLDS["hr_slack_s"]:
            break
        last = when
        block.append(bpm)
        if when - block_start >= 15 * 60:
            if statistics.median(block) > limit:
                break
            reached = when
            block, block_start = [], when
    return reached - end


def _longest_level(levels: Any) -> tuple[float, str] | None:
    """The longest stretch of one stage in the hypnogram, adjacent segments of the same
    stage joined."""
    best: tuple[float, str] | None = None
    current: list[Any] | None = None  # [start, end, stage]
    rows = sorted(
        (
            (_seconds(r.get("startGMT")), _seconds(r.get("endGMT")), r.get("activityLevel"))
            for r in levels or []
            if isinstance(r, dict)
        ),
        key=lambda r: r[0] or 0,
    )
    for start, end, level in rows:
        found = _num(level)
        stage = _LEVELS.get(found) if found is not None else None
        if start is None or end is None or stage is None:
            continue
        if current and current[2] == stage and start - current[1] <= 60:
            current[1] = max(current[1], end)
        else:
            current = [start, end, stage]
        length = current[1] - current[0]
        if best is None or length > best[0]:
            best = (length, stage)
    return best


# --- the usual night --------------------------------------------------------------------------


def _sessions(engine: Engine, window: Window) -> list[tuple[date, datetime, datetime]]:
    """Garmin nights (local date, start, end) from ``usual_days`` before ``window`` to its
    end."""
    days = int(THRESHOLDS["usual_days"])
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sleep_session.c.local_date, sleep_session.c.start_utc, sleep_session.c.end_utc
            ).where(
                sleep_session.c.source == SOURCE,
                sleep_session.c.local_date.between(
                    window.start - timedelta(days=days), window.end - timedelta(days=1)
                ),
                sleep_session.c.start_utc.is_not(None),
                sleep_session.c.end_utc.is_not(None),
            )
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def _usual(sessions, day: date, zone: ZoneInfo) -> dict[str, Any] | None:
    first = day - timedelta(days=int(THRESHOLDS["usual_days"]))
    rows = [(s, e) for d, s, e in sessions if first <= d < day]
    if not rows:
        return None

    def noon(moment: datetime) -> float:
        moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        return _minutes_from_noon(moment.timestamp(), zone)

    return {
        "bed": statistics.median(noon(r[0]) for r in rows),
        "wake": statistics.median(noon(r[1]) for r in rows),
        "nights": len(rows),
    }


def usual_window(engine: Engine, day: date, timezone: str) -> dict[str, Any] | None:
    """The median bedtime and wake time (minutes from noon, local) of the Garmin nights in
    the ``usual_days`` before ``day``."""
    return _usual(_sessions(engine, Window(day, day)), day, ZoneInfo(timezone))


# --- the athlete's choice ---------------------------------------------------------------------


def override_of(day_checkin: dict | None) -> str | None:
    """``incomplete``, ``ok`` or ``None`` from the check-in's ``sleep_incomplete``."""
    found = (day_checkin or {}).get("sleep_incomplete")
    if found is True:
        return "incomplete"
    if found is False:
        return "ok"
    return None


def decide(auto: dict[str, Any] | None, override: str | None) -> dict[str, Any] | None:
    """The night's mark with the athlete's choice on top of the check (``auto``)."""
    if auto is None and override is None:
        return None
    base = auto or {"state": "ok", "metrics": {}, "reasons": []}
    out = {
        **base,
        "auto_state": base["state"],
        "auto_metrics": dict(base["metrics"]),
        "override": override,
    }
    if override == "ok":
        out["state"], out["metrics"] = "ok", {}
    elif override == "incomplete":
        names = set(base["metrics"]) | set(OVERRIDE_DEFAULT)
        out["state"] = "incomplete"
        out["metrics"] = {
            m: worst([base["metrics"].get(m, "ok"), "incomplete"]) for m in METRICS if m in names
        }
    out["not_counted"] = [m for m, s in out["metrics"].items() if s in NOT_COUNTED]
    return out


# --- stored, recomputed when the inputs change ------------------------------------------------


def _current_raw(engine: Engine, datasets_days: list[tuple[str, date]]) -> dict[tuple, str]:
    keys = {(ds, d.isoformat()) for ds, d in datasets_days}
    with engine.connect() as conn:
        rows = conn.execute(
            select(raw_item.c.dataset, raw_item.c.item_key, raw_item.c.id).where(
                raw_item.c.source == SOURCE,
                raw_item.c.is_current.is_(True),
                raw_item.c.dataset.in_({ds for ds, _ in keys}),
                raw_item.c.item_key.in_({k for _, k in keys}),
            )
        ).all()
    return {(ds, key): rid for ds, key, rid in rows if (ds, key) in keys}


def _versions(engine: Engine, window: Window) -> dict[str, list[str]]:
    """Every sleep version per night in ``window``, oldest first."""
    keys = [d.isoformat() for d in window.days()]
    out: dict[str, list[str]] = {k: [] for k in keys}
    with engine.connect() as conn:
        rows = conn.execute(
            select(raw_item.c.item_key, raw_item.c.id)
            .where(
                raw_item.c.source == SOURCE,
                raw_item.c.dataset == "sleep",
                raw_item.c.item_key.in_(keys),
            )
            .order_by(raw_item.c.fetched_at)
        ).all()
    for key, rid in rows:
        out[key].append(rid)
    return out


def _inputs(day: date, versions: list[str], current: dict, quality: Any, usual: Any) -> str:
    key = day.isoformat()
    before = (day - timedelta(days=1)).isoformat()
    usual_key = (
        [round(usual["bed"] / 15), round(usual["wake"] / 15), usual["nights"]] if usual else None
    )
    found = {
        "v": VERSION,
        "t": THRESHOLDS,
        "sleep": versions,
        "heart": [current.get(("heart_rate", before)), current.get(("heart_rate", key))],
        "ready": current.get(("training_readiness", key)),
        "quality": quality,
        "usual": usual_key,
    }
    text = json.dumps(found, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _checkins(engine: Engine, window: Window) -> dict[date, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(checkin.c.date, checkin.c.sleep_quality, checkin.c.sleep_incomplete).where(
                checkin.c.subject_id == SUBJECT, checkin.c.date.between(window.start, window.end)
            )
        ).all()
    return {d: {"sleep_quality": q, "sleep_incomplete": i} for d, q, i in rows}


def _load(raw, item_id: str | None) -> Any:
    if not item_id:
        return None
    try:
        return raw.load(item_id).json()
    except Exception as exc:  # a missing blob is no reason to lose the page
        log.debug("sleep check: raw item %s could not be read: %s", item_id, exc)
        return None


def _compute(engine: Engine, raw, day: date, timezone: str, current: dict, quality, usual):
    key, before = day.isoformat(), (day - timedelta(days=1)).isoformat()
    payload = _load(raw, current.get(("sleep", key)))
    heart_ids = [current.get(("heart_rate", before)), current.get(("heart_rate", key))]
    heart = heart_samples([_load(raw, i) for i in heart_ids if i]) if any(heart_ids) else None
    gap = measurement.night(engine, raw, day)
    return assess(payload, heart, timezone=timezone, usual=usual, sleep_quality=quality, gap=gap)


def nights(
    engine: Engine,
    raw,
    window: Window,
    timezone: str,
    *,
    store: bool = True,
    fresh_days: int | None = FRESH_DAYS,
) -> dict[date, dict[str, Any]]:
    """The mark of every night in ``window`` that has one (``decide``), with ``checked_at``.
    A night whose inputs changed since its stored check is checked again (and stored, unless
    ``store`` is false: the read-only replay), for the last ``fresh_days`` of the window
    (``None``: all of it); older nights keep their stored check. Without a raw archive only
    the stored checks and the athlete's choices are known."""
    days = list(window.days())
    recent = (
        Window(max(window.start, window.end - timedelta(days=fresh_days - 1)), window.end)
        if fresh_days
        else window
    )
    choices = _checkins(engine, window)
    with engine.connect() as conn:
        stored = {
            row.date: row
            for row in conn.execute(
                select(sleep_check).where(
                    sleep_check.c.subject_id == SUBJECT,
                    sleep_check.c.date.between(window.start, window.end),
                )
            )
        }
    fresh: dict[date, tuple[str, dict[str, Any] | None]] = {}
    if raw is not None:
        wanted = []
        for d in recent.days():
            for ds, when in (
                ("sleep", d),
                ("heart_rate", d),
                ("heart_rate", d - timedelta(days=1)),
                ("training_readiness", d),
            ):
                wanted.append((ds, when))
        current = _current_raw(engine, wanted)
        versions = _versions(engine, recent)
        sessions = _sessions(engine, recent)
        zone = ZoneInfo(timezone)
        for d in recent.days():
            if ("sleep", d.isoformat()) not in current:
                continue
            quality = (choices.get(d) or {}).get("sleep_quality")
            usual = _usual(sessions, d, zone)
            inputs = _inputs(d, versions[d.isoformat()], current, quality, usual)
            row = stored.get(d)
            if row is not None and row.inputs == inputs:
                continue
            try:
                result = _compute(engine, raw, d, timezone, current, quality, usual)
            except Exception:
                log.exception("sleep check of %s failed", d)
                continue
            fresh[d] = (inputs, result)
    now = utc_now()
    if store and fresh:
        with engine.begin() as conn:
            for d, (inputs, result) in fresh.items():
                values = {
                    "inputs": inputs,
                    "state": (result or {}).get("state", "none"),
                    "result": result or {},
                    "checked_at": now,
                }
                conn.execute(
                    insert(sleep_check)
                    .values(subject_id=SUBJECT, date=d, **values)
                    .on_conflict_do_update(index_elements=["subject_id", "date"], set_=values)
                )
    out: dict[date, dict[str, Any]] = {}
    for d in days:
        if d in fresh:
            auto, checked = fresh[d][1], now
        elif d in stored:
            auto = stored[d].result or None
            checked = stored[d].checked_at
        else:
            auto, checked = None, None
        found = decide(auto or None, override_of(choices.get(d)))
        if found is None:
            continue
        found["date"] = d.isoformat()
        found["checked_at"] = checked.isoformat() if checked else None
        out[d] = found
    return out


def from_checkin(day_checkin: dict | None, sleep_s: float | None) -> dict[str, Any] | None:
    """The mark when no night could be checked (no Garmin night in the raw history): the
    check-in rule (#53) on the day's sleep from the best source, and the athlete's choice."""
    t = THRESHOLDS
    quality = (day_checkin or {}).get("sleep_quality")
    auto = None
    if (
        isinstance(quality, int)
        and quality >= t["checkin_quality"]
        and sleep_s is not None
        and sleep_s < t["checkin_sleep_s"]
    ):
        reason = {
            "rule": "checkin",
            "state": "suspect",
            "metrics": ["sleep", "readiness"],
            "key": TEXTS["checkin"],
            "values": {"seconds": {"time": sleep_s}},
        }
        auto = {
            "state": "suspect",
            "metrics": {"sleep": "suspect", "readiness": "suspect"},
            "reasons": [reason],
            "sleep_s": sleep_s,
        }
    return decide(auto, override_of(day_checkin))


def night(engine: Engine, raw, day: date, timezone: str) -> dict[str, Any] | None:
    """The mark of ``day``'s night (the night that ends that morning), or ``None`` when there
    is neither a night to check nor a choice of the athlete."""
    return nights(engine, raw, Window(day, day), timezone).get(day)


def not_counted_days(engine: Engine, metric: str, window: Window) -> set[date]:
    """Days in ``window`` whose ``metric`` (a daily metric id) comes from a night that does
    not count (``bad`` or ``incomplete``, after the athlete's choice). Stored checks only, so
    it is cheap and needs no raw archive; the Today page and the sleep page keep them up to
    date."""
    group = group_of(metric)
    if group is None:
        return set()
    found = nights(engine, None, window, "UTC")
    return {d for d, mark in found.items() if group in mark["not_counted"]}


def group_of(metric: str) -> str | None:
    for group, prefixes in _METRIC_PREFIXES.items():
        if metric.startswith(prefixes):
            return group
    return None


def set_override(engine: Engine, day: date, choice: str | None) -> dict[str, Any]:
    """Save the athlete's choice for ``day``'s night: ``incomplete``, ``ok`` or ``None``
    (undo). It beats the automatic check."""
    from vibesynergy.query import journal

    if choice not in ("incomplete", "ok", None):
        raise ValueError("choice must be incomplete, ok or empty")
    value = None if choice is None else choice == "incomplete"
    return journal.set_sleep_incomplete(engine, day, value)


# --- words ------------------------------------------------------------------------------------


def _hm(seconds: float, tr: Translator) -> str:
    """As the Today page writes a duration: "3 h 29 min", "45 min"."""
    minutes = round(seconds / 60)
    if minutes >= 60:
        return tr("{h} h {m} min", h=minutes // 60, m=f"{minutes % 60:02d}")
    return tr("{m} min", m=minutes)


def reason_text(reason: dict[str, Any], tr: Translator = ENGLISH) -> str:
    values = dict(reason.get("values") or {})
    if "stage" in values:
        values["stage"] = tr(values["stage"])
    for name, seconds in (values.pop("seconds", None) or {}).items():
        values[name] = _hm(seconds, tr)
    return tr(reason["key"], **values)


def describe(mark: dict[str, Any] | None, tr: Translator = ENGLISH) -> dict[str, Any] | None:
    """The mark in words for the page: headline, the reasons (the ones that move the mark
    first) and what it means for the advice. ``None`` when there is nothing to say."""
    if not mark:
        return None
    auto = mark.get("auto_state", mark["state"])
    reasons = sorted(
        (r for r in mark.get("reasons") or [] if r["state"] != "ok"),
        key=lambda r: -_RANK[r["state"]],
    )
    info = [r for r in mark.get("reasons") or [] if r["state"] == "ok"]
    if auto == "ok" and not mark.get("override") and not info:
        return None
    shown = reasons or info
    out = {
        "state": mark["state"],
        "auto_state": auto,
        "override": mark.get("override"),
        "headline": tr(HEADLINES[auto]) if auto in HEADLINES else None,
        "reasons": [reason_text(r, tr) for r in shown[:3]],
        "not_counted": [tr(METRIC_NAMES[m]) for m in mark.get("not_counted") or []],
    }
    if out["not_counted"]:
        out["effect"] = tr(
            "Its {metrics} don't count for the advice.",
            metrics=join(out["not_counted"], tr),
        )
    return out


def join(items: list[str], tr: Translator) -> str:
    if len(items) <= 1:
        return "".join(items)
    return tr("{a} and {b}", a=", ".join(items[:-1]), b=items[-1])


def for_context(mark: dict[str, Any] | None) -> dict[str, Any] | None:
    """The mark for the AI coach's safety block: compact, English reasons."""
    if not mark:
        return None
    return {
        "date": mark.get("date"),
        "state": mark["state"],
        "auto_state": mark.get("auto_state", mark["state"]),
        "override": mark.get("override"),
        "metrics": mark.get("metrics") or {},
        "not_counted": mark.get("not_counted") or [],
        "reasons": [
            {"rule": r["rule"], "state": r["state"], "text": reason_text(r)}
            for r in mark.get("reasons") or []
        ],
        "checked_at": mark.get("checked_at"),
    }
