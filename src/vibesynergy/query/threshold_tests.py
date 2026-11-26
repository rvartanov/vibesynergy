"""Threshold values from a marked test or race (#84): deterministic code, one proposal each.

An activity with the purpose ``test`` or ``race`` (chosen, or the automatic mark of
``race_test_marks``) is read here. Which test it is follows from the sport and the laps:

- **FTP** (a ride test): a fixed share of the mean power over the test length. A lap (a
  workout step) of that length is the test itself; otherwise the best rolling mean of that
  length in the power stream.
- **CSS** (a swim test): from the long and the short all-out swim, (long - short) /
  (t_long - t_short), as seconds per 100 m. The times come from the laps (one per step).
- **LTHR and threshold pace** (a run test): from the lap of the test length, the average
  heart rate over its last part and the average pace over the whole lap.
- **Max HR** (any marked run or ride, a race too): the activity's highest heart rate when it
  is reliable and above the current value (the same margin as ``thresholds.signals``).

Lengths and distances come from the knowledge-base method (``workout_method``: the
templates ``ride_ftp_test``, ``run_test`` and ``swim_css_test``). The factor and the part of
the run test the knowledge base states only in words are in ``PROTOCOL`` below, in one
place, until the method holds them as values.

Each value becomes a proposal (``athlete_threshold.propose_threshold``, method ``test`` or
``race``, the activity in its history); the athlete approves it on Zones & thresholds, as
any other proposal. Not enough data (no power, no laps of the test) gives no proposal and
the reason. ``evaluate`` only reads; ``propose`` writes.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, select, update

from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import athlete_threshold, thresholds, workout_method
from vibesynergy.query.plan import family
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.schema import activity_lap, activity_unified
from vibesynergy.store.schema import athlete_threshold as threshold_table


@dataclass(frozen=True)
class Protocol:
    """What the knowledge base states only in words (workout-method: ``ride_ftp_test``
    "FTP = 95% of the average power", ``run_test`` "average HR of the last 20'"). To be
    replaced by values from the method once it holds them (see the pull request of #84)."""

    ftp_factor: float = 0.95  # FTP = this share of the mean power over the test length
    run_lthr_last_min: float = 20  # LTHR: the mean heart rate over the last minutes of the test
    # A lap is the test's step when its length is within this share of the planned length.
    step_match: float = 0.05
    # A distance lap (the CSS swims) matches within this share of the planned distance.
    distance_match: float = 0.03
    # Share of a window that must hold samples before its mean counts.
    coverage: float = 0.9
    hold_s: float = 10  # a stream sample stands for at most this many seconds (smart recording)


PROTOCOL = Protocol()

TEMPLATES = {"bike": "ride_ftp_test", "run": "run_test", "swim": "swim_css_test"}
SPORT = {"ride": "bike", "run": "run", "swim": "swim"}
CALCULATED = "calculated"  # the history event of a proposal made here


# --- the protocol in use ------------------------------------------------------------------


def protocol(engine: Engine | None) -> dict[str, Any]:
    """Test lengths and distances from the knowledge-base method, with the fixed rules."""
    method = workout_method.load(engine)
    ride = method.values(TEMPLATES["bike"])
    run = method.values(TEMPLATES["run"])
    swim = workout_method.Values(TEMPLATES["swim"], method.values(TEMPLATES["swim"]))
    return {
        "ftp_minutes": float((ride.get("params") or {}).get("minutes") or 0) or None,
        "run_minutes": float((run.get("params") or {}).get("minutes") or 0) or None,
        "css_long_m": _metres(swim, "long_effort"),
        "css_short_m": _metres(swim, "short_effort"),
        **asdict(PROTOCOL),
        "method": {"slug": workout_method.SLUG, "version": method.version, "source": method.source},
    }


def _metres(values: workout_method.Values, step: str) -> float | None:
    try:
        return float(values.metres(step))
    except ValueError:
        return None


# --- reading the activity -----------------------------------------------------------------


def _activity(engine: Engine, group_id: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(select(activity_unified).where(activity_unified.c.group_id == group_id))
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _laps(engine: Engine, activity: dict[str, Any]) -> list[dict[str, Any]]:
    """The laps of the first source that has them (Garmin first), with their start."""
    refs = sorted(
        (str(r) for r in activity.get("sources") or [] if ":" in str(r)),
        key=lambda r: not r.startswith("garmin:"),
    )
    with engine.connect() as conn:
        for ref in refs:
            source, source_id = ref.split(":", 1)
            rows = [
                dict(r)
                for r in conn.execute(
                    select(activity_lap)
                    .where(
                        activity_lap.c.source == source,
                        activity_lap.c.activity_source_id == source_id,
                    )
                    .order_by(activity_lap.c.lap_index)
                ).mappings()
            ]
            if rows:
                return _with_offsets(rows, activity.get("start_utc"))
    return []


def _comparable(a: Any, b: Any) -> bool:
    """Both datetimes, both with or both without a zone."""
    return (
        isinstance(a, datetime)
        and isinstance(b, datetime)
        and (a.tzinfo is None) == (b.tzinfo is None)
    )


def _with_offsets(rows: list[dict[str, Any]], start: datetime | None) -> list[dict[str, Any]]:
    """Each lap's start and length in seconds from the activity's start."""
    out = []
    at = 0.0
    for row in rows:
        seconds = float(row.get("elapsed_s") or row.get("moving_s") or 0)
        begin = row.get("start_utc")
        if (
            isinstance(begin, datetime)
            and isinstance(start, datetime)
            and _comparable(start, begin)
        ):
            at = max(0.0, (begin - start).total_seconds())
        out.append(
            {
                "index": row["lap_index"],
                "start_s": at,
                "seconds": seconds,
                "distance_m": float(row.get("distance_m") or 0),
                "avg_hr": row.get("avg_hr"),
                "avg_power": row.get("avg_power"),
            }
        )
        at += seconds
    return out


def _stream(parquet: ParquetStore | None, activity: dict[str, Any]) -> dict[str, list] | None:
    """``{"t": seconds from the activity's start, channel: values}``, or None."""
    key = activity.get("stream_key")
    if parquet is None or not key or not parquet.stream_path(key).exists():
        return None
    table = parquet.read_stream(key)
    times = table.column("t").to_pylist()
    if not times:
        return None
    start = activity.get("start_utc")
    zero = start if _comparable(start, times[0]) else times[0]
    out: dict[str, list] = {"t": [(t - zero).total_seconds() for t in times]}
    for name in ("power", "hr"):
        if name in table.column_names:
            out[name] = table.column(name).to_pylist()
    return out


# --- stream arithmetic --------------------------------------------------------------------


def grid(times: list[float], values: list[float | None], hold: float) -> list[float | None]:
    """One value per second: each sample holds until the next one, at most ``hold`` s."""
    if not times:
        return []
    size = math.floor(max(times)) + 1
    out: list[float | None] = [None] * max(size, 0)
    for i, (at, value) in enumerate(zip(times, values, strict=False)):
        if value is None or at < 0 or (isinstance(value, float) and math.isnan(value)):
            continue
        until = times[i + 1] if i + 1 < len(times) else at + 1
        stop = min(until, at + hold)
        for second in range(math.ceil(at), max(math.ceil(stop), math.ceil(at) + 1)):
            if 0 <= second < size:
                out[second] = float(value)
    return out


def best_mean(series: list[float | None], window: int, coverage: float) -> dict | None:
    """The highest mean over ``window`` seconds (a missing second counts as 0, and a window
    needs ``coverage`` of its seconds measured): ``{"value", "start_s"}``."""
    if window <= 0 or len(series) < window:
        return None
    sums = [0.0]
    counts = [0]
    for value in series:
        sums.append(sums[-1] + (value or 0.0))
        counts.append(counts[-1] + (value is not None))
    best = None
    for start in range(0, len(series) - window + 1):
        if counts[start + window] - counts[start] < coverage * window:
            continue
        mean = (sums[start + window] - sums[start]) / window
        if best is None or mean > best["value"]:
            best = {"value": mean, "start_s": start}
    return best


def mean_between(
    series: list[float | None], start: float, end: float, coverage: float
) -> float | None:
    """The mean of the measured seconds in [start, end), when enough of them are measured."""
    lo, hi = max(0, round(start)), min(len(series), round(end))
    values = [v for v in series[lo:hi] if v is not None]
    if hi <= lo or len(values) < coverage * (hi - lo):
        return None
    return sum(values) / len(values)


def _matching(laps: list[dict], seconds: float, share: float) -> list[dict]:
    return [
        lap for lap in laps if lap["seconds"] and abs(lap["seconds"] - seconds) <= share * seconds
    ]


def _distance_laps(laps: list[dict], metres: float, share: float) -> list[dict]:
    return [
        lap
        for lap in laps
        if lap["distance_m"]
        and lap["seconds"]
        and abs(lap["distance_m"] - metres) <= share * metres
    ]


# --- the calculations ---------------------------------------------------------------------


def _missing(metric: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"metric": metric, "reason": reason, **detail}


def ftp(laps: list[dict], stream: dict | None, proto: dict[str, Any]) -> dict[str, Any]:
    """FTP from a ride test: the test lap's mean power, else the best rolling mean."""
    minutes = proto.get("ftp_minutes")
    if not minutes:
        return _missing("ftp", "no_protocol")
    seconds = minutes * 60
    steps = [lap for lap in _matching(laps, seconds, proto["step_match"]) if lap["avg_power"]]
    if steps:
        lap = max(steps, key=lambda item: float(item["avg_power"]))
        mean = float(lap["avg_power"])
        basis = {"from": "lap", "lap": lap["index"], "seconds": round(lap["seconds"])}
    else:
        power = (stream or {}).get("power")
        if not power or not any(v for v in power):
            return _missing("ftp", "no_power")
        series = grid(stream["t"], power, proto["hold_s"])  # type: ignore[index]
        found = best_mean(series, int(seconds), proto["coverage"])
        if found is None:
            return _missing("ftp", "too_short", minutes=minutes)
        mean = found["value"]
        basis = {"from": "stream", "start_s": found["start_s"], "seconds": int(seconds)}
    return {
        "metric": "ftp",
        "value": round(mean * proto["ftp_factor"]),
        "unit": "W",
        "basis": {**basis, "mean_power": round(mean, 1), "factor": proto["ftp_factor"]},
    }


def css(laps: list[dict], proto: dict[str, Any]) -> dict[str, Any]:
    """CSS from the long and the short all-out swim (the fastest lap of each distance)."""
    long_m, short_m = proto.get("css_long_m"), proto.get("css_short_m")
    if not long_m or not short_m or long_m <= short_m:
        return _missing("css", "no_protocol")
    if not laps:
        return _missing("css", "no_laps")
    longs = _distance_laps(laps, long_m, proto["distance_match"])
    shorts = _distance_laps(laps, short_m, proto["distance_match"])
    if not longs or not shorts:
        return _missing("css", "no_test_laps", long_m=long_m, short_m=short_m)
    t_long = min(longs, key=lambda lap: lap["seconds"] / lap["distance_m"])
    t_short = min(shorts, key=lambda lap: lap["seconds"] / lap["distance_m"])
    # Each time at the planned distance (a pool's length can make a lap a few metres off).
    long_s = t_long["seconds"] * long_m / t_long["distance_m"]
    short_s = t_short["seconds"] * short_m / t_short["distance_m"]
    if long_s <= short_s:
        return _missing("css", "times_do_not_fit")
    per_100 = (long_s - short_s) / (long_m - short_m) * 100
    return {
        "metric": "css",
        "value": round(per_100, 1),
        "unit": "s/100m",
        "basis": {
            "from": "laps",
            "long": {"lap": t_long["index"], "metres": long_m, "seconds": round(long_s, 1)},
            "short": {"lap": t_short["index"], "metres": short_m, "seconds": round(short_s, 1)},
        },
    }


def run_test(laps: list[dict], stream: dict | None, proto: dict[str, Any]) -> list[dict[str, Any]]:
    """LTHR (mean HR over the last part of the test lap) and threshold pace (its mean pace)."""
    minutes = proto.get("run_minutes")
    if not minutes:
        return [_missing("lthr", "no_protocol"), _missing("threshold_pace", "no_protocol")]
    seconds = minutes * 60
    steps = _matching(laps, seconds, proto["step_match"])
    if not steps:
        reason = "no_laps" if not laps else "no_test_lap"
        return [
            _missing("lthr", reason, minutes=minutes),
            _missing("threshold_pace", reason, minutes=minutes),
        ]
    # The fastest lap of the test length is the test.
    lap = max(
        steps, key=lambda item: (item["distance_m"] / item["seconds"]) if item["seconds"] else 0
    )
    out: list[dict[str, Any]] = []
    basis = {"from": "lap", "lap": lap["index"], "seconds": round(lap["seconds"])}
    hr = (stream or {}).get("hr")
    last = min(proto["run_lthr_last_min"] * 60, lap["seconds"])
    mean_hr = None
    if hr and any(v for v in hr):
        series = grid(stream["t"], hr, proto["hold_s"])  # type: ignore[index]
        end = lap["start_s"] + lap["seconds"]
        mean_hr = mean_between(series, end - last, end, proto["coverage"])
    if mean_hr is None:
        out.append(_missing("lthr", "no_heart_rate"))
    else:
        out.append(
            {
                "metric": "lthr",
                "value": round(mean_hr),
                "unit": "bpm",
                "basis": {**basis, "last_seconds": round(last), "mean_hr": round(mean_hr, 1)},
            }
        )
    if lap["distance_m"] > 0:
        pace = lap["seconds"] / lap["distance_m"] * 1000
        out.append(
            {
                "metric": "threshold_pace",
                "value": round(pace, 1),
                "unit": "s/km",
                "basis": {**basis, "metres": round(lap["distance_m"])},
            }
        )
    else:
        out.append(_missing("threshold_pace", "no_distance"))
    return out


def max_hr(activity: dict[str, Any], current: float | None) -> dict[str, Any]:
    """The activity's highest heart rate, when reliable and above the current max HR."""
    value = activity.get("max_hr")
    low, high = thresholds.RANGES["max_hr"]
    if not value:
        return _missing("max_hr", "no_heart_rate")
    if not low <= float(value) <= min(high, 220):
        return _missing("max_hr", "not_reliable")
    if current is not None and float(value) <= current + thresholds.MAX_HR_MARGIN:
        return _missing("max_hr", "not_higher", current=current)
    return {
        "metric": "max_hr",
        "value": round(float(value)),
        "unit": "bpm",
        "basis": {"from": "activity"},
    }


# --- one activity -------------------------------------------------------------------------


def evaluate(engine: Engine, parquet: ParquetStore | None, activity_id: str) -> dict[str, Any]:
    """What a marked test or race gives, without writing: ``values`` (each a would-be
    proposal) and ``missing`` (each metric without one, with the reason)."""
    activity = _activity(engine, activity_id)
    if activity is None:
        raise LookupError("activity not found")
    purpose = activity.get("purpose")
    sport = SPORT.get(family(activity.get("sport")) or "")
    out: dict[str, Any] = {
        "activity_id": activity_id,
        "date": activity["local_date"].isoformat()
        if isinstance(activity.get("local_date"), date)
        else activity.get("local_date"),
        "name": activity.get("name"),
        "purpose": purpose,
        "sport": sport,
        "test": None,
        "values": [],
        "missing": [],
    }
    if purpose not in ("test", "race"):
        out["missing"].append(_missing("*", "not_marked"))
        return out
    if sport is None:
        out["missing"].append(_missing("*", "sport_without_thresholds"))
        return out
    proto = protocol(engine)
    out["protocol"] = proto
    current = thresholds.current_values(engine).get(sport) or {}
    found: list[dict[str, Any]] = []
    if purpose == "test":
        laps = _laps(engine, activity)
        stream = _stream(parquet, activity) if sport in ("bike", "run") else None
        if sport == "bike":
            out["test"] = "ftp"
            found.append(ftp(laps, stream, proto))
        elif sport == "run":
            out["test"] = "run_threshold"
            found.extend(run_test(laps, stream, proto))
        else:
            out["test"] = "css"
            found.append(css(laps, proto))
    if sport in ("bike", "run"):
        have = current.get("max_hr")
        found.append(max_hr(activity, float(have["value"]) if have else None))
    for item in found:
        if "value" not in item:
            out["missing"].append(item)
            continue
        try:
            thresholds.validate(sport, item["metric"], item["value"])
        except ValueError:
            out["missing"].append(_missing(item["metric"], "out_of_range", value=item["value"]))
            continue
        have = current.get(item["metric"])
        if have is not None and thresholds._same(item["metric"], have["value"], item["value"]):
            out["missing"].append(_missing(item["metric"], "same_as_current", value=item["value"]))
            continue
        item["current"] = have["value"] if have else None
        out["values"].append(item)
    return out


def _calculated_rows(engine: Engine, activity_id: str) -> list[dict[str, Any]]:
    """The ledger rows calculated from this activity."""
    out = []
    with engine.connect() as conn:
        for row in conn.execute(select(threshold_table)).mappings():
            history = row["history"] or []
            if any(
                isinstance(e, dict)
                and e.get("event") == CALCULATED
                and e.get("activity_id") == activity_id
                for e in history
            ):
                out.append(dict(row))
    return out


def propose(engine: Engine, parquet: ParquetStore | None, activity_id: str) -> dict[str, Any]:
    """Evaluate and store each value as a proposal (once per activity and metric)."""
    result = evaluate(engine, parquet, activity_id)
    if not result["values"]:
        result["proposed"] = []
        return result
    done = {(r["sport"], r["metric"]) for r in _calculated_rows(engine, activity_id)}
    proposed = []
    method = "race" if result["purpose"] == "race" else "test"
    when = date.fromisoformat(result["date"]) if result.get("date") else None
    for item in result["values"]:
        if (result["sport"], item["metric"]) in done:
            item["already_proposed"] = True
            continue
        row = athlete_threshold.propose_threshold(
            engine,
            sport=result["sport"],
            metric=item["metric"],
            value=float(item["value"]),
            unit=item["unit"],
            method=method,
            date_set=when,
            history=[
                {
                    "event": CALCULATED,
                    "activity_id": activity_id,
                    "test": result["test"] or method,
                    "basis": item["basis"],
                }
            ],
        )
        item["proposal_id"] = row["id"]
        proposed.append(row["id"])
    result["proposed"] = proposed
    return result


def withdraw(engine: Engine, activity_id: str) -> list[int]:
    """The open proposals calculated from this activity are dismissed (its mark was removed)."""
    now = utc_now()
    ids = []
    for row in _calculated_rows(engine, activity_id):
        if row["status"] != "proposed":
            continue
        history = [
            *(row["history"] or []),
            {"at": now.isoformat(), "event": "withdrawn", "why": "unmarked"},
        ]
        with engine.begin() as conn:
            conn.execute(
                update(threshold_table)
                .where(threshold_table.c.id == row["id"])
                .values(status="dismissed", history=history, updated_at=now)
            )
        ids.append(row["id"])
    return ids


def follow_purpose(
    engine: Engine, parquet: ParquetStore | None, activity_id: str
) -> dict[str, Any]:
    """After a purpose change: a test or race proposes its values, anything else withdraws
    the open proposals it gave before."""
    activity = _activity(engine, activity_id)
    if activity is None:
        raise LookupError("activity not found")
    if activity.get("purpose") in ("test", "race"):
        return propose(engine, parquet, activity_id)
    return {"activity_id": activity_id, "withdrawn": withdraw(engine, activity_id)}


def sources_of(engine: Engine, activity_id: str) -> list[dict[str, Any]]:
    """The proposals, current and past values that came from this activity (the activity
    page says so)."""
    return [
        {
            "id": r["id"],
            "sport": thresholds.canonical(r["sport"], r["metric"])[0],
            "metric": thresholds.canonical(r["sport"], r["metric"])[1],
            "value": thresholds.stored_value(r),
            "unit": r["unit"],
            "status": r["status"],
        }
        for r in _calculated_rows(engine, activity_id)
    ]
