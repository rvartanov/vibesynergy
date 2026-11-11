"""Synthetic regression for shared zone colours and labels (Server vibesynergy#18).

Locks acceptance from the zone-colour issue: one presentation contract, configured
boundaries only, independent HR/pace/power zone sets, labels beside colour, and design
tokens for contrast. No live accounts or provider calls.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from vibesynergy.core.timeutil import Window
from vibesynergy.model import Activity, ActivityZoneTime
from vibesynergy.query.views import intensity_distribution

REPO = Path(__file__).resolve().parents[1]
DESIGN_CSS = REPO / "src/vibesynergy/portal/static/css/design.css"
TOKENS_CSS = REPO / "src/vibesynergy/portal/static/css/design-tokens.css"
ZONE_JS = REPO / "tools/check_zone_presentation.mjs"


def test_zone_presentation_js_contract() -> None:
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    subprocess.run(["node", str(ZONE_JS)], check=True, cwd=REPO)


def test_design_css_maps_zone_roles_to_palette() -> None:
    design = DESIGN_CSS.read_text()
    tokens = TOKENS_CSS.read_text()
    assert "--zone-other:" in design
    for n in range(8):
        assert re.search(rf"--zone-{n}:\s*var\(--", design)
    for n in range(1, 8):
        assert f"--z{n}:" in tokens


def test_intensity_distribution_three_zone_share_is_hr_only(runtime) -> None:
    from vibesynergy.derive import unify
    from vibesynergy.normalize.normaliser import write_records

    start = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    raw = runtime.raw.put(
        source="garmin",
        dataset="synthetic",
        item_key="hr-only-zones",
        payload=b"synthetic",
        content_type="text/plain",
    )
    with runtime.engine.begin() as conn:
        write_records(
            conn,
            [
                Activity("hr-only", start, start.date(), "running", name="HR zones"),
                ActivityZoneTime("hr-only", "hr", 1, 400, 120),
                ActivityZoneTime("hr-only", "hr", 2, 300, 140),
                ActivityZoneTime("hr-only", "hr", 3, 200, 160),
                ActivityZoneTime("hr-only", "hr", 4, 80, 175),
                ActivityZoneTime("hr-only", "hr", 5, 20, 190),
                ActivityZoneTime("hr-only", "pace", 0, 90, 0.0),
                ActivityZoneTime("hr-only", "pace", 1, 400, 2.4),
                ActivityZoneTime("hr-only", "pace", 2, 600, 2.8),
                ActivityZoneTime("hr-only", "pace", 3, 210, 3.2),
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
    unify.rebuild(runtime.engine)
    window = Window(date(2020, 1, 1), date(2030, 1, 1))
    hr = intensity_distribution(runtime.engine, window, basis="hr")
    pace = intensity_distribution(runtime.engine, window, basis="pace")
    assert "three_zone_share" in hr
    assert sum(hr["total_seconds_by_zone"].values()) > 0
    assert sum(pace["total_seconds_by_zone"].values()) > 0
    assert "three_zone_share" not in pace
