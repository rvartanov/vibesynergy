// What changed (#140): after a release, the first page shows a "What changed in version …"
// card once (the server then counts the version as seen, per account); after that every
// version's items are under Waiting for you → Notices. No push, no mail.
import { get } from "./api.js";
import { esc, userDate } from "./format.js";
import { t } from "./i18n.js";

const items = (release) => (release.items || []).map((item) => `<li>${esc(t(item.text))}</li>`).join("");

/** The card: the newest unseen version in the title, the items of every unseen one. */
export function newsCardHtml(news) {
  const list = news?.releases || [];
  if (!list.length) return "";
  return `<section class="card release-news" data-release-news aria-labelledby="release-news-title">
    <div class="release-news-head"><h2 id="release-news-title">${esc(t("What changed in version {version}", { version: list[0].version }))}</h2>
      <button class="btn" type="button" data-release-news-close>${esc(t("Close"))}</button></div>
    <ul>${list.map(items).join("")}</ul>
    <p class="sub">${esc(t("You find this later under"))} <a href="#/inbox">${esc(t("Waiting for you → Notices"))}</a>.</p>
  </section>`;
}

/** Under Notices: every version with items, newest first; the newest one open. */
export function historyHtml(releases) {
  if (!releases?.length) return "";
  const versions = releases
    .map(
      (release, i) => `<details class="details"${i === 0 ? " open" : ""} data-release="${esc(release.version)}">
        <summary>${esc(t("Version {version}", { version: release.version }))} <span class="sub">${esc(userDate(release.date))}</span></summary>
        <ul>${items(release)}</ul></details>`,
    )
    .join("");
  return `<div class="release-history" data-release-history><h3>${esc(t("What changed in VibeSynergy"))}</h3>${versions}</div>`;
}

/** Ask once per page load; show the card above the page until you go to another page. */
export async function showNews(view) {
  let news;
  try {
    news = (await get("releases/news")).data;
  } catch {
    return;
  }
  const html = newsCardHtml(news);
  if (!html || !view.isConnected) return;
  view.insertAdjacentHTML("beforebegin", html);
  const card = view.previousElementSibling;
  card.querySelector("[data-release-news-close]")?.addEventListener("click", () => {
    card.remove();
    view.focus();
  });
}

export function hideNews() {
  document.querySelector("[data-release-news]")?.remove();
}
