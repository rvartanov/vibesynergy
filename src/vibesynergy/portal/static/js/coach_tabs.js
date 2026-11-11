// The Coach page's tabs and the page head every tab shares.
import { esc } from "./format.js";
import { N, t } from "./i18n.js";

export const COACH_TABS = [
  ["", N("Advice")],
  ["knowledge", N("Knowledge")],
  ["methods", N("Methods & rules")],
  ["memory", N("Memory")],
  ["tasks", N("Tasks")],
  ["log", N("Change log")],
  ["settings", N("Settings")],
];

// Behind a share link: with Coach shared, every tab but the owner's memory and settings
//; with only Knowledge shared, just that tab.
const OWNER_TABS = new Set(["memory", "settings"]);
export function coachTabsHtml(current, { share = null } = {}) {
  const coach = share?.pages?.includes("coach");
  const tabs = !share
    ? COACH_TABS
    : COACH_TABS.filter(([id]) => (coach ? !OWNER_TABS.has(id) : id === "knowledge"));
  if (tabs.length < 2) return "";
  return `<nav class="tabs settings-nav" aria-label="${esc(t("Coach"))}">${tabs
    .map(([id, label]) => `<a class="btn" href="#/coach${id ? `/${id}` : ""}"${id === current ? ' aria-current="page"' : ""}>${esc(t(label))}</a>`)
    .join("")}</nav>`;
}

export function coachTabLabel(id) {
  const found = COACH_TABS.find(([key]) => key === id);
  return found ? t(found[1]) : t("Coach");
}

// Head, tabs and an empty body for one tab. `heading` defaults to the tab's own name.
export function coachShell(view, tab, { heading, intro = "", share = null } = {}) {
  const title = heading || coachTabLabel(tab);
  document.title = `${title} · ${t("Coach")} · VibeSynergy`;
  view.innerHTML = `<div class="page-head settings-head"><div><p class="settings-caption">${esc(t("Coach"))}</p><h1>${esc(title)}</h1>${intro ? `<p class="sub">${esc(intro)}</p>` : ""}</div></div>
    ${coachTabsHtml(tab, { share })}
    <div data-coach-tab></div>`;
  return view.querySelector("[data-coach-tab]");
}
