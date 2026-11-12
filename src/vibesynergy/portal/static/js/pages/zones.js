// Training → Zones & thresholds: the values the plan uses per sport (FTP, LTHR, max HR,
// threshold pace, CSS), their zones, what Garmin and intervals.icu hold now, the history of
// each value and the coach's open proposals. A change saves at once; Garmin and
// intervals.icu only change when the athlete ticks them. The cards are reusable: the
// planning & progress page can mount sportCard() on its own.
import { fitnessHeadHtml } from "../fitness_tabs.js";
import { mountBests } from "../bests_block.js";
import { FITNESS_RANGES, formHtml, mountForm } from "../form_block.js";
import { mountPredictions } from "../race_predictions.js";
import { get, post } from "../api.js";
import { esc, number, userDate, userDateTime } from "../format.js";
import { N, t } from "../i18n.js";
import { isoToday } from "../period_picker.js";
import { sportIcon, sportKey, withSportIcon } from "../symbols.js";
import { athleteProfile } from "../athlete_profile.js";

const SPORT_NAMES = { bike: N("Cycling"), run: N("Running"), swim: N("Swimming") };
const METRIC_NAMES = {
  ftp: N("FTP"),
  threshold_power: N("Threshold power"),
  lthr: N("Threshold heart rate (LTHR)"),
  max_hr: N("Maximum heart rate"),
  threshold_pace: N("Threshold pace"),
  css: N("CSS (swim threshold pace)"),
  resting_hr_baseline: N("Resting heart rate"),
  hrv_baseline: N("HRV baseline"),
  hr_zones: N("Heart rate zones"),
};
const METHODS = [
  ["manual", N("Entered yourself")],
  ["test", N("Test")],
  ["race", N("Race")],
  ["field", N("Training")],
  ["device_auto", N("Measured by your watch")],
  ["estimate", N("Estimate")],
];
const METHOD_NAMES = Object.fromEntries(METHODS);
const PLATFORMS = { garmin: "Garmin", intervals: "intervals.icu" };
// What VibeSynergy cannot set on a platform (the page says so instead of offering it).
// Empty since Garmin takes the running threshold pace (profile lactate threshold).
const UNSUPPORTED = { garmin: {} };
const PUSH_ERRORS = {
  not_connected: N("not connected. Connect it in Settings → Sources."),
  refused: N("it refused the change. Try again later."),
  not_applied: N("it kept its own value. Change it there yourself."),
  nothing_to_send: N("there was nothing to set for this sport yet."),
  no_sport_settings: N("there are no settings for this sport there yet."),
};

export async function render(view, { args = [], query, meta, stale }) {
  if (args[0] === "bests") return renderBests(view, { meta, stale });
  if (args[0] === "form") return renderForm(view, { query, stale });
  document.title = `${t("Zones & thresholds")} · ${t("Fitness")} · VibeSynergy`;
  view.innerHTML = `<p class="sub" role="status" aria-busy="true">${esc(t("Loading…"))}</p>`;
  let reply;
  try {
    reply = await get("thresholds");
  } catch (error) {
    if (stale()) return;
    view.innerHTML = `${fitnessHeadHtml("")}
      <p class="notice error" role="alert">${esc(t("Could not load your zones: {error}", { error: error.message }))}</p>`;
    return;
  }
  if (stale()) return;
  // About you (sex, heart rates) keeps its own state: the cards above redraw after a change.
  const profile = document.createElement("section");
  profile.className = "card";
  profile.id = "about-you";
  draw(view, reply.data, [], stale, profile);
  athleteProfile(profile, { stale }).catch((error) => {
    if (!stale()) profile.textContent = t("Could not load: {error}", { error: error.message });
  });
}

// Fitness → Form: fitness, fatigue and form, projected to the races.
async function renderForm(view, { query, stale }) {
  document.title = `${t("Form")} · ${t("Fitness")} · VibeSynergy`;
  const asked = query?.get("range");
  const range = FITNESS_RANGES.some(([v]) => v === asked) ? asked : "90";
  const reply = await get("form", range !== "90" ? { range } : {});
  if (stale()) return;
  const d = reply.data;
  const body = formHtml(d.fitness, d.projection, range);
  view.innerHTML = `${fitnessHeadHtml("form", t("How fit and how fresh you are, and where the plan takes you on race day."))}
    ${body || `<div class="card empty">${esc(t("No training load yet."))}</div>`}
    <section class="card" aria-labelledby="predictions-title"><h2 id="predictions-title">${esc(t("Race predictions"))}</h2>
      <div data-predictions><p class="sub" role="status">${esc(t("Loading…"))}</p></div></section>`;
  mountPredictions(view.querySelector("[data-predictions]"), { stale });
  mountForm(view, d, (value) => {
    location.hash = `#/zones/form${value !== "90" ? `?range=${value}` : ""}`;
  });
}

// Fitness → Best efforts: the start page's block with sport and period.
async function renderBests(view, { meta, stale }) {
  document.title = `${t("Best efforts")} · ${t("Fitness")} · VibeSynergy`;
  const share = meta?.data?.share;
  const prefs = share ? {} : await get("prefs").then((reply) => reply.data || {}, () => ({}));
  if (stale()) return;
  view.innerHTML = `${fitnessHeadHtml("bests", t("Your best times, power and longest sessions, per sport or all together."))}
    <section class="card" aria-label="${esc(t("Best efforts"))}"><div data-bests></div></section>`;
  mountBests(view.querySelector("[data-bests]"), {
    period: prefs.best_runs_period,
    sport: prefs.best_sport,
    canSave: !share,
    stale,
    linkActivities: !share || share.pages.includes("activities"),
  });
}

function draw(view, data, results, stale, profile) {
  // A tile you opened stays open when the page redraws after a change.
  const open = new Set(
    [...view.querySelectorAll("[data-sport-card] > details[open]")].map((d) => d.parentElement.dataset.sportCard),
  );
  view.innerHTML = `
    ${fitnessHeadHtml("", t("Your plan and workouts use these values. Update them after a test or race."))}
    <p class="notice info" data-zones-status role="status" aria-live="polite"${results.length ? "" : " hidden"}>${resultsText(results)}</p>
    ${proposalsCard(data.proposals || [])}
    ${data.intervals && data.intervals.connected !== false && !data.intervals.ok ? `<p class="notice warning">${esc(t("intervals.icu could not be reached; its values are missing below."))}</p>` : ""}
    <div class="zones-grid">${(data.sports || []).map((sport) => sportCard(sport, data, { open: open.has(sport.sport) })).join("")}</div>
    <div data-athlete-profile-slot></div>
    ${generalCard(data)}`;
  view.querySelector("[data-athlete-profile-slot]").replaceWith(profile);
  bind(view, data, stale, profile);
}

// Proposals ---------------------------------------------------------------------------------

function proposalsCard(proposals) {
  if (!proposals.length) return "";
  return `<section class="card zones-proposals" aria-labelledby="zones-proposals-title">
    <h2 id="zones-proposals-title">${esc(t("Your coach suggests new values"))}</h2>
    <ul class="zones-proposal-list">${proposals.map((p) => `<li>
      <p><strong>${withSportIcon(p.sport, esc(sportLabel(p.sport)))} · ${esc(t(METRIC_NAMES[p.metric] || p.metric))}: ${esc(valueText(p.metric, p.value))}</strong>
        <span class="sub">${esc(methodLine(p))}</span>${activityLink(p.activity_id)}</p>
      ${p.summary ? `<details><summary>${esc(t("Why"))}</summary><p>${esc(p.summary)}</p></details>` : ""}
      <div class="zones-actions">
        <button class="btn primary" type="button" data-accept="${esc(String(p.id))}" data-sport="${esc(p.sport)}">${esc(t("Take over"))}</button>
        <button class="btn" type="button" data-dismiss="${esc(String(p.id))}">${esc(t("Ignore"))}</button>
      </div></li>`).join("")}</ul>
    <fieldset class="zones-push"><legend>${esc(t("When you take one over, also set it in"))}</legend>
      ${pushBoxes("proposal")}</fieldset>
    ${proposals.length > 1 ? `<button class="btn" type="button" data-accept-all>${esc(t("Take over all"))}</button>` : ""}
  </section>`;
}

function pushBoxes(prefix, sport) {
  return Object.entries(PLATFORMS).map(([key, name]) => {
    const id = `${prefix}-push-${key}${sport ? `-${sport}` : ""}`;
    return `<label class="check" for="${id}"><input type="checkbox" id="${id}" name="push-${key}"> ${esc(t("Also in {platform}", { platform: name }))}</label>`;
  }).join("");
}

// One sport ---------------------------------------------------------------------------------

// One line for the collapsed tile: the values your plan uses, e.g. "FTP 265 W · LTHR 150".
export function summaryLine(sport) {
  const parts = (sport.metrics || [])
    .filter((m) => m.plan && m.plan.value != null)
    .map((m) => `${t(METRIC_NAMES[m.metric] || m.metric)} ${valueText(m.metric, m.plan.value)}`);
  return parts.length ? parts.join(" · ") : t("Not set");
}

export function sportCard(sport, data = {}, { open = false } = {}) {
  const name = sportLabel(sport.sport);
  const titleId = `zones-${sport.sport}-title`;
  return `<section class="card zones-sport" data-sport-card="${esc(sport.sport)}" data-sport="${sportKey(sport.sport)}" aria-labelledby="${titleId}">
    <h2 id="${titleId}"><span class="sport-icon">${sportIcon(sport.sport)}</span>${esc(name)}</h2>
    <p class="zones-summary">${esc(summaryLine(sport))}</p>
    ${mismatchNotice(sport)}
    <details class="collapsible zones-more"${open ? " open" : ""}><summary>${esc(t("Zones, change and history"))}</summary>
    <div class="table-wrap"><table class="zones-values">
      <caption class="visually-hidden">${esc(t("Thresholds for {sport}", { sport: name }))}</caption>
      <thead><tr><th scope="col">${esc(t("Value"))}</th><th scope="col">${esc(t("In your plan"))}</th>
        <th scope="col">Garmin</th><th scope="col">intervals.icu</th><th scope="col"><span class="visually-hidden">${esc(t("Change"))}</span></th></tr></thead>
      <tbody>${sport.metrics.map((m) => metricRow(sport, m)).join("")}</tbody></table></div>
    ${editForm(sport)}
    ${zonesBlock(sport)}
    ${measuredBlock(sport)}
    ${platformNotes(sport, data)}
    ${historyBlock(sport)}
    </details>
  </section>`;
}

function metricRow(sport, m) {
  const plan = m.plan;
  const label = t(METRIC_NAMES[m.metric] || m.metric);
  const planCell = plan
    ? `<strong>${esc(valueText(m.metric, plan.value))}</strong><span class="sub zones-source">${esc(methodLine(plan))}</span>${activityLink(plan.activity_id)}`
    : `<span class="sub">${esc(t("Not set"))}</span>`;
  return `<tr><th scope="row">${esc(label)}</th><td data-label="${esc(t("In your plan"))}">${planCell}</td>
    ${platformCell(sport, m, "garmin")}${platformCell(sport, m, "intervals")}
    <td class="zones-edit-cell"><button class="btn small" type="button" data-edit="${esc(m.metric)}" aria-label="${esc(t("Change {metric}", { metric: label }))}">${esc(t("Change"))}</button></td></tr>`;
}

function platformCell(sport, m, platform) {
  const value = m[platform];
  if ((UNSUPPORTED[platform]?.[sport.sport] || []).includes(m.metric)) {
    return `<td data-label="${PLATFORMS[platform]}"><span class="sub">${esc(t("not available"))}</span></td>`;
  }
  if (value === null || value === undefined) return `<td data-label="${PLATFORMS[platform]}"><span class="sub">–</span></td>`;
  const differs = (sport.mismatches || []).some((x) => x.platform === platform && x.metric === m.metric);
  return `<td data-label="${PLATFORMS[platform]}">${esc(valueText(m.metric, value))}${differs ? ` <span class="chip warning">${esc(t("differs"))}</span>` : ""}</td>`;
}

function mismatchNotice(sport) {
  const list = sport.mismatches || [];
  if (!list.length) return "";
  const lines = list.map((x) => {
    const metric = t(METRIC_NAMES[x.metric] || x.metric);
    const text = x.metric === "hr_zones"
      ? t("{platform} has other heart rate zones than your plan", { platform: PLATFORMS[x.platform] })
      : x.metric === "hr_default"
      ? t("{platform}'s general heart rate zones differ from running", { platform: PLATFORMS[x.platform] })
      : x.metric === "auto_detect"
      ? t("{platform} still detects your thresholds automatically; turn it off", { platform: PLATFORMS[x.platform] })
      : x.metric === "profile_lthr" || x.metric === "profile_pace"
      ? t("The lactate threshold in your {platform} profile differs from your plan", { platform: PLATFORMS[x.platform] })
      : t("{platform} has {metric} {have}, your plan {want}", {
        platform: PLATFORMS[x.platform], metric, have: valueText(x.metric, x.platform_value), want: valueText(x.metric, x.plan_value),
      });
    return `<li>${esc(text)}</li>`;
  }).join("");
  const platforms = [...new Set(list.map((x) => x.platform))];
  return `<div class="notice warning zones-mismatch"><p><strong>${esc(t("Not the same everywhere"))}</strong></p><ul>${lines}</ul>
    <div class="zones-actions">${platforms.map((p) => `<button class="btn" type="button" data-push-now="${esc(p)}" data-sport="${esc(sport.sport)}">${esc(t("Set your plan in {platform}", { platform: PLATFORMS[p] }))}</button>`).join("")}</div></div>`;
}

function editForm(sport) {
  const options = sport.metrics.map((m) => `<option value="${esc(m.metric)}">${esc(t(METRIC_NAMES[m.metric] || m.metric))}</option>`).join("");
  const today = isoToday();
  const id = `edit-${sport.sport}`;
  return `<form class="zones-edit" data-edit-form="${esc(sport.sport)}" hidden aria-labelledby="${id}-title">
    <h3 id="${id}-title">${esc(t("Change a value"))}</h3>
    <div class="zones-edit-fields">
      <div class="settings-field"><label for="${id}-metric">${esc(t("Value"))}</label><select class="btn" id="${id}-metric" name="metric">${options}</select></div>
      <div class="settings-field"><label for="${id}-value">${esc(t("New value"))} <span class="sub" data-unit></span></label>
        <input class="btn" id="${id}-value" name="value" inputmode="decimal" autocomplete="off" required aria-describedby="${id}-hint">
        <p class="sub" id="${id}-hint" data-hint></p></div>
      <div class="settings-field"><label for="${id}-date">${esc(t("Date"))}</label><input class="btn" type="date" id="${id}-date" name="date_set" value="${today}" max="${today}"></div>
      <div class="settings-field"><label for="${id}-method">${esc(t("Where it comes from"))}</label><select class="btn" id="${id}-method" name="method">
        ${METHODS.map(([key, label]) => `<option value="${key}">${esc(t(label))}</option>`).join("")}</select></div>
    </div>
    <fieldset class="zones-push"><legend>${esc(t("Also set it in"))}</legend>${pushBoxes("edit", sport.sport)}
</fieldset>
    <p class="notice error" data-edit-error role="alert" hidden></p>
    <div class="zones-actions"><button class="btn primary" type="submit">${esc(t("Save"))}</button>
      <button class="btn" type="button" data-edit-cancel>${esc(t("Cancel"))}</button></div>
  </form>`;
}

function zonesBlock(sport) {
  const blocks = [];
  const z = sport.zones || {};
  if (z.hr) blocks.push(zoneTable(t("Heart rate zones"), z.hr.zones, (row) => rangeText(row.low, row.high, "bpm")));
  if (z.power) blocks.push(zoneTable(t("Power zones"), z.power.zones, (row) => rangeText(row.low, row.high, "W")));
  if (z.pace) {
    const unit = sport.sport === "swim" ? t("/100 m") : t("/km");
    blocks.push(zoneTable(t("Pace zones"), z.pace.zones, (row) => paceRange(row.slow, row.fast, unit)));
  }
  if (!blocks.length) return `<p class="sub">${esc(t("Zones appear once the plan has a threshold for this sport."))}</p>`;
  return `<div class="zones-tables">${blocks.join("")}</div>`;
}

function zoneTable(title, rows, text) {
  return `<div class="table-wrap"><table class="zones-zone-table"><caption>${esc(title)}</caption>
    <thead><tr><th scope="col">${esc(t("Zone"))}</th><th scope="col">${esc(t("Range"))}</th></tr></thead>
    <tbody>${rows.map((row) => `<tr><th scope="row"><span class="zone-badge"><i class="zone-swatch" style="background:var(--zone-${Math.min(row.zone, 7)})" aria-hidden="true"></i><span class="zone-code" aria-hidden="true">Z${esc(row.label)}</span><span class="visually-hidden">${esc(t("Zone {n}", { n: row.label }))}</span></span></th><td>${esc(text(row))}</td></tr>`).join("")}</tbody></table></div>`;
}

function measuredBlock(sport) {
  const items = [];
  if (sport.highest_hr) {
    items.push(`<li>${esc(t("Highest heart rate in the last 12 months: {value} bpm", { value: number(sport.highest_hr.value) }))} ${activityLink(sport.highest_hr.activity_id, userDate(sport.highest_hr.date))}</li>`);
  }
  if (sport.vo2max) items.push(`<li>${esc(t("VO2max (Garmin): {value}", { value: number(sport.vo2max.value, 1) }))}</li>`);
  return items.length ? `<ul class="zones-measured sub">${items.join("")}</ul>` : "";
}

function platformNotes(sport, data) {
  const notes = [];
  for (const metric of UNSUPPORTED.garmin[sport.sport] || []) {
    notes.push(t("VibeSynergy cannot set {metric} in Garmin; your watch keeps its own.", { metric: t(METRIC_NAMES[metric]) }));
  }
  if (sport.sport === "run") notes.push(t("Garmin may change your running threshold itself after a run."));
  return `${notes.length ? `<ul class="sub zones-notes">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`;
}

function historyBlock(sport) {
  const rows = sport.metrics.flatMap((m) => m.history.map((h) => ({ ...h, metric: m.metric })));
  if (!rows.length) return "";
  return `<details class="collapsible zones-history"><summary>${esc(t("History"))}</summary>
    <ul>${rows.map((h) => `<li><strong>${esc(t(METRIC_NAMES[h.metric] || h.metric))}</strong>: ${esc(h.previous != null ? `${valueText(h.metric, h.previous)} → ` : "")}${esc(valueText(h.metric, h.value))}
      <span class="sub">${esc([h.date_set ? userDate(h.date_set) : "", t(METHOD_NAMES[h.method] || h.method), statusName(h.status)].filter(Boolean).join(" · "))}</span>${activityLink(h.activity_id)}
      ${h.pushes.length ? `<span class="sub"> · ${esc(h.pushes.map((p) => `${PLATFORMS[p.platform] || p.platform} ${p.outcome === "ok" ? t("done") : t("failed")} (${userDateTime(p.at)})`).join(", "))}</span>` : ""}</li>`).join("")}</ul></details>`;
}

function generalCard(data) {
  const m = data.measured || {};
  const items = [];
  if (m.resting_hr) items.push(t("Resting heart rate: {value} bpm on average over 60 days", { value: number(m.resting_hr.value) }));
  if (m.hrv_low && m.hrv_high) items.push(t("HRV: your normal range is {low}–{high} ms", { low: number(m.hrv_low.value), high: number(m.hrv_high.value) }));
  if (m.hrv_week) items.push(t("HRV this week: {value} ms", { value: number(m.hrv_week.value) }));
  for (const g of data.general || []) {
    if (g.plan) items.push(`${t(METRIC_NAMES[g.metric] || g.metric)} (${t("plan")}): ${valueText(g.metric, g.plan.value)}`);
  }
  if (!items.length) return "";
  return `<section class="card" aria-labelledby="zones-general-title"><h2 id="zones-general-title">${esc(t("Recovery markers"))}</h2>
    <ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    <p class="sub">${esc(t("Measured by Garmin. Your coach uses them to judge recovery."))}</p></section>`;
}

// Formatting --------------------------------------------------------------------------------

function sportLabel(sport) {
  return t(SPORT_NAMES[sport] || sport);
}

function statusName(status) {
  return { current: t("in use"), proposed: t("suggested"), dismissed: t("ignored"), superseded: "" }[status] ?? "";
}

function clockText(seconds) {
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function valueText(metric, value) {
  if (value === null || value === undefined) return "–";
  if (Array.isArray(value)) return value.map((v) => number(v)).join(" / ");
  if (metric === "threshold_pace") return `${clockText(value)} ${t("/km")}`;
  if (metric === "css") return `${clockText(value)} ${t("/100 m")}`;
  if (["ftp", "threshold_power"].includes(metric)) return `${number(value)} W`;
  if (metric === "hrv_baseline") return `${number(value)} ms`;
  return `${number(value)} bpm`;
}

function rangeText(low, high, unit) {
  if (low === null || low === undefined) return t("up to {high} {unit}", { high: number(high), unit });
  if (high === null || high === undefined) return t("{low} {unit} and up", { low: number(low), unit });
  return `${number(low)}–${number(high)} ${unit}`;
}

function paceRange(slow, fast, unit) {
  if (slow === null || slow === undefined) return t("slower than {pace}", { pace: `${clockText(fast)} ${unit}` });
  if (fast === null || fast === undefined) return t("faster than {pace}", { pace: `${clockText(slow)} ${unit}` });
  return `${clockText(slow)}–${clockText(fast)} ${unit}`;
}

function methodLine(row) {
  const parts = [t(METHOD_NAMES[row.method] || row.method)];
  if (row.date_set) parts.push(userDate(row.date_set));
  return parts.join(" · ");
}

function activityLink(id, text) {
  if (!id) return "";
  return ` <a href="#/activity/${encodeURIComponent(id)}">${esc(text || t("activity"))}</a>`;
}

function resultsText(results) {
  if (!results.length) return "";
  return esc(results.map((r) => {
    const name = PLATFORMS[r.platform] || r.platform;
    if (r.ok) return t("{platform}: done.", { platform: name });
    return t("{platform}: not changed, {reason}", { platform: name, reason: t(PUSH_ERRORS[r.error] || PUSH_ERRORS.refused) });
  }).join(" "));
}

// Parsing the edit form: pace and CSS as m:ss, the rest as a number.
export function parseValue(metric, text) {
  const raw = String(text || "").trim().replace(",", ".");
  if (!raw) return null;
  if (["threshold_pace", "css"].includes(metric)) {
    const match = raw.match(/^(\d{1,2}):(\d{2})$/);
    if (match) return Number(match[1]) * 60 + Number(match[2]);
    return null;
  }
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

const HINTS = {
  threshold_pace: N("Minutes and seconds per km, for example 5:30."),
  css: N("Minutes and seconds per 100 m, for example 2:30."),
};
const ENTER_NUMBER = N("Enter a number.");
const UNITS = { ftp: "W", threshold_power: "W", lthr: "bpm", max_hr: "bpm", threshold_pace: N("/km"), css: N("/100 m") };

// Events ------------------------------------------------------------------------------------

function bind(view, data, stale, profile) {
  const status = view.querySelector("[data-zones-status]");
  const redraw = (reply) => {
    if (stale() || !view.isConnected) return;
    draw(view, reply.data, reply.data.push_results || [], stale, profile);
  };
  const fail = (error) => {
    if (stale() || !view.isConnected) return;
    status.hidden = false;
    status.className = "notice error";
    status.textContent = t("Not saved: {error}", { error: error.message });
  };
  const pushChoice = (scope) => ({
    garmin: Boolean(scope.querySelector("[name=push-garmin]")?.checked),
    intervals: Boolean(scope.querySelector("[name=push-intervals]")?.checked),
  });

  for (const card of view.querySelectorAll("[data-sport-card]")) {
    const sport = card.dataset.sportCard;
    const form = card.querySelector("[data-edit-form]");
    const metricSelect = form.querySelector("[name=metric]");
    const sync = () => {
      const metric = metricSelect.value;
      form.querySelector("[data-unit]").textContent = UNITS[metric] ? `(${t(UNITS[metric])})` : "";
      form.querySelector("[data-hint]").textContent = HINTS[metric] ? t(HINTS[metric]) : "";
      const garmin = form.querySelector("[name=push-garmin]");
      const blocked = (UNSUPPORTED.garmin[sport] || []).includes(metric);
      garmin.disabled = blocked;
      if (blocked) garmin.checked = false;
    };
    metricSelect.addEventListener("change", sync);
    for (const button of card.querySelectorAll("[data-edit]")) {
      button.addEventListener("click", () => {
        metricSelect.value = button.dataset.edit;
        sync();
        form.hidden = false;
        form.querySelector("[name=value]").focus();
      });
    }
    form.querySelector("[data-edit-cancel]").addEventListener("click", () => {
      form.hidden = true;
      card.querySelector(`[data-edit="${metricSelect.value}"]`)?.focus();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = form.querySelector("[data-edit-error]");
      const metric = metricSelect.value;
      const value = parseValue(metric, form.querySelector("[name=value]").value);
      if (value === null) {
        error.hidden = false;
        error.textContent = t(HINTS[metric] || ENTER_NUMBER);
        form.querySelector("[name=value]").focus();
        return;
      }
      error.hidden = true;
      const submit = form.querySelector("[type=submit]");
      submit.disabled = true;
      try {
        redraw(await post("thresholds/set", {
          sport, metric, value,
          method: form.querySelector("[name=method]").value,
          date_set: form.querySelector("[name=date_set]").value || null,
          push: pushChoice(form),
        }));
      } catch (problem) {
        submit.disabled = false;
        error.hidden = false;
        error.textContent = t("Not saved: {error}", { error: problem.message });
      }
    });
  }

  for (const button of view.querySelectorAll("[data-push-now]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        redraw(await post("thresholds/push", {
          sport: button.dataset.sport,
          push: { garmin: button.dataset.pushNow === "garmin", intervals: button.dataset.pushNow === "intervals" },
        }));
      } catch (error) {
        button.disabled = false;
        fail(error);
      }
    });
  }

  const proposals = view.querySelector(".zones-proposals");
  if (!proposals) return;
  for (const button of proposals.querySelectorAll("[data-accept]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        redraw(await post(`thresholds/${button.dataset.accept}/accept`, { push: pushChoice(proposals) }));
      } catch (error) {
        button.disabled = false;
        fail(error);
      }
    });
  }
  for (const button of proposals.querySelectorAll("[data-dismiss]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        redraw(await post(`thresholds/${button.dataset.dismiss}/dismiss`));
      } catch (error) {
        button.disabled = false;
        fail(error);
      }
    });
  }
  proposals.querySelector("[data-accept-all]")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    const choice = pushChoice(proposals);
    try {
      let reply = null;
      const sports = new Set();
      for (const p of data.proposals) {
        reply = await post(`thresholds/${p.id}/accept`, {});
        sports.add(p.sport);
      }
      const results = [];
      if (choice.garmin || choice.intervals) {
        for (const sport of sports) {
          if (!data.sports.some((s) => s.sport === sport)) continue;
          reply = await post("thresholds/push", { sport, push: choice });
          results.push(...(reply.data.push_results || []));
        }
        reply.data.push_results = results;
      }
      redraw(reply);
    } catch (error) {
      fail(error);
    }
  });
}
