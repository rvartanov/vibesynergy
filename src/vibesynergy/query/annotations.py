"""Read-only public annotation block from Garmin statistics (Strava's own figures when an
activity has no Garmin copy: one block, under the same label, with whatever there is).

The block is statistics only. Weather and a quote are added by the publication plugins.
This module does not contact a provider, require a saved assessment, authorize publication,
adopt existing markers, or modify a human-written description.
"""

from __future__ import annotations

from vibesynergy.core.config import load_settings
from vibesynergy.query import coach, evidence, publication_format

FORMAT_VERSION = 2


def preview(rt, activity_id: str, *, language: str = "en") -> dict:
    if language not in publication_format.LABELS:
        raise ValueError("Supported annotation languages: en, nl")
    active_profile = coach.profile_binding(coach.snapshot(rt.engine, raw=rt.raw))
    result = {
        "assessment_state": None,
        "state": "blocked",
        "block": None,
        "publication_available": False,
        "publication_authorized": False,
        "plugins": {"quote": "off", "weather": "off"},
        "limitations": [
            "Preview of an additional block only; no existing description is changed.",
            "Live account, target, scopes, ownership and human-text checks are still required.",
            "No delivery ledger or publisher is enabled by this preview.",
        ],
        "active_profile": active_profile,
        "needs_revision": False,
    }
    # Only whitelisted Garmin numbers, formatted deterministically; never freeform text.
    fields = publication_format.garmin_fields(rt, activity_id)
    scale = rt.settings.temperature
    stats = publication_format.stats_lines(fields, language, temperature=scale)
    source = "garmin"
    if not stats and not publication_format.has_garmin(rt, activity_id):
        fields = publication_format.strava_fields(rt, activity_id)
        stats = publication_format.strava_stats_lines(fields, language, temperature=scale)
        source = "strava"
    if not stats:
        return {**result, "reason": "no_public_statistics"}
    labels = publication_format.section_labels(
        load_settings(rt.settings.config_path).publication, language
    )
    block = publication_format.compose(
        insight=None,
        stats=stats,
        weather=None,
        quote=None,
        language=language,
        labels=labels,
    )
    valid = publication_format.clean(fields)
    return {
        **result,
        "state": "preview",
        "stats_source": source,
        "block": block,
        "stats": stats,
        "statistics": valid,
        "assessment_id": None,
        "input_version": None,
        "profile_version": active_profile.get("profile_version"),
        "active_profile": active_profile,
        "bound_versions": {},
        "needs_revision": False,
        "preview_version": evidence.digest(
            {"format": FORMAT_VERSION, "language": language, "block": block}
        ),
        "language": language,
    }
