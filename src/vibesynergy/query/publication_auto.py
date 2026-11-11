"""Garmin → Strava without a click: publish new activities by themselves.

Until October 2026 an external coach published each new activity over MCP. When it was
retired nothing called the publication any more, so Strava descriptions stopped getting
the Garmin figures although every part of the chain still worked.

With ``[publication] auto_publish = true`` (switched on in Settings → Sync between sources)
the scheduler looks for new activities every 5 minutes. A candidate:

- started after the switch went on (``auto_publish_since``) and within the last two days,
  and at least ``SETTLE`` ago, so Garmin's figures are in;
- has exactly one Garmin and one Strava copy in the unified activity, or only one Strava copy
  and still no Garmin copy ``WAIT_FOR_GARMIN`` after it ended (its block then carries
  whatever Strava has);
- has no publication ledger row on Strava yet (published by hand, or already done).

Each candidate goes through the same guarded path as the Publish button: preview, publish
with that preview version, read-back and the ledger checkpoint, then the Garmin copy when
that is on. A description that already holds a VibeSynergy section or changed under a pending
write is a conflict: that activity is left for review and never retried. Other failures are
retried with growing gaps, at most ``MAX_TRIES`` times. Strava's rate limit ends the run.
Attempts are kept in the ``publication`` secret store (no description text).

The coach's public insight (``publication_insight``) usually arrives later, with the
evening routine. Each run therefore also looks at activities in the same window that are
verified on Strava but whose block lacks the coach's current insight (or still shows a
withdrawn one), and publishes them again through that same path: only the VibeSynergy block is
replaced, the Garmin mirror follows, and each update gets a ``publication.insight`` audit row.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from vibesynergy.core.errors import RateLimited
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import activity_unified, audit_log, publication_ledger

log = logging.getLogger(__name__)

JOB = "publication_auto"
LOOKBACK = timedelta(days=2)
SETTLE = timedelta(minutes=10)
WAIT_FOR_GARMIN = timedelta(minutes=30)  # as agent_events: a Garmin copy is usually in by then
RETRY = timedelta(minutes=30)
MAX_TRIES = 5
KEEP = timedelta(days=14)
STATE = "auto"
DONE = ("published", "needs_review", "gave_up")


def _settings(rt) -> dict[str, Any]:
    from vibesynergy.core.config import load_settings

    return load_settings(rt.settings.config_path).publication


def enabled(rt) -> bool:
    return _settings(rt).get("auto_publish") is True


def since(rt) -> datetime | None:
    raw = _settings(rt).get("auto_publish_since")
    if not raw:
        return None
    value = datetime.fromisoformat(str(raw))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def language(rt) -> str:
    """The text's language: [publication] language, else the portal language, else Dutch."""
    chosen = _settings(rt).get("language")
    if chosen in ("en", "nl"):
        return chosen
    from vibesynergy.portal import prefs

    portal = prefs.load(rt.settings.data_dir).get("language")
    return portal if portal in ("en", "nl") else "nl"


def _store(rt) -> SecretStore:
    return SecretStore(rt.settings.secrets_dir, "publication")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def attempts(rt) -> dict[str, dict[str, Any]]:
    return dict(_store(rt).get(STATE) or {})


def candidates(rt, *, now: datetime | None = None) -> list[dict[str, Any]]:
    if not enabled(rt):
        return []
    now = now or datetime.now(UTC)
    start = since(rt)
    if start is None:
        return []  # switched on outside the portal: no starting point, so nothing old moves
    floor = max(start, now - LOOKBACK)
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    activity_unified.c.group_id,
                    activity_unified.c.sources,
                    activity_unified.c.start_utc,
                    activity_unified.c.elapsed_s,
                )
                .where(
                    activity_unified.c.start_utc >= floor,
                    activity_unified.c.start_utc <= now - SETTLE,
                )
                .order_by(activity_unified.c.start_utc)
            )
            .mappings()
            .all()
        )
        published = set(
            conn.execute(
                select(publication_ledger.c.source_id).where(
                    publication_ledger.c.source == "strava"
                )
            ).scalars()
        )
    tried = attempts(rt)
    out = []
    for row in rows:
        refs = row["sources"] or []
        strava = [r.split(":", 1)[1] for r in refs if r.startswith("strava:")]
        garmin = [r.split(":", 1)[1] for r in refs if r.startswith("garmin:")]
        if len(strava) != 1 or len(garmin) > 1 or strava[0] in published:
            continue
        if not garmin and not _no_garmin_coming(row, now):
            continue
        seen = tried.get(strava[0]) or {}
        if seen.get("state") in DONE:
            continue
        retry_at = seen.get("retry_at")
        if retry_at and datetime.fromisoformat(retry_at) > now:
            continue
        out.append(
            {
                "group_id": row["group_id"],
                "strava_id": strava[0],
                "garmin_id": garmin[0] if garmin else None,
            }
        )
    return out


def _no_garmin_coming(row, now: datetime) -> bool:
    """A Strava-only activity is published once its Garmin copy would long be in."""
    ended = _aware(row["start_utc"]) + timedelta(seconds=float(row["elapsed_s"] or 0))
    return ended + WAIT_FOR_GARMIN <= now


def _has_insight_line(owned: str) -> bool:
    from vibesynergy.query import publication_format

    return any(
        line.startswith(f"-{labels['insight']}:")
        for line in owned.splitlines()
        for labels in publication_format.LABELS.values()
    )


def refresh_candidates(rt, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Automatically published activities whose block lacks the coach's current insight.

    Same window as new publications. The insight usually arrives with the evening routine,
    after the activity was published; a withdrawn insight removes the line again.
    """
    from vibesynergy.query import publication_insight

    if not enabled(rt):
        return []
    now = now or datetime.now(UTC)
    start = since(rt)
    if start is None:
        return []
    floor = max(start, now - LOOKBACK)
    with rt.engine.connect() as conn:
        rows = (
            conn.execute(
                select(activity_unified.c.group_id, activity_unified.c.sources)
                .where(activity_unified.c.start_utc >= floor)
                .order_by(activity_unified.c.start_utc)
            )
            .mappings()
            .all()
        )
        verified = {
            row.source_id: row.owned_block
            for row in conn.execute(
                select(publication_ledger.c.source_id, publication_ledger.c.owned_block).where(
                    publication_ledger.c.source == "strava",
                    publication_ledger.c.state == "verified",
                )
            )
        }
    tried = attempts(rt)
    out = []
    for row in rows:
        strava = [r.split(":", 1)[1] for r in row["sources"] or [] if r.startswith("strava:")]
        if len(strava) != 1 or strava[0] not in verified:
            continue
        owned = verified[strava[0]] or ""
        insight = publication_insight.for_activity(rt.engine, row["group_id"])
        if insight and insight["text"] in owned:
            continue
        if not insight and not _has_insight_line(owned):
            continue
        entry = insight["entry_id"] if insight else None
        seen = tried.get(f"insight:{strava[0]}") or {}
        if seen.get("entry") == entry:
            if seen.get("state") in DONE:
                continue
            retry_at = seen.get("retry_at")
            if retry_at and datetime.fromisoformat(retry_at) > now:
                continue
        out.append({"group_id": row["group_id"], "strava_id": strava[0], "entry_id": entry})
    return out


def due(rt, *, now: datetime | None = None) -> bool:
    """Asked on every scheduler tick: a failure here must not stop the other syncs."""
    from vibesynergy.query import publication_icu

    try:
        return (
            bool(candidates(rt, now=now))
            or bool(refresh_candidates(rt, now=now))
            or bool(publication_icu.candidates(rt, now=now, attempts=attempts(rt)))
        )
    except Exception:
        log.exception("checking for activities to publish failed")
        return False


def _record(rt, strava_id: str, now: datetime, **values: Any) -> None:
    store = _store(rt)
    with store.lock("auto"):
        state = dict(store.get(STATE) or {})
        cutoff = now - KEEP
        state = {
            key: entry
            for key, entry in state.items()
            if _aware(datetime.fromisoformat(entry.get("at") or now.isoformat())) >= cutoff
        }
        previous = state.get(strava_id) or {}
        state[strava_id] = {**previous, **values, "at": now.isoformat()}
        store.set(STATE, state)


def _audit(rt, outcome: str, candidate: dict, detail: dict, action="publication.auto") -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor="vibesynergy",
                action=action,
                target=f"strava:{candidate['strava_id']}",
                outcome=outcome,
                detail={"group_id": candidate["group_id"], **detail},
            )
        )


def run(rt, *, now: datetime | None = None, client=None) -> dict[str, Any]:
    """The scheduled job: publish each due candidate once, oldest first; then put a later
    coach insight into blocks already published (``_refresh_insights``)."""
    now = now or datetime.now(UTC)
    summary: dict[str, Any] = {"published": 0, "waiting": 0, "review": 0, "failed": 0}
    summary["rate_limited"] = False
    summary["insight_updated"] = 0
    found = candidates(rt, now=now)
    lang = language(rt)
    if found:
        _publish_new(rt, found, summary, lang, now, client)
    if not summary["rate_limited"]:
        _refresh_insights(rt, summary, lang, now, client)
    _copy_to_icu(rt, summary, now)
    return summary


def _copy_to_icu(rt, summary: dict, now: datetime, icu=None) -> None:
    """Strava blocks that intervals.icu does not hold yet: the activity reached
    intervals.icu later, or an earlier copy failed (publication_icu)."""
    from vibesynergy.query import publication_icu

    summary["icu_updated"] = 0
    for candidate in publication_icu.candidates(rt, now=now, attempts=attempts(rt)):
        key = f"icu:{candidate['strava_id']}"
        seen = attempts(rt).get(key) or {}
        same = seen.get("block") == candidate["block"]
        tries = (int(seen.get("tries", 0)) if same else 0) + 1
        try:
            result = publication_icu.sync(rt, candidate["group_id"], client=icu)
        except RateLimited as exc:
            retry = getattr(exc, "retry_at", None)
            later = retry if isinstance(retry, datetime) and retry > now else now + RETRY
            _record(rt, key, now, block=candidate["block"], state="waiting",
                    tries=tries - 1, retry_at=later.isoformat())  # fmt: skip
            return
        except publication_icu.Review as exc:
            _record(rt, key, now, block=candidate["block"], state="needs_review",
                    tries=tries, error=str(exc)[:300])  # fmt: skip
            continue
        except Exception as exc:  # network or provider trouble: try again later
            state = "gave_up" if tries >= MAX_TRIES else "failed"
            _record(rt, key, now, block=candidate["block"], state=state, tries=tries,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                    retry_at=(now + RETRY * tries).isoformat())  # fmt: skip
            log.warning("intervals.icu copy of %s failed: %s", candidate["group_id"], exc)
            continue
        state = result["state"]
        if state in ("updated", "unchanged"):
            _record(rt, key, now, block=candidate["block"], state="published", tries=tries,
                    error=None)  # fmt: skip
            summary["icu_updated"] += state == "updated"
        elif state == "not_available":
            _record(rt, key, now, block=candidate["block"], state="not_available", tries=tries)
        else:  # waiting for intervals.icu to have the activity
            final = "gave_up" if tries >= MAX_TRIES else "waiting"
            _record(rt, key, now, block=candidate["block"], state=final, tries=tries,
                    retry_at=(now + RETRY * tries).isoformat())  # fmt: skip


def _refresh_insights(rt, summary: dict, lang: str, now: datetime, client) -> None:
    """Put the coach's later insight into blocks already published, via the guarded path.

    Only the VibeSynergy block changes (publication._compose checks it is still exactly the
    verified one, else the activity is left for review); the athlete's text stays.
    """
    from vibesynergy.query import publication

    for candidate in refresh_candidates(rt, now=now):
        key = f"insight:{candidate['strava_id']}"
        seen = attempts(rt).get(key) or {}
        same = seen.get("entry") == candidate["entry_id"]
        tries = (int(seen.get("tries", 0)) if same else 0) + 1
        detail = {"entry_id": candidate["entry_id"], "language": lang}
        try:
            item = publication.preview(
                rt, candidate["group_id"], "strava", language=lang, client=client
            )
            if item["state"] != "ready":
                raise publication.Conflict("Garmin statistics are required before publication")
            if not item["already_published"]:
                result = publication.publish(
                    rt,
                    candidate["group_id"],
                    "strava",
                    item["preview_version"],
                    language=lang,
                    client=client,
                )
                detail["mirror"] = (result.get("mirror") or {}).get("state")
        except RateLimited as exc:
            retry = getattr(exc, "retry_at", None)
            later = retry if isinstance(retry, datetime) and retry > now else now + RETRY
            _record(
                rt,
                key,
                now,
                entry=candidate["entry_id"],
                state="waiting",
                tries=tries - 1,
                retry_at=later.isoformat(),
            )
            summary["rate_limited"] = True
            return
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, publication.Conflict)
                else f"{type(exc).__name__}: {exc}"
            )[:300]
            review = isinstance(exc, publication.Conflict) and "review" in message.lower()
            state = "needs_review" if review else "gave_up" if tries >= MAX_TRIES else "failed"
            _record(
                rt,
                key,
                now,
                entry=candidate["entry_id"],
                state=state,
                tries=tries,
                error=message,
                retry_at=(now + RETRY * tries).isoformat(),
            )
            if review:
                _audit(
                    rt,
                    "needs_review",
                    candidate,
                    {**detail, "error": message},
                    action="publication.insight",
                )
            summary["review" if review else "failed"] += 1
            log.warning("insight update of %s on Strava: %s", candidate["group_id"], message)
            continue
        _record(
            rt, key, now, entry=candidate["entry_id"], state="published", tries=tries, error=None
        )
        _audit(rt, "ok", candidate, detail, action="publication.insight")
        summary["insight_updated"] += 1


def _publish_new(rt, found: list, summary: dict, lang: str, now: datetime, client) -> None:
    from vibesynergy.query import publication

    for candidate in found:
        key = candidate["strava_id"]
        tries = int((attempts(rt).get(key) or {}).get("tries", 0)) + 1
        try:
            item = publication.preview(
                rt, candidate["group_id"], "strava", language=lang, client=client
            )
            if item["state"] != "ready":
                # No Garmin figures yet. Try again later; give up after MAX_TRIES.
                state = "gave_up" if tries >= MAX_TRIES else "waiting"
                _record(
                    rt,
                    key,
                    now,
                    state=state,
                    tries=tries,
                    retry_at=(now + RETRY * tries).isoformat(),
                )
                summary["waiting"] += 1
                continue
            if item["already_published"]:
                _record(rt, key, now, state="published", tries=tries)
                continue
            result = publication.publish(
                rt,
                candidate["group_id"],
                "strava",
                item["preview_version"],
                language=lang,
                client=client,
            )
        except RateLimited as exc:
            retry = getattr(exc, "retry_at", None)
            later = retry if isinstance(retry, datetime) and retry > now else now + RETRY
            _record(rt, key, now, state="waiting", tries=tries - 1, retry_at=later.isoformat())
            summary["rate_limited"] = True
            break
        except publication.Conflict as exc:
            message = str(exc)[:300]
            # Conflicts that ask for review never fix themselves; the rest may on a retry.
            review = "review" in message.lower()
            state = "needs_review" if review else "gave_up" if tries >= MAX_TRIES else "failed"
            _record(
                rt,
                key,
                now,
                state=state,
                tries=tries,
                error=message,
                retry_at=(now + RETRY * tries).isoformat(),
            )
            if review:
                _audit(rt, "needs_review", candidate, {"error": message})
            summary["review" if review else "failed"] += 1
            log.warning("automatic Strava publication of %s: %s", candidate["group_id"], message)
            continue
        except Exception as exc:  # network or provider trouble: try again later
            message = f"{type(exc).__name__}: {exc}"[:300]
            state = "gave_up" if tries >= MAX_TRIES else "failed"
            _record(
                rt,
                key,
                now,
                state=state,
                tries=tries,
                error=message,
                retry_at=(now + RETRY * tries).isoformat(),
            )
            summary["failed"] += 1
            log.warning(
                "automatic Strava publication of %s failed: %s", candidate["group_id"], message
            )
            continue
        mirror = (result.get("mirror") or {}).get("state")
        _record(rt, key, now, state="published", tries=tries, mirror=mirror, error=None)
        _audit(rt, "ok", candidate, {"language": lang, "mirror": mirror})
        summary["published"] += 1


def status(rt) -> dict[str, Any]:
    """For Settings: switched on, since when, and the last automatic attempts."""
    tried = attempts(rt)
    recent = sorted(tried.values(), key=lambda entry: entry.get("at") or "", reverse=True)
    last_ok = next((entry["at"] for entry in recent if entry.get("state") == "published"), None)
    review = sum(1 for entry in tried.values() if entry.get("state") in ("needs_review", "gave_up"))
    problem = next(
        (
            entry.get("error")
            for entry in recent
            if entry.get("state") in ("needs_review", "gave_up", "failed") and entry.get("error")
        ),
        None,
    )
    start = since(rt)
    return {
        "enabled": enabled(rt),
        "since": start.isoformat() if start else None,
        "language": language(rt),
        "last_published_at": last_ok,
        "needs_review": review,
        "problem": problem,
    }
