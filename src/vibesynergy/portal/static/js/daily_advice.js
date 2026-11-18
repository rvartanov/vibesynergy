// The day's advice (#89), the same on the start page and the Coach page: the AI coach's
// advice when it is valid, else the fixed rules' with the reason. The current version says
// when it was written and what came in before it; the Coach page lists the earlier
// versions (#90). Under "Why this advice":
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
// What came in before a new version (#90), and the chip tone per colour.
const CHANGES = {
  sleep: N("your sleep"),
  hrv: N("your HRV"),
  checkin: N("your check-in"),
  note: N("a note"),
  injury: N("pain or an injury"),
  activity: N("a workout"),
  plan: N("the plan"),
  reply: N("your reply"),
};
const TONES = { green: "good", amber: "warning", red: "danger" };
const POLL_MS = 3000;
const POLL_LIMIT_MS = 12 * 60 * 1000;

export function failedAdvice(advice) {
  return !advice || (advice.error === "unavailable" && Object.keys(advice).length === 1);
}

/** The card's colour class: green, amber, red, or none (off, failed). */
export function adviceLevel(advice) {
  return failedAdvice(advice) ? "" : advice.level || "";
}

function sourceLine(advice) {
  if (advice.source === "ai") {
    const at = advice.entry?.at ? userTime(advice.entry.at) : "";
    return at ? t("From your AI coach, written at {time}.", { time: at }) : t("From your AI coach.");
  }
  if (advice.mode === "rules") return t("From the fixed rules (your setting: rules only).");
  return t("From the fixed rules.");
}

function changesText(changes) {
  return (changes || []).map((c) => t(CHANGES[c] || c)).join(", ");
}

/** What really changed against the version before (colour, safety level, sessions). */
function versionDiffText(differences) {
  return (differences || []).map((d) => {
    if (d.field === "colour") return t("colour {from} → {to}", { from: t(COLOURS[d.from] || d.from || "—").toLowerCase(), to: t(COLOURS[d.to] || d.to || "—").toLowerCase() });
    if (d.field === "safety_level") return t("now: {level}", { level: t(LEVELS[d.to] || d.to || "—") });
    return t("session decision changed: {sessions}", { sessions: (d.sessions || []).join(", ") });
  }).join("; ");
}

/** When the shown AI advice was written and, for a new version, what came in before it. */
function updatedText(advice) {
  const entry = advice.entry;
  if (advice.source !== "ai" || !entry?.at) return "";
  const time = userTime(entry.at);
  const what = changesText(entry.changes);
  let line = what
    ? t("Updated at {time} after {what}.", { time, what })
    : entry.for_date && entry.for_date !== advice.date
      ? t("From yesterday evening's review, written at {time}.", { time })
      : t("Written at {time}.", { time });
  const diff = versionDiffText(entry.differences);
  if (diff) line += ` ${t("Changed: {what}.", { what: diff })}`;
  return line;
}

/** Every version of the day's advice, newest first, for the Coach page (#90). */
export function versionsHtml(advice) {
  const all = advice?.versions || [];
  const shown = advice?.source === "ai" ? advice.entry?.id : null;
  const earlier = all.filter((v) => v.id !== shown);
  if (!earlier.length) return "";
  const items = earlier.map((v) => {
    const time = v.at ? userTime(v.at) : "";
    const colour = v.valid
      ? `<span class="chip ${TONES[v.colour] || ""}">${esc(t(COLOURS[v.colour] || v.colour))}</span>`
      : `<span class="chip">${esc(t("Not used: incomplete"))}</span>`;
    const notes = [
      v.changes?.length ? t("after {what}", { what: changesText(v.changes) }) : "",
      v.first ? t("first advice for this day") : versionDiffText(v.differences),
    ].filter(Boolean).join(" · ");
    return `<li data-advice-version="${esc(String(v.id))}"><span class="advice-version-head"><time datetime="${esc(v.at || "")}">${esc(time)}</time> ${colour}</span>
      <span>${esc(v.advice || "")}</span>${notes ? ` <span class="sub">${esc(notes)}</span>` : ""}</li>`;
  }).join("");
  return `<details class="advice-versions" data-refresh-key="coach-advice-versions" data-advice-versions><summary>${esc(t("Earlier versions ({n})", { n: earlier.length }))}</summary>
    <ol>${items}</ol></details>`;
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
  const details = `<details data-refresh-key="${esc(keyPrefix)}-guidance-details" data-advice-details><summary>${t("Why this advice")}</summary>
      ${why}<p class="sub">${esc(sourceLine(advice))}</p>${differencesHtml(advice)}</details>`;
  // Where the advice stands and the update button on one line, so the card stays within one
  // screen on a phone.
  const status = updated || updating || fallback || refresh
    ? `<p class="sub advice-status${fallback ? " advice-fallback" : ""}">${updated}${updating}${fallback}${refresh ? refreshButtonHtml() : ""}</p>`
    : "";
  return `<p class="verdict-advice dash-keep-line" data-dash-main>${esc(advice.advice || "")}</p>${status}${details}`;
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
