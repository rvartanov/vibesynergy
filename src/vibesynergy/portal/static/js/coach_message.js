// One message format for the coach: every entry leads with its type (icon + word +
// colour from the tones), then the advice, then the coach's verdict per session as a
// status chip. Colour is never the only signal: each chip has an icon and a word.
import { esc } from "./format.js";
import { N, t } from "./i18n.js";

// Verdict per planned session -> [tone, label]. The same words in portal and mail.
export const DECISIONS = {
  keep: ["planned", N("Keep")],
  adjust: ["advice", N("Adjust")],
  skip: ["recovery", N("Skip")],
  done: ["success", N("Done")],
  missed: ["warning", N("Missed")],
  proposal: ["info", N("Proposal")],
};

// Entry type -> message tone.
const TONES = {
  morning: "advice",
  evening: "advice",
  weekly: "advice",
  block: "advice",
  activity_assessment: "success",
  plan_change: "planned",
  threshold_change: "info",
  note: "info",
  data_check: "warning",
};

export function entryTone(type) {
  return TONES[type] || "info";
}

export function sessionsHtml(sessions) {
  const rows = (Array.isArray(sessions) ? sessions : [])
    .filter((s) => s && DECISIONS[s.decision] && s.title)
    .map((s) => {
      const [tone, label] = DECISIONS[s.decision];
      const note = s.note ? ` <span class="sub">${esc(s.note)}</span>` : "";
      return `<li><span class="chip ${tone}">${esc(t(label))}</span> ${esc(s.title)}${note}</li>`;
    });
  return rows.length ? `<ul class="coach-sessions">${rows.join("")}</ul>` : "";
}

// Entries of the last two days are "now"; older ones go to the collapsed archive.
export function splitCurrent(entries, today) {
  const cutoff = new Date(`${today}T00:00:00Z`);
  cutoff.setUTCDate(cutoff.getUTCDate() - 1);
  const edge = cutoff.toISOString().slice(0, 10);
  const day = (e) => e.for_date || (e.at ? e.at.slice(0, 10) : "");
  const open = (e) => e.entry_type === "threshold_change" && e.meta?.status === "proposed";
  return {
    current: entries.filter((e) => day(e) >= edge || open(e)),
    archive: entries.filter((e) => day(e) < edge && !open(e)),
  };
}
