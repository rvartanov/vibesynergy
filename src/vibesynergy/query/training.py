"""Training and intraday queries (docs/MCP.md §3.4 to §3.6).

Load model: the performance management chart (Banister). CTL is a 42-day and ATL a 7-day
exponentially weighted average of daily load. Loads:

- ``intervals`` (the portal's and MCP's default): intervals.icu's own numbers
  (wellness fitness/fatigue/ramp rate and each activity's load), so they match its app;
  days it has nothing for are computed with its algorithm from VibeSynergy's TRIMP and
  labelled as such. Form is the same day's CTL - ATL. See ``query.icu_fitness``.
- ``trimp``: Banister's TRIMP from each activity's duration and average heart rate, which
  every activity with heart rate has from any device; ``tss`` from power and FTP; and
  ``garmin``, Garmin's per-activity training load. For these, TSB = CTL - ATL of the
  previous day (VibeSynergy's own model, which internal rules still read).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.compute as pc
import pyarrow.dataset as pads
from sqlalchemy import Engine, and_, func, select

from vibesynergy.core.timeutil import Window
from vibesynergy.query import series as qs
from vibesynergy.query.plan import family as plan_family
from vibesynergy.store.parquet import ParquetStore
from vibesynergy.store.schema import activity_unified, zone_set

CURVE_DURATIONS = (5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600)
FITNESS_METRICS = (
    "fitness.vo2max.run",
    "fitness.vo2max.ride",
    "fitness.fitness_age",
    "fitness.race_pred.5k",
    "fitness.race_pred.10k",
    "fitness.race_pred.half",
    "fitness.race_pred.marathon",
)


LOADS = ("intervals", "trimp", "garmin", "tss", "vibesynergy")
BEST_DISTANCES = (400, 1000, 1609.34, 5000, 10000, 21097.5)
# Faster than this between two samples is not running (a bike, car or train mid-activity);
# best efforts are only looked for between such stretches.
MAX_RUN_SPEED = 6.0  # m/s, 2:47 per km, averaged over NOT_RUNNING_S
NOT_RUNNING_S = 10
DEFAULT_REST_HR = 60.0
# Banister's weighting of heart-rate reserve (y = k * e^(b * hrr)); the male one when unset.
TRIMP_WEIGHTS = {"male": (0.64, 1.92), "female": (0.86, 1.67)}


def trimp(minutes: float, avg_hr: float, rest_hr: float, max_hr: float, sex: str | None) -> float:
    """Banister's TRIMP: minutes x heart-rate reserve x a weighting that grows with effort."""
    if max_hr <= rest_hr:
        return 0.0
    hrr = min(max((avg_hr - rest_hr) / (max_hr - rest_hr), 0.0), 1.0)
    k, b = TRIMP_WEIGHTS.get(sex or "male", TRIMP_WEIGHTS["male"])
    return minutes * hrr * k * math.exp(b * hrr)


def hr_profile(engine: Engine, athlete: dict[str, Any] | None = None) -> dict[str, Any]:
    """Max and resting heart rate for TRIMP: the config, else the highest max HR in the zone
    history and the median resting HR on record."""
    athlete = athlete or {}
    max_hr, max_from = athlete.get("max_hr"), "config"
    if max_hr is None:
        with engine.connect() as conn:
            max_hr = conn.execute(
                select(func.max(zone_set.c.max_value)).where(zone_set.c.kind == "hr")
            ).scalar()
        max_from = "heart-rate zones"
    rest_hr, rest_from = athlete.get("resting_hr"), "config"
    if rest_hr is None:
        resting = sorted(
            qs.daily_values(engine, ["hr.resting"], Window(date(1900, 1, 1), date(2100, 1, 1)))[
                "hr.resting"
            ].values()
        )
        rest_hr = resting[len(resting) // 2] if resting else DEFAULT_REST_HR
        rest_from = "median resting HR" if resting else "default"
    return {
        "max_hr": float(max_hr) if max_hr else None,
        "rest_hr": float(rest_hr),
        "sex": athlete.get("sex"),
        "max_from": max_from,
        "rest_from": rest_from,
    }


def daily_trimp(
    engine: Engine,
    window: Window,
    profile: dict[str, Any],
    *,
    sport: str | None = None,
    include_coaching: bool = True,
) -> dict[date, float]:
    t = activity_unified
    where = [t.c.local_date.between(window.start, window.end), t.c.avg_hr.is_not(None)]
    if sport:
        where.append(t.c.sport.ilike(f"%{sport}%"))
    if not include_coaching:
        where.append(t.c.kind.is_distinct_from("coaching"))
    out: dict[date, float] = defaultdict(float)
    if not profile["max_hr"]:
        return out
    with engine.connect() as conn:
        for day, seconds, avg_hr in conn.execute(
            select(t.c.local_date, qs.activity_column("moving_s"), t.c.avg_hr).where(and_(*where))
        ):
            if seconds:
                out[day] += trimp(
                    seconds / 60, avg_hr, profile["rest_hr"], profile["max_hr"], profile["sex"]
                )
    return out


def ftp_by_sport(engine: Engine) -> dict[str, float]:
    """The latest FTP per sport from the power zone history (e.g. cycling, running)."""
    out: dict[str, float] = {}
    with engine.connect() as conn:
        for sport, threshold in conn.execute(
            select(zone_set.c.sport, zone_set.c.threshold)
            .where(zone_set.c.kind == "power", zone_set.c.threshold.is_not(None))
            .order_by(zone_set.c.valid_from)
        ):
            out[sport] = float(threshold)
    return out


def tss(seconds: float, power: float, ftp: float) -> float:
    """Training Stress Score: hours x IF^2 x 100, IF = (normalized) power / FTP."""
    intensity = power / ftp
    return seconds / 3600 * intensity * intensity * 100


def daily_tss(
    engine: Engine,
    window: Window,
    ftps: dict[str, float],
    *,
    sport: str | None = None,
    include_coaching: bool = True,
) -> dict[date, float]:
    """TSS per day from activities with power and an FTP for their sport (normalized power
    when present, else average power, which reads low for uneven efforts)."""
    t = activity_unified
    where = [
        t.c.local_date.between(window.start, window.end),
        (t.c.norm_power.is_not(None)) | (t.c.avg_power.is_not(None)),
    ]
    if sport:
        where.append(t.c.sport.ilike(f"%{sport}%"))
    if not include_coaching:
        where.append(t.c.kind.is_distinct_from("coaching"))
    out: dict[date, float] = defaultdict(float)
    with engine.connect() as conn:
        for day, kind, seconds, norm, avg in conn.execute(
            select(
                t.c.local_date,
                t.c.sport,
                qs.activity_column("moving_s"),
                t.c.norm_power,
                t.c.avg_power,
            ).where(and_(*where))
        ):
            text = (kind or "").lower()
            ftp = (
                ftps.get("running")
                if "run" in text
                else ftps.get("cycling")
                if "cycl" in text
                else None
            )
            if ftp and seconds:
                out[day] += tss(seconds, norm or avg, ftp)
    return out


def daily_vibesynergy(
    engine: Engine,
    window: Window,
    athlete: dict[str, Any] | None = None,
    *,
    sport: str | None = None,
    include_coaching: bool = True,
) -> dict[date, float]:
    """VibeSynergy's own load per day, from its own data only: power (TSS from NP and
    FTP) for rides with power, else heart rate (TRIMP scaled so an hour at LTHR is 100), else
    pace (swim and run, from CSS and threshold pace). The thresholds are the confirmed ones
    (Zones & thresholds); resting and max heart rate as for TRIMP."""
    from vibesynergy.query import own_load, thresholds
    from vibesynergy.query import plan as plan_q

    values = thresholds.current_values(engine)
    profile = hr_profile(engine, athlete)
    rest = profile["rest_hr"]

    def value(fam: str, metric: str) -> float | None:
        key = {"ride": "bike", "run": "run", "swim": "swim"}.get(fam, fam)
        row = (values.get(key) or {}).get(metric) or {}
        return row.get("value")

    t = activity_unified
    where: list[Any] = [t.c.local_date.between(window.start, window.end)]
    if sport:
        where.append(t.c.sport.ilike(f"%{sport}%"))
    if not include_coaching:
        where.append(t.c.kind.is_distinct_from("coaching"))
    daily: dict[date, float] = defaultdict(float)
    css = value("swim", "css")
    threshold_pace = value("run", "threshold_pace")
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                t.c.local_date,
                t.c.sport,
                t.c.moving_s,
                t.c.elapsed_s,
                t.c.norm_power,
                t.c.avg_power,
                t.c.avg_hr,
                t.c.avg_speed,
            ).where(and_(*where))
        ).all()
    for day, kind, moving, elapsed, np_watts, avg_power, avg_hr, speed in rows:
        seconds = moving or elapsed or 0
        if seconds <= 0:
            continue
        fam = plan_q.family(kind) or "other"
        top = value(fam, "max_hr") or profile["max_hr"]
        lthr = value(fam, "lthr") or value("run", "lthr")
        ftp = value("ride", "ftp")
        load = 0.0
        if fam == "ride" and (np_watts or avg_power) and ftp:
            load = own_load.power_load(seconds, float(np_watts or avg_power), float(ftp))
        elif avg_hr and lthr and top:
            load = own_load.hr_load(seconds, float(avg_hr), float(lthr), rest, float(top))
        elif speed and fam == "swim" and css:
            load = own_load.pace_load(seconds, float(speed), 100 / float(css), 3)
        elif speed and fam == "run" and threshold_pace:
            load = own_load.pace_load(seconds, float(speed), 1000 / float(threshold_pace), 2)
        daily[day] += load
    return daily


def training_load(
    engine: Engine,
    window: Window,
    *,
    sport: str | None = None,
    load: str = "trimp",
    athlete: dict[str, Any] | None = None,
    include_coaching: bool = True,
    warmup_days: int = 126,
    raw=None,
) -> dict[str, Any]:
    if load not in LOADS:
        raise ValueError(f"load must be one of {', '.join(LOADS)}")
    # Which model is "the" load: intervals.icu's, or VibeSynergy's own when the athlete
    # chose it ([athlete] load_source = "vibesynergy"). Every page and the coach follow it.
    if load == "intervals" and (athlete or {}).get("load_source") == "vibesynergy":
        load = "vibesynergy"
    if load == "intervals":
        return _intervals_load(
            engine,
            window,
            sport=sport,
            athlete=athlete,
            include_coaching=include_coaching,
            warmup_days=warmup_days,
            raw=raw,
        )
    # Default warm-up is 3 x the 42-day CTL constant, before the window starts.
    warmup = Window(window.start - timedelta(days=warmup_days), window.end)
    profile = None
    if load == "trimp":
        profile = hr_profile(engine, athlete)
        daily = daily_trimp(engine, warmup, profile, sport=sport, include_coaching=include_coaching)
    elif load == "tss":
        daily = daily_tss(
            engine, warmup, ftp_by_sport(engine), sport=sport, include_coaching=include_coaching
        )
    elif load == "vibesynergy":
        # The coach and the assessments read hr_profile whatever the model (8-11 crash).
        profile = hr_profile(engine, athlete)
        daily = daily_vibesynergy(
            engine, warmup, athlete, sport=sport, include_coaching=include_coaching
        )
    else:
        t = activity_unified
        where = [t.c.local_date.between(warmup.start, warmup.end)]
        if sport:
            where.append(t.c.sport.ilike(f"%{sport}%"))
        if not include_coaching:
            where.append(t.c.kind.is_distinct_from("coaching"))
        daily = defaultdict(float)
        with engine.connect() as conn:
            for day, value in conn.execute(
                select(t.c.local_date, t.c.training_load).where(and_(*where))
            ):
                daily[day] += value or 0.0
    coaching_daily = {}
    if include_coaching:
        if load == "trimp":
            all_daily = daily_trimp(engine, warmup, profile, sport=sport, include_coaching=True)
            own_daily = daily_trimp(engine, warmup, profile, sport=sport, include_coaching=False)
        elif load == "tss":
            ftps = ftp_by_sport(engine)
            all_daily = daily_tss(engine, warmup, ftps, sport=sport, include_coaching=True)
            own_daily = daily_tss(engine, warmup, ftps, sport=sport, include_coaching=False)
        elif load == "vibesynergy":
            all_daily = daily
            own_daily = daily_vibesynergy(
                engine, warmup, athlete, sport=sport, include_coaching=False
            )
        else:
            own_daily = defaultdict(float)
            with engine.connect() as conn:
                for day, value in conn.execute(
                    select(t.c.local_date, t.c.training_load).where(
                        and_(*where), t.c.kind.is_distinct_from("coaching")
                    )
                ):
                    own_daily[day] += value or 0.0
            all_daily = daily
        coaching_daily = {
            d: round(value - own_daily.get(d, 0), 1)
            for d, value in all_daily.items()
            if value != own_daily.get(d, 0)
        }
    ctl = atl = 0.0
    rows = []
    k_ctl, k_atl = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)
    for d in warmup.days():
        tsb = ctl - atl
        day_load = daily.get(d, 0.0)
        ctl += (day_load - ctl) * k_ctl
        atl += (day_load - atl) * k_atl
        if d >= window.start:
            rows.append([str(d), round(day_load, 1), round(ctl, 1), round(atl, 1), round(tsb, 1)])
    last = rows[-1] if rows else None
    ramp = None
    if len(rows) >= 8:
        ramp = round(rows[-1][2] - rows[-8][2], 1)
    form = None
    if last:
        tsb = last[4]
        form = (
            "fresh"
            if tsb > 5
            else "neutral"
            if tsb > -10
            else "optimal training"
            if tsb > -30
            else "overreaching risk"
        )
    result = {
        "columns": ["date", "load", "ctl", "atl", "tsb"],
        "rows": rows,
        "coaching_daily_load": {str(d): v for d, v in coaching_daily.items() if d >= window.start},
        "include_coaching": include_coaching,
        "current": {
            "ctl": last[2],
            "atl": last[3],
            "tsb": last[4],
            "form": form,
            "ctl_ramp_7d": ramp,
        }
        if last
        else None,
        "load": load,
        "load_metric": {
            "trimp": "TRIMP (Banister) from duration and average heart rate",
            "tss": "TSS from power and FTP (normalized power when present, else average "
            "power); activities without power add nothing",
            "garmin": "Garmin training load per activity",
            "vibesynergy": "VibeSynergy's own load: power (TSS) for rides with power, else heart "
            "rate (scaled so an hour at LTHR is 100), else pace; from the confirmed thresholds",
        }[load],
        "hr_profile": profile,
        "warmup_note": (
            f"CTL/ATL start {warmup_days} days before the period so early values are settled; "
            "history before the first synced activity counts as zero."
            if warmup_days
            else "CTL/ATL start at the beginning of the period; earlier history counts as zero."
        ),
    }
    return _vibesynergy_shape(result) if load == "vibesynergy" else result


def _vibesynergy_shape(result: dict[str, Any]) -> dict[str, Any]:
    """VibeSynergy's own model in the same shape as intervals.icu's fitness, so the
    Form tab, the dashboard and the coach read it unchanged."""
    from vibesynergy.query import icu_fitness

    rows = []
    for i, (day, load, ctl, atl, _previous) in enumerate(result["rows"]):
        tsb = round(ctl - atl, 1)  # form = same-day CTL - ATL, like intervals.icu
        ramp = round(ctl - result["rows"][i - 7][2], 1) if i >= 7 else None
        rows.append(
            [
                day,
                load,
                ctl,
                atl,
                tsb,
                ramp,
                icu_fitness.form_pct(ctl, tsb),
                "vibesynergy",
                "vibesynergy",
            ]
        )
    current = result["current"]
    if current and rows:
        ctl, atl, tsb = rows[-1][2], rows[-1][3], rows[-1][4]
        current = {
            **current,
            "tsb": tsb,
            "form_pct": icu_fitness.form_pct(ctl, tsb),
            "form_zone": icu_fitness.form_zone(ctl, tsb),
            "form": icu_fitness.legacy_form(tsb),
            "source": "vibesynergy",
        }
    return {
        **result,
        "columns": [
            "date",
            "load",
            "ctl",
            "atl",
            "tsb",
            "ramp",
            "form_pct",
            "source",
            "load_source",
        ],
        "rows": rows,
        "current": current,
        "source": "vibesynergy",
        "days_by_source": {"intervals": 0, "computed": 0, "vibesynergy": len(rows)},
        "load_days_by_source": {"vibesynergy": len(rows)},
        "has_intervals": False,
    }


def _intervals_load(
    engine: Engine,
    window: Window,
    *,
    sport: str | None,
    athlete: dict[str, Any] | None,
    include_coaching: bool,
    warmup_days: int,
    raw,
) -> dict[str, Any]:
    """intervals.icu's fitness model, with VibeSynergy's TRIMP for days it has nothing for."""
    from vibesynergy.query import icu_fitness

    warmup = Window(window.start - timedelta(days=warmup_days), window.end)
    profile = hr_profile(engine, athlete)
    every = daily_trimp(engine, warmup, profile, sport=sport, include_coaching=True)
    own = daily_trimp(engine, warmup, profile, sport=sport, include_coaching=False)
    coaching = {d: v - own.get(d, 0.0) for d, v in every.items() if v != own.get(d, 0.0)}
    result = icu_fitness.fitness(
        engine,
        raw,
        window,
        sport=sport,
        include_coaching=include_coaching,
        trimp_daily=every,
        trimp_coaching=coaching,
        warmup_days=warmup_days,
    )
    days = result["days_by_source"]
    return {
        **result,
        "include_coaching": include_coaching,
        "load": "intervals",
        "load_metric": "intervals.icu fitness, fatigue and load where it has them; other "
        "days computed with its algorithm (42/7-day EWMA, form = same-day CTL - ATL) from "
        "intervals.icu's activity load, else TRIMP",
        "hr_profile": profile,
        "source": "intervals"
        if days["intervals"] and not days["computed"]
        else "mixed"
        if days["intervals"]
        else "intervals_load"
        if result["load_days_by_source"]["intervals"]
        else "vibesynergy",
        "warmup_note": (
            f"Computed days start {warmup_days} days before the period; intervals.icu's "
            "own values replace them wherever it has a wellness record."
        ),
    }


# Dashboard and the Training page share this snapshot. 180 days is long enough for CTL
# (42-day constant) to forget a zero start. The window ends on ``today``.
CURRENT_WARMUP_DAYS = 180


def current_load(rt, today: date) -> dict[str, Any]:
    """CTL, ATL and TSB for ``today``: intervals.icu's when it has them, else computed with
    its algorithm after a fixed 180-day warm-up ending that day."""
    start = today - timedelta(days=CURRENT_WARMUP_DAYS - 1)
    result = training_load(
        rt.engine,
        Window(start, today),
        athlete=rt.settings.athlete,
        warmup_days=0,
        load="intervals",
        raw=getattr(rt, "raw", None),
    )
    current = result.get("current") or {}
    return {
        "ctl": current.get("ctl"),
        "atl": current.get("atl"),
        "tsb": current.get("tsb"),
        "form": current.get("form"),
        "form_zone": current.get("form_zone"),
        "form_pct": current.get("form_pct"),
        "ctl_ramp_7d": current.get("ctl_ramp_7d"),
        "source": current.get("source"),
    }


def volume(
    engine: Engine,
    window: Window,
    *,
    granularity: str = "week",
    by: str = "sport",
    include_coaching: bool = True,
) -> dict[str, Any]:
    t = activity_unified
    rows = []
    with engine.connect() as conn:
        for r in conn.execute(
            select(
                t.c.local_date,
                t.c.sport,
                qs.activity_column("moving_s"),
                t.c.distance_m,
                t.c.ascent_m,
                t.c.training_load,
                t.c.kind,
            ).where(t.c.local_date.between(window.start, window.end))
        ):
            rows.append(r)
    buckets: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"count": 0, "moving_s": 0.0, "distance_m": 0.0, "ascent_m": 0.0, "load": 0.0}
    )
    for d, sport, moving, dist, ascent, load, kind in rows:
        if kind == "coaching" and not include_coaching:
            continue
        category = "coaching" if kind == "coaching" else sport if by == "sport" else "all"
        key = (str(qs.bucket(d, granularity)), category)
        b = buckets[key]
        b["count"] += 1
        b["moving_s"] += moving or 0
        b["distance_m"] += dist or 0
        b["ascent_m"] += ascent or 0
        b["load"] += load or 0
    out = [
        {"period": p, by: s, **{k: round(v, 1) for k, v in vals.items()}}
        for (p, s), vals in sorted(buckets.items())
    ]
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in out:
        for k in ("count", "moving_s", "distance_m", "ascent_m", "load"):
            totals[row[by]][k] += row[k]
    return {"buckets": out, "totals": {s: dict(v) for s, v in totals.items()}}


def fitness(engine: Engine, window: Window) -> dict[str, Any]:
    """Markers over time, as change points: a value is listed when it differs from the
    previous one. Thresholds come from the zone history (LTHR, FTP)."""
    values = qs.daily_values(engine, list(FITNESS_METRICS), window)
    markers: dict[str, list[dict]] = {}
    for metric, by_day in values.items():
        changes, previous = [], None
        for d in sorted(by_day):
            if by_day[d] != previous:
                changes.append({"date": str(d), "value": round(by_day[d], 2)})
                previous = by_day[d]
        if changes:
            markers[metric] = changes
    with engine.connect() as conn:
        zones = conn.execute(
            select(
                zone_set.c.kind,
                zone_set.c.sport,
                zone_set.c.valid_from,
                zone_set.c.threshold,
                zone_set.c.source,
            )
            .where(
                zone_set.c.kind.in_(("hr", "power")),
                zone_set.c.threshold.is_not(None),
                zone_set.c.valid_from <= window.end,
            )
            .order_by(zone_set.c.valid_from)
        ).all()
    thresholds: dict[str, list[dict]] = defaultdict(list)
    for kind, sport, valid_from, threshold, source in zones:
        name = f"{'lthr' if kind == 'hr' else 'ftp'}.{sport}"
        if not thresholds[name] or thresholds[name][-1]["value"] != threshold:
            thresholds[name].append({"date": str(valid_from), "value": threshold, "source": source})
    return {"markers": markers, "thresholds": dict(thresholds)}


# Sport names that mean a whole family for the power curve (``plan.family``).
_POWER_FAMILIES = {"cycl": "ride", "ride": "ride", "bike": "ride", "run": "run"}


def power_curve(
    engine: Engine,
    parquet: ParquetStore,
    window: Window,
    *,
    sport: str | None = None,
    durations: tuple[int, ...] = CURVE_DURATIONS,
) -> dict[str, Any]:
    """Best average power for each duration across activities with a power stream."""
    t = activity_unified
    where = [t.c.local_date.between(window.start, window.end), t.c.stream_key.is_not(None)]
    family = _POWER_FAMILIES.get(sport or "")
    if sport and family is None:
        where.append(t.c.sport.ilike(f"%{sport}%"))
    with engine.connect() as conn:
        candidates = conn.execute(
            select(t.c.group_id, t.c.local_date, t.c.name, t.c.sport, t.c.stream_key).where(
                and_(*where)
            )
        ).all()
    if family is not None:
        # By sport family, so road, virtual (Zwift), gravel and MTB rides all count and
        # running power never lands in a cycling curve.
        candidates = [c for c in candidates if plan_family(c.sport) == family]
    best: dict[int, dict[str, Any]] = {}
    used = 0
    for group_id, day, name, sport_name, key in candidates:
        path = parquet.stream_path(key)
        if not path.exists():
            continue
        table = parquet.read_stream(key)
        if "power" not in table.column_names:
            continue
        used += 1
        power = _per_second(table)
        for seconds in durations:
            avg = _best_rolling_mean(power, seconds)
            if avg is not None and avg > best.get(seconds, {}).get("watts", -1):
                best[seconds] = {
                    "watts": round(avg, 1),
                    "activity": group_id,
                    "date": str(day),
                    "name": name,
                    "sport": sport_name,
                }
    return {
        "activities_with_power": used,
        "curve": [{"seconds": s, **best[s]} for s in durations if s in best],
    }


def _per_second(table) -> list[float]:
    """Power resampled to 1 Hz (gaps count as 0 W, as recording pauses do)."""
    times = [v.timestamp() for v in table.column("t").to_pylist()]
    watts = table.column("power").to_pylist()
    if not times:
        return []
    start = int(times[0])
    out = [0.0] * (int(times[-1]) - start + 1)
    for ts, w in zip(times, watts, strict=True):
        if w is not None:
            out[int(ts) - start] = float(w)
    return out


def _best_rolling_mean(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    total = sum(values[:window])
    best = total
    for i in range(window, len(values)):
        total += values[i] - values[i - window]
        best = max(best, total)
    return best / window


def period_digest(engine: Engine, window: Window, previous: Window | None) -> dict[str, Any]:
    """Totals per sport plus sleep, recovery and body averages, with deltas."""
    keys = [
        "sleep.score",
        "sleep.duration",
        "hrv.night_avg",
        "hr.resting",
        "recovery.readiness",
        "daily.steps",
        "body.weight",
    ]

    def summarise(w: Window) -> dict[str, Any]:
        vol = volume(engine, w, granularity="year", by="sport")["totals"]
        avg = {m: v.get("mean") for m, v in qs.stats(engine, keys, w).items()}
        return {"by_sport": vol, "averages": avg}

    now = summarise(window)
    result: dict[str, Any] = {"period": now}
    if previous:
        before = summarise(previous)
        result["previous"] = before
        result["delta"] = {
            m: round(now["averages"][m] - before["averages"][m], 3)
            for m in keys
            if now["averages"].get(m) is not None and before["averages"].get(m) is not None
        }
    return result


def intraday(
    parquet: ParquetStore,
    day: date,
    timezone: str,
    *,
    metrics: list[str] | None = None,
    resample_min: int = 5,
) -> dict[str, Any]:
    """Wellness samples of one local day, averaged per ``resample_min`` minutes."""
    tz = ZoneInfo(timezone)
    start = datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)
    end = start + timedelta(days=1)
    files = parquet.wellness_files(start, end)  # not all ~70,000 files: those of this day
    if not files:
        return {"columns": [], "rows": []}
    dataset = pads.dataset([str(f) for f in files], format="parquet")
    flt = (pc.field("ts") >= start) & (pc.field("ts") < end)
    if metrics:
        flt = flt & pc.field("metric").isin(metrics)
    table = dataset.to_table(filter=flt, columns=["ts", "metric", "value"])
    buckets: dict[tuple[int, str], list[float]] = defaultdict(list)
    seen: set[tuple] = set()
    for ts, metric, value in zip(
        table.column("ts").to_pylist(),
        table.column("metric").to_pylist(),
        table.column("value").to_pylist(),
        strict=True,
    ):
        if (ts, metric) in seen:  # the same sample from two copies of a file
            continue
        seen.add((ts, metric))
        slot = int((ts - start).total_seconds() // (resample_min * 60))
        buckets[(slot, metric)].append(value)
    names = sorted({m for _, m in buckets})
    slots = sorted({s for s, _ in buckets})
    rows = []
    for s in slots:
        local = (start + timedelta(minutes=s * resample_min)).astimezone(tz)
        # Data contract (MCP and portal): the slot key keeps seconds; displays trim them.
        row: list[Any] = [local.strftime("%H:%M:%S")]
        for m in names:
            vals = buckets.get((s, m))
            row.append(round(sum(vals) / len(vals), 1) if vals else None)
        rows.append(row)
    return {"columns": ["time", *names], "rows": rows, "resample_minutes": resample_min}


def pace_curve(
    engine: Engine,
    parquet: ParquetStore,
    window: Window,
    *,
    sport: str = "run",
    distances: tuple[float, ...] = BEST_DISTANCES,
) -> dict[str, Any]:
    """Fastest time over each distance within any activity (from the distance stream)."""
    t = activity_unified
    where = [
        t.c.local_date.between(window.start, window.end),
        t.c.stream_key.is_not(None),
        t.c.sport.ilike(f"%{sport}%"),
    ]
    with engine.connect() as conn:
        candidates = conn.execute(
            select(t.c.group_id, t.c.local_date, t.c.name, t.c.sport, t.c.stream_key).where(
                and_(*where)
            )
        ).all()
    best: dict[float, dict[str, Any]] = {}
    used = 0
    for group_id, day, name, kind, key in candidates:
        if not parquet.stream_path(key).exists():
            continue
        table = parquet.read_stream(key)
        if "dist" not in table.column_names:
            continue
        times = [v.timestamp() for v in table.column("t").to_pylist()]
        dist = table.column("dist").to_pylist()
        pairs = [(s, d) for s, d in zip(times, dist, strict=True) if d is not None]
        if len(pairs) < 2:
            continue
        used += 1
        pieces = _running_pieces(pairs)
        for target in distances:
            found = [f for piece in pieces if (f := _fastest(piece, target)) is not None]
            fastest = min(found) if found else None
            if fastest is not None and fastest < best.get(target, {}).get("seconds", float("inf")):
                best[target] = {
                    "seconds": round(fastest, 1),
                    "activity": group_id,
                    "date": str(day),
                    "name": name,
                    "sport": kind,
                }
    return {
        "activities_with_distance": used,
        "curve": [{"distance_m": d, **best[d]} for d in distances if d in best],
    }


def _running_pieces(pairs: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Cut out stretches where the average speed over NOT_RUNNING_S seconds is faster than
    running can be (see MAX_RUN_SPEED): a bike, car or train mid-activity. Single-sample GPS
    spikes don't reach that average, so real runs stay whole."""
    fast = [False] * len(pairs)
    j = 0
    for i in range(len(pairs)):
        j = max(j, i)
        while j < len(pairs) - 1 and pairs[j][0] - pairs[i][0] < NOT_RUNNING_S:
            j += 1
        if (
            pairs[j][0] - pairs[i][0] >= NOT_RUNNING_S
            and _speed(pairs[i], pairs[j]) > MAX_RUN_SPEED
        ):
            for k in range(i, j + 1):
                fast[k] = True
    pieces, current = [], []
    for pair, drop in zip(pairs, fast, strict=True):
        if drop:
            if len(current) > 1:
                pieces.append(current)
            current = []
        else:
            current.append(pair)
    if len(current) > 1:
        pieces.append(current)
    return pieces


def _speed(a: tuple[float, float], b: tuple[float, float]) -> float:
    dt = b[0] - a[0]
    return (b[1] - a[1]) / dt if dt > 0 else 0.0


def _fastest(pairs: list[tuple[float, float]], target: float) -> float | None:
    """Shortest time covering ``target`` metres (two pointers over time, distance)."""
    best = None
    start = 0
    for end in range(len(pairs)):
        while start < end and pairs[end][1] - pairs[start + 1][1] >= target:
            start += 1
        if pairs[end][1] - pairs[start][1] >= target:
            span = pairs[end][0] - pairs[start][0]
            if span > 0 and (best is None or span < best):
                best = span
    return best
