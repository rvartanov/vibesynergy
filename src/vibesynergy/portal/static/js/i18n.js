// Translations. English text is the catalogue key; values take {name} placeholders.
// Catalogues live in static/i18n/{en,nl}.json (a test requires the same keys in both).
// The choice is kept on this device (localStorage, like the theme) and, when the portal
// can save preferences, also on the server so alerts match. auto follows the browser.
const STORAGE_KEY = "vibesynergy-language";
const COOKIE = "vibesynergy-language";
const LANGUAGES = ["en", "nl"];

let choice = "auto";
let lang = "en";
let catalogue = {};

export function language() {
  return lang;
}

export function languageChoice() {
  return choice;
}

// For numbers: Dutch "12,5"; English "12.5". User-facing dates always use Dutch numeric
// notation (d-m-yyyy, HH:mm); weekday and month names follow the UI language.
export function locale() {
  return lang === "nl" ? "nl-NL" : "en-GB";
}

export function browserLanguage() {
  return (navigator.language || "en").toLowerCase().startsWith("nl") ? "nl" : "en";
}

export function storedLanguage() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === "auto" || LANGUAGES.includes(value)) return value;
  } catch {
    // private window
  }
  return null;
}

export function persistLanguage(value) {
  const next = value === "auto" || LANGUAGES.includes(value) ? value : "auto";
  choice = next;
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // private window: the switch still applies until reload
  }
  try {
    document.cookie = `${COOKIE}=${next}; path=/; max-age=31536000; samesite=lax`;
  } catch {
    // cookie blocked: localStorage is enough for the portal
  }
  return next;
}

export function resolveLanguage(stored, prefsLanguage, browser = browserLanguage()) {
  const wanted = stored || prefsLanguage || "auto";
  if (LANGUAGES.includes(wanted)) return wanted;
  return browser === "nl" ? "nl" : "en";
}

export async function setLanguage(wanted) {
  choice = wanted === "auto" || LANGUAGES.includes(wanted) ? wanted : "auto";
  const resolved = resolveLanguage(choice, null, browserLanguage());
  catalogue = {};
  lang = "en";
  try {
    catalogue = await (await fetch(new URL(`../i18n/${resolved}.json`, import.meta.url))).json();
    lang = resolved;
  } catch {
    if (resolved === "en") {
      lang = "en";
    }
    // the catalogue didn't load: English keys
  }
  if (typeof document !== "undefined") document.documentElement.lang = lang;
}

export function t(text, vars) {
  let out = catalogue[text] || text;
  if (vars) out = out.replace(/\{(\w+)\}/g, (match, key) => (key in vars ? String(vars[key]) : match));
  return out;
}

// Marks a text in a constant table for translation where it's shown (with t()); the test
// finds it like t("…").
export function N(text) {
  return text;
}
