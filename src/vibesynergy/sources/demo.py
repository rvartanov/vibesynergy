"""Synthetic source used by tests and for trying VibeSynergy without accounts.

Enable with ``[sources.demo] enabled = true``. Everything is made up and deterministic: the
same date always gives the same values, so a second sync changes nothing.

- ``daily``: steps, resting heart rate, last night's sleep (with stages), overnight HRV with
  its baseline, and a weigh-in every few days.
- ``activities``: a running and cycling week (intervals on Tuesday, an easy run on Wednesday,
  a tempo run on Thursday, a ride on most Saturdays, a long run on Sunday), each with a
  summary and a stream of one sample per ``SAMPLE_S`` seconds: heart rate, speed, cadence,
  elevation, distance, a GPS loop, and power on the bike. The loop is drawn around
  ``HOME`` in open polder land and follows no real road or person.
- ``profile``: a snapshot.

``steps_offset`` changes the generated steps, which exercises the raw store's versioning.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from vibesynergy.core.timeutil import Window, utc_now
from vibesynergy.model import (
    Activity,
    ActivityLap,
    BodyMeasurement,
    DailyMetric,
    Record,
    SleepSession,
    SleepStage,
    Stream,
)
from vibesynergy.sources.base import DatasetKind, DatasetSpec, FetchedItem, Mapper, Source
from vibesynergy.store.raw import RawRecord

SAMPLE_S = 10  # one stream sample per ten seconds keeps a raw item around 20 kB
HOME = (52.7000, 5.7500)  # centre of the made-up loops
HISTORY_DAYS = 90  # a first sync fills three months, enough for the fitness charts
_EARTH_M = 111_320.0  # metres per degree of latitude


def _seed(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")


class DemoSource(Source):
    id = "demo"
    name = "Demo (synthetic data)"

    def datasets(self) -> Sequence[DatasetSpec]:
        return [
            DatasetSpec(
                "daily",
                DatasetKind.DAILY,
                "Synthetic daily steps, resting heart rate, sleep, HRV and weight",
                initial_days=HISTORY_DAYS,
                refetch_days=1,
            ),
            DatasetSpec(
                "activities",
                DatasetKind.ENTITY,
                "Synthetic runs and rides with heart rate, pace, cadence, elevation and a track",
                initial_days=HISTORY_DAYS,
                refetch_days=1,
            ),
            DatasetSpec("profile", DatasetKind.SNAPSHOT, "Synthetic athlete profile"),
        ]

    def fetch(self, dataset: DatasetSpec, window: Window | None) -> Iterator[FetchedItem]:
        if dataset.name == "profile":
            yield FetchedItem("profile", {"name": "Demo Athlete", "ftp_w": 250, "max_hr": 188})
            return
        assert window is not None
        zone = ZoneInfo(self.ctx.settings.timezone)
        now = utc_now()
        if dataset.name == "activities":
            for day in window.days():
                payload = activity_payload(day, zone)
                # A session that has not ended yet is not there yet; the next sync has it.
                if payload is None or datetime.fromisoformat(payload["end_utc"]) > now:
                    continue
                yield FetchedItem(
                    item_key=payload["id"],
                    payload=payload,
                    request={"endpoint": "demo/activities", "date": day.isoformat()},
                    window=Window(day, day),
                )
            return
        offset = int(self.ctx.config.get("steps_offset", 0))
        for day in window.days():
            yield FetchedItem(
                item_key=day.isoformat(),
                payload=daily_payload(day, zone, steps_offset=offset, now=now),
                request={"endpoint": "demo/daily", "date": day.isoformat()},
                window=Window(day, day),
            )

    def mappers(self) -> Mapping[str, Mapper]:
        return {"daily": map_daily, "activities": map_activity}


# Daily -----------------------------------------------------------------------------------


def daily_payload(
    day: date, zone: ZoneInfo, *, steps_offset: int = 0, now: datetime | None = None
) -> dict[str, Any]:
    """One day as the demo "service" reports it. The night belongs to the morning it ends."""
    seed = _seed(day.isoformat())
    rng = random.Random(_seed(f"daily:{day.isoformat()}"))
    payload: dict[str, Any] = {
        "date": day.isoformat(),
        "steps": 4000 + seed % 8000 + steps_offset,
        "resting_hr": 44 + seed % 9,
    }
    # Sleep: to bed around 22:45, up around 6:45; a short night now and then.
    bedtime = datetime.combine(day - timedelta(days=1), time(22, 15), zone) + timedelta(
        minutes=rng.randrange(0, 75)
    )
    in_bed_s = rng.randrange(int(6.2 * 3600), int(8.6 * 3600))
    stages = _sleep_stages(bedtime, in_bed_s, rng)
    wake = stages[-1][1]
    if now is None or wake <= now:
        totals = {name: 0.0 for name in ("deep", "light", "rem", "awake")}
        for start, end, stage in stages:
            totals[stage] += (end - start).total_seconds()
        asleep = totals["deep"] + totals["light"] + totals["rem"]
        score = 30 + 45 * min(1.0, asleep / (8 * 3600)) + 55 * totals["deep"] / asleep
        payload["sleep"] = {
            "start_utc": bedtime.astimezone(UTC).isoformat(),
            "end_utc": wake.astimezone(UTC).isoformat(),
            "duration_s": round(asleep),
            "deep_s": round(totals["deep"]),
            "light_s": round(totals["light"]),
            "rem_s": round(totals["rem"]),
            "awake_s": round(totals["awake"]),
            "score": round(min(98.0, score)),
            "stages": [
                [start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(), stage]
                for start, end, stage in stages
            ],
        }
        # HRV follows the night: lower after a short one, and with a higher resting pulse.
        hrv = 62 + 10 * (asleep / 3600 - 7.2) - 1.2 * (payload["resting_hr"] - 48)
        payload["hrv"] = {
            "night_avg": round(hrv + rng.uniform(-4, 4)),
            "baseline_low": 52,
            "baseline_high": 70,
        }
    if seed % 3 == 0:  # on the scale every few days, drifting slowly through the year
        weight = 72.4 + 0.8 * math.sin(day.toordinal() / 58) + rng.uniform(-0.4, 0.4)
        measured = datetime.combine(day, time(7, 10), zone) + timedelta(minutes=seed % 40)
        if now is None or measured <= now:
            payload["weight"] = {
                "kg": round(weight, 1),
                "measured_utc": measured.astimezone(UTC).isoformat(),
            }
    return payload


def _sleep_stages(
    bedtime: datetime, in_bed_s: int, rng: random.Random
) -> list[tuple[datetime, datetime, str]]:
    """Cycles of light, deep, light and REM; deep sleep early, REM late, a short wake between."""
    out: list[tuple[datetime, datetime, str]] = []
    at, end = bedtime, bedtime + timedelta(seconds=in_bed_s)
    cycle = 0
    while at < end:
        deep = max(4, 26 - 7 * cycle) + rng.randrange(0, 8)
        rem = min(34, 8 + 7 * cycle) + rng.randrange(0, 8)
        for stage, minutes in (
            ("light", 14 + rng.randrange(0, 12)),
            ("deep", deep),
            ("light", 16 + rng.randrange(0, 14)),
            ("rem", rem),
            ("awake", rng.choice((0, 0, 2, 4))),
        ):
            if not minutes or at >= end:
                continue
            until = min(end, at + timedelta(minutes=minutes))
            out.append((at, until, stage))
            at = until
        cycle += 1
    return out


def map_daily(raw: RawRecord) -> Iterable[Record]:
    data = raw.json()
    day = Window.parse(data["date"]).start
    yield DailyMetric(day, "daily.steps", float(data["steps"]), "count")
    yield DailyMetric(day, "hr.resting", float(data["resting_hr"]), "bpm")
    sleep = data.get("sleep")  # absent in items archived before the demo had nights
    if sleep:
        for key, metric in (
            ("duration_s", "sleep.duration"),
            ("deep_s", "sleep.deep"),
            ("light_s", "sleep.light"),
            ("rem_s", "sleep.rem"),
            ("awake_s", "sleep.awake"),
        ):
            yield DailyMetric(day, metric, float(sleep[key]), "s")
        yield DailyMetric(day, "sleep.score", float(sleep["score"]), "score")
        session_id = f"demo-sleep-{day.isoformat()}"
        yield SleepSession(
            source_id=session_id,
            local_date=day,
            start_utc=datetime.fromisoformat(sleep["start_utc"]),
            end_utc=datetime.fromisoformat(sleep["end_utc"]),
            duration_s=float(sleep["duration_s"]),
            deep_s=float(sleep["deep_s"]),
            light_s=float(sleep["light_s"]),
            rem_s=float(sleep["rem_s"]),
            awake_s=float(sleep["awake_s"]),
            score=float(sleep["score"]),
        )
        for start, end, stage in sleep.get("stages") or []:
            yield SleepStage(
                session_id, datetime.fromisoformat(start), datetime.fromisoformat(end), stage
            )
    hrv = data.get("hrv")
    if hrv:
        for key, metric in (
            ("night_avg", "hrv.night_avg"),
            ("baseline_low", "hrv.baseline_low"),
            ("baseline_high", "hrv.baseline_high"),
        ):
            yield DailyMetric(day, metric, float(hrv[key]), "ms")
    weight = data.get("weight")
    if weight:
        measured = datetime.fromisoformat(weight["measured_utc"])
        yield BodyMeasurement(
            f"demo-weight-{day.isoformat()}", measured, "body.weight", float(weight["kg"]), "kg"
        )
        yield DailyMetric(day, "body.weight", float(weight["kg"]), "kg")


# Activities --------------------------------------------------------------------------------

# Weekday (Monday is 0) -> session. Monday and Friday are rest days.
_WEEK = {1: "intervals", 2: "easy", 3: "tempo", 5: "ride", 6: "long"}


def _plan(kind: str, rng: random.Random) -> tuple[str, str, list[tuple[int, float, float]]]:
    """Name, sport and the blocks of one session: (minutes, speed in m/s, heart rate)."""
    if kind == "easy":
        minutes = rng.choice((40, 45, 50, 55))
        return "Easy run", "running", [(minutes, 2.85, 136)]
    if kind == "intervals":
        reps = rng.choice((5, 6))
        blocks = [(15, 2.8, 134)]
        for _ in range(reps):
            blocks += [(3, 4.15, 172), (2, 2.4, 140)]
        return f"Intervals {reps} x 3 min", "running", [*blocks, (10, 2.7, 132)]
    if kind == "tempo":
        minutes = rng.choice((20, 25, 30))
        blocks = [(15, 2.8, 134), (minutes, 3.6, 162), (10, 2.7, 132)]
        return f"Tempo run {minutes} min", "running", blocks
    if kind == "long":
        minutes = rng.choice((85, 95, 105, 115))
        return "Long run", "running", [(minutes, 2.75, 139)]
    minutes = rng.choice((75, 90, 105, 120))
    return "Endurance ride", "cycling", [(10, 6.6, 118), (minutes - 20, 7.6, 134), (10, 6.4, 120)]


def activity_payload(day: date, zone: ZoneInfo) -> dict[str, Any] | None:
    """The session of one day (None on a rest day), as the demo "service" reports it:
    a summary and the samples."""
    kind = _WEEK.get(day.weekday())
    rng = random.Random(_seed(f"activity:{day.isoformat()}"))
    if kind is None or rng.random() < (0.3 if kind == "ride" else 0.08):
        return None  # a rest day, an occasional skipped session, a Saturday without a ride
    name, sport, blocks = _plan(kind, rng)
    ride = sport == "cycling"
    start = datetime.combine(day, time(6, 45) if not ride else time(9, 0), zone) + timedelta(
        minutes=5 * rng.randrange(0, 18)
    )
    form = rng.uniform(0.96, 1.04)  # a good or a heavy day

    # Target speed and heart rate per sample, then smoothed: the body and the legs lag.
    targets: list[tuple[float, float]] = []
    for minutes, speed, hr in blocks:
        targets += [(speed * form, hr)] * (minutes * 60 // SAMPLE_S)
    count = len(targets)
    total_m_guess = sum(speed for speed, _ in targets) * SAMPLE_S
    turn = rng.uniform(0, 2 * math.pi)
    radius = total_m_guess / (2 * math.pi)
    speeds: list[float] = []
    hrs: list[int] = []
    cadences: list[int] = []
    alts: list[float] = []
    dists: list[float] = []
    lats: list[float] = []
    lons: list[float] = []
    powers: list[int] = []
    speed, hr, dist = targets[0][0] * 0.6, 95.0, 0.0
    wave = rng.uniform(0, 6)
    for i, (want_speed, want_hr) in enumerate(targets):
        speed += (want_speed - speed) * 0.45 + rng.uniform(-0.06, 0.06)
        speed = max(0.8, speed)
        drift = 8.0 * i / count  # the pulse creeps up over a long session
        hr += (want_hr + drift - hr) * 0.12 + rng.uniform(-0.8, 0.8)
        dist += speed * SAMPLE_S
        angle = 2 * math.pi * dist / total_m_guess
        # A flattened loop back to the start, turned differently every day.
        x = radius * (math.cos(angle) - 1.0)
        y = 0.6 * radius * math.sin(angle)
        east = x * math.cos(turn) - y * math.sin(turn)
        north = x * math.sin(turn) + y * math.cos(turn)
        speeds.append(round(speed, 2))
        hrs.append(round(hr))
        dists.append(round(dist, 1))
        lats.append(round(HOME[0] + north / _EARTH_M, 5))
        lons.append(round(HOME[1] + east / (_EARTH_M * math.cos(math.radians(HOME[0]))), 5))
        alts.append(round(3.0 + 2.5 * math.sin(dist / 900 + wave) + 1.5 * math.sin(dist / 260), 1))
        if ride:
            cadences.append(round(84 + (speed - 7.2) * 4 + rng.uniform(-3, 3)))
            powers.append(max(0, round(165 + (speed - 7.3) * 60 + rng.uniform(-18, 18))))
        else:
            cadences.append(round(78 + speed * 3.1 + rng.uniform(-1.5, 1.5)))  # strides a minute
    elapsed = count * SAMPLE_S
    end = start + timedelta(seconds=elapsed)
    samples: dict[str, list[Any]] = {
        "hr": hrs,
        "speed": speeds,
        "cadence": cadences,
        "alt": alts,
        "dist": dists,
        "lat": lats,
        "lon": lons,
    }
    if ride:
        samples["power"] = powers
    ascent = sum(max(0.0, b - a) for a, b in pairwise(alts))
    summary: dict[str, Any] = {
        "elapsed_s": elapsed,
        "moving_s": elapsed,
        "distance_m": dists[-1],
        "ascent_m": round(ascent, 1),
        "avg_hr": round(sum(hrs) / count, 1),
        "max_hr": max(hrs),
        "avg_speed": round(dists[-1] / elapsed, 3),
        "calories": round(elapsed / 60 * (10.5 if ride else 12.5)),
    }
    if ride:
        summary["avg_power"] = round(sum(powers) / count, 1)
    return {
        "id": f"demo-{day.isoformat()}",
        "date": day.isoformat(),
        "start_utc": start.astimezone(UTC).isoformat(),
        "end_utc": end.astimezone(UTC).isoformat(),
        "tz_offset_s": int(start.utcoffset().total_seconds()) if start.utcoffset() else 0,
        "sport": sport,
        "name": name,
        "kind": kind,
        "lap_m": 5000 if ride else 1000,
        "sample_s": SAMPLE_S,
        "summary": summary,
        "samples": samples,
    }


def map_activity(raw: RawRecord) -> Iterable[Record]:
    data = raw.json()
    source_id = str(data["id"])
    start = datetime.fromisoformat(data["start_utc"])
    step = float(data["sample_s"])
    samples: dict[str, list[Any]] = data["samples"]
    count = len(samples["hr"])
    summary = data["summary"]
    yield Activity(
        source_id,
        start,
        date.fromisoformat(data["date"]),
        data["sport"],
        tz_offset_s=data.get("tz_offset_s"),
        name=data.get("name"),
        elapsed_s=summary.get("elapsed_s"),
        moving_s=summary.get("moving_s"),
        distance_m=summary.get("distance_m"),
        ascent_m=summary.get("ascent_m"),
        avg_hr=summary.get("avg_hr"),
        max_hr=summary.get("max_hr"),
        avg_power=summary.get("avg_power"),
        avg_speed=summary.get("avg_speed"),
        calories=summary.get("calories"),
        device="Demo watch",
        fit_id=source_id,  # the stream's key
        extra={"kind": data.get("kind"), "synthetic": True},
    )
    times = [start + timedelta(seconds=step * (i + 1)) for i in range(count)]
    channels = {name: list(values) for name, values in samples.items()}
    yield Stream(source_id, {"t": times, **channels})
    # Laps: one per kilometre on foot, per five on the bike; the last one is what is left.
    lap_m = float(data.get("lap_m") or 1000)
    first, lap_index = 0, 0
    dists, hrs = samples["dist"], samples["hr"]
    for i in range(count):
        last = i == count - 1
        if dists[i] < lap_m * (lap_index + 1) and not last:
            continue
        before = dists[first - 1] if first else 0.0
        seconds = (i - first + 1) * step
        part = hrs[first : i + 1]
        yield ActivityLap(
            source_id,
            lap_index,
            start_utc=start + timedelta(seconds=step * first),
            elapsed_s=seconds,
            moving_s=seconds,
            distance_m=round(dists[i] - before, 1),
            avg_hr=round(sum(part) / len(part), 1),
            max_hr=max(part),
            avg_speed=round((dists[i] - before) / seconds, 3),
        )
        first, lap_index = i + 1, lap_index + 1
