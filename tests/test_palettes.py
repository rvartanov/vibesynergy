"""Colour themes: the stored choice, and the same list in the portal and the server."""

import re
from pathlib import Path

import pytest

from vibesynergy.portal import prefs

STATIC = Path(__file__).parents[1] / "src/vibesynergy/portal/static"


def test_palette_is_saved_and_validated(tmp_path: Path) -> None:
    assert prefs.save(tmp_path, {"palette": "stroom"})["palette"] == "stroom"
    assert prefs.load(tmp_path)["palette"] == "stroom"
    with pytest.raises(ValueError, match="palette"):
        prefs.save(tmp_path, {"palette": "neon"})
    assert "palette" not in prefs.save(tmp_path, {"palette": None})


def test_portal_and_server_know_the_same_palettes() -> None:
    js = (STATIC / "js/theme.js").read_text()
    listed = re.search(r"PALETTES = \[([^\]]+)\]", js).group(1)
    assert tuple(re.findall(r'"(\w+)"', listed)) == prefs.PALETTES
    css = (STATIC / "css/design-tokens.css").read_text()
    for palette in prefs.PALETTES[1:]:  # the standard one is the plain :root
        assert css.count(f'data-palette="{palette}"') == 3
