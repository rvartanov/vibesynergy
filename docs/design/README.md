# VibeSynergy UI design, version 1

> Issue and PR numbers in text written before 30-10-2026 refer to the old tracker (they are marked "old #N" where they were links); the GitHub tracker started fresh then.

Status: design contract for later builds, 2026-10-05. Tracked in
old with zone presentation in
old. This is a researched design system,
not a claim of user-tested usability or complete WCAG conformance. Existing data contracts
and [coach rules](../COACH.md) apply.

The portal already uses the shared tokens and the route map below. A populated task and
axe pass is recorded in [VALIDATION.md](VALIDATION.md). What remains on is a
screen-reader walkthrough and deployment with live verification. A merged design document
is not a finished redesign.

Open [the interactive prototype](index.html) through a static HTTP server rooted at the repository
(`python3 -m http.server 18449 --bind 127.0.0.1`, then `/docs/design/`). It contains
**synthetic examples only**, makes no API calls and changes no VibeSynergy settings. The
prototype explores overview, activity/zones, health, insight, source status and coach editing.
Its navigation uses the current Day, Training, Health and Insight groups; source status is
the Settings → Data specimen. The prototype's display type is larger than production.
Later builds follow the production tokens in section 4, not the specimen's heading sizes.
Test light/dark, narrow viewport, keyboard navigation, zone metric selection,
empty/stale/error states and draft preview/activation.

## 1. Method and evidence

Use a short discover → define → develop → deliver loop: inspect real tasks and existing
constraints, state the problem, compare a concrete design, then validate and refine it.
This applies the [Design Council Double Diamond](https://www.designcouncil.org.uk/resources/the-double-diamond/)
without claiming that a process diagram guarantees a good product. The athlete's reports about
stale workouts and disconnected coaching are direct input; proposed navigation and density
choices still need task-based feedback.

Evaluate designs using [Nielsen's usability heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/):
make status visible, use familiar terms, preserve user control, keep patterns consistent,
prevent errors, and expose detail when needed. These are inspection guidelines, not a
numerical quality score or proof that one visual style is best.

Use [WCAG 2.2](https://www.w3.org/WAI/WCAG22/quickref/) as the accessibility target (AA).
Use the [GOV.UK error summary pattern](https://design-system.service.gov.uk/components/error-summary/)
for failed form submission: an actionable summary plus matching errors by the fields,
with focus on the summary. Service outages instead need a page/section status and retry.
We reuse the interaction reasoning, not government branding or its whole component stack.

### 1.1 Decisions and the guideline behind each one

Later builds follow this table. A heuristic is an inspection guide, not a usability score.
Passing token checks is not a WCAG certificate.

Sources: [Nielsen Norman Group's 10 usability heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/);
[WCAG 2.2](https://www.w3.org/TR/WCAG22/);
[Apple Human Interface Guidelines: Layout](https://developer.apple.com/design/human-interface-guidelines/layout)
and [Typography](https://developer.apple.com/design/human-interface-guidelines/typography);
[Material 3 design tokens](https://m3.material.io/foundations/design-tokens/overview),
[accessible design](https://m3.material.io/foundations/accessible-design/overview)
and [colour system](https://m3.material.io/styles/color/overview);
[GOV.UK error summary](https://design-system.service.gov.uk/components/error-summary/)
and [layout](https://design-system.service.gov.uk/styles/layout/).
Athlete-facing copy also follows the communication rule in `CONTRIBUTING.md`.

| Decision | Contract | Guideline |
|---|---|---|
| Three-step reading | What happened, what it means, what to do next, in that order | NN/g: match the real world; aesthetic and minimalist design. Communication rule: advice first, short, plain language |
| Separate evidence kinds | Measured value, source estimate, interpretation and proposal each have a text label | NN/g: match the real world. WCAG 1.3.1 info and relationships. A green surface never means medical clearance |
| Four groups plus Settings | Day, Training, Health, Insight in the menu; Settings behind the gear; Data lives under Settings | NN/g: recognition rather than recall; consistency. Apple HIG: few persistent top-level destinations. Material: stable navigation destinations |
| Visible labels | Group and page names stay visible. Narrow screens use one labelled menu | NN/g: recognition rather than recall. WCAG 1.1.1 non-text content and 2.4.6 headings and labels |
| Stable addresses | Existing hashes keep working. Moved pages redirect with `replaceState`. Hiding a menu entry is not access control | NN/g: user control and freedom; consistency. WCAG 3.2.3 consistent navigation |
| Page links, not a fake tab widget | Settings and Data sub-navigation are links with `aria-current="page"` | [WAI-ARIA `aria-current`](https://www.w3.org/TR/wai-aria-1.2/#aria-current). GOV.UK treats a change of page as navigation, not an in-page tab panel |
| One column when narrow | Flexible columns via `minmax(min(..., 100%), 1fr)`. Patterns that cannot wrap stack at 600px. Wide tables scroll inside a labelled, focusable region | WCAG 1.4.10 reflow (320 CSS px). GOV.UK layout: content stays in the viewport |
| Reading width | `--content: 1200px` for the header inset and notices. `main` is 1232px including 16px padding, so the inner column is 1200px. Prose stays within `--measure: 75ch` | GOV.UK layout and NN/g line length (about 65–75 characters) |
| Type | System font. Production: body 16px/1.5, secondary 14px/1.5, headings 28/20/16px. Tabular numerals on measurements | Apple HIG typography: system font and a clear hierarchy. WCAG 1.4.4 resize text. The prototype's 32px title is specimen display type |
| Colour roles | One token file for page, surface, ink, action, status and zones. Light, explicit dark and system dark | Material colour roles. WCAG 1.4.3 contrast (text 4.5:1, large text 3:1) and 1.4.11 non-text contrast (3:1) |
| Status separate from zones | Success, warning and error use their own roles. Zone swatches never carry alert meaning | WCAG 1.4.1 use of colour. NN/g: consistency |
| Controls 44px | Default interactive height is `--control: 44px` | Apple HIG layout: 44×44 pt minimum. This is above WCAG 2.5.8's 24×24 CSS px minimum |
| Dense targets 24px | Calendar year cells and equivalent inline marks use `--target-min: 24px` inside a labelled group, with text that carries the meaning | WCAG 2.5.8 target size minimum. Spacing in the grid supplies the required separation |
| Focus and skip | 3px focus ring, skip link, one `h1`, landmarks, reduced motion | WCAG 2.4.1 bypass blocks, 2.4.7 focus visible, 2.3.3 animation from interactions (AAA; honoured here as the platform setting). Apple HIG and Material both respect reduced motion |
| Sticky header | The header stays put, one row also on a phone: Quick add, Search, Waiting for you and Settings, each with an accessible name and a tooltip (theme and bug report live under Settings → System and in the footer). Nothing floats over the page: on a phone Quick add is in the header too, and the name gives way to the mark below 440px. Focused controls must not sit fully underneath it | WCAG 2.4.11. Scroll padding keeps a focused control clear of the header. A screen-reader pass is still open on |
| One main action | A card or form has at most one `.btn.primary` (filled, `--action` on `--on-action`): the action the card is for. Everything else is a plain `.btn` | NN/g: aesthetic and minimalist design; recognition rather than recall |
| Nothing to show takes no space | A start-page block or key number without data is left out and named in one line ("Not shown, no data yet: …") with the next step; a block the athlete ticks in the arrange panel stays | NN/g: aesthetic and minimalist design; visibility of system status |
| Errors | Keep the entered values. Move focus to a linked summary, and repeat the error on the field | GOV.UK error summary. WCAG 3.3.1 error identification. NN/g: help users recover from errors |
| Direct save | Edits save on change. No reason-for-change field. Draft and activate stay different actions | NN/g: error prevention; user control. Communication rule |
| Missing data | Absence is `—` with an explanation. It is never stored or shown as zero | NN/g: visibility of system status; error prevention |
| Refresh | Keep the last useful result, name its evidence time, and say when a newer fetch failed | NN/g: visibility of system status. WCAG 4.1.3 status messages |
| Language and dates | User-facing strings are Dutch and English through i18n. Dates and times use `vibesynergy.core.user_datetime` (`d-m-yyyy HH:mm`; clock times and chart axes have no seconds, durations, laps and splits keep them) | WCAG 3.1.1 language of page. NN/g: match the real world |
| Charts and zones | Direct labels, a data table, and stable Z1…Zn swatches. Colour is an extra channel | WCAG 1.4.1 use of colour. See section 5 |

Recorded deviations from a cited source:

- Material's 48dp touch target is not the VibeSynergy size. Controls use Apple's 44pt target. The calendar year grid uses WCAG's 24px minimum.
- GOV.UK body text is 19px. VibeSynergy body text is 16px on the system font, which is the production scale in `design.css`.
- The portal does not import a component library. Controls are native `button`, `a`, `details`, `table` and form elements styled with the shared tokens. There is no build step.
- Diary body-map dots stay small because the anatomy overlaps. The equivalent full-width location selector is the large target. See `docs/design/VALIDATION.md`.
- A metric chip's remove control is 28×28 CSS px, above the WCAG 2.5.8 minimum of 24px. The chip text names the metric and the button has its own accessible name.

## 2. Product tasks and decision hierarchy

VibeSynergy helps an athlete understand **what happened, what it means, and what to do next**.
The interface should make those three steps visible without turning every page into an AI chat.

| Task | First answer | Evidence/detail | Permitted action |
|---|---|---|---|
| Return after a workout | The completed activity is present, with time/source freshness | Activity duration, streams, zones and matching plan evidence | Read assessment; correct a note |
| Start the day | Today's commitments, plan and available recovery context | Recovery trends and source coverage | Review advice; record a check-in |
| Examine a trend | Metric, period, sport, units and coverage | Chart plus accessible table and methodology | Adjust filters; compare a period |
| Change coaching | Active approach and proposed differences | Method limitations, version history, confirmed goals | Save draft, preview, explicitly activate |
| Review a workout proposal | What changes and why | Owner, conflicts, safety checks, source snapshot | Approve one clearly scoped action when implemented |
| Diagnose missing data | Which source/dataset is late or failing | Last attempt, last success and next retry | Refresh/reconnect the affected source |

A measured value, a source estimate, an inferred interpretation and an action proposal are
four different things. Give them explicit labels near the content; never make a green
background imply that uncertain data is a medical clearance. One shared coach assessment
appears in several views; presentation style must not create a second assessment.

## 3. Information architecture and full route map

The menu has four groups. Settings stays on the gear. Saved order and visibility preferences
still apply. The group label stays visible with the current page; choosing a group opens a
short list of its pages. On a narrow screen that list is one labelled menu.

Day opens on the dashboard. The diary is the daily record. Do not rename or remove a hash
as a side effect of a visual change. Moved pages redirect on load and keep their query string.

| Group | Route(s) | Template and primary contents |
|---|---|---|
| Day | `#/dashboard`, `#/dashboard/<date>` | The one start page: the day as `h1` with a day switcher; advice and the plan against what was done (Garmin workouts outside the intervals.icu plan labelled "not in your plan"); check-in, complaints and the day's notes; recovery and the week (targets per sport, a day strip); the day in numbers (tiles not shown elsewhere, last night, timeline); the calendar (races with priority, appointments, year grid); the diary history behind a disclosure. A past day shows that day's record without advice or entry forms |
| Training | `#/activities` | Filterable list: date, sport, duration, distance, merged sources |
| Training | `#/activity/<id>` | Summary, assessment, streams and map, laps, zones, source evidence in a disclosure |
| Training | `#/training` | Period and sport controls, volume, fitness estimates, zones, best efforts |
| Training | `#/plan` | The intervals.icu plan, read-only: races with countdown, the phase timeline, one week (targets per sport, the week's note) and the next nine days |
| Training | `#/coach` | Overview tab: messages from the coach, what the coach knows about you (read-only), morning and evening advice, week and block |
| Training | `#/coach/settings` | Settings tab: advice language, detail and tone, notes for the coach, what to remember, coaching name rules. Saves as you edit. Tabs are page links with `aria-current` |
| Health | `#/sleep` | Latest night, personal trend, coverage, estimate label |
| Health | `#/body` | Metric selector, recent value and trend. Absent measurements stay absent |
| Insight | `#/metrics` | Metric, period, trend, units, coverage and data table |
| Insight | `#/correlations` | Two named metrics, matched dates, sample count and uncertainty. No causation claim |
| Insight | `#/analysis` | Report library, then report detail: run, progress, captured coach context, follow-up |
| Insight | `#/sql` | Advanced tool, labelled as such. Bounded read-only query and an accessible result table |
| Training | `#/zones` | Zones & thresholds per sport, plus About you (sex, max and resting heart rate). `#/settings/you` redirects here |
| Settings | `#/settings/system` | Time zone and units, datasets, schedule, best source per measurement (reorder per metric, saved at once), folders |
| Settings | `#/settings/appearance` | Language, system/light/dark, navigation and dashboard preferences |
| Settings | `#/settings/ai` | Provider, model, readiness and usage. Separate from the training approach |
| Settings | `#/settings/alerts` | Channels, recipients, rhythm and a test delivery |
| Settings | `#/settings/calendar` | Sign-in and calendar choice in the page. Credentials stay on the server |
| Settings | `#/settings/sharing` | Scope, recipient and status, with preview and revocation |
| Settings | `#/settings/data/sources` | Connection and dataset freshness, separately. One next action for the source that failed |
| Settings | `#/settings/data/sync` | Attempts, queue and coverage. Technical detail in a disclosure |
| Settings | `#/settings/data/history` | Import and backfill progress, pause and resume, completed and failed ranges |
| Settings | `#/settings/system` | Maintenance, configuration and version. Diagnostic exports contain no health content |

Legacy hashes redirect: `#/today` and `#/day/<date>` to `#/dashboard[/<date>]`, `#/diary` and
`#/calendar` to the start page's diary and calendar sections (`?section=`), `#/data` to `#/settings/data/sync`,
`#/sources` to `#/settings/data/sources`, `#/history` to `#/settings/data/history`,
`#/settings/coach` to `#/coach/settings`.
`#/activity/<id>` keeps its parent page highlighted.

Shared views keep their own allow-list. A hidden menu entry is not that allow-list.
Share links may still name `diary`, `data`, `history` or `sources`; the shell maps those
names onto the routes above and does not expose the rest of Settings.

Morning and evening advice is the same daily coaching the dashboard shows, repeated on
Coach and kept on each day page. VibeSynergy's own workout proposals were removed; the
plan is edited in intervals.icu. Do not ship a control that pretends to approve a workout.

## 4. Visual system and reusable components

Preserve the Confluence mark and [brand](../BRAND.md) until the **VibeSynergy** rebrand (decided 5-11-2026, see BRAND.md) is rolled out; new components should already work on the dark-first navy look and leave room for the cyan-to-violet brand accent, which is never a status or zone colour. Surfaces stay quiet, text stays
strong, primary actions use the teal action role, and charts use a restrained accent set.
Zone colours are a separate ramp. These colour choices are VibeSynergy's; the contrast floors
come from WCAG.

Colour roles live in `src/vibesynergy/portal/static/css/design-tokens.css` (`--page`, `--surface`,
`--ink`, `--secondary`, `--action`, status and `--z1`…`--z7`). Layout, type and target sizes
live in `src/vibesynergy/portal/static/css/design.css` and override the older literals in
`style.css`. Do not copy a component's styles into each page, and do not add a UI framework
to get them.

Production tokens, already applied:

| Token | Value | Use |
|---|---|---|
| `--type-body`, `--leading-body` | 16px, 1.5 | Body |
| `--type-secondary` | 14px | Supporting text |
| `--type-h1`, `--type-h2`, `--type-h3` | 28px, 20px, 16px | Page, section and subsection titles |
| `--space-1` … `--space-7` | 4, 8, 12, 16, 24, 32, 48px | Spacing scale. 48px is available for section breaks |
| `--content` | 1200px | Header inset and notice width |
| `--measure` | 75ch | Coach text and other reading blocks |
| `--control` | 44px | Default control size |
| `--target-min` | 24px | Dense targets that meet WCAG 2.5.8 |
| `--radius` | 12px | Cards and controls |
| `--motion` | 150ms ease | Removed under `prefers-reduced-motion` |

Use tabular numerals for measurements. Meaningful text uses `--ink` or `--secondary`.
`--muted` is only for secondary chrome and still has to clear 4.5:1 on `--page` and `--surface`.

Body text contrast is at least 4.5:1 and large text at least 3:1
([W3C contrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)).
Test both themes and the real foreground and background pairs. The focus ring is 3px
`--focus` with 3px offset. A skip link, landmarks, labelled controls and
`prefers-reduced-motion` are part of the shell. Auto-refresh keeps focus, scroll, open
disclosures and unsaved values.

| Component | Required variants/behaviour |
|---|---|
| Page header | Title, period/date, freshness, one main action; wraps on narrow screens |
| Freshness status | Updated, updating, partial, stale, failed; data time distinct from page-read time |
| Metric summary | Value + unit + period + source/estimate label; missing is `—` with explanation, never zero |
| Activity row | Sport/text icon, title, start, duration/distance, completed label and detail link |
| Coach panel | Shared assessment, scope/time, reason summary, evidence disclosure, next step; pending/stale visible |
| Filter bar | Explicit labels; persists in URL where supported; clear reset; no input-triggered external write |
| Chart frame | Title, metric/unit/time range, legend, coverage, text summary/table and relevant controls |
| Form field | Label, hint, control, error; preserves value on error; optional fields marked |
| Draft review | Active version, differences, preview, separate save and activate, and restore history |
| Status notice | Icon + text + action; success/warning/error/neutral are independent of zone colours |
| Source card | Connection and dataset freshness independently; local failure does not mark every source offline |
| Destructive/action review | Exact object, ownership, outcome, cancellation and read-back status |
| Settings navigation | Page links with `aria-current="page"`, including Data → Sources, Sync and History. Wraps on a narrow screen |
| Coach conversation | Plain assessment first, evidence in a disclosure, chat log with a pending status, reading width `--measure` |
| Route status | After navigation, announce the page title. Do not mark the whole main region as a live region |

Edits save directly; never require a reason-for-change field. Audit logs may record who/what/when automatically.

### 4.1 Colour and symbol language

Message types and sports look the same on every page and in every app, as the shared
[design system](https://github.com/rvartanov/design-system) defines them. VibeSynergy vendors
its `css/semantic.css` (the `--msg-*` and `--sport-*` colour families: text, background,
border) and its `js/icons.js` as `js/ds_icons.js` (Lucide icons, inline SVG), unmodified;
the Lucide SVG files sit in `vendor/lucide/`. A colour changes in the design system and is
re-vendored, so there is one place for it. `design.css` only maps VibeSynergy's classes onto
the tokens; `js/symbols.js` gives pages translated sport chips and icons.

| Use | Markup | Signals |
|---|---|---|
| Status chip (done, changed, missed, not in plan, differs, A race) | `.chip` plus a tone: `success` (or `good`), `warning`, `danger` (or `critical`), `info`, `planned`, `race`, `recovery`, `advice` | Tone colour, the tone's icon, the word |
| Notice (ok, warning, error, info) | `.notice` plus the same tones (`error` = `danger`); plain `.notice` is neutral | Tone border and icon, the message text in `--ink` |
| Sport | `data-sport` (swim, bike, run, strength, mobility, brick, walk, other) on the row or card; `sportChip()`, `withSportIcon()` or `sportIcon()` from `symbols.js` | Sport colour (border, bar, icon), the sport's icon, the sport name |
| Races and appointments | `ds-icon-list` with `ds-icon-item--race` / `--planned` | Type icon and colour, the text |

Every icon is `aria-hidden`; the word or name beside it carries the meaning (WCAG 1.4.1).
Sport colours are not zone colours (`check_design.py` checks they never coincide).
`check_design.py` checks every family on every theme (VibeSynergy, Volt, Stroom, Hartslag),
light, explicit dark and system dark: text and icon on the family's background, on
`--surface` and on `--page` (4.5:1), `--ink` and `--secondary` on the background (4.5:1),
the border on all three (3:1). It also fails when another stylesheet redefines a family
or a portal module hardcodes a hex colour. Labels: the design system's `msg.*` and
`sport.*` keys are in both catalogues; they are ids, so their English value differs from
the key (the one exception to "English text is the key", because "Run" is also a button).

### 4.2 Collapsible by default

Long overviews are collapsed by default: lists, tables and groups that run past about one
screen on a phone, history, logs, per-item settings, and "more" sections. They use the shared
`.collapsible` disclosure (`design.css`, the design system's `ds-collapsible`): a native
`details`/`summary` with a visible chevron, so a closed block reads as one that opens.

- The summary names what is inside and, where it helps, gives a one-line meaning ("3 phases to
  come; next: Build from 16-11"), never only "Details".
- What the page is for stays open: today's advice, the week against the plan, the next sessions.
- A disclosure the user opened stays open while the page refreshes (keyed state, see §6).
- Keyboard: the summary is the one focus stop; Enter and Space toggle it (native behaviour).

## 5. Zones, charts and evidence

old uses a shared `zonePresentation`
contract in implementation: metric + sport + boundary-source/version + ordered zone ID,
name, units, lower/upper bounds, measured time and coverage. Preserve configured zone count;
never silently remap seven power zones to five heart-rate zones. Heart rate uses bpm, power W,
and pace the user's distance/time unit with correctly ordered pace boundaries.

Use stable ordinal zone swatches, **Z1…Zn labels**, names and values together. Higher zone is
higher intensity for that particular configuration, not automatically better/worse or unsafe.
A zone palette is separate from status alerts and source brand colours. For >5 zones extend
the palette/labels instead of cycling colours. Unknown boundaries show “Zones not configured”
and an appropriate settings link, with the unzoned stream still readable.

Do not rely on colour alone: direct labels, different line styles/markers when series overlap,
and a data table preserve the meaning. This applies
[W3C use of colour](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html).
A label/table does not waive contrast requirements on essential chart marks. Candidate
colours must be tested in both themes and colour-vision simulations before production.

Streams show gaps as gaps. The horizontal axis states elapsed/moving/clock time. Pace,
heart rate and power use separate aligned panels by default, avoiding misleading mixed units.
Summaries distinguish average from moving average. Bars start at zero; zoomed line axes
show their scale clearly. Hover details also work through keyboard/focus or an equivalent
adjacent table. Legends identify metric, unit and zone set. No 3D charts or decorative gauges.
The prototype uses a five-zone synthetic example only, not production thresholds.

## 6. Loading, errors and editing

Keep a useful previous result while refreshing. Mark its actual evidence time and any failed
refresh. A source can be current while a new assessment is pending; show both. A data
correction invalidates derived advice visibly until the shared service replaces it.
First load uses a quiet loading message, not invented metric skeleton numbers. Empty state
says whether no activities exist, no source is connected, or a filter excluded everything.

When a form fails, preserve entries, move focus to a linked error summary, and show field
errors. Explain stale-edit conflicts and offer reload/review without silently overwriting.
Saving a draft and activating it have different button labels and success messages. On
activation show what future reports will use and what historical records retain. Profile
changes never imply authority to rewrite workouts or publish private context.

Route changes announce the new page title in a short status element. Asynchronous updates
announce in the region they belong to. The sticky header must not cover a focused control
completely ([WCAG 2.4.11](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html)).


## 7. Validation and rollout

The prototype is a design specimen. Automated contrast checks cover token pairs, not total
accessibility. Browser checks cover interactions, not real-user usability. Record gaps honestly.

Before a template ships: keyboard-only task completion, visible focus, 200% zoom and 320px
reflow, light/dark contrast, screen-reader names/status, missing/error/stale data, long Dutch
labels, multiple sports/zone counts, and preservation of form state across refresh. Verify
navigation preferences, deep links and shared-view permissions. No empty promise of WCAG
conformance without a full evaluation.

Representative tasks: find the latest workout; explain which metric is an estimate; inspect
zone time without colour; locate a failing source; save a coach draft without activating it;
recover a field error; compare a report's captured profile with the active one. First perform
an expert walkthrough with synthetic data, then get athlete feedback during ordinary use.
Track task success and observed mistakes; any completion-time targets are provisional.

What is already in the portal, and what still needs:

1. Shared tokens, focus, controls, zone presentation and state styles are in
   `design-tokens.css` and `design.css`. Layout and type tokens name the values already shipping.
2. Shell, Training, activity zones, Today, Health, Insight, Data-under-Settings and the
   other routes use those roles. Evidence is in `VALIDATION.md`.
3. Still open on a screen-reader walkthrough, deployment, and live verification.
   The populated axe and keyboard pass is in `VALIDATION.md`.
4. Colour themes (VibeSynergy (standard) plus Volt, Stroom and Hartslag
   from RunSynergy. A theme is `data-palette` on `<html>` and only redefines the colour roles
   in `design-tokens.css` (light, explicit dark and system dark each). Light/dark/system stays
   the separate `data-theme` choice. Zones keep their own ramp in every theme. The choice is
   kept on the device and in the portal preferences (`palette`), so every device follows it.
   `check_design.py` checks every theme's token pairs, including `--muted` and text on `--subtle`.

Link each further implementation PR to and record which templates are actually live.
Do not declare the redesign finished because this specification is merged.


Coach source selection addition (2026-09-30; retired 31-10-2026 with races now come from the
plan calendar): show two labelled checkboxes for
Garmin goals and Garmin events; all four combinations are valid. Preview the included items,
provenance, freshness and duplicate/conflict status alongside manual context. Explain that
switching off excludes items from coaching while keeping the source records. Follow the
existing draft/preview/activation flow; see [COACH.md §6.5](../COACH.md#65-optional-garmin-goals-and-events-as-coaching-inputs).
The prototype does not yet implement these new controls.

Assessment/dashboard refresh now preserves explicitly keyed disclosures and focus. Keys include
activity identity, so a newly selected latest workout does not inherit the previous workout's
open evidence state. The dashboard waits for its shared assessment card before restoring
state; manual status refresh preserves the same card's disclosure. Restoration does not take
focus away from a still-connected control the user selected during loading. Apply this shared
helper progressively to other templates; it is not yet a site-wide focus/scroll audit.
