# Brand

## VibeSynergy

Name and mark of the app; domain **vibesynergy.nl**. The favicon, app icons, header, mail
footer and MCP connector use the mark below.

### Elements

- **Mark:** a "V" made of two swooshes, cyan to violet, with a dot above: a person moving, two streams becoming one (synergy). Variants in the set:
  - full-colour mark on dark and on white;
  - white mark on dark;
  - app icon: mark on a dark rounded tile;
  - round badge: white mark in a cyan-to-violet ring on a dark tile.
- **Wordmark:** "Vibe" in white (on dark) or navy (on light), "Synergy" in a cyan-to-violet gradient. A geometric sans, medium weight.
- **Tagline:** *Your data. Your health. Your potential.* NL: *Jouw data. Jouw gezondheid. Jouw potentieel.*
- **Feature icons:** thin line icons with a cyan-to-violet gradient: Health Insights (pulse), Fitness Tracking (dumbbell), Run Synergy (runner), AI Coach (brain), Progress (bars), Sleep (moon), Wellbeing (heart). These map to VibeSynergy's sections: Gezondheid/Herstel, Training, Activiteiten, Coach, Planning & voortgang, Slaap, Gezondheid.
- **Mood:** dark first. A deep navy background with soft cyan and violet light waves.

### Colours

| Role | Value | Use |
|---|---|---|
| Navy (background, dark) | `#06102B` (deep `#020C1E`) | page and tile background in dark mode, wordmark "Vibe" on light |
| Cyan | `#12D6F0` | gradient start, highlights, the dot |
| Sky | `#4BCAF2` | mid tone, icons |
| Violet | `#9C6AFC` | gradient end |
| White | `#FFFFFF` | mark and text on dark |

Gradient: `linear-gradient(90deg, #12D6F0, #9C6AFC)`.

### Rules

- **Accessibility first (WCAG 2.2 AA).** The gradient and cyan are brand accents for the logo, icons and large display text. Body text, buttons and status keep the AA-contrast tokens in `design-tokens.css`. Never put cyan text on white, and never make gradient text smaller than 24 px.
- **The gradient is no status colour.** Status (ok/warning/stop), sport and zone colours stay their own roles (`docs/design/README.md` §4.1); the brand gradient must not be read as a zone or status.
- **Light and dark:** dark is the primary look. The light theme uses white surfaces with the navy wordmark and gradient accents.
- **Source brands stay neutral:** the palette avoids the Garmin, Strava and Withings colours.

## Files

The approved source set is in `docs/brand/vibesynergy/`. `tools/brand.py` builds the
portal files from the same mark; all in `src/vibesynergy/portal/static/brand/` unless noted:

| File                                         | Use                                                   |
| -------------------------------------------- | ----------------------------------------------------- |
| `../icon.svg`                                | Favicon: the mark on a navy tile (works on light and dark tabs) |
| `favicon.ico`, `favicon-16/32.png`           | Favicon fallbacks                                     |
| `mark-dark.svg`, `mark-light.svg`            | App icon (rounded tile), navy is the default          |
| `glyph.svg`                                  | Mark without tile                                     |
| `square-dark.svg`                            | Source of the maskable icon and apple-touch icon      |
| `avatar-dark-512.png`, `avatar-light-512.png` | Avatar (MCP connector, GitHub); dark is the default |
| `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, `apple-touch-icon.png` | Web manifest, home screen, push notifications |

The portal header draws the mark inline with the `--brand-cyan`, `--brand-sky` and
`--brand-violet` tokens from `design-tokens.css`.

## Regenerate

The mark and colours live in `tools/brand.py`. It writes the SVGs and renders the PNGs with
headless Chrome:

```bash
uv run --no-project --with pillow python tools/brand.py
```
