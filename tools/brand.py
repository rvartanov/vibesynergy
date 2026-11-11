"""Regenerate the portal's brand files (the VibeSynergy mark), see docs/BRAND.md.

    uv run --no-project --with pillow python tools/brand.py

Builds the favicon, app icons and avatars from the mark in docs/brand/vibesynergy/, then
renders PNGs and favicon.ico with headless Chrome and Pillow.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src/vibesynergy/portal/static"
OUT = STATIC / "brand"
OUT.mkdir(parents=True, exist_ok=True)

NAVY = "#06102B"
GRADIENTS = (
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="0.35">'
    '<stop offset="0" stop-color="#12D6F0"/><stop offset=".55" stop-color="#4BCAF2"/>'
    '<stop offset="1" stop-color="#9C6AFC"/></linearGradient>'
    '<linearGradient id="d" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4BCAF2"/>'
    '<stop offset="1" stop-color="#12D6F0"/></linearGradient></defs>'
)
# The V of two swooshes with the dot above (docs/brand/vibesynergy/mark.svg).
MARK = (
    '<path d="M3 19C16 14 27 28 32 53C26 49 22 41 18 34C14 27 9 22 3 19Z" fill="url(#g)"/>'
    '<path d="M32 53C38 34 47 19 62 11C53 22 46 35 41 53C38 57.5 34 57.5 32 53Z" fill="url(#g)"/>'
    '<path d="M16 25C21 27 25 33 27.5 41" fill="none" stroke="url(#g)" stroke-width="4.2" '
    'stroke-linecap="round"/><circle cx="45" cy="11.5" r="5.6" fill="url(#d)"/>'
)


def svg(body, title="VibeSynergy"):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" '
        f'aria-label="{title}">{GRADIENTS}{body}</svg>\n'
    )


def scaled(scale):
    return f'<g transform="translate(32 32) scale({scale}) translate(-33 -32)">{MARK}</g>'


def tile(rx, scale, bg=NAVY):
    return f'<rect width="64" height="64" rx="{rx}" fill="{bg}"/>' + scaled(scale)


# Dark first: the mark on a navy tile everywhere; on white only where the page is white.
(OUT / "mark-dark.svg").write_text(svg(tile(15, 0.82)))
(OUT / "mark-light.svg").write_text(svg(tile(15, 0.82, "#FFFFFF")))
(OUT / "glyph.svg").write_text(svg(MARK))
for v, bg in (("dark", NAVY), ("light", "#FFFFFF")):
    (OUT / f"avatar-{v}.svg").write_text(svg(tile(32, 0.66, bg)))
# full-bleed square with the mark inside the maskable safe zone (inner 80%)
(OUT / "square-dark.svg").write_text(svg(tile(0, 0.62)))
(STATIC / "icon.svg").write_text(svg(tile(15, 0.86)))
for old in ("glyph-light.svg", "glyph-dark.svg", "square-light.svg"):
    (OUT / old).unlink(missing_ok=True)


def render(svg_path: Path, png: Path, size: int = 1024) -> None:
    """Rasterise an SVG with headless Chrome (transparent background)."""
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    page = png.with_suffix(".html")
    page.write_text(
        f'<body style="margin:0"><img src="{svg_path.as_uri()}" '
        f'width="{size}" height="{size}" style="display:block"></body>'
    )
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            f"--window-size={size},{size}",
            f"--screenshot={png}",
            page.as_uri(),
        ],
        check=True,
        capture_output=True,
    )


def pngs() -> None:
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        big = {}
        for name, src in [("icon", STATIC / "icon.svg")] + [
            (n, OUT / f"{n}.svg")
            for n in ("mark-dark", "square-dark", "avatar-light", "avatar-dark")
        ]:
            big[name] = Path(tmp) / f"{name}.png"
            render(src, big[name])

        def fit(name, size):
            return Image.open(big[name]).convert("RGBA").resize((size, size), Image.LANCZOS)

        fit("icon", 1024).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
        for s in (16, 32):
            fit("icon", s).save(OUT / f"favicon-{s}.png")
        fit("square-dark", 180).convert("RGB").save(OUT / "apple-touch-icon.png")
        for s in (192, 512):
            fit("mark-dark", s).save(OUT / f"icon-{s}.png")
        fit("square-dark", 512).save(OUT / "icon-maskable-512.png")
        for v in ("light", "dark"):
            fit(f"avatar-{v}", 512).save(OUT / f"avatar-{v}-512.png")


pngs()
