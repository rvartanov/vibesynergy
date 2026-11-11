# Third-party code and licenses

VibeSynergy is licensed under MIT (see LICENSE). This file records every
piece of third-party code so the licence stays clear. Keep it current: **any PR that
adds a dependency or vendors/adapts outside code must update this file.**

## Rules

- Prefer dependencies over copying code.
- Vendored or adapted code goes in `vendor/<name>/` (or is clearly marked in place) with its
  original LICENSE file, the upstream URL and commit, and a note of local changes.
- Before vendoring, check compatibility: permissive licenses (MIT, BSD, Apache-2.0) are fine;
  copyleft licenses (GPL, AGPL, LGPL for static inclusion) constrain the final license and
  need an explicit decision first.
- Data formats and API endpoint knowledge (e.g. which Garmin endpoints exist) are not code;
  learning them from another project is fine, copying its implementation is vendoring.

## Runtime dependencies

| Package | Version | License | Upstream |
|---|---|---|---|
| defusedxml | 0.7.1 | PSF-2.0 | https://github.com/tiran/defusedxml |
| icalendar | 7.3.0 | BSD-2-Clause | https://github.com/collective/icalendar |
| recurring-ical-events | 3.8.2 | LGPL-3.0-or-later | https://github.com/niccokunzmann/python-recurring-ical-events |
| alembic | 1.20.0 | MIT | https://alembic.sqlalchemy.org |
| fastapi | 0.141.1 | MIT | https://github.com/fastapi/fastapi |
| fitdecode | 0.11.0 | MIT | https://github.com/polyvertex/fitdecode |
| mcp | 2.2.0 | MIT | https://github.com/modelcontextprotocol/python-sdk |
| garminconnect | 0.3.16 | MIT | https://github.com/cyberjunky/python-garminconnect |
| pyarrow | 25.0.1 | Apache-2.0 | https://arrow.apache.org |
| requests | 2.34.2 | Apache-2.0 | https://requests.readthedocs.io |
| python-ulid | 4.0.1 | MIT | https://github.com/mdomke/python-ulid |
| rich | 15.0.0 | MIT | https://github.com/Textualize/rich |
| sqlalchemy | 2.1.0 | MIT | https://www.sqlalchemy.org |
| typer | 0.27.2 | MIT | https://github.com/fastapi/typer |
| uvicorn | 0.53.0 | BSD-3-Clause | https://github.com/encode/uvicorn |
| zstandard | 0.25.0 | BSD-3-Clause | https://github.com/indygreg/python-zstandard |

Pulled in by the above (not used directly): curl_cffi 0.16.3 (MIT; ships a
libcurl-impersonate build, curl license), cffi 2.1.1 (MIT-0), pycparser 3.0 (BSD-3-Clause),
ua-generator 2.1.6 (Apache-2.0), urllib3 2.8.0 (MIT),
idna 3.20 (BSD-3-Clause), charset-normalizer 3.5.1 (MIT), certifi 2026.7.22 (MPL-2.0: used
unmodified as a separate package, so no obligations beyond keeping its notice).
starlette 1.7.0 (BSD-3-Clause), pydantic 2.13.5 and pydantic-core 2.46.5 (MIT),
annotated-types 0.8.0 (MIT), typing-inspection 0.4.4 (MIT), anyio 4.15.1 (MIT),
h11 0.16.0 (MIT), click 8.5.0 (BSD-3-Clause); via mcp: mcp-types 2.2.0 (MIT),
httpx2 2.13.1 (BSD-3-Clause), jsonschema 4.26.0 + jsonschema-specifications, referencing,
rpds-py, attrs (all MIT), opentelemetry-api 1.44.0 (Apache-2.0), pyjwt 2.15.0 (MIT),
cryptography 50.0.1 (Apache-2.0 OR BSD-3-Clause), python-multipart 0.0.32 (Apache-2.0),
sse-starlette 3.4.11 (BSD-3-Clause), typing-extensions 4.16.0 (PSF-2.0).
garminconnect 0.3 no longer depends on garth.

Notes:
- fitdecode ships a FIT profile generated from Garmin's FIT SDK. VibeSynergy uses it only
  through the fitdecode package and does not bundle the SDK itself.
- Garmin Connect is accessed through an unofficial API; that is a terms-of-use matter for
  the account owner, not a code license, and is recorded in the design

Development-only: pytest (MIT), ruff (MIT).

## Vendored or adapted code

Browser libraries for the web portal, shipped unmodified as static files under
`src/vibesynergy/portal/static/vendor/` (the portal has no build step, so there is no Node at
build or run time). Each folder keeps the upstream licence; `VERSIONS` there records the
exact files. Only change: the trailing `sourceMappingURL` comment is removed, because the
source maps are not shipped.

| Package | Version | License | Files | Upstream |
|---|---|---|---|---|
| echarts | 6.1.0 | Apache-2.0 (LICENSE + NOTICE kept) | `dist/echarts.esm.min.mjs` | https://github.com/apache/echarts |
| maplibre-gl | 6.11.2 | BSD-3-Clause | `dist/maplibre-gl.mjs`, `-shared.mjs`, `-worker.mjs`, `maplibre-gl.css` | https://github.com/maplibre/maplibre-gl-js |
| Lucide icons (16) | via design system `5c5496ecd7e6` | ISC (LICENSE kept) | `vendor/lucide/*.svg`; the same shapes inline in `js/ds_icons.js` | https://lucide.dev |

the author's design system ([rvartanov/design-system](https://github.com/rvartanov/design-system),
private) is vendored unmodified at the same commit: `css/semantic.css` (message and sport
colour tokens) and `js/ds_icons.js` (from its `js/icons.js`). Each file names the commit.

Map tiles come from OpenStreetMap's tile server by default (`[portal] tiles`). The portal
fetches them server-side with an identifying User-Agent, caches them on the server (as the OSM
tile usage policy asks) and serves them to the browser; prefetching is limited to the tiles
along a route. The map shows the required "© OpenStreetMap contributors" attribution. OSM
data is ODbL; the tiles are displayed for personal use, not redistributed.

## Planned (to review when added)

| Component | Expected license | Use |
|---|---|---|
| Garmin FIT SDK profile | Garmin FIT SDK license (FIT Protocol License) | only if a newer profile than fitdecode's is ever needed; check terms before bundling |
| duckdb, polars, numpy, scipy, statsmodels | MIT / MIT / BSD / BSD / BSD | analytics (dependencies) |
| Taxuspt/garmin_mcp | MIT | reference only, not copied |
| Golden Cheetah | GPL-3.0 | formula reference only; do not copy code |
| ECharts / MapLibre GL | Apache-2.0 / BSD-3-Clause | portal (dependencies) |
| OpenStreetMap tiles | ODbL data, tile usage policy | portal maps; attribution required |

Calendar dependencies also bring x-wr-timezone 2.0.1 (LGPL-3.0-or-later), python-dateutil
2.9.0.post0 (BSD-3-Clause OR Apache-2.0), six 1.17.0 (MIT), and tzdata 2026.4 (Apache-2.0).
The LGPL calendar packages are installed unmodified as separate, replaceable Python packages;
no implementation is copied or statically bundled. Review redistribution obligations before
shipping a packaged application; do not silently freeze these dependencies into an executable.
