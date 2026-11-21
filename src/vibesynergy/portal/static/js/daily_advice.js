// The day's advice (#89), the same on the start page and the Coach page: the AI coach's
// advice when it is valid, else the fixed rules' with the reason. The current version says
// when it was written, what came in before it and why it changed; the Coach page lists the
// earlier versions, marked replaced, each linked to its own report (#90, #115). An advisory AI coach's entry for the day is listed under "Second
// opinion", flagged where it differs from the primary AI coach (#118). Under "Why this advice":
// the reasons, where it came from and where the AI and the rules differ. The update button
// syncs every source and then asks the AI coach for new advice, with its progress.
import { get, post } from "./api.js";
import { esc, userTime } from "./format.js";
import { N, t } from "./i18n.js";

const COLOURS = { green: N("Green"), amber: N("Amber"), red: N("Red") };
const LEVELS = {
  ok: N("go ahead"),
  ease: N("take it a bit easier"),
  flag: N("no hard training"),
  refuse: N("rest or very easy"),
};
// The chip tone per colour.
const TONES = { green: "good", amber: "warning", red: "danger" };
const POLL_MS = 3000;
const POLL_LIMIT_MS = 12 * 60 * 1000;

// One view for every advice (#147): the server makes the label ("Advice for tomorrow – Sat
// 21 Nov"), when and why a version came ("Updated 13:48, after your workout"), the colour
// state ("Amber – Take it a bit easier", or "Done for today" with a recovery line) and the
// notice (query/advice_view.py). The pages show those words and build none of their own,
// so the notice, the push, the mail and every page read alike.

/** The colour state of an advice view: a chip with the colour and its meaning, or "Done for
 * today" with the recovery colour in a small line beside it. */
export function verdictHtml(view) {
  if (!view || view.state === "none" || !view.verdict) return "";
  if (view.state === "done") {
    return `<p class="advice-verdict" data-advice-state="done"><span class="chip good">${esc(view.headline)}</span>${view.recovery ? ` <span class="sub" data-advice-recovery>${esc(view.recovery)}</span>` : ""}</p>`;
  }
  return `<p class="advice-verdict" data-advice-state="colour"><span class="chip ${TONES[view.colour] || ""}">${esc(view.colour_name)}</span> ${esc(view.headline)}</p>`;
}

/** The colour class of an advice card: none once the day is done, so the card does not
 * call for a colour the text no longer asks for. */
export function viewLevel(view, fallback = "") {
  if (view?.state === "done") return "done";
  return view?.colour || fallback || "";
}

/** The head of an advice entry (Coach page, report page): its label and stamp. */
export function viewHeadHtml(view) {
  if (!view) return "";
  return `<strong data-advice-label="${esc(view.date)}">${esc(view.label)}</strong>${view.stamp ? ` <span class="sub" data-advice-stamp>${esc(view.stamp)}</span>` : ""}`;
}

/** Tomorrow's advice on today's start page (#138), once the evening run wrote it: compact,
 * with its colour, its first line, when it was written and a link to its report (where the
 * notice links too). Empty while there is none. */
export function tomorrowAdviceHtml(item) {
  if (!item || item.error || !item.level) return "";
  const view = item.view || {};
  const label = view.label || item.label || "";
  const stamp = view.stamp || item.stamp || "";
  const colour = view.colour_name
    ? `<span class="chip ${TONES[view.colour] || ""}">${esc(view.colour_name)}</span>`
    : `<span class="chip ${TONES[item.level] || ""}">${esc(t(COLOURS[item.level] || item.level))}</span>`;
  return `<section class="dash-tomorrow verdict ${esc(item.level)}" data-advice-tomorrow="${esc(item.date)}" aria-labelledby="dash-tomorrow-title">
    <h3 id="dash-tomorrow-title" class="dash-kicker">${esc(label)}</h3>
    <p>${colour} ${esc(item.advice || item.headline || "")}</p>
    <p class="sub">${stamp ? `${esc(stamp)} · ` : ""}<a href="${esc(item.link || `#/coach/report/${item.entry_id}`)}">${esc(t("Open tomorrow's advice"))}</a></p>
  </section>`;
}

export function failedAdvice(advice) {
  return !advice || (advice.error === "unavailable" && Object.keys(advice).length === 1);
}

/** The card's colour class: green, amber, red, or none (off, failed). */
export function adviceLevel(advice) {
  return failedAdvice(advice) ? "" : viewLevel(advice.view, advice.level);
}

function sourceLine(advice) {
  if (advice.source === "ai") {
    const at = advice.entry?.at ? userTime(advice.entry.at) : "";
    return at ? t("From your AI coach, written at {time}.", { time: at }) : t("From your AI coach.");
  }
  if (advice.mode === "rules") return t("From the fixed rules (your setting: rules only).");
  return t("From the fixed rules.");
}

// What a version did to one session (#108), said per session: "Easy ride Z2 1h: dropped".
const SESSION_CHANGES = {
  skip: N("{title}: dropped"),
  adjust: N("{title}: adjusted"),
  keep: N("{title}: kept"),
  done: N("{title}: done"),
  missed: N("{title}: missed"),
  proposal: N("{title}: proposed"),
};

const capital = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

/** One line per changed session, with its title as written and a normal capital. */
export function sessionChangeText(change) {
  const title = capital(String(change.title || "").trim());
  if (change.to == null) return t("{title}: removed", { title });
  if (change.from == null) return t("{title}: added", { title });
  return t(SESSION_CHANGES[change.to] || "{title}: changed", { title });
}

/** What really changed against the version before (colour, safety level, sessions). */
export function versionDiffText(differences) {
  return (differences || []).map((d) => {
    if (d.field === "colour") return t("colour {from} → {to}", { from: t(COLOURS[d.from] || d.from || "—").toLowerCase(), to: t(COLOURS[d.to] || d.to || "—").toLowerCase() });
    if (d.field === "safety_level") return t("now: {level}", { level: t(LEVELS[d.to] || d.to || "—") });
    const changes = d.changes?.length
      ? d.changes
      : (d.sessions || []).map((title) => ({ title, from: "?", to: "?" }));
    return changes.map(sessionChangeText).join("; ");
  }).join("; ");
}

/** When and why the shown AI advice came (the view's stamp) and what really changed. */
function updatedText(advice) {
  if (advice.source !== "ai") return "";
  let line = advice.view?.stamp || "";
  const diff = versionDiffText(advice.entry?.differences);
  if (diff) line += `${line ? ". " : ""}${t("Changed: {what}.", { what: diff })}`;
  else if (line) line += ".";
  return line;
}

/** The AI coach's own reason for a later version (#115), as one sentence. */
export function becauseText(version) {
  const reason = String(version?.changed_because?.reason || "").trim().replace(/[.\s]+$/, "");
  return reason ? t("Changed because: {reason}.", { reason }) : "";
}

const STATUS = { replaced: N("Replaced"), invalid: N("Not used: incomplete") };

/** The head of the report page for a daily advice (#147): its label, when and why this
 * version came and its earlier versions; a replaced version says so and links to the
 * current one. */
export function reportHeadHtml(view, versions = []) {
  if (!view) return "";
  const earlier = (versions || []).filter((v) => v.id !== view.entry_id);
  const list = earlier.length ? versionsListHtml(earlier) : "";
  const more = earlier.length
    ? `<details class="advice-versions" data-advice-versions><summary>${esc(t("Earlier versions ({n})", { n: earlier.length }))}</summary><ol>${list}</ol></details>`
    : "";
  const replaced = view.status && view.status !== "current"
    ? `<p class="notice warning" data-thread-status="${esc(view.status)}">${esc(t("Replaced by a newer version: this is not the current advice."))}${view.current_id ? ` <a href="#/coach/report/${esc(String(view.current_id))}">${esc(t("Open the current version"))}</a>` : ""}</p>`
    : "";
  return `<header class="advice-report-head" data-thread-status-of="${esc(view.status || "")}">
    <h2 data-advice-label="${esc(view.date)}">${esc(view.label)}</h2>
    ${view.stamp ? `<p class="sub" data-advice-stamp>${esc(view.stamp)}</p>` : ""}
    ${verdictHtml(view)}${replaced}${more}</header>`;
}

/** The versions of a day, each with its time, colour, status, why it came, what changed
 * and why, linked to exactly that version (#115). No numbers: the times say the order. */
function versionsListHtml(list) {
  return list.map((v) => {
    const time = v.at ? userTime(v.at) : "";
    const colour = v.valid
      ? `<span class="chip ${TONES[v.colour] || ""}">${esc(t(COLOURS[v.colour] || v.colour))}</span>`
      : "";
    const status = STATUS[v.status] ? `<span class="chip" data-version-status="${esc(v.status)}">${esc(t(STATUS[v.status]))}</span>` : "";
    const notes = [
      v.why || "",
      v.first ? t("first advice for this day") : versionDiffText(v.differences),
      becauseText(v),
    ].filter(Boolean).join(" · ");
    return `<li data-advice-version="${esc(String(v.id))}"><span class="advice-version-head"><a href="#/coach/report/${esc(String(v.id))}"><time datetime="${esc(v.at || "")}">${esc(time)}</time></a> ${colour} ${status}</span>
      <span>${esc(v.advice || "")}</span>${notes ? ` <span class="sub">${esc(notes)}</span>` : ""}</li>`;
  }).join("");
}

/** Every version of the day's advice, newest first, for the Coach page (#90). */
export function versionsHtml(advice) {
  const all = advice?.versions || [];
  const shown = advice?.source === "ai" ? advice.entry?.id : null;
  const earlier = all.filter((v) => v.id !== shown);
  if (!earlier.length) return "";
  return `<details class="advice-versions" data-refresh-key="coach-advice-versions" data-advice-versions><summary>${esc(t("Earlier versions ({n})", { n: earlier.length }))}</summary>
    <ol>${versionsListHtml(earlier)}</ol></details>`;
}

/** The continuity check's gaps (#115): what was sent against what the AI coach stored. */
export function continuityHtml(findings) {
  if (!findings?.length) return "";
  const items = findings.map((f) => `<li data-continuity-rule="${esc(f.rule)}">${esc(f.message || f.rule)}</li>`).join("");
  return `<div class="notice warning" data-continuity><p><strong>${esc(t("Check of the thread"))}</strong> ${esc(t("What your AI coach stored and what you were sent do not match everywhere:"))}</p><ul>${items}</ul></div>`;
}

const DECISIONS = {
  keep: N("keep"),
  adjust: N("adjust"),
  skip: N("skip"),
  done: N("done"),
  missed: N("missed"),
  proposal: N("proposal"),
};

/** Where a second opinion differs from the primary AI coach, one line per difference. */
export function disagreementLines(differences) {
  return (differences || []).flatMap((d) => {
    if (d.field === "safety_level") {
      return [t("Safety level: your AI coach “{mine}”, second opinion “{theirs}”.", { mine: t(LEVELS[d.primary] || d.primary || "—"), theirs: t(LEVELS[d.advisory] || d.advisory || "—") })];
    }
    return (d.changes || []).map((c) => t("{title}: your AI coach “{mine}”, second opinion “{theirs}”.", { title: capital(String(c.title || "").trim()), mine: t(DECISIONS[c.primary] || c.primary || "—"), theirs: t(DECISIONS[c.advisory] || c.advisory || "—") }));
  });
}

/** The advisory AI coaches' entries for the day (#118), in the same thread as the advice:
 * never the advice itself, flagged where one differs from the primary AI coach. */
export function secondOpinionsHtml(advice, { keyPrefix = "dashboard" } = {}) {
  const all = advice?.second_opinions || [];
  if (!all.length) return "";
  const flagged = all.some((o) => o.disagrees);
  const items = all.map((o) => {
    const time = o.at ? userTime(o.at) : "";
    const who = o.author_name ? t("{name}, advisory AI coach", { name: o.author_name }) : t("Advisory AI coach");
    const colour = o.colour ? `<span class="chip ${TONES[o.colour] || ""}">${esc(t(COLOURS[o.colour] || o.colour))}</span> ` : "";
    const lines = disagreementLines(o.differences);
    const differs = lines.length
      ? `<p class="notice warning" data-second-opinion-differs><strong>${esc(t("Differs from your AI coach"))}</strong> ${esc(lines.join(" "))}</p>`
      : `<p class="sub">${esc(t("Agrees with your AI coach on the safety level and the sessions."))}</p>`;
    return `<li data-second-opinion="${esc(String(o.id))}"><span class="advice-version-head">${colour}<span class="sub">${esc(who)}${time ? ` · <time datetime="${esc(o.at)}">${esc(time)}</time>` : ""}</span></span>
      <span>${esc(o.advice || "")}</span>${differs}</li>`;
  }).join("");
  const flag = flagged ? ` <span class="chip warning">${esc(t("Differs"))}</span>` : "";
  const last = flagged
    ? `<p class="sub">${esc(t("Your AI coach has the last word and weighs this in its next advice, unless you choose otherwise."))}</p>`
    : "";
  return `<details class="advice-versions second-opinions" data-refresh-key="${esc(keyPrefix)}-second-opinions" data-second-opinions${flagged ? " open" : ""}><summary>${esc(t("Second opinion ({n})", { n: all.length }))}${flag}</summary>
    <ol>${items}</ol>${last}</details>`;
}

function differencesHtml(advice) {
  const rules = advice.rules;
  if (advice.source !== "ai" || !rules) return "";
  const rows = (advice.differences || []).map((d) => {
    const names = d.field === "colour" ? COLOURS : LEVELS;
    const label = d.field === "colour" ? t("Colour") : t("Safety level");
    return `<li>${esc(label)}: ${esc(t("AI coach {ai}, rules {rules}", { ai: t(names[d.ai] || d.ai || "—"), rules: t(names[d.rules] || d.rules || "—") }))}</li>`;
  }).join("");
  const same = rows
    ? `<p>${esc(t("Where your AI coach and the fixed rules differ:"))}</p><ul>${rows}</ul>`
    : `<p>${esc(t("Your AI coach and the fixed rules agree on the colour and the safety level."))}</p>`;
  const override = advice.override_reason
    ? `<p>${esc(t("Why your AI coach differs: {reason}", { reason: advice.override_reason }))}</p>`
    : "";
  const said = `<p class="sub">${esc(t("The fixed rules say: {text}", { text: `${rules.headline || ""}. ${rules.advice || ""}`.trim() }))}</p>`;
  return `${same}${override}${said}`;
}

/** The advice itself: the main line, updating and fallback notes, why, and details. */
export function adviceHtml(advice, { keyPrefix = "dashboard", refresh = false } = {}) {
  if (failedAdvice(advice)) {
    return `<p class="notice error" role="alert">${esc(t("This part could not load right now."))}</p>`;
  }
  if (advice.mode === "off") {
    return `<p class="sub" data-advice-off>${esc(t("Daily advice is off."))} <a href="#/settings/system">${esc(t("Turn it on in Settings → System."))}</a></p>`;
  }
  const reasons = advice.reasons || [];
  const updating = advice.updating?.text
    ? `<span role="status" data-advice-updating>${esc(advice.updating.text)}</span> `
    : "";
  const fallback = advice.fallback?.text
    ? `<span data-advice-fallback="${esc(advice.fallback.reason)}">${esc(advice.fallback.text)}</span> `
    : "";
  const written = updatedText(advice);
  const updated = written ? `<span data-advice-updated>${esc(written)}</span> ` : "";
  // One disclosure: the reasons, then the details (where the advice came from and where
  // the AI and the rules differ). One target keeps the card calm on a phone.
  const why = reasons.length
    ? `<p>${esc(reasons.map((r) => String(r).replace(/[.\s]+$/, "")).join(". "))}.</p>`
    : "";
  const because = advice.source === "ai" ? becauseText(advice.entry) : "";
  const details = `<details data-refresh-key="${esc(keyPrefix)}-guidance-details" data-advice-details><summary>${t("Why this advice")}</summary>
      ${why}${because ? `<p>${esc(because)}</p>` : ""}<p class="sub">${esc(sourceLine(advice))}</p>${differencesHtml(advice)}</details>`;
  // Where the advice stands and the update button on one line, so the card stays within one
  // screen on a phone.
  const status = updated || updating || fallback || refresh
    ? `<p class="sub advice-status${fallback ? " advice-fallback" : ""}">${updated}${updating}${fallback}${refresh ? refreshButtonHtml() : ""}</p>`
    : "";
  // The colour state first (#147): the AI coach's view, or the rules' colour and headline.
  const verdict = advice.view
    ? verdictHtml(advice.view)
    : advice.level
      ? `<p class="advice-verdict" data-advice-state="colour"><span class="chip ${TONES[advice.level] || ""}">${esc(t(COLOURS[advice.level] || advice.level))}</span> ${esc(advice.headline || "")}</p>`
      : "";
  return `${verdict}<p class="verdict-advice dash-keep-line" data-dash-main>${esc(advice.advice || "")}</p>${status}${details}${secondOpinionsHtml(advice, { keyPrefix })}`;
}

export function refreshButtonHtml() {
  return `<button class="linkish advice-refresh" type="button" data-advice-refresh>${t("Update advice")}</button>
    <span data-advice-progress role="status" aria-live="polite"></span>`;
}

function progressText(state) {
  const sync = state.sync || { total: 0, done: 0, failed: [] };
  if (sync.done < sync.total) {
    return t("Syncing your sources ({done} of {total})…", { done: sync.done, total: sync.total });
  }
  if (state.run === "waiting" || state.run === "running") return t("Your AI coach is writing the advice…");
  return "";
}

function finalText(start, state) {
  const failed = state.sync?.failed || [];
  const parts = [];
  if (failed.length) parts.push(t("Some sources did not sync: {sources}.", { sources: failed.join(", ") }));
  if (!start.ai) parts.push(t("Your sources are synced."));
  else if (state.run === "done") parts.push(t("The advice is updated."));
  else parts.push(t("The AI coach could not update the advice; the fixed rules give it, with the reason."));
  return parts.join(" ");
}

/** Bind the update button. ``done`` re-renders the page once the run has ended. */
export function bindAdviceRefresh(root, { stale, done }) {
  const button = root.querySelector("[data-advice-refresh]");
  const status = root.querySelector("[data-advice-progress]");
  if (!button || !status) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = t("Starting…");
    try {
      const { data: start } = await post("advice/refresh", {});
      const jobs = Object.values(start.sync || {}).filter(Boolean).join(",");
      const began = Date.now();
      let state = { sync: { total: jobs ? jobs.split(",").length : 0, done: 0, failed: [] }, run: start.ai ? "waiting" : null };
      while (!stale() && button.isConnected && Date.now() - began < POLL_LIMIT_MS) {
        const { data } = await get("advice/refresh", { key: start.key, jobs });
        state = data;
        if (state.finished) break;
        status.textContent = progressText(state);
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      }
      if (stale() || !button.isConnected) return;
      const message = finalText(start, state);
      await done?.();
      const again = root.isConnected ? root.querySelector("[data-advice-progress]") : null;
      (again || status).textContent = message;
    } catch (error) {
      if (stale()) return;
      status.textContent = t("Updating failed: {error}", { error: error.message });
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  });
}
