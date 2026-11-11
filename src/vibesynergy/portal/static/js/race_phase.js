// Shared presentation of the query layer's next_race and plan_phase contract.
import { esc, km, longDate, number } from "./format.js";
import { N, t } from "./i18n.js";

const PHASES = { build: N("Build"), peak: N("Peak"), taper: N("Taper"), recovery: N("Recovery") };
const PRIORITIES = {
  A: N("Primary race"),
  B: N("Supporting"),
  C: N("No priority"),
  primary: N("Primary race"),
  other: N("Other race"),
};

function tuneUpHtml(supporting) {
  const event = Array.isArray(supporting) ? supporting[0] : null;
  if (!event) return "";
  const name = event.name || event.title || "";
  const days = Number(event.days_to_go);
  if (!name || !Number.isFinite(days)) return "";
  const key = days === 1 ? "Tune-up: {name} in {n} day" : "Tune-up: {name} in {n} days";
  return `<p class="sub">${esc(t(key, { name, n: number(days) }))}</p>`;
}

export function racePhaseHtml({ next_race: race, plan_phase: phase, supporting } = {}) {
  const heading = `<h2>${t("Next race and plan phase")}</h2>`;
  if (!race) return `${heading}<div class="empty">${t("No upcoming race in the plan.")}</div>`;
  const distance = typeof race.distance_m === "number" ? ` · ${esc(km(race.distance_m))}` : "";
  const priority = ` · ${t(PRIORITIES[race.priority] || PRIORITIES.other)}`;
  const countdown = race.days_to_go === 0 ? t("Today") : t("{n} days to go", { n: number(race.days_to_go) });
  const left = phase ? Number(phase.days_left) : null;
  const leftKey = left === 1 ? "{n} day left in phase" : "{n} days left in phase";
  const phaseText = phase ? `${t("Plan phase")}: ${t(PHASES[phase.phase] || phase.phase)} · ${t(leftKey, { n: number(left) })}` : "";
  return `${heading}<div class="race"><div class="race-days">${esc(countdown)}</div>
    <div><b>${esc(race.name || t("Race"))}</b><div class="sub">${esc(longDate(race.date))}${distance}${priority}</div></div></div>
    ${tuneUpHtml(supporting)}
    ${phaseText ? `<p class="sub">${esc(phaseText)}</p>` : ""}`;
}
