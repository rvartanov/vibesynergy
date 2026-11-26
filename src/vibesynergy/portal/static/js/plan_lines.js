// Planned against done for one session (#116): two short lines, and the reason on top when
// it is changed (#103). Used by the start page, the day view and This week.
//   Gepland: Zwemmen duur 40' — 3x300 Middel
//   Gedaan:  Zwemmen 2500 m, 68' — 5x50 + 3x100 + 4x(200+50) + 7x50 · RPE 4
// The sets come from query/plan_match.py (steps and laps), already as short text.
import { esc, hoursMinutes, isNum, km, number, sportName } from "./format.js";
import { N, t } from "./i18n.js";

const INTENSITY = { easy: N("easy"), moderate: N("moderate"), hard: N("hard"), race: N("race pace") };

// Why a done session is "changed" against the plan (plan.deviation): one plain sentence.
export function deviationText(dev) {
  if (!dev) return t("Done differently from the plan.");
  const time = (s) => (isNum(s) ? hoursMinutes(s) : "?");
  switch (dev.kind) {
    case "harder":
      return isNum(dev.ceiling)
        ? t("Harder than planned: {n} min above zone {z}.", { n: Math.round(dev.above_s / 60), z: dev.ceiling })
        : t("Harder than planned: {n} min above the planned zone.", { n: Math.round(dev.above_s / 60) });
    case "longer":
      return t("Longer than planned: {done} instead of {planned}.", { done: time(dev.actual_s), planned: time(dev.planned_s) });
    case "shorter":
      return t("Shorter than planned: {done} instead of {planned}.", { done: time(dev.actual_s), planned: time(dev.planned_s) });
    case "distance":
      return isNum(dev.actual_m) && isNum(dev.planned_m)
        ? t("Another distance than planned: {done} instead of {planned}.", { done: km(dev.actual_m), planned: km(dev.planned_m) })
        : t("Another distance than planned.");
    case "intensity":
      return t("Another intensity than planned: {done} instead of {planned}.", { done: t(INTENSITY[dev.done] || dev.done), planned: t(INTENSITY[dev.planned] || dev.planned) });
    case "structure": {
      const done = dev.done || (dev.done_shape === "steady" ? t("steady") : "");
      if (done && dev.planned) return t("Another workout than planned: {done} instead of {planned}.", { done, planned: dev.planned });
      return t("Another workout than planned.");
    }
    default:
      return t("Done differently from the plan.");
  }
}

// The status word: "done, not checked" when the laps were too thin to compare (#116).
export function statusWord(status, comparison, fallback) {
  if (status === "done" && comparison?.check === "not_checked") return t("done, not checked");
  return fallback;
}

function isSwim(sport) {
  return /swim/i.test(String(sport || ""));
}

export function plannedLine(title, comparison) {
  const sets = comparison?.planned;
  return sets ? `${title} — ${sets}` : String(title || "");
}

// actual: {sport, moving_s | duration_s, distance_m}. ``sport``: the planned session's, so a
// pool swim reads "Swimming", not "Lap swimming".
export function doneLine(actual, comparison, rpe, sport) {
  if (!actual) return "";
  const bits = [];
  const metres = actual.distance_m;
  if (isNum(metres) && metres > 0) bits.push(isSwim(actual.sport) ? `${number(metres)} m` : km(metres));
  const seconds = isNum(actual.moving_s) ? actual.moving_s : isNum(actual.duration_s) ? actual.duration_s : actual.elapsed_s;
  if (isNum(seconds) && seconds > 0) bits.push(`${Math.round(seconds / 60)}'`);
  let text = [sportName(sport || actual.sport) || t("Activity"), bits.join(", ")].filter(Boolean).join(" ");
  if (comparison?.done) text += ` — ${comparison.done}`;
  const effort = isNum(rpe) ? rpe : comparison?.rpe;
  if (isNum(effort)) text += ` · ${t("RPE {n}", { n: number(effort) })}`;
  return text;
}

// The reason (changed only), then Gepland and Gedaan (without an activity: the reason).
export function planLinesHtml({ title, sport, status, comparison, deviation, actual, rpe }) {
  if (status !== "done" && status !== "changed") return "";
  const why = status === "changed" ? `<span class="sub dash-why">${esc(deviationText(deviation))}</span>` : "";
  if (!actual) return why;
  return `${why}<span class="sub plan-line"><b>${esc(t("Planned:"))}</b> ${esc(plannedLine(title, comparison))}</span>`
    + `<span class="sub plan-line"><b>${esc(t("Done:"))}</b> ${esc(doneLine(actual, comparison, rpe, sport))}</span>`;
}
