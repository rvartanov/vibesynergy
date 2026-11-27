"""Keep the design-token contrast checks (docs/design/check_design.py) passing in CI.

This is the "tokens for colour, with checked light/dark contrast" check for a
candidate-token contrast check, not a full page accessibility audit (see check_design.py).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "docs/design/check_design.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_design", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_design_token_contrast_pairs() -> None:
    _load().check_design_tokens()


def test_message_and_sport_families_contrast_in_every_theme() -> None:
    """every message type and sport, on every theme, light and dark (WCAG 2.2 AA)."""
    _load().check_semantic_tokens()


def test_semantic_colours_have_one_home() -> None:
    """sport and message colours live in semantic.css only; no hex colours in portal JS."""
    _load().check_one_place_for_semantic_colours()


def test_vendored_icons_match_the_svg_files() -> None:
    """The CSS mask files (vendor/lucide) are the inline icons' shapes (ds_icons.js), and
    only the ones design.css loads are shipped (the sport icons are inline only, #208)."""
    import re

    static = Path(__file__).parents[1] / "src/vibesynergy/portal/static"
    icons = dict(
        re.findall(r'^  "([\w-]+)": "(.*)",$', (static / "js/ds_icons.js").read_text(), re.M)
    )
    assert len(icons) == 16
    css = (static / "css/design.css").read_text()
    used = set(re.findall(r'url\("\.\./vendor/lucide/([\w-]+)\.svg"\)', css))
    shipped = {path.stem for path in (static / "vendor/lucide").glob("*.svg")}
    assert used == shipped, {"unused": sorted(shipped - used), "missing": sorted(used - shipped)}
    for name in used:
        assert name in icons, name
        svg = (static / f"vendor/lucide/{name}.svg").read_text()
        assert icons[name].replace('\\"', '"') in svg, name
        assert 'aria-hidden="true"' in svg
    assert (static / "vendor/lucide/LICENSE").exists()
    index = (static / "index.html").read_text()
    assert (
        index.index("css/design-tokens.css")
        < index.index("css/semantic.css")
        < index.index("css/design.css")
    )


def test_layout_tokens_match_design_contract() -> None:
    """Named layout tokens stay at the values documented in docs/design/README.md."""
    css = (Path(__file__).parents[1] / "src/vibesynergy/portal/static/css/design.css").read_text()
    expected = {
        "--space-1": "4px",
        "--space-2": "8px",
        "--space-3": "12px",
        "--space-4": "16px",
        "--space-5": "24px",
        "--space-6": "32px",
        "--space-7": "48px",
        "--content": "1200px",
        "--measure": "75ch",
        "--control": "44px",
        "--target-min": "24px",
        "--type-body": "16px",
        "--type-secondary": "14px",
        "--leading-body": "1.5",
        "--type-h1": "28px",
        "--leading-h1": "1.25",
        "--type-h2": "20px",
        "--leading-h2": "1.35",
        "--type-h3": "16px",
        "--leading-h3": "1.4",
    }
    for name, value in expected.items():
        assert f"{name}: {value}" in css, name


def test_legacy_muted_token_contrast() -> None:
    """design.css's --muted, per theme, against --page/--surface from design-tokens.css."""
    _load().check_legacy_muted()
