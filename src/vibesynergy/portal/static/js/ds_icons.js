// Vendored unmodified from rvartanov/design-system js/icons.js @ 42239ed (content unchanged since 5c5496ecd7e6, checked 2-11-2026).
// VibeSynergy's wrapper with translated labels is symbols.js; re-vendor instead of editing.
/* Semantic colour and symbol language (VibeSynergy). Lucide icons (ISC licence, see
   icons/lucide/LICENSE), inline SVG, no CDN. Every icon is aria-hidden: the text label next
   to it carries the meaning, and the colour is a third, extra signal (WCAG 1.4.1).
   The same shapes are in icons/lucide/<name>.svg for server-rendered apps. */

export const ICONS = {
  "info": "<circle cx=\"12\" cy=\"12\" r=\"10\"/><path d=\"M12 16v-4M12 8h.01\"/>",
  "lightbulb": "<path d=\"M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5M9 18h6M10 22h4\"/>",
  "circle-check": "<circle cx=\"12\" cy=\"12\" r=\"10\"/><path d=\"m16 9-5.5 5.5L8 12\"/>",
  "triangle-alert": "<path d=\"m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3M12 9v4M12 17h.01\"/>",
  "octagon-alert": "<path d=\"M12 16h.01M12 8v4M15.312 2a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586l-4.688-4.688A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2z\"/>",
  "calendar-clock": "<path d=\"M16 14v2.2l1.6 1M16 2v3M21 7.338V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h2.338M3 9h5.859M8 2v3\"/><circle cx=\"16\" cy=\"16\" r=\"6\"/>",
  "battery-charging": "<path d=\"m11 7-3 5h4l-3 5M14.856 6H16a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.935M22 14v-4M5.14 18H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h2.936\"/>",
  "flag": "<path d=\"M4 22V4a1 1 0 0 1 .4-.8A6 6 0 0 1 8 2c3 0 5 2 7.333 2q2 0 3.067-.8A1 1 0 0 1 20 4v10a1 1 0 0 1-.4.8A6 6 0 0 1 16 16c-3 0-5-2-8-2a6 6 0 0 0-4 1.528\"/>",
  "waves": "<path d=\"M2 12q2.5 2 5 0t5 0 5 0 5 0M2 19q2.5 2 5 0t5 0 5 0 5 0M2 5q2.5 2 5 0t5 0 5 0 5 0\"/>",
  "bike": "<circle cx=\"18.5\" cy=\"17.5\" r=\"3.5\"/><circle cx=\"5.5\" cy=\"17.5\" r=\"3.5\"/><circle cx=\"15\" cy=\"5\" r=\"1\"/><path d=\"M12 17.5V14l-3-3 4-3 2 3h2\"/>",
  "sport-shoe": "<path d=\"m15 10.42 4.8-5.07M19 18h3M9.5 22 21.414 9.415A2 2 0 0 0 21.2 6.4l-5.61-4.208A1 1 0 0 0 14 3v2a2 2 0 0 1-1.394 1.906L8.677 8.053A1 1 0 0 0 8 9c-.155 6.393-2.082 9-4 9a2 2 0 0 0 0 4h14\"/>",
  "dumbbell": "<path d=\"M17.596 12.768a2 2 0 1 0 2.829-2.829l-1.768-1.767a2 2 0 0 0 2.828-2.829l-2.828-2.828a2 2 0 0 0-2.829 2.828l-1.767-1.768a2 2 0 1 0-2.829 2.829zM2.5 21.5l1.4-1.4M20.1 3.9l1.4-1.4M5.343 21.485a2 2 0 1 0 2.829-2.828l1.767 1.768a2 2 0 1 0 2.829-2.829l-6.364-6.364a2 2 0 1 0-2.829 2.829l1.768 1.767a2 2 0 0 0-2.828 2.829zM9.6 14.4l4.8-4.8\"/>",
  "person-standing": "<circle cx=\"12\" cy=\"5\" r=\"1\"/><path d=\"m9 20 3-6 3 6M6 8l6 2 6-2M12 10v4\"/>",
  "repeat": "<path d=\"m17 2 4 4-4 4\"/><path d=\"M3 11v-1a4 4 0 0 1 4-4h14M7 22l-4-4 4-4\"/><path d=\"M21 13v1a4 4 0 0 1-4 4H3\"/>",
  "footprints": "<path d=\"M4 16v-2.38C4 11.5 2.97 10.5 3 8c.03-2.72 1.49-6 4.5-6C9.37 2 10 3.8 10 5.5c0 3.11-2 5.66-2 8.68V16a2 2 0 1 1-4 0M20 20v-2.38c0-2.12 1.03-3.12 1-5.62-.03-2.72-1.49-6-4.5-6C14.63 6 14 7.8 14 9.5c0 3.11 2 5.66 2 8.68V20a2 2 0 1 0 4 0M16 17h4M4 13h4\"/>",
  "activity": "<path d=\"M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2\"/>",
};

/** Message types: class suffix → Lucide icon and i18n key. */
export const MESSAGE_TYPES = {
  info: { icon: "info", label: "msg.info" },
  advice: { icon: "lightbulb", label: "msg.advice" },
  success: { icon: "circle-check", label: "msg.success" },
  warning: { icon: "triangle-alert", label: "msg.warning" },
  danger: { icon: "octagon-alert", label: "msg.danger" },
  planned: { icon: "calendar-clock", label: "msg.planned" },
  recovery: { icon: "battery-charging", label: "msg.recovery" },
  race: { icon: "flag", label: "msg.race" },
};

/** Sport identities: class suffix → Lucide icon and i18n key. */
export const SPORTS = {
  swim: { icon: "waves", label: "sport.swim" },
  bike: { icon: "bike", label: "sport.bike" },
  run: { icon: "sport-shoe", label: "sport.run" },
  strength: { icon: "dumbbell", label: "sport.strength" },
  mobility: { icon: "person-standing", label: "sport.mobility" },
  brick: { icon: "repeat", label: "sport.brick" },
  walk: { icon: "footprints", label: "sport.walk" },
  other: { icon: "activity", label: "sport.other" },
};

const SPORT_ALIASES = {
  swim: ["swim", "swimming", "poolswim", "lapswimming", "openwaterswim", "openwaterswimming"],
  bike: ["bike", "ride", "cycling", "virtualride", "virtualcycling", "indoorcycling", "roadbiking", "roadcycling",
    "mountainbiking", "mountainbikeride", "gravelride", "gravelcycling", "ebikeride", "ebikemountainbikeride"],
  run: ["run", "running", "trailrun", "trailrunning", "virtualrun", "treadmillrunning", "indoorrunning", "trackrunning"],
  strength: ["strength", "strengthtraining", "weighttraining", "crossfit"],
  mobility: ["mobility", "pilates", "yoga", "stretching", "flexibility", "breathwork"],
  brick: ["brick", "multisport", "triathlon", "duathlon", "transition"],
  walk: ["walk", "walking", "hike", "hiking"],
};
const SPORT_BY_ALIAS = new Map(Object.entries(SPORT_ALIASES).flatMap(([sport, names]) => names.map((n) => [n, sport])));

/** Garmin, Strava or intervals.icu sport type ("TrailRun", "virtual_ride") → a SPORTS key. */
export function sportOf(type) {
  const key = String(type ?? "").toLowerCase().replace(/[^a-z]/g, "");
  return SPORT_BY_ALIAS.get(key) ?? "other";
}

/** Inline SVG markup for a Lucide icon. Decorative by design: always aria-hidden. */
export function iconSvg(name, { size = 20, className = "ds-i" } = {}) {
  const body = ICONS[name];
  if (!body) throw new Error(`unknown icon: ${name}`);
  return `<svg class="${className}" xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/** Status chip: icon + label. `t` is the catalog lookup (createI18n().t); `text` overrides the label. */
export function statusChipHtml(type, t, text) {
  const def = MESSAGE_TYPES[type];
  if (!def) throw new Error(`unknown message type: ${type}`);
  return `<span class="ds-status ds-status--${type}">${iconSvg(def.icon, { size: 16 })}${esc(text ?? t(def.label))}</span>`;
}

/** Sport chip: icon + sport name. Accepts a SPORTS key or a source type ("TrailRun"). */
export function sportChipHtml(type, t, text) {
  const sport = SPORTS[type] ? type : sportOf(type);
  const def = SPORTS[sport];
  return `<span class="ds-sport ds-sport--${sport}">${iconSvg(def.icon, { size: 16 })}${esc(text ?? t(def.label))}</span>`;
}
