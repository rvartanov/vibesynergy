// Waiting for you: the proposals only you can take or leave, then notices worth a
// look. Each line links to the page where it is handled; once handled there it leaves
// this list by itself.
import { get } from "../api.js";
import { esc, longDate, number } from "../format.js";
import { N, t } from "../i18n.js";
import { screenAlerts } from "../screen_alerts.js";
import { sportLabel } from "../symbols.js";

const KB_KINDS = { article: N("article"), rule: N("rule"), source: N("source") };

function decisionHtml(item) {
  if (item.kind === "kb_proposal") {
    const what = t(KB_KINDS[item.what] || item.what || "");
    return `<li><span class="chip info">${esc(t("Knowledge base"))}</span>
      <a href="${esc(item.link)}">${esc(t("Change to the {what} {target}", { what, target: item.target || "" }))}</a>
      ${item.why ? `<p class="sub">${esc(item.why)}</p>` : ""}</li>`;
  }
  const value = `${number(item.value, item.value % 1 ? 1 : 0)} ${item.unit || ""}`.trim();
  return `<li><span class="chip planned">${esc(t("Thresholds"))}</span>
    <a href="${esc(item.link)}">${esc(t("New value for {sport}: {metric} {value}", { sport: sportLabel(item.sport), metric: item.metric, value }))}</a></li>`;
}

function noticeText(item) {
  const day = item.date ? longDate(item.date) : "";
  switch (item.kind) {
    case "coach_missed":
      return t("Your coach skipped the {task} ({day}).", { task: t(item.task).toLowerCase(), day });
    case "horizon_missing":
      return t("Not on your Garmin yet: {title} ({day}).", { title: item.title, day });
    case "horizon_duplicates":
      return t("Twice on your Garmin: {title} ({day}).", { title: item.title, day });
    case "horizon_outdated":
      return t("Old version on your Garmin: {title} ({day}).", { title: item.title, day });
    case "horizon_behind":
      return t("Your plan runs only to {day}. Ask your coach to fill it up to {end}.", { day, end: item.end ? longDate(item.end) : "" });
    case "screen_alert":
      return item.text;
    default:
      return item.kind;
  }
}

export async function render(view, { stale }) {
  document.title = `${t("Waiting for you")} · VibeSynergy`;
  const [{ data }, alerts] = await Promise.all([get("inbox"), get("alerts").catch(() => null)]);
  if (stale()) return;
  const decisions = data.decisions || [];
  const notices = [...(data.notices || []), ...screenAlerts(alerts?.data)];
  view.innerHTML = `<div class="page-head"><div><h1>${esc(t("Waiting for you"))}</h1>
      <p class="sub">${esc(t("Proposals only you can take or leave, then things worth a look. Each one opens where you handle it."))}</p></div></div>
    <section class="card" aria-labelledby="inbox-decisions"><h2 id="inbox-decisions">${esc(t("Your decision ({n})", { n: decisions.length }))}</h2>
      ${decisions.length ? `<ul class="inbox-list">${decisions.map(decisionHtml).join("")}</ul>` : `<p class="empty">${esc(t("Nothing waits for your decision."))}</p>`}</section>
    <section class="card" aria-labelledby="inbox-notices"><h2 id="inbox-notices">${esc(t("Worth a look ({n})", { n: notices.length }))}</h2>
      ${notices.length ? `<ul class="inbox-list">${notices.map((n) => `<li><span class="chip warning">${esc(t("Notice"))}</span> <a href="${esc(n.link)}">${esc(noticeText(n))}</a></li>`).join("")}</ul>` : `<p class="empty">${esc(t("All quiet."))}</p>`}</section>`;
}
