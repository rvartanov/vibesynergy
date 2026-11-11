"""Every API call a shared page makes is part of its share link, or explicitly not.

The page modules are read as text: each ``get("…")`` / ``post("…")`` of a page and the
modules it imports must be allowed by ``share.PAGES`` for that page, or be listed in
NOT_SHARED with a reason. A new call on a shared page then can't silently fall outside
the link again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vibesynergy.portal import share

JS = Path(__file__).resolve().parents[1] / "src/vibesynergy/portal/static/js"
IMPORT = re.compile(r"""(?:import\s[^'"]*?from\s*|import\(\s*)["'](\.{1,2}/[^"']+)["']""")
CALL = re.compile(r"""(?<![\w.])(get|post)\(\s*[`"']([^`"'?]+)""")

# Share page -> its page module.
MODULES = {
    "dashboard": "dashboard",
    "activities": "activities",
    "planning": "planning",
    "schedule": "schedule",
    "zones": "zones",
    "coach": "coach",
    "gear": "gear",
    "sleep": "sleep",
    "recovery": "recovery",
    "injuries": "injuries",
    "body": "body",
    "metrics": "metrics",
    "correlations": "correlations",
    "sql": "sql",
}
# The start page shows the sections of the pages it took in (and the coach's advice).
GROUP = {"dashboard": ("today", "calendar", "diary", "coach")}
# Calls a shared page makes that stay with the owner; the page copes without them.
OWNER_ONLY = {
    ("GET", "/meta"),  # always allowed (share.ALWAYS)
    ("GET", "/prefs"),  # the owner's menu and blocks; a shared view uses the defaults
    ("GET", "/weather"),  # a coming day's forecast on the day page: never in a shared view
    ("POST", "/prefs"),
    ("POST", "/activity-effort"),  # the owner's own RPE
    ("POST", "/race-priority"),
    ("GET", "/activity-annotation-preview"),  # publishing to Strava/Garmin is the owner's
    ("GET", "/activity-publication-preview"),
    ("POST", "/activity-publication"),
    ("POST", "/activity-publication/remove"),
    ("POST", "/activity-purpose"),
    ("POST", "/activity-gear"),  # gear choices are the owner's
    ("POST", "/gear-default"),
    ("GET", "/settings"),  # configuration
    ("POST", "/settings"),
    ("POST", "/settings/restart"),
    ("GET", "/weather/places"),  # choosing the weather place is the owner's (Settings)
    ("GET", "/events/subscriptions"),  # the coach's webhook links (Coach → Settings)
    ("POST", "/events/subscriptions"),
    ("GET", "/plan/builder"),  # the plan builder's settings are the owner's
    ("POST", "/plan/session/move"),  # changing the plan is the owner's
    ("POST", "/plan/session/skip"),
    ("GET", "/push"),  # push notifications are the owner's device
    ("POST", "/push/subscribe"),
    ("POST", "/push/unsubscribe"),
    ("POST", "/push/test"),
    ("POST", "/settings/update"),
    ("POST", "/thresholds"),  # thresholds change in the owner's portal only
    ("POST", "/thresholds/set"),
    ("POST", "/thresholds/push"),
    ("GET", "/coach"),  # the coach's memory and settings
    ("POST", "/coach"),
    ("GET", "/coach-runner"),
    ("POST", "/coach-runner"),
    ("GET", "/mcp_clients"),
    ("GET", "/knowledge/archive"),
    ("POST", "/coach-log/revert"),
    ("POST", "/coach/replies"),
    ("POST", "/coach/threshold-confirm"),
    ("POST", "/knowledge/article/edit"),
    ("POST", "/knowledge/rule/edit"),
    ("POST", "/knowledge/proposal"),
    ("POST", "/knowledge/seed-prompts"),
    ("POST", "/gear"),  # gear settings are the owner's
}
# Diary reads and writes count under the Diary page, also where another page shows them.
DIARY = {
    ("GET", "/diary"),
    ("GET", "/notes"),
    ("POST", "/diary/note"),
    ("POST", "/diary/checkin"),
    ("POST", "/diary/injury"),
}


def _modules(name: str) -> set[Path]:
    seen: set[Path] = set()
    todo = [(JS / "pages" / f"{name}.js").resolve()]
    while todo:
        path = todo.pop()
        if path in seen or not path.exists() or "vendor" in path.parts:
            continue
        seen.add(path)
        todo += [(path.parent / m).resolve() for m in IMPORT.findall(path.read_text())]
    return seen


def _calls(name: str) -> set[tuple[str, str]]:
    out = set()
    for path in _modules(name):
        for method, target in CALL.findall(path.read_text()):
            route = "/" + target.split("${")[0].strip("/")
            if "${" in target:
                route += "/x"  # an id after the route: /coach/report/<id>
            out.add((method.upper(), route))
    return out


def _owner_only(method: str, route: str) -> bool:
    return any(method == m and (route == r or route.startswith(r + "/")) for m, r in OWNER_ONLY)


@pytest.mark.parametrize("page", sorted(MODULES))
def test_every_call_of_a_shared_page_is_in_its_link(page: str) -> None:
    link = share.Share("x", "t", (page, "diary", *GROUP.get(page, ())), True)
    missing = sorted(
        f"{m} {r}"
        for m, r in _calls(MODULES[page])
        if not link.allows(m, r) and not _owner_only(m, r) and (m, r) not in DIARY
    )
    assert not missing, f"{page}: add to share.PAGES or OWNER_ONLY: {missing}"


def test_everything_means_every_page_also_later_ones() -> None:
    link = share.Share("x", "t", (share.ALL,), False)
    assert set(link.public()["pages"]) == set(share.PAGES) and link.public()["all"]
    assert link.allows("GET", "/gear") and link.allows("GET", "/coach/report/12")
    assert not link.allows("GET", "/coach/report/12/x") and not link.allows("GET", "/settings")
    assert not link.allows("POST", "/diary/note")  # read only
    assert set(share.CHOICES) <= set(share.PAGES)
