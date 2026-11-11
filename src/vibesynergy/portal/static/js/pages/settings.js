// Settings: VibeSynergy's configuration without editing config.toml on the server. Each section autosaves
// on change or blur (short debounce) with a Saved confirmation or inline error. The server validates
// previous file. The service applies saved settings when it restarts, which the banner offers.
// Paths, the public address and the ports are shown but only change in the file.
import { get, post } from "../api.js";
import { esc, number } from "../format.js";
import { language, N, persistLanguage, t } from "../i18n.js";
import { persistPalette, persistTheme } from "../theme.js";
import { caldavSettings } from "../calendar_settings.js";
import { garminTargetSettings } from "../calendar_target.js";
import { menuEditor } from "../nav.js";
import { bindAutosave, reportSaveError } from "../settings_autosave.js";
import { field, saveRow, select } from "../settings_pieces.js";
import { syncTabHtml } from "../settings_sync.js";
import { appearanceHtml, bindPortalHosts, configurationHtml, regionHtml } from "../settings_system.js";
import { icuNotesCheck, scheduleHtml, sourcesTabHtml } from "../settings_sources.js";
import { alertGroupsHtml, alertsHtml, bindAlertGroups, bindRoutes, mailHtml, routesHtml, syncEmailState } from "../settings_alerts.js";
import { mountPush, pushHtml } from "../push_settings.js";
import { loadShares } from "../settings_shares.js";
import { bindCoachApps, coachAppsHtml, logHtml } from "../settings_access.js";
import { archiveImport } from "../archive_import.js";
import { mountAbout } from "../about_version.js";
import { mountAccount } from "../account_settings.js";
import { bestSourceSettings } from "../best_source_settings.js";
import { bindIcuConnect } from "../icu_connect.js";
import { weatherLocationSettings } from "../weather_location.js";
import * as dataPage from "./data.js";
import * as historyPage from "./history.js";

// Settings per theme (#/settings/<theme>). The athlete's own values (sex, heart rates,
// thresholds) live on Training → Zones & thresholds; #/settings/you redirects there.
const THEMES = [
  ["sources", N("Sources")],
  ["sync", N("Sync between sources")],
  ["alerts", N("Alerts")],
  ["sharing", N("Sharing")],
  ["account", N("Account")],
  ["system", N("System")],
];

function resolveTheme(args, { legacySources, dataRoute, dataSub }) {
  if (legacySources || (dataRoute && dataSub === "sources")) {
    return { theme: "sources", dataSub: null, focus: null };
  }
  if (dataRoute && (dataSub === "sync" || dataSub === "history")) {
    return { theme: "sources", dataSub, focus: null };
  }
  const first = args?.[0];
  if (first === "calendar") return { theme: "sources", dataSub: null, focus: "calendar" };
  if (THEMES.some(([id]) => id === first)) return { theme: first, dataSub: null, focus: null };
  // Old tab names keep working (bookmarks, links in coach notes).
  const legacy = {
    // Display moved into System.
    appearance: "system",
    ai: "system",
    advanced: "system",
    "coach-apps": "sharing",
  };
  return { theme: legacy[first] || "sources", dataSub: null, focus: first || null };
}
const DATA_TABS = [
  ["sources", N("Sources")],
  ["sync", N("Sync")],
  ["history", N("History")],
];

function sharedDataAccess(share) {
  const pages = new Set(share.pages);
  const syncHistory = pages.has("data") || pages.has("history");
  return { sync: syncHistory, history: syncHistory, sources: pages.has("sources") };
}

async function renderSharedData(view, ctx, dataSub) {
  const { stale, query, meta } = ctx;
  const access = sharedDataAccess(meta.data.share);
  if (
    (dataSub === "sync" && !access.sync)
    || (dataSub === "history" && !access.history)
    || (dataSub === "sources" && !access.sources)
  ) {
    view.innerHTML = `<div class="card empty">${esc(t("This page isn't part of this shared view."))}</div>`;
    return;
  }
  if (dataSub === "sync" || dataSub === "history") {
    const title = dataSub === "sync" ? t("Sync") : t("History");
    document.title = `${title} · VibeSynergy`;
    const tabs = sharedDataTabs(dataSub, access);
    view.innerHTML = `
      <div class="page-head"><div><h1>${esc(title)}</h1></div></div>
      ${tabs}
      <div data-data-panel class="settings-data-panel"></div>`;
    await mountEmbeddedDataPanels(view, { theme: "data", dataSub, query, meta, stale });
    return;
  }
  const settings = await get("settings");
  if (stale()) return;
  document.title = `${t("Sources")} · VibeSynergy`;
  const shell = {
    s: settings.data,
    clients: [],
    prefs: { language: "auto", refresh_min: 5 },
    theme: "data",
    dataSub: "sources",
    stale,
    query,
    meta,
  };
  drawSharedSources(view, shell);
}

function sharedDataTabs(dataSub, access) {
  const tabs = DATA_TABS.filter(([id]) => (id === "sync" && access.sync) || (id === "history" && access.history) || (id === "sources" && access.sources));
  if (tabs.length < 2) return "";
  return `<nav class="tabs settings-data-tabs" aria-label="${esc(t("Data"))}">${tabs
    .map(([id, label]) => `<a class="btn" href="#/settings/data/${id}"${id === dataSub ? ' aria-current="page"' : ""}>${esc(t(label))}</a>`)
    .join("")}</nav>`;
}

export async function render(view, ctx, mode = "settings") {
  const { args, stale, query, meta } = ctx;
  const legacySources = mode === "sources" || args?.[0] === "sources";
  const dataRoute = legacySources || args?.[0] === "data";
  const dataSub = dataRoute ? (legacySources ? "sources" : args[1] || "sources") : null;
  if (meta?.data?.share && dataRoute) {
    await renderSharedData(view, ctx, dataSub);
    return;
  }
  const [settings, clients, prefs] = await Promise.all([
    get("settings"),
    get("mcp_clients"),
    get("prefs"),
  ]);
  if (stale()) return;
  const route = resolveTheme(args, { legacySources, dataRoute, dataSub });
  document.title = `${t("Settings")} · VibeSynergy`;
  const shell = {
    s: settings.data,
    clients: clients.data.clients,
    access: clients.data,
    prefs: prefs.data,
    theme: route.theme,
    dataSub: route.dataSub,
    focus: route.focus,
    stale,
    query,
    meta,
  };
  draw(view, shell);
  await mountEmbeddedDataPanels(view, shell);
}

async function mountEmbeddedDataPanels(view, { theme, dataSub, query, meta, stale }) {
  if (theme === "sources" && dataSub === "sync") {
    const panel = view.querySelector("[data-data-panel]");
    if (panel) await dataPage.render(panel, { query, meta, stale }, { embedded: true });
  } else if (theme === "sources" && dataSub === "history") {
    const panel = view.querySelector("[data-data-panel]");
    if (panel) await historyPage.render(panel, { meta, stale }, { embedded: true });
  }
}

function drawSharedSources(view, ctx) {
  const { s, dataSub, query } = ctx;
  const tabs = sharedDataTabs(dataSub, sharedDataAccess(ctx.meta.data.share));
  view.innerHTML = `
    <div class="page-head"><div><h1>${t("Sources")}</h1></div></div>
    ${tabs}
    ${sourcesTabHtml(s, { shared: true })}`;
  bind(view, ctx);
  showStravaConnectResult(view, query);
}
























function draw(view, ctx, flash = "") {
  const { s, prefs, theme, stale, query, access } = ctx;
  const parts = {
    account: `<div data-account-host></div>`,
    sharing: `${coachAppsHtml(access || {})}<section class="card" data-shares aria-labelledby="shares-title"></section>${logHtml()}`,
    sync: syncTabHtml(s),
    sources: ctx.dataSub ? `<div data-data-panel class="settings-data-panel"></div>` : sourcesTabHtml(s, { focus: ctx.focus }),
    alerts: `${alertsHtml(s.alerts, s.mail)}${pushHtml()}${alertGroupsHtml(s.alerts)}
      <details class="card collapsible settings-more" data-mail-details><summary>${t("Mail settings")}</summary>${mailHtml(s.mail)}</details>
      <details class="card collapsible settings-more"><summary>${t("Per kind of alert")}</summary>${routesHtml(s.alerts)}</details>`,
    system: `<div data-about-host></div>
      ${regionHtml(s)}
      <section class="card settings-section" data-weather-location aria-labelledby="weather-location-title"><h2 id="weather-location-title">${t("Weather on the calendar")}</h2><p class="sub" role="status" aria-busy="true">${t("Loading…")}</p></section>
      ${appearanceHtml(prefs)}
      <details class="card collapsible settings-more"><summary>${t("Change the menu")}</summary><section data-menu></section></details>
      ${scheduleHtml(s)}
      <details class="card collapsible settings-more" data-best-source-details><summary>${t("Best source per measurement")}</summary><section data-best-source></section></details>
      <section class="card settings-section" data-section="dropfolder">
        <h2>${t("Drop folder")}</h2>
        <p class="sub">${t("Activity files (FIT, GPX, TCX, zip) put in this folder on this server are imported every few minutes, then moved to imported/ (or failed/ with the reason). Uploads on Data → History land here too.")}</p>
        <div class="settings-grid">${field(t("Folder on this server"), "dir", s.dropfolder.dir, `placeholder="${esc(s.dropfolder.effective)}"`)}</div>
        <p class="sub">${t("Now")}: <code>${esc(s.dropfolder.effective)}</code></p>
        ${saveRow()}
      </section>
      <section class="card settings-section" data-section="portal">
        <h2>${t("Map tiles")}</h2>
        <div class="settings-grid">
          <label class="check"><input type="checkbox" name="tile_cache"${s.portal.tile_cache ? " checked" : ""}> ${t("Cache map tiles on this server (maps work offline; the browser never contacts the tile server)")}</label>
          <label>${t("Refresh tiles older than (days)")} <input class="btn" type="number" name="tile_max_age_days" min="1" value="${esc(s.portal.tile_max_age_days ?? "")}" placeholder="${esc(t("default"))}"></label>
        </div>
        ${saveRow()}
      </section>
      ${configurationHtml(s)}`,
  };
  const tabs = `<nav class="tabs settings-nav" aria-label="${esc(t("Settings"))}">${THEMES.map(([id, label]) => `<a class="btn" href="#/settings/${id}"${id === theme ? ' aria-current="page"' : ""}>${esc(t(label))}</a>`).join("")}</nav>`;
  const tabLabel = t((THEMES.find(([id]) => id === theme) || THEMES[0])[1]);
  const dataTabs = theme === "sources"
    ? `<nav class="tabs settings-subnav" aria-label="${esc(t("Sources"))}">${[
      ["#/settings/sources", t("Connections"), !ctx.dataSub],
      ["#/settings/data/sync", t("Status"), ctx.dataSub === "sync"],
      ["#/settings/data/history", t("History"), ctx.dataSub === "history"],
    ].map(([href, label, on]) => `<a href="${href}"${on ? ' aria-current="page"' : ""}>${esc(label)}</a>`).join("")}</nav>`
    : "";
  document.title = `${tabLabel} · ${t("Settings")} · VibeSynergy`;
  view.innerHTML = `
    <div class="page-head settings-head"><div><p class="settings-caption">${t("Settings")}</p><h1>${esc(tabLabel)}</h1></div></div>
    ${tabs}
    ${dataTabs}
    ${flash ? `<div class="notice success" role="status">${esc(flash)}</div>` : ""}
    ${restartHere(s, theme) ? restartBanner(s.can_restart) : ""}
    ${parts[theme] || parts.sources}`;

  bind(view, ctx);
  if (theme === "sources") showStravaConnectResult(view, query);
  const caldav = view.querySelector("[data-caldav]");
  if (caldav) caldavSettings(caldav, { stale, query }).catch((error) => {
    if (!stale()) caldav.textContent = error.message;
  });
  const calendarCopy = view.querySelector("[data-calendar-copy]");
  if (calendarCopy) garminTargetSettings(calendarCopy, { stale, sources: true }).catch((error) => {
    if (!stale()) calendarCopy.textContent = error.message;
  });
  for (const archiveEl of view.querySelectorAll("[data-archive-import]")) {
    archiveImport(archiveEl, { stale, source: archiveEl.dataset.archiveImport }).catch((error) => {
      if (!stale()) archiveEl.textContent = error.message;
    });
  }
  const files = view.querySelector("[data-file-import]");
  if (files) dataPage.filesHtml(files, stale).catch((error) => {
    if (!stale()) files.textContent = error.message;
  });
  const shares = view.querySelector("[data-shares]");
  if (shares) loadShares(shares, stale);
  const coachApps = view.querySelector("[data-coach-apps]");
  if (coachApps) bindCoachApps(view, ctx, draw);
  const weatherEl = view.querySelector("[data-weather-location]");
  if (weatherEl) weatherLocationSettings(weatherEl, stale).catch((error) => {
    if (!stale()) weatherEl.textContent = t("Could not load: {error}", { error: error.message });
  });
  const bestSource = view.querySelector("[data-best-source]");
  if (bestSource) bestSourceSettings(bestSource, { stale, catalogue: ctx.meta?.data?.metrics }).catch((error) => {
    if (!stale()) bestSource.textContent = t("Could not load: {error}", { error: error.message });
  });
  const menu = view.querySelector("[data-menu]");
  if (menu) menuEditor(menu, () => window.dispatchEvent(new Event("vibesynergy-nav")));
  const table = view.querySelector("[data-routes]");
  if (table) bindRoutes(table, view, ctx);
  view.querySelector("[data-refresh] select")?.addEventListener("change", async (event) => {
    const status = view.querySelector("[data-refresh-status]");
    try {
      await post("prefs", { changes: { refresh_min: Number(event.target.value) } });
      status.textContent = t("Saved.");
      window.dispatchEvent(new Event("vibesynergy-prefs"));
    } catch (error) {
      reportSaveError(status, () => event.target.dispatchEvent(new Event("change")));
    }
  });
  const languageSelect = view.querySelector('[data-section="appearance"] [name=language]');
  languageSelect?.addEventListener("change", async (event) => {
    const choice = event.target.value;
    persistLanguage(choice);
    window.dispatchEvent(new CustomEvent("vibesynergy-language", { detail: choice }));
    const status = view.querySelector("[data-appearance-status]");
    try {
      await post("prefs", { changes: { language: choice } });
      if (status) status.textContent = t("Saved.");
    } catch {
      // Logged out / share / offline: localStorage still applies the switch.
      if (status) status.textContent = t("Saved.");
    }
  });
  const paletteSelect = view.querySelector('[data-section="appearance"] [name=palette]');
  paletteSelect?.addEventListener("change", async (event) => {
    persistPalette(event.target.value);
    window.dispatchEvent(new Event("vibesynergy-theme"));
    const status = view.querySelector("[data-appearance-status]");
    try {
      // Every device follows the choice; this device already switched.
      await post("prefs", { changes: { palette: event.target.value } });
      if (status) status.textContent = t("Saved.");
    } catch {
      if (status) status.textContent = t("Saved on this device only.");
    }
  });
  const themeSelect = view.querySelector('[data-section="appearance"] [name=theme]');
  themeSelect?.addEventListener("change", (event) => {
    persistTheme(event.target.value);
    window.dispatchEvent(new Event("vibesynergy-theme"));
  });
}





// Pieces ------------------------------------------------------------------------------








function restartHere(settings, theme) {
  const tabs = settings.restart_tabs;
  return Array.isArray(tabs) ? tabs.includes(theme) : Boolean(settings.restart_needed);
}

function restartBanner(canRestart) {
  return `<div class="notice settings-restart">${t("Saved settings apply after the service restarts.")}
    ${canRestart ? `<button class="btn primary" type="button" data-restart>${t("Restart now")}</button>` : t("Restart the service on this server to apply them.")}</div>`;
}


const SERVER_MESSAGES = [
  N("Use at least 12 characters."),
  N("The current passphrase does not match."),
  N("Choose 30, 90 or 365 days."),
  N("A name is required."),
  N("That name is already used."),
  N("No active token with that id."),
  N("No active app with that id."),
  N("No app with that id."),
  N("No token with that id."),
  N("The two entries differ. Nothing was saved."),
  N("This install has no config file yet."),
];

// Saving ------------------------------------------------------------------------------

function numberOrNull(value) {
  return value === "" ? null : Number(value);
}

function changesFor(section) {
  const kind = section.dataset.section;
  const field = (name) => section.querySelector(`[name=${name}]`);
  if (kind === "user") {
    return {
      user: {
        timezone: field("timezone").value.trim(),
        units: field("units").value,
        temperature: field("temperature").value,
        wind: field("wind").value,
        load_source: field("load_source").value,
      },
    };
  }
  if (kind === "source") {
    const id = section.dataset.source;
    const datasets = [...section.querySelectorAll("[name=dataset]:checked")].map((c) => c.value);
    return { sources: { [id]: { enabled: field("enabled").checked, datasets } } };
  }
  if (kind === "schedule") {
    const schedule = {};
    for (const input of section.querySelectorAll("[data-interval]")) {
      schedule[input.dataset.interval] = numberOrNull(input.value);
    }
    return { schedule };
  }
  if (kind === "dropfolder") {
    const value = field("dir").value.trim();
    return { dropfolder: { dir: value === "" ? null : value } };
  }
  if (kind === "mail" || kind === "alerts") {
    const keys = kind === "mail"
      ? ["smtp_host", "smtp_port", "smtp_security", "username", "from", "imap_host", "imap_port", "sent_folder"]
      : ["email_to"];
    const out = {};
    for (const key of keys) {
      const el = field(key);
      const value = el.value.trim();
      out[key] = value === "" ? null : el.type === "number" ? Number(value) : value;
    }
    if (kind === "alerts") out.enabled = field("enabled").checked;
    return { [kind]: out };
  }
  if (kind === "portal") {
    return { portal: { tile_cache: field("tile_cache").checked, tile_max_age_days: numberOrNull(field("tile_max_age_days").value) } };
  }
  return {};
}

const STRAVA_CONNECT_RESULTS = {
  connected: "Strava connected with write access.",
  denied: "Strava access was not approved.",
  write_missing: "Strava did not grant write access; try again and keep 'Upload your activities' checked.",
  expired: "The sign-in link expired; try again.",
  failed: "Strava connection failed; try again.",
};

function showStravaConnectResult(view, query) {
  const result = query?.get("strava");
  if (!result || !STRAVA_CONNECT_RESULTS[result]) return;
  const status = view.querySelector('[data-source="strava"] [data-connect-status]');
  if (status) status.textContent = t(STRAVA_CONNECT_RESULTS[result]);
  const [path, search = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const params = new URLSearchParams(search);
  params.delete("strava");
  const next = params.toString();
  history.replaceState(null, "", `#/${path}${next ? `?${next}` : ""}`);
}

const OPENER_TOKEN = /^\[\/\*.*\]$/;
const CLOSER_TOKEN = /^\[.*\*\/\]$/;
const LABEL_FORBIDDEN = /[\[\]:]|\n/;

function publicationFieldError(section, field) {
  const raw = section.querySelector(`[name=${field}]`)?.value ?? "";
  const value = raw.trim();
  if (!value) return null;
  if (field === "opener") {
    if (value.length > 100) return t("Opening line must be at most 100 characters.");
    if (
      !value.startsWith("[/*")
      || !value.endsWith("]")
      || value.endsWith("*/]")
      || CLOSER_TOKEN.test(value)
      || !OPENER_TOKEN.test(value)
    ) {
      return t("Opening line must start with [/* and end with ], on one line.");
    }
    return null;
  }
  if (field === "closer") {
    if (value.length > 100) return t("Closing line must be at most 100 characters.");
    if (!value.startsWith("[") || !value.endsWith("*/]") || !CLOSER_TOKEN.test(value)) {
      return t("Closing line must start with [ and end with */], on one line.");
    }
    if (OPENER_TOKEN.test(value)) {
      return t("Closing line must not use the same shape as the opening line.");
    }
    return null;
  }
  if (field.startsWith("label_")) {
    if (value.length > 40) return t("Section name must be at most 40 characters.");
    if (LABEL_FORBIDDEN.test(value)) {
      return t("Section name must not contain [, ], : or a line break.");
    }
  }
  return null;
}

function validatePublication(publication) {
  for (const field of ["opener", "closer", "label_stats", "label_quote"]) {
    const message = publicationFieldError(publication, field);
    if (message) return message;
  }
  return null;
}

function publicationChanges(publication) {
  const has = (name) => publication.querySelector(`[name=${name}]`);
  const text = (name) => has(name).value.trim();
  const pub = {};
  if (has("quote_enabled")) pub.quote_enabled = has("quote_enabled").checked;
  if (has("weather_enabled")) pub.weather_enabled = has("weather_enabled").checked;
  if (has("garmin_mirror_enabled")) pub.garmin_mirror_enabled = has("garmin_mirror_enabled").checked;
  if (has("icu_description_enabled")) pub.icu_description_enabled = has("icu_description_enabled").checked;
  if (has("auto_publish")) pub.auto_publish = has("auto_publish").checked;
  if (has("publication_language")) pub.language = has("publication_language").value;
  if (has("quote_theme")) {
    pub.quote_themes = [...publication.querySelectorAll("[name=quote_theme]:checked")].map((item) => item.value);
    pub.quote_sources = [...publication.querySelectorAll("[name=quote_source]:checked")].map((item) => item.value);
    pub.quote_providers = [...publication.querySelectorAll("[name=quote_provider]:checked")].map((item) => item.value);
  }
  for (const name of ["opener", "closer", "label_stats", "label_quote"]) {
    if (has(name)) pub[name] = text(name);
  }
  return { publication: pub };
}

function bind(view, ctx) {
  const { s, stale } = ctx;
  view.querySelectorAll("[data-publication]").forEach((publication) => {
    publication.querySelectorAll("input:not(:disabled), select").forEach((input) => input.addEventListener("change", async () => {
      const status = publication.querySelector("[data-publication-status]");
      const invalid = validatePublication(publication);
      if (invalid) {
        status.textContent = invalid;
        status.classList.add("error-text");
        return;
      }
      const changes = publicationChanges(publication);
      status.textContent = t("Saving…");
      status.classList.remove("error-text");
      try { await post("settings", { changes }); status.textContent = t("Saved."); }
      catch { reportSaveError(status, () => input.dispatchEvent(new Event("change"))); }
    }));
  });
  view.querySelectorAll("[name=garmin_delivery]").forEach((input) => {
    input.addEventListener("change", async () => {
      const box = input.closest("fieldset");
      const status = box.querySelector("[data-delivery-status]");
      const chosen = [...box.querySelectorAll("[name=garmin_delivery]:checked")].map((x) => x.value);
      status.textContent = t("Saving…");
      status.classList.remove("error-text");
      try {
        await post("prefs", { changes: { garmin_delivery: chosen.length ? chosen : null } });
        status.textContent = t("Saved. Garmin is updated within a few minutes.");
      } catch (error) {
        status.textContent = t("Not saved: {error}", { error: error.message });
        status.classList.add("error-text");
      }
    });
  });
  view.querySelectorAll("[name=icu_private_notes]").forEach((input) => {
    const status = input.closest("section")?.querySelector("[data-icu-notes-status]");
    if (input.checked && status) icuNotesCheck(status);
    input.addEventListener("change", async () => {
      if (status) status.textContent = t("Saving…");
      try {
        await post("prefs", { changes: { "intervals_icu.private_notes": input.checked } });
        if (!status) return;
        status.classList.remove("error-text");
        if (input.checked) icuNotesCheck(status);
        else status.textContent = t("Saved. Notes stay in VibeSynergy.");
      } catch (error) {
        if (!status) return;
        status.textContent = t("Not saved: {error}", { error: error.message });
        status.classList.add("error-text");
      }
    });
  });
  bindIcuConnect(view);
  view.querySelectorAll("[data-source-connect]").forEach((box) => {
    const status = box.querySelector("[data-connect-status]");
    box.querySelector("[data-disconnect]")?.addEventListener("click", async (event) => {
      const source = event.currentTarget.dataset.disconnect;
      if (!window.confirm(t("Disconnect {source}? You can reconnect in Sources.", {source}))) return;
      try {
        await post(`sources/${source}/disconnect`);
        status.textContent = t("Disconnected. Reload Sources to connect again.");
      } catch (error) { status.textContent = error.message; }
    });
    box.querySelector("[data-connect-strava]")?.addEventListener("click", async (event) => {
      const consentTab = window.open("about:blank", "_blank");
      event.currentTarget.disabled = true;
      status.textContent = t("Opening Strava sign-in…");
      try {
        const client_id = box.querySelector("[name=client_id]")?.value || null;
        const client_secret = box.querySelector("[name=client_secret]")?.value || null;
        const reply = await post("sources/strava/connect", {
          client_id,
          client_secret,
          portal: location.origin + location.pathname,
        });
        if (consentTab) { consentTab.opener = null; consentTab.location.href = reply.data.url; }
        else window.location.assign(reply.data.url);
        status.textContent = t("Approve access in Strava; you return here when done.");
      } catch (error) { consentTab?.close(); status.textContent = error.message; event.currentTarget.disabled = false; }
    });
    box.querySelector("[data-connect-garmin]")?.addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      status.textContent = t("Connecting Garmin…");
      try {
        const reply = await post("sources/garmin/connect", {
          email: box.querySelector("[name=garmin_email]").value,
          password: box.querySelector("[name=garmin_password]").value,
        });
        box.querySelector("[name=garmin_password]").value = "";
        const session = reply.data.session;
        box.querySelector("[data-submit-mfa]").addEventListener("click", async () => {
          try {
            await post("sources/garmin/mfa", {session, code: box.querySelector("[name=garmin_code]").value});
            box.querySelector("[name=garmin_code]").value = "";
            box.querySelector("[data-garmin-mfa]").hidden = true;
            status.textContent = t("Connecting Garmin…");
          } catch (error) { status.textContent = error.message; }
        });
        for (let i = 0; i < 300 && !stale(); i++) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const state = (await get(`sources/garmin/connect/${session}`)).data.state;
          if (state === "mfa") { box.querySelector("[data-garmin-mfa]").hidden = false; status.textContent = t("Enter the Garmin sign-in code."); }
          if (state === "connected") { status.textContent = t("Garmin connected. Reload Sources to see access."); break; }
          if (state === "failed") { status.textContent = t("Garmin sign-in failed. Check your details and try again."); break; }
        }
      } catch (error) { status.textContent = error.message; }
      event.currentTarget.disabled = false;
    });
  });
  const syncRestartBanner = (settings) => {
    const banner = view.querySelector(".settings-restart");
    if (restartHere(settings, ctx.theme) && !banner) {
      view.querySelector(".page-head")?.insertAdjacentHTML("afterend", restartBanner(settings.can_restart));
      view.querySelector("[data-restart]")?.addEventListener("click", restartHandler);
    } else if (!restartHere(settings, ctx.theme) && banner) {
      banner.remove();
    }
  };
  const restartHandler = async (event) => {
    event.target.disabled = true;
    event.target.textContent = t("Restarting…");
    await post("settings/restart");
    for (let i = 0; i < 30; i++) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        const reply = await get("settings");
        if (!reply.data.restart_needed) {
          location.reload();
          return;
        }
      } catch {
        // still restarting
      }
    }
    event.target.textContent = t("Still restarting; reload the page in a moment");
  };
  for (const section of view.querySelectorAll(".settings-section[data-section]")) {
    const status = section.querySelector("[data-status]");
    if (!status) continue;
    bindAutosave(section, {
      statusEl: status,
      save: async () => {
        const reply = await post("settings", { changes: changesFor(section) });
        if (stale()) return;
        ctx.s = reply.data;
        syncRestartBanner(reply.data);
        syncEmailState(view, reply.data);
      },
    });
  }
  view.querySelectorAll("[data-secret]").forEach((button) =>
    button.addEventListener("click", async () => {
      const section = button.closest(".settings-section");
      const input = section.querySelector("[name=password]");
      const status = button.parentElement.querySelector("[data-secret-status]");
      try {
        const reply = await post(button.dataset.secret, { value: input.value });
        if (stale()) return;
        draw(view, { ...ctx, s: reply.data }, t("Saved on this server."));
      } catch (error) {
        status.textContent = error.message;
        status.classList.add("error-text");
      }
    }),
  );
  const mailSection = view.querySelector('.settings-section[data-section="mail"]');
  const mailPassword = mailSection?.querySelector("[name=password]");
  const mailSecretStatus = mailSection?.querySelector("[data-secret-status]");
  mailPassword?.addEventListener("focusout", async () => {
    if (!mailPassword.value.trim()) return;
    try {
      const reply = await post("mail/password", { value: mailPassword.value });
      mailPassword.value = "";
      if (stale()) return;
      ctx.s = reply.data;
      syncEmailState(view, reply.data);
      mailSecretStatus.textContent = t("Saved.");
    } catch (error) {
      mailSecretStatus.textContent = t("Not saved: {error}", { error: error.message });
      mailSecretStatus.classList.add("error-text");
    }
  });
  view.querySelector("[data-test-alert]")?.addEventListener("click", async (event) => {
    const status = event.target.parentElement.querySelector("[data-test-status]");
    status.textContent = t("Sending…");
    try {
      const reply = await post("alerts/test");
      const r = reply.data;
      status.textContent = [r.sent.length ? t("Sent by {channels}.", { channels: r.sent.join(` ${t("and")} `) }) : "", ...r.errors].filter(Boolean).join(" ");
      status.classList.toggle("error-text", r.errors.length > 0);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error-text");
    }
  });
  view.querySelectorAll("[data-revoke]").forEach((button) =>
    button.addEventListener("click", async () => {
      if (!window.confirm(t("Revoke {name}? It loses access to VibeSynergy at once.", { name: button.dataset.name }))) return;
      const reply = await post("mcp_clients/revoke", { client_id: button.dataset.revoke });
      if (stale()) return;
      draw(view, { ...ctx, clients: reply.data.clients, access: reply.data }, t("Revoked {name}.", { name: button.dataset.name }));
    }),
  );
  view.querySelector("[data-restart]")?.addEventListener("click", restartHandler);
  view.querySelectorAll("[data-sync-now]").forEach((button) => {
    button.addEventListener("click", async () => {
      const status = button.closest("section")?.querySelector("[data-sync-status]");
      const today = ctx.meta?.data?.today;
      if (status) status.textContent = t("Asking for new data…");
      try {
        await post("resync", { source: button.dataset.syncNow, start: today, end: today });
        if (status) status.textContent = t("On its way. New data shows up within a few minutes.");
      } catch {
        if (status) reportSaveError(status, () => button.click());
      }
    });
  });
  bindAlertGroups(view, ctx);
  bindPortalHosts(view, ctx, draw);
  mountAbout(view.querySelector("[data-about-host]"), { open: ctx.query?.get?.("section") === "about" });
  mountAccount(view.querySelector("[data-account-host]"));
  mountPush(view.querySelector("[data-push]"));
}


