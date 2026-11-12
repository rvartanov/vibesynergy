// Display formatting. The API sends SI values (s, m, kg, m/s); pages show them here, in the
// chosen language's formats (i18n.js).
import { locale, t } from "./i18n.js";

const formats = new Map();
function nf(digits) {
  const key = `${locale()}:${digits}`;
  if (!formats.has(key)) {
    formats.set(key, new Intl.NumberFormat(locale(), { maximumFractionDigits: digits, minimumFractionDigits: digits }));
  }
  return formats.get(key);
}

export function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

export function number(v, digits = 0) {
  if (!isNum(v)) return "–";
  return nf(digits === 2 ? 2 : digits === 1 ? 1 : 0).format(v);
}

// 27000 -> "7 h 30 min"; 2700 -> "45 min"
export function hoursMinutes(seconds) {
  if (!isNum(seconds)) return "–";
  const total = Math.round(seconds / 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h ? t("{h} h {m} min", { h, m: String(m).padStart(2, "0") }) : t("{m} min", { m });
}

// 3723 -> "1:02:03"; 125 -> "2:05"
export function clock(seconds) {
  if (!isNum(seconds)) return "–";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

export function km(metres) {
  return isNum(metres) ? `${number(metres / 1000, metres < 100000 ? 2 : 1)} km` : "–";
}

const UNIT_LABEL = {
  per_h: "/h",
  degC: "°C",
  m_s: "m/s",
  kg_m2: "kg/m²",
  raw: "",
  bpm: "bpm",
  ms: "ms",
  kg: "kg",
  pct: "%",
  kcal: "kcal",
  mmHg: "mmHg",
  brpm: "brpm",
  min: "min",
  ml_kg_min: "ml/kg/min",
  count: "",
  score: "",
  load: "",
  score_1_5: "/5",
  score_0_10: "/10",
};

// A value with its unit, split so the page can style the unit: {text, unit}.
export function valueParts(value, unit) {
  if (!isNum(value)) return { text: "–", unit: "" };
  if (unit === "s") return { text: hoursMinutes(value), unit: "" };
  if (unit === "m") {
    return value >= 1000 ? { text: number(value / 1000, 1), unit: "km" } : { text: number(value), unit: "m" };
  }
  if (unit === "kg") return { text: number(value, 1), unit: "kg" };
  if (unit === "pct") return { text: number(value, 1), unit: "%" };
  // Small non-whole values keep one decimal (BMI 23.4, skin temperature -0.3 °C).
  const digits = !Number.isInteger(value) && Math.abs(value) < 100 ? 1 : 0;
  return { text: number(value, digits), unit: UNIT_LABEL[unit] ?? unit ?? "" };
}

export function value(v, unit) {
  const parts = valueParts(v, unit);
  return parts.unit ? `${parts.text} ${parts.unit}` : parts.text;
}

// Dates ---------------------------------------------------------------------------------
// User-facing: Dutch numeric d-m-yyyy and HH:mm (clock times have no seconds; durations,
// laps and splits keep them); weekday/month names follow UI language.
export const USER_TZ_DEFAULT = "Europe/Amsterdam";

// A missing or malformed day gives an invalid Date, never an exception: one absent date must
// not take a whole page down. The date texts below then show MISSING_DAY.
export const MISSING_DAY = "—";

export function parseDay(iso) {
  const [y, m, d] = String(iso ?? "").slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return new Date(Number.NaN);
  return new Date(Date.UTC(y, m - 1, d));
}

function validDay(iso) {
  return !Number.isNaN(parseDay(iso).getTime());
}

export function isoDay(date) {
  return date.toISOString().slice(0, 10);
}

export function addDays(iso, days) {
  const date = parseDay(iso);
  if (Number.isNaN(date.getTime())) return "";
  date.setUTCDate(date.getUTCDate() + days);
  return isoDay(date);
}

function instantParts(instant, timeZone) {
  const parts = new Intl.DateTimeFormat("nl-NL", {
    timeZone,
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(instant);
  const pick = (type) => parts.find((p) => p.type === type)?.value ?? "";
  return {
    day: Number(pick("day")),
    month: Number(pick("month")),
    year: Number(pick("year")),
    hour: pick("hour").padStart(2, "0"),
    minute: pick("minute").padStart(2, "0"),
  };
}

// "2026-09-25" -> "25-9-2026"
export function userDate(iso) {
  if (!iso) return "–";
  const day = String(iso).slice(0, 10);
  const [y, m, d] = day.split("-").map(Number);
  if (!y || !m || !d) return "–";
  return `${d}-${m}-${y}`;
}

export function userTime(isoTimestamp, timeZone = USER_TZ_DEFAULT) {
  if (!isoTimestamp) return "–";
  const p = instantParts(new Date(isoTimestamp), timeZone);
  return `${p.hour}:${p.minute}`;
}

export function userDateTime(isoTimestamp, timeZone = USER_TZ_DEFAULT) {
  if (!isoTimestamp) return "–";
  const p = instantParts(new Date(isoTimestamp), timeZone);
  return `${p.day}-${p.month}-${p.year} ${p.hour}:${p.minute}`;
}

// "2026-09-25" -> "Friday 25 September 2026" (EN) / "vrijdag 25 september 2026" (NL)
export function longDate(iso) {
  if (!validDay(iso)) return MISSING_DAY;
  const d = parseDay(iso);
  const weekdayName = d.toLocaleDateString(locale(), { weekday: "long", timeZone: "UTC" });
  const monthName = d.toLocaleDateString(locale(), { month: "long", timeZone: "UTC" });
  const [y, m, day] = String(iso).slice(0, 10).split("-").map(Number);
  return `${weekdayName} ${day} ${monthName} ${y}`;
}

export function monthName(iso, width = "long") {
  if (!validDay(iso)) return MISSING_DAY;
  return parseDay(iso).toLocaleDateString(locale(), { month: width, timeZone: "UTC" });
}

export function monthYear(iso, width = "short") {
  if (!validDay(iso)) return MISSING_DAY;
  return parseDay(iso).toLocaleDateString(locale(), { month: width, year: "numeric", timeZone: "UTC" });
}

// "2026-09-25" -> "25-9-2026"
export function shortDate(iso) {
  return userDate(iso);
}

export function weekday(iso) {
  if (!validDay(iso)) return MISSING_DAY;
  return parseDay(iso).toLocaleDateString(locale(), { weekday: "short", timeZone: "UTC" });
}

export function dayMonth(iso) {
  if (!validDay(iso)) return MISSING_DAY;
  const d = parseDay(iso);
  const monthName = d.toLocaleDateString(locale(), { month: "short", timeZone: "UTC" });
  const day = Number(String(iso).slice(8, 10));
  return `${day} ${monthName}`;
}

export function timeOfDay(isoTimestamp, timeZone = USER_TZ_DEFAULT) {
  return userTime(isoTimestamp, timeZone);
}

export function ago(isoTimestamp, now = Date.now()) {
  if (!isoTimestamp) return t("never");
  const minutes = Math.round((now - new Date(isoTimestamp).getTime()) / 60000);
  if (minutes < 1) return t("just now");
  if (minutes < 60) return t("{n} min ago", { n: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 48) return t("{n} h ago", { n: hours });
  return t("{n} days ago", { n: Math.round(hours / 24) });
}

export function sourceName(id) {
  const names = { caldav: t("iCal / CalDAV calendar"), garmin: "Garmin", withings: "Withings", strava: "Strava", files: t("Files"), garmin_export: t("Garmin export"), strava_export: t("Strava archive"), intervals_icu: "intervals.icu" };
  return names[id] ?? id;
}

export function sportName(sport) {
  if (!sport) return t("Activity");
  const text = sport.replaceAll("_", " ");
  const name = text.charAt(0).toUpperCase() + text.slice(1);
  // Sport names have their own keys ("sport: Run"): "Run" alone is also a button.
  const translated = t(`sport: ${name}`);
  return translated === `sport: ${name}` ? name : translated;
}

// A diary tag as read text ("bad_sleep" -> "bad sleep").
export function tagName(tag) {
  return t(tag.replaceAll("_", " "));
}

// Escape text for HTML templates.
export function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// A metric's label from the catalogue, or a readable form of its id
// ("sleep.apnea_hypopnea_index" -> "Apnea hypopnea index").
const ACRONYMS = new Set(["bmi", "pwv", "hr", "hrv", "spo2", "vo2max", "ecg", "afib"]);

export function metricLabel(id, catalogue) {
  if (catalogue?.[id]?.label && catalogue[id].label !== id) return t(catalogue[id].label);
  const words = (id.split(".").slice(1).join(" ").replaceAll("_", " ") || id).split(" ");
  const text = words.map((w) => (ACRONYMS.has(w) ? w.toUpperCase() : w)).join(" ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
