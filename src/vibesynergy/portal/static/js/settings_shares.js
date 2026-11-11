// Settings → Sharing: share links, split from pages/settings.js.
import { get, post } from "./api.js";
import { bindAutosave } from "./settings_autosave.js";
import { ago, esc, number, userDate } from "./format.js";
import { N, t } from "./i18n.js";
import { refreshLog } from "./settings_access.js";
import { select } from "./settings_pieces.js";

// Share links ---------------------------------------------------------------------------

const PAGE_NAMES = {
  dashboard: N("Dashboard"),
  calendar: N("Calendar"),
  today: N("Today (any day)"),
  diary: N("Diary"),
  activities: N("Activities"),
  metrics: N("Metrics"),
  training: N("Training"),
  sleep: N("Sleep"),
  body: N("Body"),
  correlations: N("Correlations"),
  sql: N("SQL"),
  data: N("Sync and History"),
  history: N("History"),
  knowledge: N("Knowledge"),
  plan: N("Training plan"),
  planning: N("Planning & progress"),
  schedule: N("Calendar"),
  zones: N("Zones & thresholds"),
  coach: N("Coach"),
  gear: N("Gear"),
  recovery: N("Recovery"),
  injuries: N("Complaints & injuries"),
  "*": N("Everything"),
};

// The page checkboxes of a link: "Everything" first (also pages added later), then
// each page. With Everything on, the single pages are checked and locked.
function sharePagesHtml(pages, all, chosen, name) {
  const everything = chosen.includes(all);
  const boxes = pages
    .map((p) => `<label class="check"><input type="checkbox" name="${name}" value="${esc(p)}"${everything || chosen.includes(p) ? " checked" : ""}${everything ? " disabled" : ""}> ${esc(t(PAGE_NAMES[p] || p))}</label>`)
    .join("");
  return `<label class="check"><input type="checkbox" name="${name}-all" value="${esc(all)}"${everything ? " checked" : ""}> <b>${esc(t("Everything, also pages added later"))}</b></label>
    <div class="share-pages">${boxes}</div>
    <p class="sub">${esc(t("Never shared: Settings, your coach's memory and settings, secrets and tokens."))}</p>`;
}

function chosenPages(root, name) {
  const all = root.querySelector(`[name="${name}-all"]`);
  if (all?.checked) return [all.value];
  return [...root.querySelectorAll(`[name="${name}"]:checked`)].map((c) => c.value);
}

function bindEverything(root, name) {
  const all = root.querySelector(`[name="${name}-all"]`);
  all?.addEventListener("change", () => {
    for (const box of root.querySelectorAll(`[name="${name}"]`)) {
      box.disabled = all.checked;
      if (all.checked) box.checked = true;
    }
  });
}
const EXPIRIES = [
  ["1", N("1 day")],
  ["7", N("7 days")],
  ["30", N("30 days")],
  ["90", N("90 days")],
  ["365", N("1 year")],
  ["", N("Never")],
];

export async function loadShares(el, stale, created = null) {
  const reply = await get("share_links");
  if (stale()) return;
  const now = Date.now();
  const links = reply.data.links.filter((l) => l.state === "active" && !(l.expires_at && Date.parse(l.expires_at) <= now));
  const { pages } = reply.data;
  el.innerHTML = `
    <h2 id="shares-title">${t("Share links")}</h2>
    <p class="sub">${t("A secret public address that shows the pages you choose, for example to a coach. Anyone with the link can open it until it expires or you revoke it. Settings is never shared.")}</p>
    ${created ? `<div class="share-new"><b>${esc(t("Link for {name}", { name: created.link.title }))}</b><code>${esc(created.url)}</code>
      <div><button class="btn primary" type="button" data-copy>${t("Copy link")}</button> <span class="sub">${t("Shown only now: VibeSynergy keeps just a fingerprint of it. If it's lost, revoke it and make a new one.")}</span></div></div>` : ""}
    <form class="share-form">
      <div class="settings-grid">
        <label>${t("Title (who it's for)")} <input class="btn" name="title" maxlength="80" placeholder="${esc(t("e.g. Coach Anna"))}" required></label>
        <label>${t("Expires after")} ${select("expires", EXPIRIES.map(([v, label]) => [v, t(label)]), "7")}</label>
        <label class="check"><input type="checkbox" name="can_write"> ${t("Allow changes (e.g. re-sync on Sync)")}</label>
      </div>
      <fieldset><legend>${esc(t("Pages"))}</legend>${sharePagesHtml(pages, reply.data.all, pages.filter((p) => p !== "sql" && p !== "data"), "page")}</fieldset>
      <div class="settings-save"><button class="btn primary" type="submit">${t("Create link")}</button><span class="sub" data-status role="status" aria-live="polite"></span></div>
    </form>
    <h3 class="settings-subhead">${t("Open links")}</h3>
    ${links.length ? sharesTable(links) + links.map((l) => shareEditHtml(l, pages, reply.data.all)).join("") : `<p class="sub">${t("No share links are open.")}</p>`}`;
  // Redraw when the next link expires, so an expired link leaves the list by itself.
  clearTimeout(el._expiry);
  const next = Math.min(...links.map((l) => (l.expires_at ? Date.parse(l.expires_at) : Infinity)));
  if (Number.isFinite(next)) {
    el._expiry = setTimeout(() => {
      if (el.isConnected && !stale()) loadShares(el, stale);
    }, Math.min(Math.max(next - now + 1000, 1000), 2 ** 31 - 1));
  }

  el.querySelector("[data-copy]")?.addEventListener("click", async (event) => {
    try {
      await navigator.clipboard.writeText(created.url);
      event.target.textContent = t("Copied");
    } catch {
      event.target.textContent = t("Select the link above and copy it");
    }
  });
  const form = el.querySelector(".share-form");
  bindEverything(form, "page");
  for (const edit of el.querySelectorAll("[data-share-edit]")) {
    const id = edit.dataset.shareEdit;
    bindEverything(edit, `pages-${id}`);
    bindAutosave(edit, {
      statusEl: edit.querySelector("[data-share-status]"),
      save: () =>
        post(`share_links/${encodeURIComponent(id)}`, {
          title: edit.querySelector("[name=title]").value,
          pages: chosenPages(edit, `pages-${id}`),
          can_write: edit.querySelector("[name=can_write]").checked,
        }),
    });
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = form.querySelector("[data-status]");
    const expires = form.querySelector("[name=expires]").value;
    try {
      const reply = await post("share_links", {
        title: form.querySelector("[name=title]").value,
        pages: chosenPages(form, "page"),
        can_write: form.querySelector("[name=can_write]").checked,
        expires_in_days: expires === "" ? null : Number(expires),
      });
      if (stale()) return;
      loadShares(el, stale, reply.data);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error-text");
    }
  });
  el.querySelectorAll("[data-revoke-share]").forEach((button) =>
    button.addEventListener("click", async () => {
      if (!window.confirm(t("Revoke the link for {name}? It stops working at once.", { name: button.dataset.title }))) return;
      await post("share_links/revoke", { id: button.dataset.revokeShare });
      if (stale()) return;
      await loadShares(el, stale);
      el.querySelector("[data-status]").textContent = t("Revoked {name}.", { name: button.dataset.title });
      refreshLog(stale);
    }),
  );
}

// Change an open link in place: same address, saved directly.
function shareEditHtml(link, pages, all) {
  const id = esc(link.id);
  return `<details class="collapsible" data-share-edit="${id}"><summary>${esc(t("Change the link for {name}", { name: link.title }))}</summary>
    <label>${t("Title (who it's for)")} <input class="btn" name="title" maxlength="80" value="${esc(link.title)}"></label>
    <fieldset><legend>${esc(t("Pages"))}</legend>${sharePagesHtml(pages, all, link.pages, `pages-${id}`)}</fieldset>
    <label class="check"><input type="checkbox" name="can_write"${link.can_write ? " checked" : ""}> ${t("Allow changes (e.g. re-sync on Sync)")}</label>
    <p class="sub" role="status" aria-live="polite" data-share-status></p>
  </details>`;
}

function sharesTable(links) {
  const when = (iso) => (iso ? ago(iso) : "–");
  return `<div class="table-wrap"><table><thead><tr><th>${t("For")}</th><th>${t("Pages")}</th><th>${t("Access")}</th><th>${t("Expires")}</th><th>${t("Last opened")}</th><th></th></tr></thead><tbody>${links
    .map(
      (l) => `<tr><td>${esc(l.title)}</td>
        <td>${esc(l.pages.map((p) => t(PAGE_NAMES[p] || p)).join(", "))}</td>
        <td>${l.can_write ? t("read and change") : t("read only")}</td>
        <td>${l.expires_at ? esc(userDate(l.expires_at.slice(0, 10))) : t("never")}</td>
        <td>${esc(when(l.last_used_at))}${l.uses ? ` · ${number(l.uses)}×` : ""}</td>
        <td>${l.state === "active" ? `<button class="btn" type="button" data-revoke-share="${esc(l.id)}" data-title="${esc(l.title)}">${t("Revoke")}</button>` : ""}</td></tr>`,
    )
    .join("")}</tbody></table></div>`;
}
