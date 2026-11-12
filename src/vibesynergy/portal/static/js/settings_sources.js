// Settings → Sources (split from pages/settings.js): each source's card with its
// connection, data and sync frequency, Garmin delivery per sport and intervals.icu notes.
import { get } from "./api.js";
import { esc, number, sourceName, timeOfDay } from "./format.js";
import { N, t } from "./i18n.js";
import { icuConnectHtml } from "./icu_connect.js";
import { saveRow } from "./settings_pieces.js";

export function sourcesTabHtml(s, { focus = null, shared = false } = {}) {
  const rows = s.sources.filter((src) => src.id !== "demo" && src.id !== "caldav" && !src.import_only);
  const byId = Object.fromEntries(rows.map((src) => [src.id, src]));
  const preferred = ["garmin", "strava", "withings"];
  const ids = [
    ...preferred.filter((id) => byId[id]),
    ...rows.map((src) => src.id).filter((id) => !preferred.includes(id)),
  ];
  const cards = ids.map((id) => connectionCard(byId[id], { shared, settings: s }));
  const calendar = shared
    ? ""
    : `<details${focus === "calendar" ? " open" : ""}><summary>${t("Calendar")}</summary><section class="card" data-caldav></section></details>`;
  const upload = shared
    ? ""
    : `<details><summary>${t("Upload activity files")}</summary><div data-file-import></div></details>`;
  const parts = focus === "calendar" ? [calendar, ...cards, upload] : [...cards, calendar, upload];
  return parts.filter(Boolean).join("");
}

export function accountLine(src) {
  const login = src.login;
  if (!login) return "";
  if (login.state === "logged in" && login.account) return t("Connected as {name}", { name: login.account });
  if (login.state === "logged in") return t("Connected");
  if (login.state === "login failing") return t(login.state);
  return t("Not connected");
}

// Sources whose past comes from an account export uploaded on their card.
export const ARCHIVE_TITLES = { garmin: N("Garmin history"), strava: N("Strava history") };

export function connectionCard(src, { shared, settings }) {
  const id = src.id;
  return `<section class="card" data-source="${esc(id)}">
    <div class="page-head"><h2>${esc(sourceName(id))}</h2><div>${esc(accountLine(src))}</div></div>
    ${budgetHtml(src.budget_wait)}
    ${shared ? "" : id === "garmin" ? garminConnect(src) : ""}
    ${shared ? "" : id === "strava" ? stravaConnect(src) : ""}
    ${shared ? "" : id === "intervals_icu" ? icuConnectHtml(src) : ""}
    ${shared ? "" : id === "intervals_icu" ? icuNotesHtml(src) : ""}
    ${shared ? "" : id === "garmin" ? garminDeliveryHtml(src) : ""}
    ${shared ? "" : `<p><button class="btn" type="button" data-sync-now="${esc(id)}">${t("Sync now")}</button></p>
      <p class="sub" data-sync-status role="status"></p>`}
    ${!shared && ARCHIVE_TITLES[id] ? `<details><summary>${t(ARCHIVE_TITLES[id])}</summary><div data-archive-import="${esc(id)}"></div></details>` : ""}
    ${!shared && id === "withings" ? `<p class="sub">${t("Your Withings history comes in automatically, back to your first measurement. You don't need a Withings export.")}</p>` : ""}
    ${shared ? "" : sourceDataHtml(src, settings)}
  </section>`;
}

// Connected: the sign-in form waits in a closed "Sign in again" disclosure. Not connected:
// the form is the first thing on the card. One connection serves data and publishing.
export function signInBlock(src, form) {
  if (src.login?.state !== "logged in") return form;
  return `<details><summary>${t("Sign in again")}</summary>${form}</details>`;
}

export function garminConnect(src) {
  const connected = src.login?.state === "logged in";
  const form = `<div class="settings-grid">
      <label>${t("Garmin email")} <input class="btn" name="garmin_email" type="email" autocomplete="username"></label>
      <label>${t("Garmin password")} <input class="btn" name="garmin_password" type="password" autocomplete="current-password"></label>
    </div>
    <p><button class="btn${connected ? "" : " primary"}" type="button" data-connect-garmin>${t(connected ? N("Sign in to Garmin") : N("Connect Garmin"))}</button></p>
    <div data-garmin-mfa hidden><label>${t("Sign-in code")} <input class="btn" name="garmin_code" inputmode="numeric" autocomplete="one-time-code"></label>
      <button class="btn" type="button" data-submit-mfa>${t("Continue sign-in")}</button></div>`;
  return `<div data-source-connect>
    ${signInBlock(src, form)}
    ${connected ? `<p><button class="btn" type="button" data-disconnect="garmin">${t("Disconnect Garmin")}</button></p>` : ""}
    <p class="sub" role="status" data-connect-status></p>
  </div>
`;
}

// Workouts VibeSynergy puts on Garmin itself, per sport; off = intervals.icu's push only.
export const DELIVERY_SPORTS = [["swim", N("Swimming")], ["run", N("Running")], ["ride", N("Cycling (outdoor and MTB)")]];

export function garminDeliveryHtml(src) {
  const on = new Set(src.garmin_delivery || []);
  return `<fieldset class="garmin-delivery"><legend>${t("Workouts from VibeSynergy on your Garmin")}</legend>
    ${DELIVERY_SPORTS.map(([k, label]) => `<label class="check"><input type="checkbox" name="garmin_delivery" value="${k}" data-autosave-skip${on.has(k) ? " checked" : ""}> ${t(label)}</label>`).join("")}
    <p class="sub">${t("VibeSynergy puts these workouts on your Garmin calendar itself. Strength and pilates always come from VibeSynergy. Indoor rides keep going to Zwift through intervals.icu.")}</p>
    <p class="sub" data-delivery-status role="status"></p></fieldset>`;
}

// Diary notes to intervals.icu: off by default, and only sent while intervals.icu
// keeps wellness comments private (Settings there: Private wellness data → Comments).
export function icuNotesHtml(src) {
  return `<label class="check"><input type="checkbox" name="icu_private_notes" data-autosave-skip${src.private_notes ? " checked" : ""}> ${t("Copy my diary notes to intervals.icu")}</label>
  <p class="sub">${t("They go to the day's wellness comments, only while those are private on intervals.icu (Settings → Private wellness data → Comments). Otherwise they stay in VibeSynergy.")}</p>
  <p class="sub" data-icu-notes-status role="status"></p>`;
}

export async function icuNotesCheck(status) {
  try {
    const { data } = await get("icu-notes/status");
    if (!status.isConnected) return;
    status.textContent = !data.connected
      ? t("intervals.icu is not connected, so nothing is copied.")
      : data.private
        ? t("Comments are private on intervals.icu: notes are copied.")
        : t("Comments are not private on intervals.icu yet, so nothing is copied.");
  } catch (error) {
    if (status.isConnected) status.textContent = t("Not saved: {error}", { error: error.message });
  }
}

export function stravaConnect(src) {
  const connected = src.login?.state === "logged in";
  const form = `${src.login?.app_set ? "" : `<div class="settings-grid">
      <label>${t("Strava app ID")} <input class="btn" name="client_id" inputmode="numeric" autocomplete="off"></label>
      <label>${t("Strava app secret")} <input class="btn" name="client_secret" type="password" autocomplete="off"></label>
    </div>`}
    <p><button class="btn${connected ? "" : " primary"}" type="button" data-connect-strava>${t(connected ? N("Sign in to Strava") : N("Connect Strava"))}</button></p>`;
  return `<div data-source-connect>
    ${signInBlock(src, form)}
    ${connected ? `<p><button class="btn" type="button" data-disconnect="strava">${t("Disconnect Strava")}</button></p>` : ""}
    <p class="sub" role="status" data-connect-status></p>
  </div>`;
}

// What a source collects and how often it syncs, on its own card, collapsed.
export function sourceDataHtml(src, s) {
  const datasets = src.datasets?.length && src.id !== "demo" ? datasetSection(src) : "";
  const schedule = scheduleHtml(s, { only: src.id });
  if (!datasets && !schedule) return "";
  const synced = (src.datasets || []).filter((d) => d.synced).length;
  const summary = src.datasets?.length
    ? t("Data and frequency: {n} of {total} kinds of data", { n: number(synced), total: number(src.datasets.length) })
    : t("How often");
  return `<details class="collapsible"><summary>${esc(summary)}</summary>${datasets}${schedule}</details>`;
}

export function datasetSection(src) {
  return `<div class="settings-section" data-section="source" data-source="${esc(src.id)}">
    <h3>${esc(t("Data to collect"))}</h3>
    <label class="check"><input type="checkbox" name="enabled"${src.enabled ? " checked" : ""}> ${t("Enabled")}</label>
    <div class="settings-datasets">${src.datasets.map((d) => `<label class="check" title="${esc(t(d.description))}"><input type="checkbox" name="dataset" value="${esc(d.name)}"${d.synced ? " checked" : ""}>
      <span>${esc(d.name)}<span class="muted"> · ${esc(t(d.description))}${d.default ? "" : ` (${t("off by default")})`}</span></span></label>`).join("")}</div>
    ${saveRow()}
  </div>`;
}

// How often each source (and the Garmin calendar, the drop folder, the alert check) runs.
export const INTERVALS = [
  ["garmin", N("Garmin Connect: everything")],
  ["garmin.scheduled_workouts", N("Garmin training calendar (plans and races)")],
  ["withings", N("Withings (instant updates arrive anyway; this is the safety net)")],
  ["strava", N("Strava (instant updates arrive anyway; this is the safety net)")],
  ["dropfolder", N("Drop folder check")],
  ["alerts", N("Alert check")],
];

// `only`: the rows of one source (on its card); without it the service rows (System).
export function scheduleHtml(s, { only = null } = {}) {
  const enabled = new Set(s.sources.filter((src) => src.enabled).map((src) => src.id));
  const service = new Set(["dropfolder", "alerts"]);
  const rows = INTERVALS.filter(([key]) =>
    only ? key.split(".")[0] === only && enabled.has(only) : service.has(key))
    .map(([key, label]) => {
      const configured = s.schedule.configured[key];
      const fallback = s.schedule.defaults[key];
      return `<label>${esc(t(label))} <input class="btn" type="number" min="0" step="1" data-interval="${esc(key)}" value="${esc(configured ?? "")}" placeholder="${esc(fallback ? t("{n} (default)", { n: number(fallback) }) : t("off"))}"></label>`;
    })
    .join("");
  if (!rows) return "";
  const head = only ? `<h3>${t("How often")}</h3>` : `<h2>${t("How often")}</h2>`;
  const tag = only ? "div" : "section";
  return `<${tag} class="${only ? "" : "card "}settings-section" data-section="schedule">
    ${head}
    <p class="sub">${t("Minutes between syncs; 0 turns one off, empty is the default. Applies straight away.")}</p>
    <div class="settings-grid">${rows}</div>
    ${saveRow()}
  </${tag}>`;
}

// Strava asked us to wait: what that means, in plain words.
export function budgetHtml(wait) {
  if (!wait) return "";
  const text = t("Strava is busy; new activities will come in after {time}.", { time: timeOfDay(wait.until) });
  return `<div class="notice info" role="status">${esc(text)}</div>`;
}
