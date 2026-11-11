"""The VibeSynergy source set: mark, wordmark, lockups and icons as SVG.

    uv run --no-project python tools/brand_vibesynergy.py

Writes SVGs to docs/brand/vibesynergy/ and a preview sheet (preview.html, preview.png with
headless Chrome). tools/brand.py takes the mark's geometry from here and writes the portal
files.
"""

import shutil
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs/brand/vibesynergy"
OUT.mkdir(parents=True, exist_ok=True)

NAVY, DEEP = "#06102B", "#020C1E"
CYAN, SKY, VIOLET = "#12D6F0", "#4BCAF2", "#9C6AFC"
WHITE = "#FFFFFF"


def defs(uid: str) -> str:
    return (
        "<defs>"
        f'<linearGradient id="g{uid}" x1="0" y1="0" x2="1" y2="0.35">'
        f'<stop offset="0" stop-color="{CYAN}"/><stop offset=".55" stop-color="{SKY}"/>'
        f'<stop offset="1" stop-color="{VIOLET}"/></linearGradient>'
        f'<linearGradient id="d{uid}" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{SKY}"/><stop offset="1" stop-color="{CYAN}"/>'
        "</linearGradient></defs>"
    )


# The mark in a 64x64 box: a "V" of two swooshes (two streams becoming one) with a small
# inner stroke, and a dot above the right arm (a person in motion).
LEFT = "M3 19C16 14 27 28 32 53C26 49 22 41 18 34C14 27 9 22 3 19Z"
INNER = "M16 25C21 27 25 33 27.5 41"
RIGHT = "M32 53C38 34 47 19 62 11C53 22 46 35 41 53C38 57.5 34 57.5 32 53Z"
DOT = (45, 11.5, 5.6)


def glyph(uid: str, mono: str | None = None) -> str:
    fill = mono or f"url(#g{uid})"
    dot = mono or f"url(#d{uid})"
    x, y, r = DOT
    return (
        f'<path d="{LEFT}" fill="{fill}"/>'
        f'<path d="{RIGHT}" fill="{fill}"/>'
        f'<path d="{INNER}" fill="none" stroke="{fill}" stroke-width="4.2" '
        'stroke-linecap="round"/>'
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="{dot}"/>'
    )


def svg(body: str, box: str = "0 0 64 64", label: str = "VibeSynergy") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}" role="img" '
        f'aria-label="{label}">{body}</svg>\n'
    )


def tile(uid: str, ring: bool = False, mono: str | None = None) -> str:
    body = defs(uid) + f'<rect width="64" height="64" rx="15" fill="{NAVY}"/>'
    if ring:
        body += (
            f'<circle cx="32" cy="33" r="23" fill="none" stroke="url(#g{uid})" stroke-width="2.4"/>'
        )
        body += (
            f'<g transform="translate(32 33) scale(.62) translate(-33 -32)">{glyph(uid, mono)}</g>'
        )
    else:
        body += (
            f'<g transform="translate(32 32) scale(.82) translate(-33 -32)">{glyph(uid, mono)}</g>'
        )
    return body


FONT = "font-family=\"'Poppins','Montserrat','Segoe UI',Arial,sans-serif\" font-weight=\"600\""


def wordmark(uid: str, vibe: str) -> str:
    return (
        f'<text x="0" y="40" {FONT} font-size="44" letter-spacing="-0.5">'
        f'<tspan fill="{vibe}">Vibe</tspan><tspan fill="url(#w{uid})">Synergy</tspan></text>'
    )


def lockup(uid: str, vibe: str, tagline: str, bg: str | None) -> str:
    grad = (
        f'<linearGradient id="w{uid}" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{CYAN}"/><stop offset="1" stop-color="{VIOLET}"/>'
        "</linearGradient>"
    )
    back = f'<rect width="360" height="88" fill="{bg}"/>' if bg else ""
    return (
        defs(uid).replace("</defs>", grad + "</defs>")
        + back
        + f'<g transform="translate(6 10) scale(1.05)">{glyph(uid)}</g>'
        + f'<g transform="translate(82 8)">{wordmark(uid, vibe)}'
        + f'<text x="2" y="66" {FONT.replace("600", "400")} font-size="12.5" fill="{vibe}" '
        f'letter-spacing="1.2">{tagline}</text></g>'
    )


# Feature icons, 24x24 line style with the brand gradient.
ICONS = {
    "health": '<path d="M2 12h4l2-5 3 10 3-13 2 8h6"/>',
    "fitness": '<path d="M3 10v4M6 7v10M9 8v8M15 8v8M18 7v10M21 10v4M9 12h6"/>',
    "run": (
        '<circle cx="15.5" cy="4.5" r="2"/><path d="M8 21l3-6 3 2v5M6 11l3-3 4 1 2 4 3 1'
        'M11 15l2-6"/>'
    ),
    "coach": (
        '<path d="M12 4a4 4 0 0 0-7.5 2A4 4 0 0 0 4 14a4 4 0 0 0 4 5 3 3 0 0 0 4 1z'
        "M12 4a4 4 0 0 1 7.5 2A4 4 0 0 1 20 14a4 4 0 0 1-4 5 3 3 0 0 1-4 1zM12 4v16"
        'M8 9h2M14 9h2M8 14h2M14 14h2"/>'
    ),
    "progress": '<path d="M3 21h18M5 18v-3M9 18v-6M13 18V9M17 18V5"/>',
    "sleep": '<path d="M20 15A8 8 0 1 1 10 4a6.5 6.5 0 0 0 10 11zM15 3h4l-4 4h4"/>',
    "wellbeing": (
        '<path d="M12 20s-8-4.8-8-10.5A4.5 4.5 0 0 1 12 7'
        'a4.5 4.5 0 0 1 8 2.5C20 15.2 12 20 12 20z"/>'
    ),
}


def icon(name: str, d: str) -> str:
    uid = f"i{name}"
    grad = (
        f'<defs><linearGradient id="{uid}" x1="0" y1="0" x2="1" y2="1" '
        f'gradientUnits="objectBoundingBox"><stop offset="0" stop-color="{CYAN}"/>'
        f'<stop offset="1" stop-color="{VIOLET}"/></linearGradient></defs>'
    )
    return svg(
        grad + f'<g fill="none" stroke="url(#{uid})" stroke-width="1.6" stroke-linecap="round" '
        f'stroke-linejoin="round">{d}</g>',
        box="0 0 24 24",
        label=name,
    )


TAG_EN = "Your data. Your health. Your potential."
files = {
    "mark.svg": svg(defs("m") + glyph("m")),
    "mark-white.svg": svg(glyph("w", WHITE)),
    "app-icon.svg": svg(tile("a")),
    "app-icon-ring.svg": svg(tile("r", ring=True, mono=WHITE)),
    "lockup-dark.svg": svg(lockup("ld", WHITE, TAG_EN, NAVY), box="0 0 360 88"),
    "lockup-light.svg": svg(lockup("ll", NAVY, TAG_EN, None), box="0 0 360 88"),
}
for name, d in ICONS.items():
    files[f"icon-{name}.svg"] = icon(name, d)
for name, body in files.items():
    (OUT / name).write_text(body)

cells = "".join(
    f'<figure><div class="c{" d" if "white" in n or "dark" in n or n.startswith("icon") else ""}">'
    f'<img src="{n}"></div><figcaption>{n}</figcaption></figure>'
    for n in files
)
(OUT / "preview.html").write_text(
    "<!doctype html><meta charset=utf-8><style>"
    f"body{{margin:0;padding:24px;background:#f4f6fb;font:13px sans-serif;display:flex;"
    "flex-wrap:wrap;gap:18px}figure{margin:0;width:300px}"
    ".c{height:150px;display:flex;align-items:center;justify-content:center;"
    "background:#fff;border-radius:12px}"
    f".c.d{{background:{NAVY}}}img{{max-width:260px;max-height:120px}}"
    "figcaption{margin-top:6px;color:#334}</style>" + cells
)
chrome = shutil.which("google-chrome") or shutil.which("chromium")
if chrome:
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--window-size=1340,760",
            f"--screenshot={OUT / 'preview.png'}",
            (OUT / "preview.html").as_uri(),
        ],
        check=True,
        capture_output=True,
    )
