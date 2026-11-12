// The colour and symbol language: one icon and one colour family per message type and
// per sport, from the design system (ds_icons.js and css/semantic.css, both vendored). The
// colour is never the only signal: an icon (aria-hidden) always sits next to a word.
// Sport colours are not zone colours; zones keep their Z1…Zn swatches.
import { MESSAGE_TYPES, SPORTS, iconSvg, sportOf } from "./ds_icons.js";
import { esc } from "./format.js";
import { N, t } from "./i18n.js";

// The design system's catalog keys (msg.*, sport.*), listed so the i18n test checks them.
export const LABELS = [
  N("msg.info"), N("msg.advice"), N("msg.success"), N("msg.warning"), N("msg.danger"),
  N("msg.planned"), N("msg.recovery"), N("msg.race"),
  N("sport.swim"), N("sport.bike"), N("sport.run"), N("sport.strength"), N("sport.mobility"),
  N("sport.brick"), N("sport.walk"), N("sport.other"),
];

// Garmin, Strava, intervals.icu or week-plan sport ("lap_swimming", "ride") → a sport key.
export function sportKey(type) {
  return sportOf(type);
}

// The short sport name ("Swim" / "Zwemmen") for a sport key or a source type.
export function sportLabel(type) {
  return t(SPORTS[SPORTS[type] ? type : sportKey(type)].label);
}

// The sport's icon alone. Decorative: the caller writes the sport name next to it.
export function sportIcon(type, size = 16) {
  return iconSvg(SPORTS[SPORTS[type] ? type : sportKey(type)].icon, { size });
}

// Sport chip: icon + name. `text` (already translated) replaces the short name, for example
// "Trail running"; with `hideName` the name is only for screen readers (dense grids).
export function sportChip(type, text, { hideName = false } = {}) {
  const key = SPORTS[type] ? type : sportKey(type);
  const name = esc(text || sportLabel(key));
  // Icon only: the name is still there for screen readers, and as a tooltip.
  if (hideName) return `<span class="ds-sport ds-sport--${key} ds-sport--icon" title="${name}">${sportIcon(key)}<span class="visually-hidden">${name}</span></span>`;
  return `<span class="ds-sport ds-sport--${key}">${sportIcon(key)}${name}</span>`;
}

// The sport's icon in its colour before a text that names the sport or the workout
// (`html` is already escaped). In a list the line still reads without the icon.
export function withSportIcon(type, html) {
  const key = SPORTS[type] ? type : sportKey(type);
  return `<span data-sport="${key}"><span class="sport-icon">${sportIcon(key)}</span>${html}</span>`;
}

// Status chips and notices are .chip / .notice with a tone class (success, warning, danger,
// info, planned, race, recovery, advice); design.css gives each tone its colour and icon.

// The icon of a message type (info, advice, success, warning, danger, planned, recovery, race).
export function messageIcon(type, size = 20) {
  return iconSvg((MESSAGE_TYPES[type] || MESSAGE_TYPES.info).icon, { size });
}


// Diary tags as tone chips: what a tag means for training, never colour alone (the
// chip has the tone's icon and the tag's name). Unknown tags are plain chips.
const TAG_TONES = {
  sick: "danger",
  injury: "warning",
  pain: "warning",
  bad_sleep: "recovery",
  stress: "recovery",
  alcohol: "recovery",
  heat: "warning",
  travel: "planned",
  vacation: "planned",
  work: "info",
  race: "race",
};

export function tagChip(tag, name) {
  const tone = TAG_TONES[tag] || "";
  return `<span class="chip${tone ? ` ${tone}` : ""}">${esc(name)}</span>`;
}

// Evidence grades (knowledge base) as tone chips: A strong … D weak.
const GRADE_TONES = { A: "success", B: "info", C: "advice", D: "warning" };

export function gradeChip(code, label) {
  if (!label) return "";
  return `<span class="chip ${GRADE_TONES[code] || "info"}">${esc(label)}</span>`;
}
