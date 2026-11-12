// RPE and Feel for one completed activity, on the start page and the activity page.
// RPE 1-10 and Feel 1 (Strong) to 5 (Weak). Garmin's value shows until you choose your own,
// which saves at once and wins; choosing the empty option brings Garmin's back.
import { post } from "./api.js";
import { esc, sourceName } from "./format.js";
import { N, t } from "./i18n.js";

const RPE = [
  N("Nothing at all"),
  N("Very easy"),
  N("Easy"),
  N("Comfortable"),
  N("Slightly challenging"),
  N("Difficult"),
  N("Hard"),
  N("Very hard"),
  N("Extremely hard"),
  N("Max effort"),
];
const FEEL = [N("Strong"), N("Good"), N("Normal"), N("Poor"), N("Weak")];
const FIELDS = [
  ["rpe", N("Effort (RPE)"), RPE],
  ["feel", N("Feel"), FEEL],
];

function label(field, value) {
  const names = field === "rpe" ? RPE : FEEL;
  return t(names[value - 1] || "");
}

// The word for an RPE (1-10) or Feel (1-5) value, in the UI language.
export function effortLabel(field, value) {
  return label(field, value);
}

function sourceText(source) {
  return source === "vibesynergy" ? "VibeSynergy" : sourceName(source);
}

function fieldHtml(key, field, name, names, entry) {
  const id = `effort-${key}-${field}`;
  if (entry.value != null && !entry.editable) {
    return `<div class="effort-field"><span class="effort-name">${esc(t(name))}</span>
      <span title="${esc(sourceText(entry.source))}"><b>${esc(String(entry.value))}</b> · ${esc(label(field, entry.value))}</span></div>`;
  }
  const own = entry.source === "vibesynergy" ? entry.value : null;
  const garmin = entry.source === "garmin" ? entry.value : (entry.others || []).find((o) => o.source === "garmin")?.value;
  const options = names
    .map((text, index) => {
      const value = index + 1;
      return `<option value="${value}"${own === value ? " selected" : ""}>${value} · ${esc(t(text))}</option>`;
    })
    .join("");
  const empty = garmin != null ? t("Garmin: {value} · {label}", { value: garmin, label: label(field, garmin) }) : t("Not filled in");
  return `<div class="effort-field"><label class="effort-name" for="${esc(id)}">${esc(t(name))}</label>
    <select class="btn" id="${esc(id)}" data-effort-field="${field}">
      <option value=""${own == null ? " selected" : ""}>${esc(empty)}</option>${options}
    </select></div>`;
}

// key: a unique, id-safe suffix for this activity on the page.
export function effortHtml(effort, key, { readOnly = false } = {}) {
  if (!effort) return "";
  const fields = FIELDS.map(([field, name, names]) => {
    const entry = effort[field] || {};
    if (readOnly && entry.value == null) return "";
    if (readOnly) return fieldHtml(key, field, name, names, { ...entry, editable: false });
    return fieldHtml(key, field, name, names, entry);
  }).join("");
  if (!fields) return "";
  return `<div class="effort" role="group" aria-label="${esc(t("How it went"))}">${fields}
    <p class="sub" data-effort-status role="status" aria-live="polite"></p></div>`;
}

// host contains effortHtml(); id is the activity id ("garmin:123"). Each change saves at
// once. The select is not redrawn, so arrow keys on a closed select keep working, and
// saves run one after another so the last choice is the one that stays.
export function bindEffort(host, id) {
  let queue = Promise.resolve();
  for (const select of host.querySelectorAll("[data-effort-field]")) {
    select.addEventListener("change", () => {
      const field = select.dataset.effortField;
      const value = select.value === "" ? null : Number(select.value);
      queue = queue.then(async () => {
        const status = host.querySelector("[data-effort-status]");
        try {
          await post("activity-effort", { id, fields: [field], [field]: value });
          if (!host.isConnected || !status) return;
          status.classList.remove("error-text");
          status.textContent = t("Saved.");
        } catch (error) {
          if (!status) return;
          status.textContent = t("Not saved: {error}", { error: error.message });
          status.classList.add("error-text");
        }
      });
    });
  }
}
