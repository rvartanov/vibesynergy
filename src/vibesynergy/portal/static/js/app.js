// Portal shell: hash routes (#/dashboard, #/dashboard/2026-09-25, …), theme switch, footer.
import { get, invalidateMeta, meta, setMetaMaxAge } from "./api.js";
import { calendarDay, shouldAutoRefresh } from "./refresh_policy.js";
import { bindBugReport } from "./bugreport.js";
import { ago, esc } from "./format.js";
import { versionLinkHtml } from "./about_version.js";
import { setLanguage, storedLanguage, t } from "./i18n.js";
import { REDIRECTS, START_PAGES, bindNav, closeMenus, drawNav, limitTo, loadPrefs, markCurrent } from "./nav.js";
import { applyPalette, applyTheme, persistPalette, storedPalette, storedTheme } from "./theme.js";
const PAGE_LOADERS = {
  dashboard: () => import("./pages/dashboard.js"),
  activities: () => import("./pages/activities.js"),
  activity: () => import("./pages/activity.js"),
  metrics: () => import("./pages/metrics.js"),
  planning: () => import("./pages/planning.js"),
  schedule: () => import("./pages/schedule.js"),
  zones: () => import("./pages/zones.js"),
  inbox: () => import("./pages/inbox.js"),
  search: () => import("./pages/search.js"),
  gear: () => import("./pages/gear.js"),
  coach: () => import("./pages/coach.js"),
  recovery: () => import("./pages/recovery.js"),
  injuries: () => import("./pages/injuries.js"),
  sleep: () => import("./pages/sleep.js"),
  body: () => import("./pages/body.js"),
  correlations: () => import("./pages/correlations.js"),
  sql: () => import("./pages/sql.js"),
  settings: () => import("./pages/settings.js"),
  data: () => import("./pages/data.js"),
  sources: () => import("./pages/sources.js"),
  history: () => import("./pages/history.js"),
};
const CHART_PAGES = new Set([
  "dashboard", "metrics", "sleep", "sql", "planning", "activity", "body", "correlations",
]);
let chartsLoaded = false;

async function disposeCharts() {
  if (!chartsLoaded) return;
  const { disposeAll } = await import("./charts.js");
  disposeAll();
}

const view = document.getElementById("view");
document.querySelector(".skip-main").addEventListener("click", () => view.focus());
// Only wide tables enter the tab order; their own scroll area stays reachable at zoom.
// Measure after layout: a mutation can fire before the table has its final width.
function prepareScrollableTables() {
  for (const wrap of view.querySelectorAll(".table-wrap")) {
    // A single pixel still shows a scrollbar. Axe then treats the region as scrollable.
    if (wrap.scrollWidth > wrap.clientWidth) {
      wrap.tabIndex = 0;
      if (!wrap.getAttribute("aria-label")) {
        wrap.setAttribute(
          "aria-label",
          wrap.closest(".card, section")?.querySelector("h2, h3")?.textContent || t("Data table"),
        );
      }
    } else {
      wrap.removeAttribute("tabindex");
      wrap.removeAttribute("aria-label");
    }
  }
}
let scrollFrame = 0;
function scheduleScrollableTables() {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0;
    prepareScrollableTables();
  });
}
new MutationObserver(scheduleScrollableTables).observe(view, { childList: true, subtree: true });
new ResizeObserver(scheduleScrollableTables).observe(view);
window.addEventListener("resize", scheduleScrollableTables);
const header = document.querySelector("header.top");
const nav = header.querySelector("nav");
let renderId = 0;

// A page module that can't be imported usually means a deploy replaced the files of this
// open tab: reload once to get the new version. Not twice within a minute, so a real outage
// shows its error instead of looping.
function reloadAfterDeploy(error) {
  const text = String(error?.message || error);
  if (!/import|module script|dynamically imported/i.test(text)) return false;
  try {
    const last = Number(sessionStorage.getItem("vibesynergy-module-reload") || 0);
    if (Date.now() - last < 60_000) return false;
    sessionStorage.setItem("vibesynergy-module-reload", String(Date.now()));
  } catch {
    return false;
  }
  window.location.reload();
  return true;
}

// Check the current deployment without replacing an open form or moving keyboard focus.
const loadedBuild = document.documentElement.dataset.buildId;
async function checkVersion() {
  if (!loadedBuild || document.hidden || document.querySelector(".version-notice")) return;
  try {
    const response = await fetch("version", { cache: "no-store" });
    if (!response.ok || (await response.json()).build_id === loadedBuild) return;
    const notice = document.createElement("div");
    notice.className = "notice version-notice";
    notice.setAttribute("role", "status");
    notice.setAttribute("aria-live", "polite");
    const message = document.createElement("span");
    message.textContent = t("New version available");
    const reload = document.createElement("button");
    reload.type = "button";
    reload.className = "btn primary";
    reload.textContent = t("Reload");
    reload.addEventListener("click", () => window.location.reload());
    notice.append(message, reload);
    header.after(notice);
  } catch {
    // An offline browser can keep using the open page and check again later.
  }
}
setInterval(checkVersion, 3 * 60_000);
document.addEventListener("visibilitychange", checkVersion);
window.addEventListener("online", checkVersion);

// "#/activities?sport=cycling" -> {name: "activities", args: [], query: URLSearchParams}
function route() {
  const [path, search = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const [name = "dashboard", ...args] = path.split("/").filter(Boolean);
  return { name, args: args.map(decodeURIComponent), query: new URLSearchParams(search) };
}

// Behind a share link: only the link's pages are in the menu, and a banner says whose view
// this is. The server enforces the same limits; this only keeps the page honest.
let sharedPages = null;
// The offline page: only in the owner's own portal at the root, never in a shared
// view. A failed registration changes nothing about the page.
let offlineRegistered = false;
function registerOffline(isShared) {
  if (isShared || !("serviceWorker" in navigator) || location.pathname !== "/") return;
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}

function applyShare(link) {
  if (!link || sharedPages) return;
  sharedPages = new Set(link.pages);
  const also = { today: "dashboard", day: "dashboard", diary: "dashboard", calendar: "dashboard", data: "history", history: "data", training: "planning", plan: "planning", knowledge: "coach" };
  limitTo([...link.pages, ...link.pages.map((p) => also[p]).filter(Boolean)]);
  drawNav(nav);
  header.querySelectorAll(".gear, .inbox-link").forEach((el) => el.remove()); // settings, inbox
  document.querySelector(".foot-actions")?.remove(); // reporting a bug is the owner's
  document.querySelector(".brand").setAttribute("href", `#/${also[link.pages[0]] === "dashboard" ? "dashboard" : link.pages[0]}`);
  const banner = document.createElement("div");
  banner.className = "share-banner";
  banner.textContent = link.can_write
    ? t("Shared view for {name} · read and change", { name: link.title })
    : t("Shared view for {name} · read only", { name: link.title });
  header.after(banner);
}

// The menu's order changed in Settings: redraw it.
window.addEventListener("vibesynergy-nav", () => {
  drawNav(nav);
  markCurrent(nav, route().name);
});

// Pages that count as another page for a share link.
const SAME_PAGE = { activity: "activities", history: "history", data: "data", sources: "sources" };
// Older share links name Training or Training plan; both open Planning & progress.
const PLANNING_SHARES = ["planning", "training", "plan"];

/** Map the current hash route to the share-link page id (after redirects). */
function routeSharePage(name, args = []) {
  if (name === "settings" && (args[0] === "data" || args[0] === "sources")) {
    const sub = args[0] === "sources" ? "sources" : args[1] || "sources";
    if (sub === "sync") return "data";
    if (sub === "history") return "history";
    return "sources";
  }
  if (name === "data" || name === "history" || name === "sources") return name;
  // Coach → Knowledge is the old Knowledge page, so a link that shares it still opens it.
  if (name === "coach" && args[0] === "knowledge") return "knowledge";
  return SAME_PAGE[name] || name;
}

function shareDataEquivalent(page) {
  return (
    sharedPages.has(page)
    || (page === "data" && sharedPages.has("history"))
    || (page === "history" && sharedPages.has("data"))
  );
}

function shareAllows(name, args = []) {
  if (!sharedPages) return true;
  const page = routeSharePage(name, args);
  if (page === "sources") return sharedPages.has("sources");
  if (page === "data" || page === "history") return shareDataEquivalent(page);
  if (page === "settings") return false;
  // Coach shares every tab but memory and settings; Knowledge alone shares that tab.
  if (page === "knowledge") return sharedPages.has("knowledge") || sharedPages.has("coach");
  if (page === "coach") return sharedPages.has("coach") && !["memory", "settings"].includes(args[0]);
  if (page === "planning") return PLANNING_SHARES.some((id) => sharedPages.has(id));
  // The start page shows the sections a link allows (its old pages: day, diary, calendar).
  if (page === "dashboard") return START_PAGES.some((id) => sharedPages.has(id));
  return sharedPages.has(page);
}

function applyRedirects() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, search = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  const name = parts[0] || "dashboard";
  // Settings → Coaching moved to the Coach page's Settings tab.
  if (name === "settings" && parts[1] === "coach") {
    history.replaceState(null, "", `#/coach/memory${search ? `?${search}` : ""}`);
    return true;
  }
  // Settings → You split up: the athlete's values went to Training → Zones &
  // thresholds, time zone and units to Settings → System.
  if (name === "settings" && (parts[1] === "you" || parts[1] === "profile")) {
    history.replaceState(null, "", `#/zones${search ? `?${search}` : ""}`);
    return true;
  }
  if (!parts.length || !Object.hasOwn(REDIRECTS, name)) return false;
  if (parts.length > 1 && name !== "day" && name !== "diary" && name !== "knowledge") return false;
  const query = search ? `?${search}` : "";
  history.replaceState(null, "", `#/${REDIRECTS[name](query, parts.slice(1))}`);
  return true;
}

function autoRefreshPage() {
  const { name, args } = route();
  if (name === "settings" && args[0] === "data") {
    const sub = args[1] || "sources";
    if (sub === "sync") return "data";
    if (sub === "history") return "history";
  }
  if (name === "data") return "data";
  if (name === "history") return "history";
  return SAME_PAGE[name] || name;
}

async function render() {
  if (applyRedirects()) return render();
  let { name, args, query } = route();
  const id = ++renderId;
  await disposeCharts();
  try {
    const snapshot = await meta();
    noteServerClock(snapshot);
    applyShare(snapshot.data.share);
    if (!offlineRegistered) {
      offlineRegistered = true;
      registerOffline(Boolean(snapshot.data.share));
    }
  } catch {
    // the page itself reports load errors
  }
  if (sharedPages && !location.hash) {
    const first = [...sharedPages][0];
    name = START_PAGES.includes(first) ? "dashboard" : first; // no Today in this view
  }
  if (!shareAllows(name, args)) {
    view.innerHTML = `<div class="card empty">${esc(t("This page isn't part of this shared view."))}</div>`;
    document.title = "VibeSynergy";
    return;
  }
  closeMenus(nav);
  markCurrent(nav, name);
  const searchLink = header.querySelector("a.search-link");
  if (searchLink && name === "search") searchLink.setAttribute("aria-current", "page");
  else if (searchLink) searchLink.removeAttribute("aria-current");
  const inboxLink = header.querySelector("a.inbox-link:not(.search-link)");
  if (inboxLink) {
    if (name === "inbox") inboxLink.setAttribute("aria-current", "page");
    else inboxLink.removeAttribute("aria-current");
    refreshInboxCount();
  }
  const gear = header.querySelector("a.gear");
  if (gear && (name === "settings" || name === "sources" || name === "data" || name === "history")) gear.setAttribute("aria-current", "page");
  else if (gear) gear.removeAttribute("aria-current");
  const loader = PAGE_LOADERS[name];
  if (!loader) {
    view.innerHTML = `<div class="card empty">${esc(t("Page not found."))} <a href="#/dashboard">${esc(t("Go to the Dashboard"))}</a></div>`;
    document.title = "VibeSynergy";
    return;
  }
  const again = view.dataset.page === name; // a reload of the same page: keep what's shown until the new one is ready
  view.dataset.page = name;
  if (!again) view.innerHTML = `<div class="loading">${esc(t("Loading…"))}</div>`;
  let rendered = false;
  try {
    const page = await loader();
    if (id !== renderId) return;
    if (CHART_PAGES.has(name)) chartsLoaded = true;
    const m = await meta();
    noteServerClock(m);
    // A newer navigation started while this one was loading: drop this one.
    if (name === "settings" && args[0] === "sources") {
      await page.render(view, { args: ["data", "sources"], query, meta: m, stale: () => id !== renderId });
    } else {
      await page.render(view, { args, query, meta: m, stale: () => id !== renderId });
    }
    rendered = true;
  } catch (error) {
    if (id !== renderId) return;
    if (reloadAfterDeploy(error)) return;
    if (again && !view.querySelector(".loading")) {
      let failure = view.querySelector("[data-auto-refresh-error]");
      if (!failure) {
        failure = document.createElement("p");
        failure.dataset.autoRefreshError = "";
        failure.className = "notice error";
        failure.setAttribute("role", "alert");
        view.prepend(failure);
      }
      failure.textContent = t("Refresh failed: {error}. The previous result is still shown.", { error: error.message });
    } else {
      view.innerHTML = `<div class="notice error" role="alert">${esc(t("Could not load this page: {error}", { error: error.message }))}</div>`;
    }
  }
  if (id !== renderId) return;
  if (rendered) renderedAt = Date.now();
  if (!again) document.getElementById("route-status").textContent = view.querySelector("h1")?.textContent || document.title;
  footer();
  alertBanner();
}

// Alerts routed to the portal: a banner under the header with what's open and the events of
// the last day. Each can be dismissed (remembered on this device).
const DISMISSED = "vibesynergy-dismissed-alerts";
const DISMISS_MS = 24 * 3600 * 1000;

function dismissedKeys() {
  try {
    const raw = JSON.parse(localStorage.getItem(DISMISSED) || "[]");
    const now = Date.now();
    const keys = new Set();
    for (const entry of raw) {
      if (typeof entry === "string") keys.add(entry);
      else if (entry?.key && now - entry.at < DISMISS_MS) keys.add(entry.key);
    }
    return keys;
  } catch {
    return new Set();
  }
}

function rememberDismiss(key) {
  try {
    const raw = JSON.parse(localStorage.getItem(DISMISSED) || "[]");
    const kept = raw.filter((entry) => (typeof entry === "string" ? entry : entry?.key) !== key);
    kept.push({ key, at: Date.now() });
    localStorage.setItem(DISMISSED, JSON.stringify(kept.slice(-200)));
  } catch {
    // private window: dismissed until the next check
  }
}

function jobChipLabel(alert) {
  if (alert.link?.includes("calendar")) {
    return t("Calendar sync problem · Fix");
  }
  return t("Sync problem · Fix");
}

async function alertBanner() {
  if (sharedPages) return;
  let data;
  try {
    data = (await get("alerts")).data;
  } catch {
    return;
  }
  const gone = dismissedKeys();
  const dayAgo = Date.now() - 24 * 3600 * 1000;
  const items = [
    ...data.open.filter((a) => a.level !== "info"),
    ...data.recent.filter((e) => Date.parse(e.at) > dayAgo),
  ].filter(
    (a) => !gone.has(a.key) && !String(a.key).startsWith("daily:") && a.kind !== "daily_coach",
  );
  const jobItems = items.filter((a) => a.kind === "job");
  const warnNotices = items.filter((a) => a.kind !== "job" && a.level === "warn");
  const bannerItems = items.filter((a) => a.kind !== "job" && a.level === "stop");
  let chips = header.querySelector(".header-sync-chips");
  if (!jobItems.length) chips?.remove();
  else {
    if (!chips) {
      chips = document.createElement("div");
      chips.className = "header-sync-chips";
      chips.setAttribute("role", "status");
      chips.setAttribute("aria-live", "polite");
      header.querySelector(".brand")?.after(chips);
      chips.addEventListener("click", (event) => {
        const button = event.target.closest("[data-dismiss]");
        if (!button) return;
        rememberDismiss(button.dataset.dismiss);
        alertBanner();
      });
    }
    chips.innerHTML = jobItems
      .slice(0, 2)
      .map(
        (a) => `<span class="header-sync-chip warn">
          <a href="${esc(a.link || "#/settings/data/sources")}">${esc(jobChipLabel(a))}</a>
          <button type="button" class="btn small" data-dismiss="${esc(a.key)}" aria-label="${esc(t("Dismiss"))}">×</button>
        </span>`,
      )
      .join("");
  }
  let notices = header.querySelector(".header-warn-notices");
  if (!warnNotices.length) notices?.remove();
  else {
    if (!notices) {
      notices = document.createElement("div");
      notices.className = "header-warn-notices";
      notices.setAttribute("role", "status");
      notices.setAttribute("aria-live", "polite");
      (chips || header.querySelector(".brand"))?.after(notices);
      notices.addEventListener("click", (event) => {
        const button = event.target.closest("[data-dismiss]");
        if (!button) return;
        rememberDismiss(button.dataset.dismiss);
        alertBanner();
      });
    }
    notices.innerHTML = warnNotices
      .slice(0, 2)
      .map(
        (a) => `<span class="header-sync-chip warn">
          ${a.link ? `<a href="${esc(a.link)}">${esc(a.text)}</a>` : `<span>${esc(a.text)}</span>`}
          <button type="button" class="btn small" data-dismiss="${esc(a.key)}" aria-label="${esc(t("Dismiss"))}">×</button>
        </span>`,
      )
      .join("");
  }
  let banner = document.querySelector(".alert-banner");
  if (!bannerItems.length) {
    banner?.remove();
    return;
  }
  if (!banner) {
    banner = document.createElement("div");
    banner.className = "alert-banner";
    banner.setAttribute("role", "alert");
    header.after(banner);
    banner.addEventListener("click", (event) => {
      const button = event.target.closest("[data-dismiss]");
      if (!button) return;
      rememberDismiss(button.dataset.dismiss);
      alertBanner();
    });
  }
  banner.innerHTML = bannerItems
    .slice(0, 3)
    .map(
      (a) => `<div class="alert-item ${esc(a.level)}"><div>${a.link ? `<a href="${esc(a.link)}">${esc(a.text)}</a>` : esc(a.text)}</div>
        <button type="button" class="btn small" data-dismiss="${esc(a.key)}" aria-label="${esc(t("Dismiss"))}">×</button></div>`,
    )
    .join("");
}
window.addEventListener("vibesynergy-alerts", alertBanner);

async function footer() {
  try {
    const m = await meta();
    const latest = Object.values(m.meta.freshness).filter(Boolean).sort().pop();
    // The running version links to Settings → System → About this version.
    const info = await get("about").then((reply) => reply.data).catch(() => null);
    document.getElementById("foot").innerHTML =
      // the old Sources and Settings links had no use here; who made it,
      // the licence and how to reach us do.
      `<p>${esc(latest ? t("Data last updated {when}", { when: ago(latest) }) : t("No data yet"))}${info ? ` · ${versionLinkHtml(info)}` : ""}</p>` +
      `<p>© 2026 VibeSynergy · ${esc(t("Open source under the MIT licence"))} · <a href="mailto:info@vibesynergy.nl">info@vibesynergy.nl</a></p>`;
  } catch {
    // the page itself reports load errors
  }
}

// Theme: follow the system unless switched under Settings → System; the choice is
// remembered on this device.
applyTheme(storedTheme());
applyPalette(storedPalette());
window.addEventListener("vibesynergy-theme", render);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);

// Pages that show data reload by themselves while open (Settings -> Appearance; default
// every 5 minutes), unless the page is hidden or you are typing in it. Pages with forms
// never do.
const RELOADS = new Set(["dashboard", "activities", "activity", "planning", "sleep", "body", "metrics", "data", "history"]);
const REFRESH_POLL_MS = 15_000;
let refreshMin = 5;
let renderedAt = Date.now();
let serverToday = "";
let serverZone = "Europe/Amsterdam";

function noteServerClock(snapshot) {
  if (!snapshot?.data?.today) return;
  serverToday = snapshot.data.today;
  if (snapshot.meta?.timezone) serverZone = snapshot.meta.timezone;
}

function dayChanged(now = new Date()) {
  if (!serverToday) return false;
  try {
    return calendarDay(serverZone, now) !== serverToday;
  } catch {
    return false;
  }
}

async function loadRefresh() {
  try {
    const prefs = (await get("prefs")).data;
    refreshMin = prefs.refresh_min ?? 5;
  } catch {
    // shared view: the default
  }
  setMetaMaxAge(refreshMin ? Math.min(5 * 60_000, refreshMin * 60_000) : 5 * 60_000);
}
window.addEventListener("vibesynergy-prefs", loadRefresh);
let refreshing = false;
async function refreshPage() {
  if (refreshing) return;
  const changedDay = dayChanged();
  if (!shouldAutoRefresh({
    now: Date.now(),
    renderedAt,
    refreshMin,
    hidden: document.hidden,
    pageAllowed: RELOADS.has(autoRefreshPage()),
    dayChanged: changedDay,
    pollMs: REFRESH_POLL_MS,
  })) return;
  const active = document.activeElement;
  if (active && active !== document.body && view.contains(active) && active.matches("input, textarea, select")) return;
  if (header.classList.contains("menu-open") || nav.querySelector("button[aria-expanded=true]")) return;
  if (changedDay) invalidateMeta();
  const y = window.scrollY;
  refreshing = true;
  try {
    await render();
    window.scrollTo(0, y);
  } finally {
    refreshing = false;
  }
}
setInterval(refreshPage, REFRESH_POLL_MS);
// Timers are suspended on mobile/background tabs. Catch up as soon as the user returns.
document.addEventListener("visibilitychange", refreshPage);
window.addEventListener("pageshow", refreshPage);
window.addEventListener("online", refreshPage);

window.addEventListener("hashchange", render);
// Report a bug: in the footer of every page (and under Settings → System).
bindBugReport(document.querySelector(".foot .bug-report"));
window.vibesynergyStarted = true; // read by boot.js
loadRefresh();
bindNav(nav, header);
// Header buttons' names, in the chosen language.
// The number next to the bell: proposals that wait on you. Never blocks a page.
async function refreshInboxCount() {
  const link = header.querySelector("a.inbox-link:not(.search-link)");
  const badge = link?.querySelector("[data-inbox-count]");
  if (!badge) return;
  try {
    const { data } = await get("inbox/count");
    const n = Number(data.count) || 0;
    badge.hidden = n === 0;
    badge.textContent = n > 99 ? "99+" : String(n);
    const text = n ? t("Waiting for you: {n}", { n }) : t("Waiting for you");
    link.setAttribute("aria-label", text);
    link.setAttribute("title", text);
  } catch {
    badge.hidden = true;
  }
}

// Quick add: check-in, note or pain from any page, in a dialog with the quick log.
const quickDialog = document.querySelector(".quick-dialog");
header.querySelector(".quick-add")?.addEventListener("click", async () => {
  if (!quickDialog) return;
  const body = quickDialog.querySelector("[data-quick-body]");
  body.innerHTML = `<p class="sub" role="status">${esc(t("Loading…"))}</p>`;
  quickDialog.showModal();
  try {
    const [{ mountQuickLog }, info] = await Promise.all([import("./quicklog.js"), meta()]);
    const slot = document.createElement("div");
    body.replaceChildren(slot);
    await mountQuickLog(slot, info, () => !quickDialog.open, { variant: "page" });
    if (!slot.isConnected) body.innerHTML = `<p>${esc(t("This shared view can't add to the diary."))}</p>`;
  } catch (error) {
    body.innerHTML = `<p class="notice error" role="alert">${esc(t(error.message || "This part could not load right now."))}</p>`;
  }
});
quickDialog?.querySelector("[data-quick-close]")?.addEventListener("click", () => quickDialog.close());
quickDialog?.addEventListener("close", () => {
  // What was saved shows on the page you are on after the next refresh of its data.
  window.dispatchEvent(new CustomEvent("vibesynergy-diary-changed"));
});

// "/" opens Search from anywhere, unless you are typing in a field.
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target;
  if (target.closest?.("input, textarea, select, [contenteditable='true']")) return;
  if (!header.querySelector("a.search-link")) return; // a shared view has no search
  event.preventDefault();
  if (location.hash.startsWith("#/search")) document.querySelector("#search-q")?.focus();
  else location.hash = "#/search";
});

function labelHeader() {
  document.querySelector(".skip-main").textContent = t("Skip to content");
  const label = (selector, text) => {
    const el = header.querySelector(selector);
    if (!el) return;
    el.setAttribute("aria-label", text);
    el.setAttribute("title", text);
  };
  label("a.gear", t("Settings"));
  label("a.inbox-link:not(.search-link)", t("Waiting for you"));
  label("a.search-link", t("Search (/)"));
  label(".quick-add", t("Quick add"));
  const quickLabel = header.querySelector(".quick-add-label");
  if (quickLabel) quickLabel.textContent = t("Quick add");
  const bug = document.querySelector(".foot .bug-report");
  if (bug) {
    bug.textContent = t("Report a bug");
    bug.setAttribute("aria-label", t("Report a bug (opens GitHub)"));
    bug.setAttribute("title", t("Report a bug (opens GitHub)"));
  }
  const quickTitle = document.querySelector("#quick-title");
  if (quickTitle) quickTitle.textContent = t("Quick add");
  const quickClose = document.querySelector("[data-quick-close]");
  if (quickClose) quickClose.textContent = t("Close");
  label(".menu-toggle", t("Menu"));
  nav.setAttribute("aria-label", t("Pages"));
}

loadPrefs().then(async (prefs) => {
  const chosen = storedLanguage() || prefs.language || "auto";
  // A colour theme chosen on another device comes along; this device's own choice wins.
  if (!storedPalette() && prefs.palette) persistPalette(prefs.palette);
  await setLanguage(chosen);
  labelHeader();
  drawNav(nav);
  render();
  checkVersion();
});
// The language changed in Settings: everything again.
window.addEventListener("vibesynergy-language", async (event) => {
  await setLanguage(event.detail);
  const notice = document.querySelector(".version-notice");
  if (notice) {
    notice.querySelector("span").textContent = t("New version available");
    notice.querySelector("button").textContent = t("Reload");
  }
  labelHeader();
  drawNav(nav);
  render();
});
