// Small form pieces shared by the Settings modules.
import { esc } from "./format.js";

export function select(name, options, current) {
  return `<select class="btn" name="${name}">${options
    .map(([v, label]) => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(label)}</option>`)
    .join("")}</select>`;
}

export function field(label, name, value, attrs = "") {
  return `<label>${esc(label)} <input class="btn" name="${name}" value="${esc(value ?? "")}" ${attrs}></label>`;
}

export function saveRow() {
  return `<p class="sub" data-status role="status" aria-live="polite"></p>`;
}
