// Search: one field over activities, diary notes, coach messages, the knowledge base
// and gear. "/" opens this page from anywhere; the query lives in the address (?q=), so
// back and forward work and a search can be bookmarked.
import { get } from "../api.js";
import { esc, longDate } from "../format.js";
import { N, t } from "../i18n.js";

const KINDS = {
  activities: N("Activities"),
  notes: N("Diary"),
  coach: N("Coach messages"),
  knowledge: N("Knowledge base"),
  gear: N("Gear"),
};

function hitHtml(hit) {
  const date = hit.date ? ` <span class="sub">${esc(longDate(hit.date))}</span>` : "";
  const snippet = hit.snippet ? `<p class="sub">${esc(hit.snippet)}</p>` : "";
  return `<li><a href="${esc(hit.link)}">${esc(hit.title || "—")}</a>${date}${snippet}</li>`;
}

export async function render(view, { query, stale }) {
  const q = (query?.get("q") || "").trim();
  document.title = `${t("Search")} · VibeSynergy`;
  view.innerHTML = `<div class="page-head"><h1>${esc(t("Search"))}</h1></div>
    <form class="search-form" role="search">
      <label for="search-q">${esc(t("Search in activities, diary, coach messages, knowledge and gear"))}</label>
      <div class="search-row"><input class="btn" type="search" id="search-q" name="q" value="${esc(q)}" minlength="2" maxlength="200" autocomplete="off">
      <button class="btn primary" type="submit">${esc(t("Search"))}</button></div>
    </form>
    <div data-search-results aria-live="polite"></div>`;
  const input = view.querySelector("#search-q");
  view.querySelector(".search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    location.hash = value ? `#/search?q=${encodeURIComponent(value)}` : "#/search";
  });
  input.focus();
  const results = view.querySelector("[data-search-results]");
  if (q.length < 2) {
    results.innerHTML = `<p class="sub">${esc(t("Type at least two letters. Tip: press / on any page to search."))}</p>`;
    return;
  }
  results.innerHTML = `<p class="sub" role="status">${esc(t("Searching…"))}</p>`;
  const { data } = await get("search", { q });
  if (stale()) return;
  if (!data.groups.length) {
    results.innerHTML = `<p class="empty">${esc(t("Nothing found for “{q}”.", { q }))}</p>`;
    return;
  }
  results.innerHTML = data.groups
    .map((group) => `<section class="card" aria-labelledby="search-${esc(group.kind)}"><h2 id="search-${esc(group.kind)}">${esc(t(KINDS[group.kind] || group.kind))} (${group.hits.length})</h2>
      <ul class="search-hits">${group.hits.map(hitHtml).join("")}</ul></section>`)
    .join("");
}
