// The menu: five sections with their pages, Settings behind the gear. The order of the
// sections and of the pages in them, and which pages are hidden, are kept on the server
// (api/prefs), so they are the same on the phone and the laptop. Routes don't change:
// #/sleep still opens Sleep, wherever it sits in the menu.
import { get, post } from "./api.js";
import { esc } from "./format.js";
import { N, t } from "./i18n.js";

export const SECTIONS = [
  {
    id: "today",
    label: N("Dashboard"),
    // One start page since Dashboard, Diary and Calendar became sections of it.
    pages: [["dashboard", N("Today")]],
  },
  { id: "training", label: N("Training"), pages: [["activities", N("Activities")], ["planning", N("Planning & progress")], ["schedule", N("Calendar")], ["zones", N("Fitness")], ["coach", N("Coach")]] },
  { id: "gear", label: N("Gear"), pages: [["gear", N("Gear")]] },
  { id: "health", label: N("Health"), pages: [["recovery", N("Recovery")], ["sleep", N("Sleep")], ["body", N("Body")], ["injuries", N("Complaints & injuries")]] },
  { id: "insight", label: N("Insight"), pages: [["metrics", N("Metrics")], ["correlations", N("Correlations")], ["sql", N("SQL")]] },
];

// Pages that belong to another page's menu entry.
export const PARENT = { activity: "activities" };

// Saved menu prefs may still list pages that moved into another section.
export const LEGACY_PAGE_SECTION = {};

function withSection(query, section) {
  const params = new URLSearchParams(query.replace(/^\?/, ""));
  params.set("section", section);
  return `?${params}`;
}

/** Top-level hash routes replaced on load (see app.js applyRedirects). `args` are the
 * parts after the name: #/day/2026-10-30 opens that day on the start page. */
export const REDIRECTS = {
  analysis: (query) => `metrics${query}`,
  // Training and Training plan became one page, Planning & progress.
  training: (query) => `planning/analysis${query}`,
  plan: (query) => `planning${query}`,
  today: (query) => `dashboard${query}`,
  day: (query, args = []) => (args[0] ? `dashboard/${args[0]}${query}` : `dashboard${query}`),
  diary: (query) => `dashboard${withSection(query, "diary")}`,
  calendar: (query) => `dashboard${withSection(query, "calendar")}`,
  data: (query) => `settings/data/sync${query}`,
  sources: (query) => `settings/data/sources${query}`,
  history: (query) => `settings/data/history${query}`,
  // Knowledge became a tab of the Coach page; article links keep working.
  knowledge: (query, args = []) =>
    `coach/knowledge${args.map((part) => `/${part}`).join("")}${query}`,
};

// Share-link pages that are now sections of the start page.
export const START_PAGES = ["dashboard", "today", "day", "diary", "calendar"];

let saved = {};
let allowed = null; // behind a share link: only its pages

// All preferences (the menu's, the language, …); {} in a shared view.
export async function loadPrefs() {
  let prefs = {};
  try {
    prefs = (await get("prefs")).data;
  } catch {
    // shared view (no prefs there) or an older server: the defaults
  }
  saved = prefs.nav || {};
  return prefs;
}

export function limitTo(pages) {
  allowed = new Set(pages);
}

function ordered(items, order, key) {
  const rank = new Map((order || []).map((id, i) => [id, i]));
  return [...items].sort((a, b) => (rank.get(key(a)) ?? 1e3) - (rank.get(key(b)) ?? 1e3));
}

const KNOWN_SECTIONS = new Set(SECTIONS.map((s) => s.id));
const KNOWN_PAGES = new Set(SECTIONS.flatMap((s) => s.pages.map((p) => p[0])));

function pageLabel(pageId) {
  for (const section of SECTIONS) {
    const found = section.pages.find(([id]) => id === pageId);
    if (found) return found;
  }
  return null;
}

function prefsListsPage(pageId, prefs) {
  return Object.values(prefs.pages || {}).some((list) => list?.includes(pageId));
}

function filterOrder(order, valid) {
  if (!order) return order;
  return order.filter((id) => valid.has(id));
}

// The sections in the saved order, each with its pages in order and a hidden flag.
export function layout(prefs = saved) {
  const hidden = new Set((prefs.hidden || []).filter((id) => id !== "diary"));
  const sectionOrder = filterOrder(prefs.sections, KNOWN_SECTIONS);
  return ordered(SECTIONS, sectionOrder, (s) => s.id).map((s) => {
    let pages = [...s.pages];
    for (const [pageId, sectionId] of Object.entries(LEGACY_PAGE_SECTION)) {
      if (sectionId !== s.id || pages.some(([id]) => id === pageId)) continue;
      if (!prefsListsPage(pageId, prefs)) continue;
      const def = pageLabel(pageId);
      if (def) pages.push(def);
    }
    const pageOrder = filterOrder(prefs.pages?.[s.id], KNOWN_PAGES);
    return {
      ...s,
      label: t(s.label),
      pages: ordered(pages, pageOrder, (p) => p[0]).map(([id, label]) => ({
        id,
        label: t(label),
        hidden: hidden.has(id),
      })),
    };
  });
}

function visible() {
  return layout()
    .map((s) => ({
      ...s,
      pages: s.pages.filter(
        (p) => !p.hidden && (!allowed || allowed.has(p.id) || (p.id === "dashboard" && START_PAGES.some((id) => allowed.has(id)))),
      ),
    }))
    .filter((s) => s.pages.length);
}

export function drawNav(nav) {
  nav.innerHTML = visible()
    .map((s) =>
      s.pages.length === 1
        ? `<a class="nav-section" href="#/${s.pages[0].id}" data-section="${s.id}" data-page="${s.pages[0].id}">${esc(s.label)}</a>`
        : `<div class="nav-menu" data-section="${s.id}">
            <button type="button" class="nav-section" aria-expanded="false">${esc(s.label)}<svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true"><path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5"/></svg></button>
            <div class="nav-list">${s.pages.map((p) => `<a href="#/${p.id}" data-page="${p.id}">${esc(p.label)}</a>`).join("")}</div>
          </div>`,
    )
    .join("");
}

// Mark the current page and its section.
export function markCurrent(nav, name) {
  const page = PARENT[name] || name;
  if (!nav) return;
  for (const a of nav.querySelectorAll("a[data-page]")) {
    if (a.dataset.page === page) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  for (const el of nav.querySelectorAll("[data-section]")) {
    el.classList.toggle("current", !!el.querySelector(`a[data-page="${page}"]`) || el.dataset.page === page);
  }
}

export function closeMenus(nav) {
  for (const b of nav.querySelectorAll("button[aria-expanded]")) b.setAttribute("aria-expanded", "false");
}

// One menu open at a time; a click elsewhere or Escape closes it.
export function bindNav(nav, header) {
  nav.addEventListener("click", (event) => {
    const button = event.target.closest("button.nav-section");
    if (!button) return;
    const open = button.getAttribute("aria-expanded") === "true";
    closeMenus(nav);
    button.setAttribute("aria-expanded", String(!open));
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".nav-menu")) closeMenus(nav);
    if (event.target.closest("a[href^='#/']") || (!event.target.closest("header.top"))) {
      header.classList.remove("menu-open");
      header.querySelector(".menu-toggle")?.setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeMenus(nav);
    header.classList.remove("menu-open");
  });
  const toggle = header.querySelector(".menu-toggle");
  toggle.addEventListener("click", () => {
    const open = header.classList.toggle("menu-open");
    toggle.setAttribute("aria-expanded", String(open));
  });
}

// Settings card: move sections and pages up or down, hide pages. Saved at once.
export function menuEditor(el, onChange) {
  let prefs = structuredClone(saved);
  const draw = () => {
    const sections = layout(prefs);
    el.innerHTML = `
      <h2>${t("Menu")}</h2>
      <p class="sub">${t("The order of the sections and of the pages in them, and which pages show. Kept on this server, so it's the same on every device. Hidden pages still open from their address.")}</p>
      <ol class="menu-editor">${sections
        .map(
          (s, si) => `<li data-section="${s.id}">
            <div class="menu-row"><strong>${esc(s.label)}</strong>${moveButtons("section", s.id, si, sections.length)}</div>
            <ol>${s.pages
              .map(
                (p, pi) => `<li><div class="menu-row">
                  <label class="check"><input type="checkbox" data-show="${p.id}"${p.hidden ? "" : " checked"}> ${esc(p.label)}</label>
                  ${moveButtons("page", p.id, pi, s.pages.length, s.id)}</div></li>`,
              )
              .join("")}</ol></li>`,
        )
        .join("")}</ol>
      <div class="save-row"><button class="btn" type="button" data-reset>${t("Back to the default menu")}</button><span class="sub" data-status></span></div>`;
  };
  const save = async (next) => {
    const status = el.querySelector("[data-status]");
    try {
      const reply = await post("prefs", { changes: { nav: next } });
      saved = reply.data.nav || {};
      prefs = structuredClone(saved);
      draw();
      onChange();
    } catch (error) {
      if (status) status.textContent = t("Not saved: {error}", { error: error.message });
    }
  };
  el.addEventListener("click", (event) => {
    const move = event.target.closest("button[data-move]");
    if (event.target.closest("[data-reset]")) return save(null);
    if (!move) return;
    const { kind, id, section, move: dir } = move.dataset;
    const sections = layout(prefs);
    const list = kind === "section" ? sections.map((s) => s.id) : sections.find((s) => s.id === section).pages.map((p) => p.id);
    const i = list.indexOf(id);
    const j = i + (dir === "up" ? -1 : 1);
    [list[i], list[j]] = [list[j], list[i]];
    const next = { ...prefs };
    if (kind === "section") next.sections = list;
    else next.pages = { ...(prefs.pages || {}), [section]: list };
    save(next);
  });
  el.addEventListener("change", (event) => {
    const box = event.target.closest("input[data-show]");
    if (!box) return;
    const hidden = new Set(prefs.hidden || []);
    if (box.checked) hidden.delete(box.dataset.show);
    else hidden.add(box.dataset.show);
    save({ ...prefs, hidden: [...hidden] });
  });
  draw();
}

function moveButtons(kind, id, index, count, section = "") {
  const button = (dir, label, disabled) =>
    `<button class="btn small" type="button" data-move="${dir}" data-kind="${kind}" data-id="${id}" data-section="${section}" aria-label="${label === "up" ? t("Move up") : t("Move down")}"${disabled ? " disabled" : ""}>${dir === "up" ? "↑" : "↓"}</button>`;
  return `<span class="menu-move">${button("up", "up", index === 0)}${button("down", "down", index === count - 1)}</span>`;
}
