// Waiting for you: the proposals only you can take or leave, then notices about
// what went wrong or needs attention, and what changed per version (#140). Each line links to the page where it is handled; once handled there it leaves
// this list by itself.
import { get, post } from "../api.js";
import { esc, longDate, number, userDateTime } from "../format.js";
import { N, t } from "../i18n.js";
import { linesHtml } from "../kb_diff.js";
import { historyHtml } from "../release_news.js";
import { fixKinds } from "../kb_lint.js";
import { screenAlerts } from "../screen_alerts.js";
import { sportLabel } from "../symbols.js";

const KB_KINDS = { article: N("article"), rule: N("rule"), source: N("source") };

const sport = (change) => (change.sport ? ` (${sportLabel(change.sport)})` : "");

/** What an AI coach proposes to change in the plan (#118), in one line. */
export function planChangeText(change) {
  const c = change || {};
  const day = c.date ? longDate(c.date) : "";
  const name = `${c.name || t("a session")}${sport(c)}`;
  switch (c.action) {
    case "add":
      return t("Add {name} on {day}.", { name, day });
    case "change":
      return c.from_date && c.from_date !== c.date
        ? t("Change {name} and move it from {from} to {day}.", { name, from: longDate(c.from_date), day })
        : t("Change {name} on {day}.", { name, day });
    case "move":
      return t("Move {name} from {from} to {day}.", { name, from: c.from_date ? longDate(c.from_date) : "", day });
    case "skip":
      return t("Drop {name} on {day}.", { name, day });
    case "week":
      return t("New note for plan week {week}.", { week: c.week });
    case "target":
      return t("New weekly target for plan week {week}{sport}.", { week: c.week, sport: sport(c) });
    // Races and events (#91).
    case "race_add":
      return t("Add the race {name} on {day}.", { name: c.name || "", day });
    case "race_change":
      return t("Change the race {name} on {day}.", { name: c.name || "", day });
    case "race_delete":
      return t("Delete the race {name} on {day}.", { name: c.name || "", day });
    case "event_add":
      return t("Add {name} to your events on {day}.", { name: c.name || "", day });
    case "event_change":
      return t("Change the event {name} on {day}.", { name: c.name || "", day });
    case "event_delete":
      return t("Delete the event {name} on {day}.", { name: c.name || "", day });
    case "race_revert":
    case "event_revert":
      return t("Undo the last change to {name}.", { name: c.name || "" });
    default:
      return t("A change to your plan.");
  }
}

function proposalHtml(item) {
  const who = item.role === "advisory"
    ? t("Proposed by {name}, advisory AI coach.", { name: item.author_name || t("an AI coach") })
    : t("Proposed by your AI coach.");
  const text = item.change?.text
    ? `<details><summary>${esc(t("What it would say"))}</summary><p class="sub proposal-text">${esc(item.change.text)}</p></details>`
    : "";
  return `<li data-plan-proposal="${esc(String(item.id))}"><span class="chip planned">${esc(t("Plan"))}</span>
    <span>${esc(planChangeText(item.change))}</span>
    <p class="sub">${esc(who)} ${esc(item.why || "")}</p>${text}
    <p class="actions"><button class="btn primary" type="button" data-proposal-approve="${esc(String(item.id))}">${esc(t("Approve and carry out"))}</button>
      <button class="btn" type="button" data-proposal-reject="${esc(String(item.id))}">${esc(t("Reject"))}</button>
      <span class="sub" data-proposal-status role="status"></span></p></li>`;
}

const LEVELS = { ok: N("go ahead"), ease: N("take it a bit easier"), flag: N("no hard training"), refuse: N("rest or very easy") };

function disagreementText(item) {
  const day = item.date ? longDate(item.date) : "";
  const level = (item.differences || []).find((d) => d.field === "safety_level");
  const name = item.author_name || t("an advisory AI coach");
  if (level) {
    return t("Second opinion for {day}: {name} says “{theirs}”, your AI coach “{mine}”.", { day, name, theirs: t(LEVELS[level.advisory] || level.advisory), mine: t(LEVELS[level.primary] || level.primary) });
  }
  return t("Second opinion for {day}: {name} decides differently on a session than your AI coach.", { day, name });
}

function decisionHtml(item) {
  if (item.kind === "plan_proposal") return proposalHtml(item);
  if (item.kind === "kb_proposal") {
    const what = t(KB_KINDS[item.what] || item.what || "");
    // What changes, not the whole article (#137).
    const lines = (item.diff || []).filter((change) => change.type === "text").flatMap((change) => change.lines);
    const diff = lines.length
      ? `<details data-kb-diff><summary>${esc(t("What changes"))}</summary>${linesHtml(lines)}</details>`
      : "";
    return `<li><span class="chip info">${esc(t("Knowledge base"))}</span>
      <a href="${esc(item.link)}">${esc(t("Change to the {what} {target}", { what, target: item.target || "" }))}</a>
      ${item.why ? `<p class="sub">${esc(item.why)}</p>` : ""}${diff}</li>`;
  }
  const value = `${number(item.value, item.value % 1 ? 1 : 0)} ${item.unit || ""}`.trim();
  return `<li><span class="chip planned">${esc(t("Thresholds"))}</span>
    <a href="${esc(item.link)}">${esc(t("New value for {sport}: {metric} {value}", { sport: sportLabel(item.sport), metric: item.metric, value }))}</a></li>`;
}

function noticeText(item) {
  const day = item.date ? longDate(item.date) : "";
  switch (item.kind) {
    case "coach_missed":
      return t("Your AI coach skipped the {task} ({day}).", { task: t(item.task).toLowerCase(), day });
    case "horizon_missing":
      return t("Not on your Garmin yet: {title} ({day}).", { title: item.title, day });
    case "horizon_duplicates":
      return t("Twice on your Garmin: {title} ({day}).", { title: item.title, day });
    case "horizon_outdated":
      return t("Old version on your Garmin: {title} ({day}).", { title: item.title, day });
    case "horizon_behind":
      return t("Your plan runs only to {day}. Ask your AI coach to fill it up to {end}.", { day, end: item.end ? longDate(item.end) : "" });
    case "coach_disagreement":
      return disagreementText(item);
    case "kb_editorial":
      return t("Texts updated ({count}): {what}. Nothing else changed; you can undo it.", { count: item.count, what: fixKinds(item.checks) });
    case "screen_alert":
      return item.text;
    default:
      return item.kind;
  }
}

// Another app keeps overwriting the plan in a mirror (#110). The same words as the alert.
const MIRRORS = { intervals: N("intervals.icu"), garmin: N("Garmin"), calendar: N("your calendar") };
const SHOWN = 4;

function writerText(writer) {
  if (writer.startsWith("icu-app:")) return t("the app with number {id} in intervals.icu", { id: writer.slice(8) });
  if (writer.startsWith("icu-user:")) return t("the intervals.icu account {id}", { id: writer.slice(9) });
  return writer.replace(/^calendar-app:/, "");
}

export function conflictText(item) {
  const all = item.names || [];
  const shown = all.slice(0, SHOWN).join(", ");
  const names = all.length > SHOWN ? t("{names} and {n} more", { names: shown, n: all.length - SHOWN }) : shown;
  const parts = [t("Another app or device keeps changing your plan in {mirror} (since {since}): {names}.", { mirror: t(MIRRORS[item.mirror] || item.mirror), since: userDateTime(item.since), names })];
  if (item.writer) parts.push(t("It was written by {writer}.", { writer: writerText(item.writer) }));
  if (item.mirror === "calendar") parts.push(t("VibeSynergy leaves calendar items as they are, so nothing was put back."));
  else if (item.held) parts.push(t("VibeSynergy put your plan back {n} times and has now stopped. Stop the other app first, then put your plan back here.", { n: item.count }));
  else parts.push(t("VibeSynergy put your plan back {n} times. Check which app writes there.", { n: item.count }));
  return parts.join(" ");
}

function conflictHtml(item) {
  const resume = item.held && item.mirror !== "calendar"
    ? `<p class="actions"><button class="btn primary" type="button" data-conflict-resume="${esc(item.mirror)}">${esc(t("The other app is stopped: put my plan back"))}</button>
      <span class="sub" data-conflict-status role="status"></span></p>`
    : "";
  return `<li data-plan-conflict="${esc(item.mirror)}"><span class="chip warning">${esc(t("Plan"))}</span>
    <span>${esc(conflictText(item))}</span> <a href="${esc(item.link)}">${esc(t("Open your plan"))}</a>${resume}</li>`;
}

function noticeHtml(n) {
  if (n.kind === "plan_conflict") return conflictHtml(n);
  return `<li${n.advice ? " data-advice-notice" : ""}><span class="chip ${n.advice ? "info" : "warning"}">${esc(n.advice ? t("Advice") : t("Notice"))}</span> <a href="${esc(n.link)}">${esc(noticeText(n))}</a></li>`;
}

export async function render(view, { stale }) {
  document.title = `${t("Waiting for you")} · VibeSynergy`;
  const [{ data }, alerts] = await Promise.all([get("inbox"), get("alerts").catch(() => null)]);
  if (stale()) return;
  const decisions = data.decisions || [];
  // A plan conflict has its own line with the confirmation (#110): not twice.
  const notices = [...(data.notices || []), ...screenAlerts(alerts?.data).filter((a) => a.alertKind !== "plan_conflict")];
  view.innerHTML = `<div class="page-head"><div><h1>${esc(t("Waiting for you"))}</h1>
      <p class="sub">${esc(t("Proposals only you can take or leave, then notices about what went wrong or needs attention. Each one opens where you handle it."))}</p></div></div>
    <section class="card" aria-labelledby="inbox-decisions"><h2 id="inbox-decisions">${esc(t("Your decision ({n})", { n: decisions.length }))}</h2>
      ${decisions.length ? `<ul class="inbox-list">${decisions.map(decisionHtml).join("")}</ul>` : `<p class="empty">${esc(t("Nothing waits for your decision."))}</p>`}</section>
    <section class="card" aria-labelledby="inbox-notices"><h2 id="inbox-notices">${esc(t("Notices ({n})", { n: notices.length }))}</h2>
      ${notices.length ? `<ul class="inbox-list">${notices.map(noticeHtml).join("")}</ul>` : `<p class="empty">${esc(t("All quiet."))}</p>`}
      ${historyHtml(data.releases)}</section>`;
  // A plan proposal is decided here: approving carries it out (#118).
  const decide = (button, decision) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-plan-proposal]");
      const status = row.querySelector("[data-proposal-status]");
      row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        await post(`plan_proposals/${row.dataset.planProposal}/${decision}`, {});
        if (stale()) return;
        await render(view, { stale });
      } catch (error) {
        if (stale() || !row.isConnected) return;
        status.textContent = t(error.message);
        row.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    });
  };
  view.querySelectorAll("[data-proposal-approve]").forEach((b) => decide(b, "approve"));
  view.querySelectorAll("[data-proposal-reject]").forEach((b) => decide(b, "reject"));
  // The other app is stopped: the mirror puts the plan back again (#110).
  view.querySelectorAll("[data-conflict-resume]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-plan-conflict]");
      const status = row.querySelector("[data-conflict-status]");
      button.disabled = true;
      try {
        await post(`plan_conflict/${button.dataset.conflictResume}/resume`, {});
        if (stale()) return;
        await render(view, { stale });
      } catch (error) {
        if (stale() || !row.isConnected) return;
        status.textContent = t(error.message);
        button.disabled = false;
      }
    });
  });
}
