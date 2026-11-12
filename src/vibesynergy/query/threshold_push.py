"""Set the plan's thresholds and zones in Garmin Connect and intervals.icu.

Only ever called from an explicit athlete action on Training → Zones & thresholds (a
ticked "also in Garmin" / "also in intervals.icu" with a save, or a "set in …" button).
A coach proposal never reaches this module by itself.

Each push sends the sport's current confirmed values (FTP, LTHR, max HR, threshold pace,
the derived zones), reads the platform back to check it took them, and writes one audit
row per platform with what was there before, what was sent and the outcome.

What each platform can take:

- Garmin: per sport HR profile (LTHR, max HR, zone floors 2-5), power profile (cycling
  FTP and its seven floors; running power threshold with the floors scaled) and critical
  swim speed (saved as a manual measurement in mm/s, 333-2000, which is 0:50-5:00 /100 m).
  Not running threshold pace: Garmin sets that itself (lactate threshold detection) and
  the Connect API has no setting for it.
- intervals.icu: sport settings FTP, LTHR, max HR, HR zones (as given, not recalculated)
  and threshold pace (m/s).
- Zwift: no public API; the page shows a reminder instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.query import thresholds
from vibesynergy.store.schema import audit_log

log = logging.getLogger(__name__)

GARMIN_PROFILE = {"bike": "CYCLING", "run": "RUNNING", "swim": "SWIMMING"}
# What a platform cannot take, per sport (the page says so instead of offering it).
UNSUPPORTED = {
    "garmin": {},
    "intervals": {},
}

# Error codes the page turns into a sentence.
NOT_CONNECTED = "not_connected"
REFUSED = "refused"
NOT_APPLIED = "not_applied"
NOTHING = "nothing_to_send"
NO_SETTINGS = "no_sport_settings"


def plan_for(engine, sport: str) -> tuple[dict[str, float], dict[str, int], dict[str, Any]]:
    current = thresholds.current_values(engine).get(sport) or {}
    values = {metric: row["value"] for metric, row in current.items()}
    ids = {metric: row["id"] for metric, row in current.items()}
    return values, ids, thresholds.derived_zones(sport, values)


# Garmin -----------------------------------------------------------------------------------


def _garmin_api(rt):
    from vibesynergy.sources.garmin.client import connect

    return connect(SecretStore(rt.settings.secrets_dir, "garmin"))


def push_garmin(rt, sport: str, *, api_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    from vibesynergy.sources.garmin import heart_rate_zones as gz
    from vibesynergy.sources.garmin import swim_speed

    values, ids, zones = plan_for(rt.engine, sport)
    result = _result("garmin", sport, ids)
    result["skipped"] = [m for m in UNSUPPORTED["garmin"].get(sport, []) if m in values]
    hr_zone_rows = (zones.get("hr") or {}).get("zones") or []
    floors = thresholds.garmin_hr_floors(hr_zone_rows) if hr_zone_rows else None
    power_metric = "ftp" if sport == "bike" else "threshold_power" if sport == "run" else None
    wants_hr = any(values.get(m) for m in ("lthr", "max_hr"))
    wants_power = bool(power_metric and values.get(power_metric))
    wants_css = sport == "swim" and bool(values.get("css"))
    wants_lt = sport == "run" and any(values.get(m) for m in ("lthr", "threshold_pace"))
    if not wants_hr and not wants_power and not wants_css and not wants_lt:
        return _fail(rt, result, NOTHING)
    try:
        api = (api_factory or (lambda: _garmin_api(rt)))()
    except Exception as exc:
        log.info("Garmin push for %s: not connected (%s)", sport, type(exc).__name__)
        return _fail(rt, result, NOT_CONNECTED)
    profile = GARMIN_PROFILE[sport]
    problems: list[str] = []
    try:
        if wants_hr:
            before = gz.hr_profile(api, profile) or {}
            result["before"]["hr"] = _hr_view(before)
            sent = {"lthr": values.get("lthr"), "max_hr": values.get("max_hr"), "floors": floors}
            result["sent"]["hr"] = sent
            gz.set_heart_rate_zones(
                max_hr=values.get("max_hr"),
                lthr=values.get("lthr"),
                floors=floors,
                sport=profile,
                api=api,
            )
            after = _hr_view(gz.hr_profile(api, profile) or {})
            result["after"]["hr"] = after
            for key in ("lthr", "max_hr"):
                if sent[key] is not None and after.get(key) != round(sent[key]):
                    problems.append(key)
            if floors and after.get("floors") != floors:
                problems.append("hr_zones")
        if wants_power:
            ftp = float(values[power_metric])
            before = gz.power_profile(api, profile) or {}
            result["before"]["power"] = {"ftp": before.get("functionalThresholdPower")}
            gz.set_power_zones(
                api,
                sport=profile,
                ftp=ftp,
                floors=thresholds.garmin_power_floors(ftp) if sport == "bike" else None,
            )
            result["sent"]["power"] = {"ftp": round(ftp)}
            after = gz.power_profile(api, profile) or {}
            result["after"]["power"] = {"ftp": after.get("functionalThresholdPower")}
            if round(float(after.get("functionalThresholdPower") or 0)) != round(ftp):
                problems.append(power_metric)
        if wants_css:
            speed = swim_speed.mm_per_s(values["css"])
            before = _css_view(swim_speed.latest(api))
            result["before"]["css"] = before
            if before.get("mm_per_s") == speed:  # already there: no extra measurement
                after = before
            else:
                result["sent"]["css"] = {"mm_per_s": speed}
                swim_speed.save(api, speed, now=utc_now(), timezone=rt.settings.timezone)
                after = _css_view(swim_speed.latest(api))
            result["after"]["css"] = after
            if after.get("mm_per_s") != speed:
                problems.append("css")
        if sport == "run":
            # Garmin keeps running thresholds in three places: the RUNNING zones, the general
            # DEFAULT zones (always equal to RUNNING) and the profile's lactate threshold.
            if wants_hr:
                gz.copy_profile(api, "RUNNING", "DEFAULT")
                general = _hr_view(gz.hr_profile(api, "DEFAULT") or {})
                result["after"]["hr_default"] = general
                if general != result["after"].get("hr"):
                    problems.append("hr_default")
            if wants_lt:
                result["before"]["lactate"] = gz.lactate_threshold(api)
                sent_lt = {
                    "lthr": values.get("lthr"),
                    "pace_s_per_km": values.get("threshold_pace"),
                }
                result["sent"]["lactate"] = sent_lt
                lt = gz.set_lactate_threshold(
                    api, lthr=sent_lt["lthr"], pace_s_per_km=sent_lt["pace_s_per_km"]
                )
                result["after"]["lactate"] = lt
                if sent_lt["lthr"] is not None and lt.get("lthr") != round(sent_lt["lthr"]):
                    problems.append("lactate_lthr")
                pace = sent_lt["pace_s_per_km"]
                if pace is not None and abs((lt.get("pace_s_per_km") or 0) - pace) > 1:
                    problems.append("lactate_pace")
                if lt.get("auto_detect"):
                    problems.append("auto_detect")
    except Exception as exc:
        log.info("Garmin push for %s refused: %s", sport, type(exc).__name__)
        result["detail"] = _short(exc)
        return _fail(rt, result, REFUSED)
    if problems:
        result["not_applied"] = problems
        return _fail(rt, result, NOT_APPLIED)
    return _ok(rt, result)


def _css_view(entry: dict[str, Any] | None) -> dict[str, Any]:
    entry = entry or {}
    return {
        "mm_per_s": entry.get("criticalSwimSpeed"),
        "at": entry.get("measurementTimestampLocal"),
        "source": entry.get("biometricSourceType"),
    }


def _hr_view(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "lthr": entry.get("lactateThresholdHeartRateUsed"),
        "max_hr": entry.get("maxHeartRateUsed"),
        "floors": [entry.get(f"zone{n}Floor") for n in (2, 3, 4, 5)],
    }


# intervals.icu ----------------------------------------------------------------------------


def _icu_client():
    from vibesynergy.sources.intervals_icu.client import IntervalsIcuClient

    return IntervalsIcuClient.from_env()


def icu_body(sport: str, values: dict[str, float], zones: dict[str, Any], item: dict[str, Any]):
    body: dict[str, Any] = {}
    if values.get("lthr"):
        body["lthr"] = round(values["lthr"])
    if values.get("max_hr"):
        body["max_hr"] = round(values["max_hr"])
    hr = (zones.get("hr") or {}).get("zones") or []
    bounds = thresholds.hr_upper_bounds(hr) if hr else []
    names = item.get("hr_zone_names") or []
    # Only send zones when intervals.icu keeps the same number of zones (names per zone).
    if len(bounds) == 7 and (not names or len(names) == len(bounds)):
        body["hr_zones"] = bounds
    if sport == "bike" and values.get("ftp"):
        body["ftp"] = round(values["ftp"])
    if sport == "run" and values.get("threshold_power"):
        body["ftp"] = round(values["threshold_power"])
    if sport == "run" and values.get("threshold_pace"):
        body["threshold_pace"] = round(1000 / values["threshold_pace"], 4)
    if sport == "swim" and values.get("css"):
        body["threshold_pace"] = round(100 / values["css"], 4)
    return body


def push_intervals(rt, sport: str, *, client_factory: Callable[[], Any] | None = None):
    values, ids, zones = plan_for(rt.engine, sport)
    result = _result("intervals", sport, ids)
    if not values:
        return _fail(rt, result, NOTHING)
    try:
        client = (client_factory or _icu_client)()
        item = thresholds.icu_settings_for(client.sport_settings(), sport)
    except Exception as exc:
        log.info("intervals.icu push for %s: %s", sport, type(exc).__name__)
        result["detail"] = _short(exc)
        code = (
            NOT_CONNECTED
            if "not set up" in str(exc) or "refused the API key" in str(exc)
            else REFUSED
        )
        return _fail(rt, result, code)
    if not item:
        return _fail(rt, result, NO_SETTINGS)
    body = icu_body(sport, values, zones, item)
    if not body:
        return _fail(rt, result, NOTHING)
    result["before"] = {key: item.get(key) for key in body}
    result["sent"] = body
    try:
        client.update_sport_settings(item["id"], body)
        after = thresholds.icu_settings_for(client.sport_settings(), sport) or {}
    except Exception as exc:
        log.info("intervals.icu push for %s refused: %s", sport, type(exc).__name__)
        result["detail"] = _short(exc)
        return _fail(rt, result, REFUSED)
    finally:
        thresholds.forget_icu_cache()
    result["after"] = {key: after.get(key) for key in body}
    problems = [key for key, wanted in body.items() if not _icu_same(key, after.get(key), wanted)]
    if problems:
        result["not_applied"] = problems
        return _fail(rt, result, NOT_APPLIED)
    return _ok(rt, result)


def _icu_same(key: str, have: Any, wanted: Any) -> bool:
    if have is None:
        return False
    if key == "hr_zones":
        return [round(v) for v in have] == list(wanted)
    if key == "threshold_pace":
        return abs(float(have) - float(wanted)) < 0.002
    return round(float(have)) == round(float(wanted))


# Shared -----------------------------------------------------------------------------------


def push(rt, sport: str, targets: list[str]) -> list[dict[str, Any]]:
    """Push one sport's plan values to each requested platform; one result per platform."""
    out = []
    for target in targets:
        if target == "garmin":
            out.append(push_garmin(rt, sport))
        elif target == "intervals":
            out.append(push_intervals(rt, sport))
    return out


def _result(platform: str, sport: str, ids: dict[str, int]) -> dict[str, Any]:
    return {
        "platform": platform,
        "sport": sport,
        "ok": False,
        "error": None,
        "threshold_ids": sorted(ids.values()),
        "before": {},
        "sent": {},
        "after": {},
        "skipped": [],
    }


def _short(exc: Exception) -> str:
    return str(exc)[:200]


def _ok(rt, result: dict[str, Any]) -> dict[str, Any]:
    result["ok"] = True
    _audit(rt, result)
    return result


def _fail(rt, result: dict[str, Any], code: str) -> dict[str, Any]:
    result["error"] = code
    _audit(rt, result)
    return result


def _audit(rt, result: dict[str, Any]) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor="portal",
                action=f"threshold.push.{result['platform']}",
                detail={k: v for k, v in result.items() if k not in {"platform", "ok"}},
                author="user",
                scope="thresholds.write",
                target=f"thresholds:{result['sport']}",
                outcome="ok" if result["ok"] else "failed",
            )
        )
