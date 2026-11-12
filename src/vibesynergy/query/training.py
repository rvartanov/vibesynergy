"""Training and intraday queries (docs/MCP.md §3.4 to §3.6).

Load model: the performance management chart (Banister). CTL is a 42-day and ATL a 7-day
exponentially weighted average of daily load. Loads:

- ``vibesynergy`` (the default everywhere: portal, MCP, coach): VibeSynergy's own load per
  activity (``activity_loads``: power, pace or the heart-rate stream, from the thresholds
  valid that day), form is the same day's CTL - ATL. See ``query.fitness_model``.
- ``trimp``: Banister's TRIMP from each activity's duration and average heart rate; ``tss``
  from power and FTP; ``garmin``, Garmin's per-activity training load. For these, TSB = CTL -
  ATL of the previous day. Alternatives for comparison over MCP; nothing leads on them.
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


LOADS = ("vibesynergy", "trimp", "garmin", "tss")
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


# Garmin's threshold history (zone_set) by family and metric: (sport, kind, which value).
_GARMIN_THRESHOLDS = {
    ("ride", "ftp"): ("cycling", "power", "threshold"),
    ("ride", "lthr"): ("cycling", "hr", "threshold"),
    ("ride", "max_hr"): ("cycling", "hr", "max_value"),
    ("run", "lthr"): ("running", "hr", "threshold"),
    ("run", "max_hr"): ("running", "hr", "max_value"),
    ("swim", "lthr"): ("swimming", "hr", "threshold"),
    ("swim", "max_hr"): ("swimming", "hr", "max_value"),
    # No CSS: Garmin's oldest one (1.000 m/s since 2023) is a placeholder that never matched
    # the athlete's swims; its later ones are the confirmed values.
    ("other", "lthr"): ("default", "hr", "threshold"),
    ("other", "max_hr"): ("default", "hr", "max_value"),
}
_FAMILY_KEYS = {"ride": "bike", "run": "run", "swim": "swim", "other": "other"}


class ThresholdTimeline:
    """The thresholds valid on a given day, per family: the confirmed ones (Zones &
    thresholds, from the day they were set) before Garmin's zone history, and before the first
    known value the oldest one (nothing older is known; not for CSS, see ``NO_BACKFILL``)."""

    def __init__(self, engine: Engine, athlete: dict[str, Any] | None = None) -> None:
        from vibesynergy.query import thresholds

        self.profile = hr_profile(engine, athlete)
        series: dict[tuple[str, str], list[tuple[date, int, float]]] = defaultdict(list)
        # Priority 1 (confirmed) wins over 0 (Garmin) on the same day.
        for row in thresholds._ledger(engine):  # pyright: ignore[reportPrivateUsage]
            fam = str({"bike": "ride"}.get(row["sport"], row["sport"]))
            when = row["date_set"]
            if row["status"] in ("current", "superseded") and when:
                series[(fam, row["metric"])].append(
                    (date.fromisoformat(str(when)), 1, float(row["value"]))
                )
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    zone_set.c.sport,
                    zone_set.c.kind,
                    zone_set.c.valid_from,
                    zone_set.c.threshold,
                    zone_set.c.max_value,
                ).where(zone_set.c.source == "garmin")
            ).all()
        for (fam, metric), (sport, kind, which) in _GARMIN_THRESHOLDS.items():
            for row in rows:
                if row.sport != sport or row.kind != kind:
                    continue
                value = row.threshold if which == "threshold" else row.max_value
                if not value:
                    continue
                series[(fam, metric)].append((row.valid_from, 0, float(value)))
        self.series = {key: sorted(points) for key, points in series.items()}
        self._cache: dict[date, Any] = {}

    # Before the first known value the oldest one stands in, except for CSS: swims before
    # the first confirmed CSS count on heart rate (a guessed CSS weighs them arbitrarily).
    NO_BACKFILL = frozenset({("swim", "css")})

    def value(self, fam: str, metric: str, day: date) -> float | None:
        points = self.series.get((fam, metric))
        if not points:
            return None
        found = None
        for when, _priority, value in points:
            if when > day:
                break
            found = value
        if found is None and (fam, metric) not in self.NO_BACKFILL:
            found = points[0][2]
        return found

    def on(self, day: date):
        from vibesynergy.query import own_load

        if day not in self._cache:
            fams = ("ride", "run", "swim", "other")
            general = float(self.profile.get("max_hr") or 0)  # 0: no heart-rate load
            top = {f: self.value(f, "max_hr", day) or general for f in fams}
            # No LTHR known yet: 90 % of max heart rate, the usual estimate (Friel), so a new
            # install has a heart-rate load until Zones & thresholds has a real one.
            lthr = {
                f: v
                for f in fams
                if (v := self.value(f, "lthr", day) or (round(0.9 * top[f]) if top[f] else None))
            }
            self._cache[day] = own_load.Inputs(
                ftp=self.value("ride", "ftp", day),
                css=self.value("swim", "css", day),
                threshold_pace=self.value("run", "threshold_pace", day),
                lthr=lthr,
                max_hr=top,
                rest_hr=float(self.profile.get("rest_hr") or DEFAULT_REST_HR),
            )
        return self._cache[day]


def own_inputs(engine: Engine, athlete: dict[str, Any] | None = None):
    """The thresholds the own load is computed from, as valid today."""
    return ThresholdTimeline(engine, athlete).on(date.today())


def activity_loads(
    engine: Engine,
    window: Window,
    athlete: dict[str, Any] | None = None,
    *,
    parquet: ParquetStore | None = None,
    sport: str | None = None,
    include_coaching: bool = True,
) -> list[dict[str, Any]]:
    """VibeSynergy's own load per activity in ``window``: day, id, method and load. Kept in
    ``activity_load`` and computed again only when what it came from changed (the activity,
    its stream or the thresholds). Without ``parquet`` the heart-rate average stands in for
    the stream, and nothing is stored, so the stored value always comes from the stream."""
    from sqlalchemy.dialects.sqlite import insert

    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.query import own_load
    from vibesynergy.query import plan as plan_q
    from vibesynergy.store.schema import activity_load

    timeline = ThresholdTimeline(engine, athlete)
    t = activity_unified
    where: list[Any] = [t.c.local_date.between(window.start, window.end)]
    if sport:
        where.append(t.c.sport.ilike(f"%{sport}%"))
    if not include_coaching:
        where.append(t.c.kind.is_distinct_from("coaching"))
    fields = (
        t.c.group_id, t.c.local_date, t.c.sport, t.c.moving_s, t.c.elapsed_s, t.c.norm_power,
        t.c.avg_power, t.c.avg_hr, t.c.avg_speed, t.c.stream_key, t.c.start_utc,
    )  # fmt: skip
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(*fields).where(and_(*where)))]
        stored = {
            r.group_id: r
            for r in conn.execute(
                select(activity_load).where(
                    activity_load.c.group_id.in_([r["group_id"] for r in rows])
                )
            )
        }
    out, fresh = [], []
    for row in rows:
        seconds = row["moving_s"] or row["elapsed_s"] or 0
        if seconds <= 0 or str(row["sport"] or "").lower() in own_load.PARENT_SPORTS:
            continue
        fam = plan_q.family(row["sport"]) or "other"
        inputs = timeline.on(row["local_date"])
        has_stream = bool(
            parquet is not None
            and row["stream_key"]
            and fam != "swim"  # in the water the average beats the stream (measured 11-11)
            and parquet.stream_path(row["stream_key"]).exists()
        )
        method = own_load.method_for(
            fam,
            np_watts=row["norm_power"],
            avg_power=row["avg_power"],
            avg_hr=row["avg_hr"],
            speed=row["avg_speed"],
            has_stream=has_stream,
            inputs=inputs,
        )
        key = own_load.fingerprint(method, fam, row, inputs)
        hit = stored.get(row["group_id"])
        if hit is not None and hit.inputs == key:
            method, load = hit.method, hit.load
        elif method is None:
            load = 0.0
        else:
            stream = None
            if method == "hr_stream" and parquet is not None:
                table = parquet.read_stream(row["stream_key"])
                if "hr" in table.column_names:
                    times = [v.timestamp() for v in table.column("t").to_pylist()]
                    stream = (times, table.column("hr").to_pylist())
                else:
                    method = "hr"
            load = own_load.load_of(
                method,
                fam,
                float(seconds),
                np_watts=row["norm_power"],
                avg_power=row["avg_power"],
                avg_hr=row["avg_hr"],
                speed=row["avg_speed"],
                inputs=inputs,
                stream=stream,
            )
            if parquet is not None:
                fresh.append(
                    {"group_id": row["group_id"], "inputs": key, "method": method,
                     "load": round(load, 2), "computed_at": utc_now()}
                )  # fmt: skip
        out.append(
            {
                "date": row["local_date"],
                "id": row["group_id"],
                "method": method,
                "load": load,
                "family": fam,
                "start": row["start_utc"],
                "seconds": float(seconds),
            }
        )
    # Inside a read-only snapshot (the coach capturing the state of the day) there is no
    # transaction of our own to write in: the loads are used now and kept by a later read.
    if fresh and hasattr(engine, "begin"):
        with engine.begin() as conn:
            for chunk in range(0, len(fresh), 500):
                statement = insert(activity_load).values(fresh[chunk : chunk + 500])
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=["group_id"],
                        set_={
                            k: statement.excluded[k]
                            for k in ("inputs", "method", "load", "computed_at")
                        },
                    )
                )
    return own_load.without_duplicates(out)


def own_loads(engine: Engine, window: Window, ids: list[str]) -> dict[str, float]:
    """VibeSynergy's load per activity id: the stored one (from the stream), else computed
    from the heart-rate average for now (the next fitness computation stores the stream's)."""
    from vibesynergy.store.schema import activity_load

    if not ids:
        return {}
    with engine.connect() as conn:
        found = {
            r.group_id: float(r.load)
            for r in conn.execute(
                select(activity_load.c.group_id, activity_load.c.load).where(
                    activity_load.c.group_id.in_(ids)
                )
            )
        }
    missing = [i for i in ids if i not in found]
    if missing:
        for item in activity_loads(engine, window):
            if item["id"] in missing:
                found[item["id"]] = item["load"]
    return found


def load_of_activity(rt, group_id: str) -> dict[str, Any] | None:
    """VibeSynergy's load for one activity and how it was counted (the activity page)."""
    with rt.engine.connect() as conn:
        day = conn.scalar(
            select(activity_unified.c.local_date).where(activity_unified.c.group_id == group_id)
        )
    if day is None:
        return None
    found = activity_loads(
        rt.engine, Window(day, day), rt.settings.athlete, parquet=getattr(rt, "parquet", None)
    )
    item = next((x for x in found if x["id"] == group_id), None)
    if item is None or not item["method"]:
        return None
    return {"load": round(item["load"]), "method": item["method"]}


def daily_vibesynergy(
    engine: Engine,
    window: Window,
    athlete: dict[str, Any] | None = None,
    *,
    parquet: ParquetStore | None = None,
    sport: str | None = None,
    include_coaching: bool = True,
) -> dict[date, float]:
    """VibeSynergy's own load per day: the sum of ``activity_loads``."""
    daily: dict[date, float] = defaultdict(float)
    for item in activity_loads(
        engine, window, athlete, parquet=parquet, sport=sport, include_coaching=include_coaching
    ):
        daily[item["date"]] += item["load"]
    return daily


def training_load(
    engine: Engine,
    window: Window,
    *,
    sport: str | None = None,
    load: str = "vibesynergy",
    athlete: dict[str, Any] | None = None,
    include_coaching: bool = True,
    warmup_days: int = 126,
    raw=None,
    parquet: ParquetStore | None = None,
) -> dict[str, Any]:
    if load not in LOADS:
        raise ValueError(f"load must be one of {', '.join(LOADS)}")
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
            engine,
            warmup,
            athlete,
            parquet=parquet,
            sport=sport,
            include_coaching=include_coaching,
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
                engine, warmup, athlete, parquet=parquet, sport=sport, include_coaching=False
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
    from vibesynergy.query import fitness_model

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
                fitness_model.form_pct(ctl, tsb),
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
            "form_pct": fitness_model.form_pct(ctl, tsb),
            "form_zone": fitness_model.form_zone(ctl, tsb),
            "form": fitness_model.legacy_form(tsb),
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


# Dashboard and the Training page share this snapshot. 180 days is long enough for CTL
# (42-day constant) to forget a zero start. The window ends on ``today``.
CURRENT_WARMUP_DAYS = 180


def current_load(rt, today: date) -> dict[str, Any]:
    """CTL, ATL and TSB for ``today``, after a fixed 180-day warm-up ending that day."""
    start = today - timedelta(days=CURRENT_WARMUP_DAYS - 1)
    result = training_load(
        rt.engine,
        Window(start, today),
        athlete=rt.settings.athlete,
        warmup_days=0,
        load="vibesynergy",
        raw=getattr(rt, "raw", None),
        parquet=getattr(rt, "parquet", None),
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
