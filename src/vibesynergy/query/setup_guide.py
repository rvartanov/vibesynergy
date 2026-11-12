"""The "Get started" guide: which of the three setup steps are done.

Read from what is already there, nothing of its own is stored: a connected source, a
threshold your zones come from, and a coach (an AI app with access, or the API coach on).
Whether the athlete put the guide away is a portal preference (``setup_guide_dismissed``).
"""

from __future__ import annotations

from typing import Any

from vibesynergy.query import thresholds

# Not a step done: the demo data, files you import, and the calendars and the plan, which
# bring no training or health data of your own.
NOT_A_DATA_SOURCE = frozenset({"demo", "files", "caldav", "intervals_icu"})


def state(rt) -> dict[str, Any]:
    steps = {"source": _source(rt), "zones": _zones(rt), "coach": coach_configured(rt)}
    return {"steps": steps, "complete": all(steps.values())}


def _source(rt) -> bool:
    return any(sid not in NOT_A_DATA_SOURCE for sid in rt.registry.enabled_ids())


def _zones(rt) -> bool:
    current = thresholds.current_values(rt.engine)
    return any(current.get(sport) for sport in thresholds.SPORTS)


def coach_configured(rt) -> bool:
    """An AI app may read and write as your coach, or the API coach is switched on."""
    from vibesynergy.api import oauth
    from vibesynergy.coach_runner import settings as runner_settings

    try:
        runner = runner_settings.load(rt)
        if runner.enabled and runner.api_key_set:
            return True
    except Exception:  # unreadable runner settings: the apps below still count
        pass
    if any(c["approved"] and not c["revoked"] for c in oauth.list_clients(rt.engine)):
        return True
    return any(not t["revoked"] for t in oauth.list_personal_access_tokens(rt.engine))
