// Shared period picker: presets, custom range, URL encoding and resolvePeriod (mirrors vibesynergy.query.periods).
import { addDays, esc, shortDate, userDate } from "./format.js";
import { N, t } from "./i18n.js";

const DAY_MS = 86400000;
export const EARLIEST = "2000-01-01";

/** @typedef {{ period?: string, start?: string, end?: string }} PeriodValue */

const BACKWARD_PRESET_ROWS = [
  ["last_7d", N("7 days")],
  ["week", N("This week")],
  ["last_14d", N("2 weeks")],
  ["last_1m", N("Month")],
  ["last_3m", N("3 months")],
  ["last_6m", N("6 months")],
  ["ytd", N("Year to date")],
  ["year", N("Year")],
  ["custom", N("Custom range")],
];

const FORWARD_PRESET_ROWS = [
  ["next_7d", N("7 days")],
  ["week", N("This week")],
  ["next_14d", N("2 weeks")],
  ["next_1m", N("Month")],
  ["next_3m", N("3 months")],
  ["next_6m", N("6 months")],
  ["ytd", N("Year to date")],
  ["year", N("Year")],
  ["custom", N("Custom range")],
];

export const BASE_PRESETS = BACKWARD_PRESET_ROWS;

const LEGACY_NAMED = new Set([
  "today",
  "yesterday",
  "last_28d",
  "last_90d",
  "last_365d",
  "this_week",
  "last_week",
  "this_month",
  "last_month",
  "this_year",
  "all",
]);

const DISPLAY_PRESET_ALIASES = { this_year: "ytd" };

export function isoToday(d = new Date()) {
  return new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())).toISOString().slice(0, 10);
}

function parseIso(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

function formatIso(date) {
  return date.toISOString().slice(0, 10);
}

function subtractMonths(dayIso, months) {
  const d = parseIso(dayIso);
  let month = d.getUTCMonth() + 1 - months;
  let year = d.getUTCFullYear();
  while (month <= 0) {
    month += 12;
    year -= 1;
  }
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return formatIso(new Date(Date.UTC(year, month - 1, Math.min(d.getUTCDate(), last))));
}

function addMonths(dayIso, months) {
  const d = parseIso(dayIso);
  let month = d.getUTCMonth() + 1 + months;
  let year = d.getUTCFullYear();
  while (month > 12) {
    month -= 12;
    year += 1;
  }
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return formatIso(new Date(Date.UTC(year, month - 1, Math.min(d.getUTCDate(), last))));
}

function mondayOf(iso) {
  const d = parseIso(iso);
  const row = (d.getUTCDay() + 6) % 7;
  return formatIso(new Date(d.getTime() - row * DAY_MS));
}

function presetRows(extra = [], direction = "backward") {
  const base = direction === "forward" ? FORWARD_PRESET_ROWS : BACKWARD_PRESET_ROWS;
  return [...base.slice(0, -2), ...extra, ...base.slice(-2)];
}

function presetIds(extra = [], direction = "backward") {
  return new Set(presetRows(extra, direction).map(([id]) => id));
}

export function isNamedPeriod(period) {
  if (!period) return false;
  const p = period.toLowerCase();
  if (LEGACY_NAMED.has(p) || /^last_\d+d$/.test(p) || /^last_\d+m$/.test(p)) return true;
  if (/^next_\d+d$/.test(p) || /^next_\d+m$/.test(p)) return true;
  if (p === "week" || p === "ytd" || p === "year" || p === "custom") return true;
  if (/^\d{4}$/.test(p)) return true;
  if (/^\d{4}-\d{2}$/.test(p)) return true;
  if (/^\d{4}-w\d{1,2}$/.test(p)) return true;
  return false;
}

/**
 * @param {PeriodValue} value
 * @param {string} today ISO date
 */
export function resolvePeriod(value, today = isoToday()) {
  const v = value || {};
  if (v.start || v.end) {
    const start = v.start || EARLIEST;
    const end = v.end || today;
    return { start, end };
  }
  const p = (v.period || "last_7d").toLowerCase();
  const mon = mondayOf(today);
  if (p === "today") return { start: today, end: today };
  if (p === "yesterday") return { start: addDays(today, -1), end: addDays(today, -1) };
  const lastD = p.match(/^last_(\d+)d$/);
  if (lastD) return { start: addDays(today, 1 - Number(lastD[1])), end: today };
  const nextD = p.match(/^next_(\d+)d$/);
  if (nextD) return { start: today, end: addDays(today, Number(nextD[1]) - 1) };
  if (p === "week") return { start: mon, end: addDays(mon, 6) };
  if (p === "this_week") return { start: mon, end: today };
  if (p === "last_week") return { start: addDays(mon, -7), end: addDays(mon, -1) };
  const lastM = p.match(/^last_(\d+)m$/);
  if (lastM) return { start: addDays(subtractMonths(today, Number(lastM[1])), 1), end: today };
  const nextM = p.match(/^next_(\d+)m$/);
  if (nextM) return { start: today, end: addDays(addMonths(today, Number(nextM[1])), -1) };
  if (p === "this_month") return { start: `${today.slice(0, 7)}-01`, end: today };
  if (p === "last_month") {
    const first = parseIso(`${today.slice(0, 7)}-01`);
    const last = new Date(first.getTime() - DAY_MS);
    const start = formatIso(new Date(Date.UTC(last.getUTCFullYear(), last.getUTCMonth(), 1)));
    return { start, end: formatIso(last) };
  }
  if (p === "this_year" || p === "ytd") return { start: `${today.slice(0, 4)}-01-01`, end: today };
  if (p === "all") return { start: EARLIEST, end: today };
  if (/^\d{4}$/.test(p)) return { start: `${p}-01-01`, end: `${p}-12-31` };
  if (/^\d{4}-\d{2}$/.test(p)) {
    const start = `${p}-01`;
    const d = parseIso(start);
    const end = formatIso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)));
    return { start, end };
  }
  const w = p.match(/^(\d{4})-w(\d{1,2})$/);
  if (w) {
    const start = isoWeekStart(Number(w[1]), Number(w[2]));
    return { start, end: addDays(start, 6) };
  }
  if (/^\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?$/.test(p)) {
    const [start, rest] = p.split("..");
    return { start, end: rest || start };
  }
  throw new Error(`unknown period ${p}`);
}

function isoWeekStart(year, week) {
  const jan4 = new Date(Date.UTC(year, 0, 4));
  const mon = new Date(jan4.getTime() - ((jan4.getUTCDay() + 6) % 7) * DAY_MS);
  return formatIso(new Date(mon.getTime() + (week - 1) * 7 * DAY_MS));
}

export function formatPeriodRange(start, end) {
  const a = userDate(start);
  const b = userDate(end);
  return a === b ? b : `${a} – ${b}`;
}

/** Years for the year preset (newest first), always including the selected year. */
export function defaultYears(today, { firstYear, selected } = {}) {
  const end = Number(today.slice(0, 4));
  const start = firstYear ?? Math.max(2000, end - 9);
  const set = new Set();
  for (let y = end; y >= start; y -= 1) set.add(y);
  if (selected != null && /^\d{4}$/.test(String(selected))) set.add(Number(selected));
  return [...set].sort((a, b) => b - a);
}

/** @type {{ id: string, field: string } | null} */
let pendingPeriodPickerFocus = null;

/** Remember which control to focus after a hash-route re-render. */
export function requestPeriodPickerFocus(id, field = "preset") {
  pendingPeriodPickerFocus = { id, field };
}

function restorePeriodPickerFocus(root, id) {
  if (!pendingPeriodPickerFocus || pendingPeriodPickerFocus.id !== id) return;
  const field = pendingPeriodPickerFocus.field;
  pendingPeriodPickerFocus = null;
  root
    .querySelector(`[data-period-picker="${id}"]`)
    ?.querySelector(`[name="${id}-${field}"]`)
    ?.focus();
}

/** Value returned from bindPeriodPicker (exported for tests). */
export function computePickerValue(current, customInputs, opts, today = isoToday()) {
  let v = { ...(current || {}) };
  const extra = opts.extra || [];
  const direction = opts.direction || "backward";
  let state = displayState(v, today, extra, direction);
  if (state.mode === "custom" && customInputs) {
    v = { start: customInputs.start, end: customInputs.end };
    state = displayState(v, today, extra, direction);
  }
  const resolved = resolvePeriod(
    v.start || v.end ? v : valueForResolve(state, today),
    today,
  );
  if (opts.max && resolved.end > opts.max) {
    return { start: resolved.start, end: opts.max };
  }
  if (state.mode === "custom" && customInputs) return v;
  return { ...(current || {}) };
}

/** Normalize heatmap storage so "current year" does not freeze as a literal year. */
export function heatmapStorageValue(value, today = isoToday()) {
  const period = value?.period || "";
  if (/^\d{4}$/.test(period) && period === today.slice(0, 4)) return { period: "year" };
  return value;
}

function urlKeys(opts = {}) {
  if (opts.heatmap) {
    return { period: "period", from: "from", to: "to", year: "year", start: null, end: null };
  }
  const p = opts.param || {};
  return {
    period: p.period ?? "period",
    start: p.start ?? "start",
    end: p.end ?? "end",
    from: null,
    to: null,
    year: null,
    legacyStart: p.legacyStart ?? null,
    legacyEnd: p.legacyEnd ?? null,
  };
}

function normalizePeriodToken(period) {
  if (!period) return period;
  const p = period.toLowerCase();
  return DISPLAY_PRESET_ALIASES[p] || p;
}

/**
 * UI mode for the picker: preset id, optional year, optional custom dates.
 */
export function displayState(value, today, extra = [], direction = "backward") {
  const ids = presetIds(extra, direction);
  const v = value || {};
  if (v.start && v.end) return { mode: "custom", start: v.start, end: v.end, year: today.slice(0, 4) };
  let period = normalizePeriodToken(v.period || "");
  if (period === "custom") {
    const start = v.start !== undefined ? v.start : today;
    const end = v.end !== undefined ? v.end : today;
    return { mode: "custom", start, end, year: today.slice(0, 4) };
  }
  if (/^\d{4}$/.test(period)) return { mode: "year", year: period, period: "year" };
  if (ids.has(period) && period !== "custom") {
    if (period === "year") return { mode: "year", year: today.slice(0, 4), period: "year" };
    return { mode: period, period };
  }
  if (period && isNamedPeriod(period)) {
    const { start, end } = resolvePeriod({ period }, today);
    return { mode: "custom", start, end, period, legacy: true };
  }
  return { mode: "custom", start: v.start || today, end: v.end || today, year: today.slice(0, 4) };
}

export function periodFromQuery(query, { defaultValue, today = isoToday(), ...opts } = {}) {
  const keys = urlKeys(opts);
  const period = query.get(keys.period);
  const start = query.get(keys.start);
  const end = query.get(keys.end);
  const from = keys.from ? query.get(keys.from) : null;
  const to = keys.to ? query.get(keys.to) : null;
  const year = keys.year ? query.get(keys.year) : null;
  const legStart = keys.legacyStart ? query.get(keys.legacyStart) : null;
  const legEnd = keys.legacyEnd ? query.get(keys.legacyEnd) : null;
  if (opts.heatmap) {
    if (from || to) {
      if (!from || !to) return defaultValue;
      return { start: from, end: to };
    }
    if (period) {
      if (period.toLowerCase() === "custom") return defaultValue;
      return { period };
    }
    if (year) return { period: year };
    return defaultValue;
  }
  const s = start || legStart;
  const e = end || legEnd;
  if (s || e) {
    const startVal = s || EARLIEST;
    let endVal = e;
    if (!endVal) {
      endVal = opts.direction === "forward" && s ? addDays(s, 13) : today;
    }
    return { start: startVal, end: endVal };
  }
  if (period) {
    if (period.toLowerCase() === "custom") return defaultValue;
    return { period };
  }
  return defaultValue;
}

export function writePeriodQuery(params, value, { defaultValue, today = isoToday(), omitDefault = true, ...opts } = {}) {
  const v = value || {};
  const def = defaultValue || {};
  const keys = urlKeys(opts);
  if (opts.heatmap) {
    params.delete("year");
    params.delete(keys.period);
    params.delete(keys.from);
    params.delete(keys.to);
    if (v.start && v.end) {
      params.set(keys.from, v.start);
      params.set(keys.to, v.end);
      return params;
    }
    const period = v.period || "";
    if (/^\d{4}$/.test(period)) {
      if (!omitDefault || period !== String(def.period)) params.set(keys.year, period);
      return params;
    }
    if (period && (!omitDefault || period !== def.period)) params.set(keys.period, period);
    return params;
  }
  if (keys.legacyStart) params.delete(keys.legacyStart);
  if (keys.legacyEnd) params.delete(keys.legacyEnd);
  params.delete(keys.period);
  params.delete(keys.start);
  params.delete(keys.end);
  if (v.start && v.end) {
    if (!omitDefault || v.start !== def.start || v.end !== def.end) {
      params.set(keys.start, v.start);
      params.set(keys.end, v.end);
    }
    return params;
  }
  const period = v.period || "";
  if (period && (!omitDefault || period !== def.period)) params.set(keys.period, period);
  return params;
}

export function apiParams(value, today = isoToday()) {
  const v = value || {};
  if (v.start || v.end) {
    const { start, end } = resolvePeriod(v, today);
    return { start, end };
  }
  const period = v.period || "";
  if (/^\d{4}$/.test(period)) return { period };
  return { period };
}

export function loadStored(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function saveStored(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // private mode
  }
}

export function queryHasPeriodKeys(query, opts = {}) {
  const keys = urlKeys(opts);
  if (opts.heatmap) {
    return (
      query.has(keys.period) ||
      query.has(keys.from) ||
      query.has(keys.to) ||
      query.has(keys.year)
    );
  }
  if (query.has(keys.period) || query.has(keys.start) || query.has(keys.end)) return true;
  if (keys.legacyStart && query.has(keys.legacyStart)) return true;
  if (keys.legacyEnd && query.has(keys.legacyEnd)) return true;
  return false;
}

export function mergePeriodValue(query, storageKey, defaultValue, opts = {}) {
  const today = opts.today || isoToday();
  const fromUrl = periodFromQuery(query, { defaultValue, today, ...opts });
  if (queryHasPeriodKeys(query, opts)) return fromUrl;
  const stored = loadStored(storageKey);
  if (stored) return stored;
  return defaultValue;
}

export function validateCustomRange(start, end, { min, max, maxDays } = {}) {
  if (!start || !end) return t("Choose a start and end date.");
  if (Number.isNaN(parseIso(start).getTime()) || Number.isNaN(parseIso(end).getTime())) {
    return t("Use a valid date (YYYY-MM-DD).");
  }
  if (start > end) return t("Start must be on or before end.");
  if (min && start < min) return t("Start cannot be before {date}.", { date: shortDate(min) });
  if (max && end > max) return t("End cannot be after {date}.", { date: shortDate(max) });
  if (maxDays) {
    const days = Math.round((parseIso(end) - parseIso(start)) / DAY_MS) + 1;
    if (days > maxDays) return t("Choose at most {n} days.", { n: maxDays });
  }
  return "";
}

function valueForResolve(state, today) {
  if (state.mode === "custom") return { start: state.start, end: state.end };
  if (state.mode === "year") return { period: String(state.year) };
  return { period: state.period || state.mode };
}

function periodPickerMarkup(opts) {
  const today = opts.today || isoToday();
  const direction = opts.direction || "backward";
  const extra = opts.extra || [];
  const presets = presetRows(extra, direction);
  const years = opts.years?.length
    ? [...opts.years]
    : defaultYears(today, { firstYear: opts.firstYear, selected: opts.value?.period });
  const state = displayState(opts.value, today, extra, direction);
  if (state.mode === "year" && state.year) {
    const y = Number(state.year);
    if (!years.includes(y)) years.push(y);
    years.sort((a, b) => b - a);
  }
  const resolved = resolvePeriod(valueForResolve(state, today), today);
  const rangeText = formatPeriodRange(resolved.start, resolved.end);
  const options = presets
    .map(([id, label]) => {
      const selected =
        (id === "year" && state.mode === "year") ||
        (id === "custom" && state.mode === "custom") ||
        (id !== "year" && id !== "custom" && state.mode === id);
      return `<option value="${esc(id)}"${selected ? " selected" : ""}>${esc(t(label))}</option>`;
    })
    .join("");
  const presetLabel = opts.accessibleName ? "" : `${t("Period")} `;
  const yearSelect =
    state.mode === "year"
      ? `<label class="period-picker-year">${t("Year")}
      <select class="btn" name="${esc(opts.id)}-year">${years
        .map((y) => `<option value="${y}"${String(y) === String(state.year) ? " selected" : ""}>${y}</option>`)
        .join("")}</select></label>`
      : "";
  const custom =
    state.mode === "custom"
      ? `<div class="period-picker-custom">
      <label>${t("From")} <input class="btn" type="date" name="${esc(opts.id)}-start" value="${esc(state.start)}"${opts.min ? ` min="${esc(opts.min)}"` : ""}${opts.max ? ` max="${esc(opts.max)}"` : ""} aria-describedby="${esc(opts.id)}-error"></label>
      <label>${t("To")} <input class="btn" type="date" name="${esc(opts.id)}-end" value="${esc(state.end)}"${opts.min ? ` min="${esc(opts.min)}"` : ""}${opts.max ? ` max="${esc(opts.max)}"` : ""} aria-describedby="${esc(opts.id)}-error"></label>
      ${opts.applyButton === false ? "" : `<button class="btn" type="button" data-apply="${esc(opts.id)}">${t("Apply")}</button>`}
      <p class="period-picker-error" id="${esc(opts.id)}-error" role="alert" hidden></p>
    </div>`
      : "";
  const legend = opts.accessibleName ? `<legend>${esc(t(opts.accessibleName))}</legend>` : "";
  const presetAria = opts.accessibleName ? ` aria-label="${esc(t(opts.accessibleName))}"` : "";
  return `${legend}
    <label>${presetLabel}<select class="btn" name="${esc(opts.id)}-preset"${presetAria} aria-describedby="${esc(opts.id)}-range">${options}</select></label>
    ${yearSelect}
    ${custom}
    <p class="period-picker-range sub" id="${esc(opts.id)}-range">${esc(rangeText)}</p>`;
}

/**
 * @param {{ id: string, value: PeriodValue, years?: number[], firstYear?: number, extra?: [string, string][], direction?: string, accessibleName?: string, maxDays?: number, min?: string, max?: string, today?: string }} opts
 */
export function periodPickerHtml(opts) {
  const inner = periodPickerMarkup(opts);
  const tag = opts.accessibleName ? "fieldset" : "div";
  return `<${tag} class="period-picker" data-period-picker="${esc(opts.id)}">${inner}</${tag}>`;
}

function showValidation(box, id, message, startInput, endInput, rangeOpts = {}) {
  const err = box.querySelector(`#${CSS.escape(id)}-error`);
  startInput?.removeAttribute("aria-invalid");
  endInput?.removeAttribute("aria-invalid");
  if (message) {
    if (err) {
      err.hidden = false;
      err.textContent = message;
    }
    const start = startInput?.value || "";
    const end = endInput?.value || "";
    let bad = startInput;
    if (!start || !end) bad = !start ? startInput : endInput;
    else if (start > end) bad = startInput;
    else if (rangeOpts.min && start < rangeOpts.min) bad = startInput;
    else if (rangeOpts.max && end > rangeOpts.max) bad = endInput;
    else if (rangeOpts.maxDays) {
      const days = Math.round((parseIso(end) - parseIso(start)) / DAY_MS) + 1;
      if (days > rangeOpts.maxDays) bad = endInput;
    }
    bad?.setAttribute("aria-invalid", "true");
    bad?.focus();
    return false;
  }
  if (err) err.hidden = true;
  return true;
}

/**
 * @param {HTMLElement} root
 * @param {{ id: string, value: PeriodValue, years?: number[], firstYear?: number, extra?: [string, string][], direction?: string, accessibleName?: string, maxDays?: number, min?: string, max?: string, today?: string, onChange: (v: PeriodValue) => void }} opts
 * @returns {{ value: () => PeriodValue, validate: () => string }}
 */
export function bindPeriodPicker(root, opts) {
  const today = opts.today || isoToday();
  let current = { ...(opts.value || {}) };
  const box = () => root.querySelector(`[data-period-picker="${opts.id}"]`);

  const refreshRange = (value) => {
    const rangeEl = box()?.querySelector(`#${opts.id}-range`);
    try {
      const { start, end } = resolvePeriod(value, today);
      if (rangeEl) rangeEl.textContent = formatPeriodRange(start, end);
    } catch {
      if (rangeEl) rangeEl.textContent = "";
    }
  };

  const commit = (value) => {
    current = { ...value };
    refreshRange(current);
    opts.onChange(current);
  };

  const readCustom = (el) => ({
    start: el.querySelector(`[name="${opts.id}-start"]`)?.value || "",
    end: el.querySelector(`[name="${opts.id}-end"]`)?.value || "",
  });

  const applyCustom = (el) => {
    const { start, end } = readCustom(el);
    const message = validateCustomRange(start, end, opts);
    const startInput = el.querySelector(`[name="${opts.id}-start"]`);
    const endInput = el.querySelector(`[name="${opts.id}-end"]`);
    if (!showValidation(el, opts.id, message, startInput, endInput, opts)) return;
    commit({ start, end });
  };

  const rerender = (focusPreset = true) => {
    const el = box();
    if (!el) return;
    el.innerHTML = periodPickerMarkup({ ...opts, value: current });
    wire(el);
    refreshRange(current);
    if (focusPreset) el.querySelector(`[name="${opts.id}-preset"]`)?.focus();
  };

  const wire = (el) => {
    const preset = el.querySelector(`[name="${opts.id}-preset"]`);
    preset?.addEventListener("change", () => {
      const id = preset.value;
      if (id === "custom") {
        const cur = resolvePeriod(current, today);
        current = { start: cur.start, end: cur.end };
        rerender(true);
        return;
      }
      if (id === "year") {
        const y = /^\d{4}$/.test(current.period || "") ? current.period : today.slice(0, 4);
        requestPeriodPickerFocus(opts.id, "preset");
        current = { period: y };
        rerender(true);
        commit({ period: y });
        return;
      }
      requestPeriodPickerFocus(opts.id, "preset");
      current = { period: id };
      rerender(true);
      commit({ period: id });
    });

    el.querySelector(`[name="${opts.id}-year"]`)?.addEventListener("change", (event) => {
      current = { period: event.target.value };
      rerender(false);
      commit({ period: event.target.value });
    });

    el.querySelector(`[data-apply="${opts.id}"]`)?.addEventListener("click", () => applyCustom(el));

    for (const input of el.querySelectorAll(`[name="${opts.id}-start"], [name="${opts.id}-end"]`)) {
      input.addEventListener("change", () => {
        const { start, end } = readCustom(el);
        current = { start, end };
        refreshRange(current);
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          applyCustom(el);
        }
      });
    }
  };

  const el = box();
  if (el) wire(el);
  refreshRange(current);
  restorePeriodPickerFocus(root, opts.id);

  return {
    value: () => {
      const pickerEl = box();
      const customInputs = pickerEl ? readCustom(pickerEl) : null;
      return computePickerValue(current, customInputs, opts, today);
    },
    validate: () => {
      const state = displayState(current, today, opts.extra || [], opts.direction || "backward");
      if (state.mode !== "custom") return "";
      const el = box();
      if (!el) return "";
      const { start, end } = readCustom(el);
      const message = validateCustomRange(start, end, opts);
      const startInput = el.querySelector(`[name="${opts.id}-start"]`);
      const endInput = el.querySelector(`[name="${opts.id}-end"]`);
      showValidation(el, opts.id, message, startInput, endInput, opts);
      if (!message) current = { start, end };
      return message;
    },
  };
}
