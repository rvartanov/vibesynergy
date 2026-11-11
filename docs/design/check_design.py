"""Check candidate design token contrast, not full page accessibility."""

import re
from pathlib import Path


def luminance(hex_color):
    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * weight for v, weight in zip(linear, [0.2126, 0.7152, 0.0722], strict=True))


def contrast(a, b):
    low, high = sorted([luminance(a), luminance(b)])
    return (high + 0.05) / (low + 0.05)


def check_design_tokens():
    css = (
        Path(__file__).parents[2] / "src/vibesynergy/portal/static/css/design-tokens.css"
    ).read_text()
    pairs = [(fg, bg, 4.5) for fg in ["ink", "secondary", "action"] for bg in ["surface", "page"]]
    pairs += [
        ("on-action", "action", 4.5),
        ("warning", "warning-bg", 4.5),
        ("error", "error-bg", 4.5),
    ]
    pairs += [(fg, "surface", 3) for fg in ["line", "focus", *(f"z{i}" for i in range(1, 8))]]
    # Colour themes also set --muted and must keep text readable on --subtle panels.
    theme_pairs = [(fg, bg, 4.5) for fg in ["muted"] for bg in ["surface", "page"]]
    theme_pairs += [(fg, "subtle", 4.5) for fg in ["ink", "secondary", "action"]]
    theme_pairs += [(fg, "page", 3) for fg in ["line", "focus"]]
    blocks = re.findall(r"(:root[^\{]*)\{([^}]+)", css)
    palettes = set()
    for name, body in blocks:
        tokens = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{6})", body))
        checks = pairs + (theme_pairs if "muted" in tokens else [])
        for fg, bg, minimum in checks:
            ratio = contrast(tokens[fg], tokens[bg])
            assert ratio >= minimum, f"{name} {fg}/{bg}: {ratio:.2f} < {minimum}"
        palettes.update(re.findall(r'data-palette="(\w+)"', name))
        print(f"{name.strip()}: {len(checks)} token pairs passed")
    # Every colour theme has light, explicit dark and system dark.
    for palette in palettes:
        count = sum(f'data-palette="{palette}"' in name for name, _ in blocks)
        assert count == 3, f"{palette}: {count} blocks, expected light, dark and system dark"


# style.css predates design-tokens.css and keeps a few of its own role tokens (notably
# --muted) that design.css overrides per theme with literal hex, since they must stay
# readable against the newer --page/--surface from design-tokens.css, not style.css's
# original warm-palette values. Check the actual cascade result, not either file alone.
def check_legacy_muted():
    tokens_css = (
        Path(__file__).parents[2] / "src/vibesynergy/portal/static/css/design-tokens.css"
    ).read_text()
    design_css = (
        Path(__file__).parents[2] / "src/vibesynergy/portal/static/css/design.css"
    ).read_text()
    page_light, surface_light = re.search(
        r":root\s*\{[^}]*--page:\s*(#[0-9a-f]{6})[^}]*--surface:\s*(#[0-9a-f]{6})", tokens_css
    ).groups()
    page_dark, surface_dark = re.search(
        r':root\[data-theme="dark"\]\s*\{[^}]*--page:\s*(#[0-9a-f]{6})[^}]*--surface:\s*(#[0-9a-f]{6})',
        tokens_css,
    ).groups()
    muted_light = re.search(r":root\s*\{\s*--muted:\s*(#[0-9a-f]{6})", design_css).group(1)
    muted_dark = re.search(
        r':root\[data-theme="dark"\]\s*\{\s*--muted:\s*(#[0-9a-f]{6})', design_css
    ).group(1)
    for theme, muted, page, surface in [
        ("light", muted_light, page_light, surface_light),
        ("dark", muted_dark, page_dark, surface_dark),
    ]:
        for bg_name, bg in [("page", page), ("surface", surface)]:
            ratio = contrast(muted, bg)
            assert ratio >= 4.5, f"{theme} muted/{bg_name}: {ratio:.2f} < 4.5"
    print("legacy --muted override: 4 pairs passed (light/dark x page/surface)")


MESSAGE_TYPES = ("info", "advice", "success", "warning", "danger", "planned", "recovery", "race")
SPORTS = ("swim", "bike", "run", "strength", "mobility", "brick", "walk", "other")
STATIC = Path(__file__).parents[2] / "src/vibesynergy/portal/static"


def _semantic_families():
    """The light and dark message/sport families from the vendored semantic.css."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "css/semantic.css").read_text(), flags=re.S)
    light = re.search(r":root\s*\{([^}]+)\}", css).group(1)
    dark = re.search(r':root\[data-theme="dark"\]\s*\{([^}]+)\}', css).group(1)
    system_dark = re.search(
        r':root:not\(\[data-mode="light"\]\):not\(\[data-theme="light"\]\)\s*\{([^}]+)\}', css
    )
    parse = lambda body: dict(re.findall(r"--((?:msg|sport)-[\w-]+):\s*([^;]+);", body))  # noqa: E731
    assert parse(dark) == parse(system_dark.group(1)), (
        "semantic.css: explicit and system dark differ"
    )
    return parse(light), parse(dark)


def check_semantic_tokens():
    """Message and sport families on every theme's own roles, light and dark.

    Per family: text/icon colour on its background, on --surface and on --page (4.5:1);
    --ink and --secondary on the background (4.5:1); the border on all three (3:1). The
    families are the design system's; warning and danger resolve to the theme's roles.
    """
    light, dark = _semantic_families()
    css = (STATIC / "css/design-tokens.css").read_text()
    checked = 0
    for name, body in re.findall(r"(:root[^\{]*)\{([^}]+)", css):
        theme = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{6})", body))
        family = dark if "color-scheme: dark" in body else light

        def value(token, theme=theme, family=family):
            raw = family[token].strip()
            ref = re.fullmatch(r"var\(--([\w-]+)\)", raw)
            return theme[ref.group(1)] if ref else raw

        for kind, names in (("msg", MESSAGE_TYPES), ("sport", SPORTS)):
            for item in names:
                fg, bg, edge = (value(f"{kind}-{item}{s}") for s in ("", "-bg", "-border"))
                pairs = [(fg, bg, 4.5), (fg, theme["surface"], 4.5), (fg, theme["page"], 4.5)]
                pairs += [(theme["ink"], bg, 4.5), (theme["secondary"], bg, 4.5)]
                pairs += [(edge, bg, 3), (edge, theme["surface"], 3), (edge, theme["page"], 3)]
                for a, b, minimum in pairs:
                    ratio = contrast(a, b)
                    assert ratio >= minimum, (
                        f"{name.strip()} {kind}-{item} {a}/{b}: {ratio:.2f} < {minimum}"
                    )
                    checked += 1
        zones = {theme[f"z{i}"] for i in range(1, 8)}
        sports = {value(f"sport-{s}") for s in SPORTS}
        assert not zones & sports, f"{name.strip()}: a sport colour equals a zone colour"
    print(f"message and sport families: {checked} token pairs passed")


def check_one_place_for_semantic_colours():
    """A sport or message colour is defined only in semantic.css (one place to change it)."""
    names = "|".join([*(f"msg-{m}" for m in MESSAGE_TYPES), *(f"sport-{s}" for s in SPORTS)])
    defined = re.compile(rf"--(?:{names})(?:-bg|-border)?\s*:")
    for path in sorted((STATIC / "css").glob("*.css")):
        if path.name != "semantic.css":
            assert not defined.search(path.read_text()), (
                f"{path.name} redefines a design-token colour"
            )
    for path in sorted((STATIC / "js").rglob("*.js")):
        assert not re.search(r"#[0-9a-fA-F]{6}\b", path.read_text()), (
            f"{path.name}: hardcoded colour"
        )


def main():
    check_design_tokens()
    check_legacy_muted()
    check_semantic_tokens()
    check_one_place_for_semantic_colours()
    print("Manual keyboard, reflow, chart and screen-reader checks still required.")


if __name__ == "__main__":
    main()
