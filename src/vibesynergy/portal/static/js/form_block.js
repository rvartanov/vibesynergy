// Fitness, fatigue and form with the projection to the races: the Form tab under
// Training → Fitness. Planning & progress links here with a one-line summary.
import { esc, isNum, number } from "./format.js";
import { N, t } from "./i18n.js";
import { fitnessChart, fitnessLegendHtml } from "./fitness_chart.js";

// intervals.icu's form zones: name and what it means for the race.
export const FORM_ZONE = {
  transition: [N("Transition"), N("very rested; fitness is fading")],
  fresh: [N("Fresh"), N("ready to race")],
  grey: [N("Grey zone"), N("keeping fitness, not building it")],
  optimal: [N("Optimal"), N("building fitness")],
  high_risk: [N("High risk"), N("very tired; plan rest soon")],
};

// The past shown in the fitness chart; the projection to the races always follows.
export const FITNESS_RANGES = [
  ["42", N("6 weeks")],
  ["90", N("3 months")],
  ["182", N("6 months")],
  ["365", N("1 year")],
];

// Fitness and the projection ------------------------------------------------------------

export function zoneText(zone) {
  return FORM_ZONE[zone] ? t(FORM_ZONE[zone][0]) : "–";
}

// Any training load at all in the past rows (fitness or fatigue above zero)?
export function hasLoad(fitness) {
  return (fitness?.rows || []).some((r) => (isNum(r[2]) && r[2] !== 0) || (isNum(r[3]) && r[3] !== 0));
}

// One plain line before the numbers: where fitness is heading over the last week.
function trendLine(c) {
  const ramp = c.ctl_ramp_7d;
  if (!isNum(ramp)) return "";
  const text = ramp >= 1 ? t("Your fitness is building.") : ramp <= -1 ? t("Your fitness is dropping.") : t("Your fitness is steady.");
  return `<p class="dash-recovery-summary">${esc(text)}</p>`;
}

export function formHtml(fitness, projection, range = "90") {
  const c = fitness?.current;
  if (!c) return "";
  const rangePicker = `<div class="filters"><label>${t("Range")} <select class="btn" id="form-range" name="range">${FITNESS_RANGES.map(([v, label]) => `<option value="${v}"${v === range ? " selected" : ""}>${esc(t(label))}</option>`).join("")}</select></label></div>`;
  if (!hasLoad(fitness)) {
    // No load yet: an empty state instead of flat zero lines and "0" tiles. The
    // range stays: a longer one may have load.
    return `<section class="card" aria-labelledby="form-title">
      <h2 id="form-title">${t("Fitness, fatigue and form")}</h2>
      ${rangePicker}
      <p class="empty">${t("No training load yet. It comes from intervals.icu, or without it from your heart rate: set your maximum heart rate under Zones & thresholds.")} <a href="#/zones">${t("Zones & thresholds")}</a></p>
    </section>`;
  }
  const zone = FORM_ZONE[c.form_zone];
  const signed = (v, digits = 0) => (isNum(v) ? `${v > 0 ? "+" : ""}${number(v, digits)}` : "–");
  const tile = (label, value, context) => `<div class="card tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="context">${context}</div></div>`;
  const source = fitness.source === "vibesynergy"
    ? t("VibeSynergy's own calculation: power on the bike, otherwise heart rate or pace, from your thresholds.")
    : !fitness.has_intervals
    ? t("No intervals.icu data yet: these are VibeSynergy's own estimates from heart rate.")
    : fitness.days_by_source?.computed
      ? t("From intervals.icu, like in its app. Days it has no figures for are VibeSynergy's estimate (dashed in the chart).")
      : t("From intervals.icu, like in its app.");
  const proj = projection?.rows?.length
    ? `<p class="sub">${esc(t("After today: a projection if you do the plan as written. It changes when the plan changes."))}</p>
      <details class="details"><summary>${t("How the projection is made")}</summary>
        <p class="sub">${esc(t("Planned load per day: the workout's load in intervals.icu ({a} workouts); without one, its planned time at your recent load per hour of {rate} ({b} workouts); weeks with only a weekly target get that target spread over the week ({c} weeks). Nothing else is added.", { a: number(projection.basis.workouts_with_load), b: number(projection.basis.workouts_from_time), c: number(projection.basis.weeks_from_targets), rate: number(projection.basis.load_per_hour) }))}</p>
      </details>`
    : "";
  return `<section class="card" aria-labelledby="form-title">
    <h2 id="form-title">${t("Fitness, fatigue and form")}</h2>
    ${trendLine(c)}
    <div class="grid tiles">
      ${tile(t("Fitness"), number(c.ctl), esc(t("42-day average load")))}
      ${tile(t("Fatigue"), number(c.atl), esc(t("7-day average load")))}
      ${tile(t("Form"), signed(c.tsb), zone ? `<span class="chip" data-form-zone="${esc(c.form_zone)}">${esc(t(zone[0]))}</span> ${esc(t(zone[1]))}` : "")}
      ${tile(t("Ramp rate"), signed(c.ctl_ramp_7d, 1), esc(t("fitness change over 7 days")))}
    </div>
    ${rangePicker}
    ${fitnessLegendHtml({ projection: Boolean(projection?.rows?.length) })}
    <div class="chart pmc form-chart" role="img" aria-label="${esc(t("Fitness, fatigue and form over the chosen range and projected to the races"))}"></div>
    <p class="sub">${esc(source)}</p>
    ${proj}
  </section>`;
}

/** Draw the chart and wire the range picker after formHtml is in the page. */
export function mountForm(root, data, onRange) {
  root.querySelector("#form-range")?.addEventListener("change", (event) => onRange(event.target.value));
  const el = root.querySelector(".form-chart");
  if (el && hasLoad(data.fitness)) fitnessChart(el, data.fitness.rows || [], { projection: data.projection, zoneText });
}

/** Planning & progress: where form stands now, and the way to the whole picture. */
export function formLinkHtml(fitness) {
  const c = fitness?.current;
  const zone = c && hasLoad(fitness) ? FORM_ZONE[c.form_zone] : null;
  const now = zone ? `<span class="chip" data-form-zone="${esc(c.form_zone)}">${esc(t(zone[0]))}</span> ${esc(t(zone[1]))}` : esc(t("No training load yet."));
  return `<section class="card" aria-labelledby="plan-form-title">
    <h2 id="plan-form-title">${t("Form")}</h2>
    <p>${now}</p>
    <p><a href="#/zones/form">${esc(t("Fitness, fatigue and form, and the projection to your races"))}</a></p>
  </section>`;
}
