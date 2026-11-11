"""MCP permission scopes.

``vibesynergy`` is the old single scope. It is no longer advertised and a new grant
cannot ask for it. Grants stored with it are rewritten once to the concrete names it stood
for (every scope except ``publish`` and ``plan.write``; see
``vibesynergy.api.oauth.expand_legacy_scope_grants``); until then reading a stored grant
still expands it. ``sql`` stays its own scope: free-form queries are not part of read,
write or publish.
"""

from __future__ import annotations

from collections.abc import Iterable

# Order is the consent-page order.
SCOPES: tuple[str, ...] = (
    "read",
    "read.expert",
    "diary.write",
    "coach.write",
    "plan.propose",
    "plan.write",
    "sync",
    "publish",
    "sql",
)
LEGACY_SCOPE = "vibesynergy"
# What the old scope stood for, and what a request or stored grant without any scope means.
LEGACY_EXPANSION: tuple[str, ...] = tuple(s for s in SCOPES if s not in {"publish", "plan.write"})
LEGACY_SCOPE_ERROR = (
    "The 'vibesynergy' scope is retired. Ask for the permissions you need by name: "
    + ", ".join(SCOPES)
    + "."
)
ADVERTISED_SCOPES: tuple[str, ...] = SCOPES
# A coach app's starting set. Publishing, SQL and plan writes stay off until added.
COACH_SCOPES: tuple[str, ...] = ("read", "coach.write", "diary.write", "plan.propose")
PAT_DAYS: tuple[int, ...] = (30, 90, 365)
PAT_DEFAULT_DAYS = 90

# Plain words for the consent page and Settings. English is the catalogue key.
SCOPE_LABELS: dict[str, str] = {
    "read": "See my training and health data",
    "read.expert": "Search my history in detail",
    "diary.write": "Log check-ins, notes and pain for me",
    "coach.write": "Write messages to me",
    "plan.propose": "Suggest changes to my plan",
    "plan.write": "Change my intervals.icu plan",
    "sync": "Fetch new data from my accounts",
    "publish": "Publish stats to Strava or Garmin",
    "sql": "Run read-only database queries",
}

# Shown to the model when a tool is outside the token. Not athlete UI copy.
SCOPE_DENIED: dict[str, str] = {
    "read": "This app isn't allowed to see your training and health data.",
    "read.expert": "This app isn't allowed to search your history in detail.",
    "diary.write": "This app isn't allowed to log check-ins, notes and pain.",
    "coach.write": "This app isn't allowed to write messages to you.",
    "plan.propose": "This app isn't allowed to suggest changes to your plan.",
    "plan.write": "This app isn't allowed to change your intervals.icu plan.",
    "sync": "This app isn't allowed to fetch new data from your accounts.",
    "publish": "This app isn't allowed to publish stats.",
    "sql": "This app isn't allowed to run database queries.",
}

READ_SCOPES = frozenset({"read", "read.expert", "sql"})
_SCOPE_SET = frozenset(SCOPES)


def expand_scopes(scopes: Iterable[str] | None) -> list[str]:
    """Turn a stored or requested scope list into concrete scopes, in catalogue order.

    The legacy name ``vibesynergy`` becomes every scope except ``publish`` and
    ``plan.write``. Unknown names
    are dropped. The result never contains ``vibesynergy`` itself.
    """
    found: list[str] = []
    for name in scopes or ():
        if name == LEGACY_SCOPE:
            for scope in LEGACY_EXPANSION:
                if scope not in found:
                    found.append(scope)
        elif name in _SCOPE_SET and name not in found:
            found.append(name)
    return found


def scope_denied(scope: str) -> str:
    return SCOPE_DENIED.get(scope, "This app isn't allowed to do that.")


def check_scope_names(names: Iterable[str]) -> list[str]:
    """Keep each known permission once, in the order given.

    The old ``vibesynergy`` word is not a scope you can set here; name each permission.
    """
    found: list[str] = []
    for name in names:
        text = name.strip()
        if text not in _SCOPE_SET:
            known = ", ".join(SCOPES)
            raise ValueError(f"unknown scope {text!r}; known: {known}")
        if text not in found:
            found.append(text)
    return found
