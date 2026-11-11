// Training analysis, the second tab of Planning & progress (the former Training page,
// #/training redirects here): fitness, fatigue and form (the performance management chart), weekly volume
// per sport, time in heart-rate, pace and power zones, fitness markers and the power curve.
// The numbers are VibeSynergy's own (load per activity from power, pace or the heart-rate
// stream, with the thresholds valid that day). TRIMP, TSS and Garmin's load stay available
// to compare. The power curve reads every power stream, so it loads on its own.
import { get } from "../api.js";
import { alpha, mount, series, token, tooltipBase } from "../charts.js";
import { addDays, clock, esc, hoursMinutes, isNum, km, monthYear, number, shortDate, sportName } from "../format.js";
import { N, t } from "../i18n.js";
import { fitnessChart, fitnessLegendHtml } from "../fitness_chart.js";
import { speedText, speedUnit } from "../sport_units.js";
import {
  apiParams,
  bindPeriodPicker,
  isoToday,
  mergePeriodValue,
  periodPickerHtml,
  resolvePeriod,
  saveStored,
  writePeriodQuery,
} from "../period_picker.js";
import { zoneBadge, zoneLabel, zoneRows, zoneTable, zoneTitle, zoneToken } from "../zone_presentation.js";

const STORAGE = "vibesynergy.period.training";
const DEFAULT = { period: "last_3m" };
const EXTRA = [["all", N("All time")]];
const SPORTS = [
  ["", N("All sports")],
  ["run", N("Running")],
  ["cycl", N("Cycling")],
  ["walk", N("Walking")],
  ["swim", N("Swimming")],
];
const LOADS = [
  ["vibesynergy", N("VibeSynergy")],
  ["trimp", N("Heart rate (TRIMP)")],
  ["tss", N("Power (TSS)")],
  ["garmin", N("Garmin training load")],
];
const BEST_NAMES = { 400: "400 m", 1000: "1 km", 1609.34: N("Mile"), 5000: "5 km", 10000: "10 km", 21097.5: N("Half marathon") };
// Volume is grouped into families, so the chart stays readable with a dozen sport names.
const FAMILIES = [
  [N("Running"), /run/i],
  [N("Cycling"), /cycl|ride/i],
  [N("Walking & hiking"), /walk|hik/i],
  [N("Swimming"), /swim/i],
  [N("Gym & strength"), /strength|training|fitness|yoga|pilates|mobility|elliptical|row/i],
];
const OTHER = N("Other");
// Friel's form zones (form as % of fitness) and, for the other loads, the older bands.
const FORM_ZONE = {
  transition: [N("Transition"), N("very rested; fitness is fading")],
  fresh: [N("Fresh"), N("ready to race")],
  grey: [N("Grey zone"), N("keeping fitness, not building it")],
  optimal: [N("Optimal"), N("building fitness")],
  high_risk: [N("High risk"), N("very tired; plan rest soon")],
};
const FORM_HELP = {
  fresh: N("rested; fitness may be dropping"),
  neutral: N("balanced"),
  "optimal training": N("productive training load"),
  "overreaching risk": N("fatigue well above fitness"),
};
const VOLUME_PER = { day: N("Volume per day"), week: N("Volume per week"), month: N("Volume per month"), year: N("Volume per year") };
const CURVE_SPORTS = [
  ["cycl", N("Cycling")],
  ["run", N("Running")],
];

export async function render(view, { query, meta, stale }) {
  const today = meta?.data?.today || isoToday();
  const periodValue = mergePeriodValue(query, STORAGE, DEFAULT, { today });
  const state = {
    sport: query.get("sport") || "",
    load: query.get("load") || "vibesynergy",
    include_coaching: query.get("include_coaching") !== "false",
    curve: query.get("curve") || "cycl",
  };
  const reply = await get("training", {
    ...apiParams(periodValue, today),
    sport: state.sport,
    load: state.load,
    include_coaching: state.include_coaching,
  });
  if (stale()) return;
  const { data, meta: replyMeta } = reply;
  const load = data.load;
  // Missing load is never drawn as flat zero lines or "0" tiles.
  const withLoad = (load.rows || []).some((r) => [1, 2, 3].some((i) => isNum(r[i]) && r[i] !== 0));

  view.innerHTML = `
    <div class="page-head"><h2>${t("Training analysis")}</h2></div>
    <form class="filters">
      ${periodPickerHtml({ id: "period", value: periodValue, extra: EXTRA, today })}
      <label>${t("Sport")} ${select("sport", SPORTS, state.sport)}</label>
      <label>${t("Load")} ${select("load", LOADS, state.load)}</label>
      <label class="check"><input type="checkbox" name="include_coaching"${state.include_coaching ? " checked" : ""}> ${t("Include coaching load")}</label>
    </form>
    ${load.current && withLoad ? tilesHtml(load.current) : ""}
    <section class="card">
      <h2>${t("Fitness, fatigue and form")}</h2>
      ${withLoad ? `${fitnessLegendHtml()}
      <div class="chart pmc" role="img" aria-label="${esc(t("Fitness, fatigue and form over the period"))}"></div>` : `<p class="empty">${t("No training load yet. VibeSynergy counts it from power, pace or heart rate: set your thresholds under Zones & thresholds.")} <a href="#/zones">${t("Zones & thresholds")}</a></p>`}
      ${withLoad ? `<p class="sub">${esc(t("Coaching load in this period: {n}", { n: number(Object.values(load.coaching_daily_load).reduce((sum, value) => sum + value, 0), 1) }))}</p>` : ""}
      <p class="sub">${esc(loadNote(load))}</p>
      ${loadDetails(load)}
    </section>
    <section class="card">
      <h2>${esc(t(VOLUME_PER[data.granularity] || "Volume"))}</h2>
      <div class="chart volume" role="img" aria-label="${esc(t("Training time per period and sport"))}"></div>
      ${volumeTable(data.volume.totals)}
    </section>
    ${zonesCard(data.zone_kinds || { hr: data.zones }, state.sport)}
    <section class="card">${fitnessHtml(data.fitness)}</section>
    <section class="card">
      <div class="page-head"><h2>${t("Power curve")}</h2>
        <label class="sub">${t("Sport")} ${select("curve", CURVE_SPORTS, state.curve)}</label></div>
      <div class="curve"><div class="loading">${t("Reading power data… (a long period takes a while)")}</div></div>
    </section>
    <section class="card" data-range-own>
      <h2>${t("Best running efforts")}</h2>
      <div class="bests"><div class="loading">${t("Reading runs…")}</div></div>
    </section>`;

  // One range for the whole page: every card says which one it shows.
  const chosen = resolvePeriod(periodValue, today);
  const pageRange = `${shortDate(chosen.start)} – ${shortDate(chosen.end)}`;
  for (const card of view.querySelectorAll("section.card:not([data-range-own])")) {
    const heading = card.querySelector("h2");
    heading?.insertAdjacentHTML("afterend", `<p class="sub card-range">${esc(t("Shown: {range}", { range: pageRange }))}</p>`);
  }
  bind(view, state, periodValue, today);
  bindZoneKind(view, state.sport, data.zone_kinds || { hr: data.zones });
  if (withLoad) fitnessChart(view.querySelector(".pmc"), load.rows);
  volumeChart(view.querySelector(".volume"), data.volume.buckets);
  const vo2 = view.querySelector(".vo2");
  if (vo2) vo2Chart(vo2, data.fitness.markers["fitness.vo2max.run"], replyMeta.period);

  const span = resolvePeriod(periodValue, today);
  const spanDays = Math.round((Date.parse(span.end) - Date.parse(span.start)) / 86400000) + 1;
  const paceParams =
    spanDays < 90 ? { start: addDays(span.end, -89), end: span.end } : apiParams(periodValue, today);
  // Best efforts need at least 90 days; say so instead of using a hidden range.
  const bestsHeading = view.querySelector("[data-range-own] h2");
  bestsHeading?.insertAdjacentHTML(
    "afterend",
    `<p class="sub card-range">${esc(spanDays < 90 ? t("Shown: the last 90 days ({range}), because the chosen period is shorter.", { range: `${shortDate(paceParams.start)} – ${shortDate(paceParams.end)}` }) : t("Shown: {range}", { range: `${shortDate(span.start)} – ${shortDate(span.end)}` }))}</p>`,
  );
  const [curve, bests] = await Promise.all([
    get("power_curve", { ...apiParams(periodValue, today), sport: state.curve }),
    get("pace_curve", paceParams),
  ]);
  if (stale()) return;
  curveHtml(view.querySelector(".curve"), curve.data);
  bestsHtml(view.querySelector(".bests"), bests.data);
}

// Axis labels: the day for periods up to a year, month and year beyond.
function dateLabel(long) {
  return long ? (d) => monthYear(d) : (d) => shortDate(d);
}

// Controls ---------------------------------------------------------------------------

function select(name, options, current) {
  return `<select class="btn" name="${name}">${options
    .map(([v, label]) => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(t(label))}</option>`)
    .join("")}</select>`;
}

function bind(view, state, periodValue, today) {
  const defaults = { sport: "", load: "vibesynergy", curve: "cycl", include_coaching: true };
  const navigate = (period) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(state)) {
      if (value && value !== defaults[key]) params.set(key, value);
    }
    writePeriodQuery(params, period, { defaultValue: DEFAULT });
    const qs = params.toString();
    location.hash = `#/planning/analysis${qs ? `?${qs}` : ""}`;
  };
  bindPeriodPicker(view, {
    id: "period",
    value: periodValue,
    extra: EXTRA,
    today,
    onChange: (next) => {
      saveStored(STORAGE, next);
      navigate(next);
    },
  });
  view.querySelectorAll("select, input[name=include_coaching]").forEach((el) => {
    if (el.name?.startsWith("period-") || el.hasAttribute("data-zone-kind")) return;
    el.addEventListener("change", () => {
      state[el.name] = el.type === "checkbox" ? el.checked : el.value;
      navigate(periodValue);
    });
  });
}

// Load --------------------------------------------------------------------------------

function tilesHtml(c) {
  const tile = (label, value, context) =>
    `<div class="card tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="context">${context}</div></div>`;
  const ramp = isNum(c.ctl_ramp_7d) ? `${c.ctl_ramp_7d > 0 ? "+" : ""}${number(c.ctl_ramp_7d, 1)}` : "–";
  const zone = FORM_ZONE[c.form_zone];
  const formContext = zone
    ? `<span class="chip" data-form-zone="${esc(c.form_zone)}">${esc(t(zone[0]))}</span> ${esc(t(zone[1]))}`
    : `<span class="chip">${esc(t(c.form))}</span> ${esc(FORM_HELP[c.form] ? t(FORM_HELP[c.form]) : "")}`;
  const pct = zone && isNum(c.form_pct) ? ` <span class="sub">(${c.form_pct > 0 ? "+" : ""}${number(Math.trunc(c.form_pct))}%)</span>` : "";
  const trend = !isNum(c.ctl_ramp_7d) ? "" : c.ctl_ramp_7d >= 1 ? t("Your fitness is building.") : c.ctl_ramp_7d <= -1 ? t("Your fitness is dropping.") : t("Your fitness is steady.");
  return `${trend ? `<p class="dash-recovery-summary">${esc(trend)}</p>` : ""}<section class="grid tiles">
    ${tile(t("Fitness"), number(c.ctl), t("42-day average load"))}
    ${tile(t("Fatigue"), number(c.atl), t("7-day average load"))}
    ${tile(t("Form"), `${c.tsb > 0 ? "+" : ""}${number(c.tsb)}`, `${formContext}${pct}`)}
    ${tile(t("Ramp rate"), ramp, t("fitness change over 7 days"))}
  </section>`;
}

// How VibeSynergy's own load is made, for who wants to know.
function loadDetails(load) {
  if (load.load !== "vibesynergy") return "";
  return `<details class="details"><summary>${esc(t("Details"))}</summary>
    <p class="sub">${esc(t("Per session: power on the bike (TSS from normalized power and FTP), pace in the water (against CSS), otherwise the heart rate over the whole session (an hour at threshold heart rate counts 100). Always with the thresholds that were valid on that day."))}</p>
    <p class="sub">${esc(t("Fitness and fatigue are 42- and 7-day weighted averages of the daily load; form is today's fitness minus today's fatigue; the form zone reads form as a share of fitness."))}</p>
  </details>`;
}

function loadNote(load) {
  if (load.load === "vibesynergy") {
    return t("Your load as VibeSynergy counts it: power on the bike, pace in the water, otherwise heart rate.");
  }
  if (load.load === "tss") {
    return t("Load is estimated from power and your FTP. Activities without power add nothing.");
  }
  if (load.load === "garmin") {
    return t("Older activities count as zero load until the Garmin history fill reaches them.");
  }
  const p = load.hr_profile || {};
  if (!isNum(p.max_hr)) return t("Set your maximum heart rate under Zones & thresholds to estimate load from heart rate.");
  return t("Load is estimated from each activity's duration and average heart rate (max {max} bpm, resting {rest} bpm). Activities without heart rate add nothing.", { max: number(p.max_hr), rest: number(p.rest_hr) });
}

// Volume ------------------------------------------------------------------------------

function family(sport) {
  if (sport === "coaching") return N("Coaching");
  const found = FAMILIES.find(([, pattern]) => pattern.test(sport || ""));
  return found ? found[0] : OTHER;
}

function volumeChart(el, buckets) {
  const periods = [...new Set(buckets.map((b) => b.period))].sort();
  const names = [...FAMILIES.map(([name]) => name), N("Coaching"), OTHER];
  const hours = Object.fromEntries(names.map((n) => [n, Object.fromEntries(periods.map((p) => [p, 0]))]));
  for (const b of buckets) hours[family(b.sport)][b.period] += b.moving_s / 3600;
  const used = names.filter((n) => periods.some((p) => hours[n][p] > 0));
  const muted = token("muted");
  mount(el, {
    grid: { left: 44, right: 16, top: 30, bottom: 28 },
    legend: { top: 0, textStyle: { color: token("ink-2") }, itemWidth: 10, itemHeight: 10 },
    xAxis: {
      type: "category",
      data: periods,
      axisLabel: { color: muted, formatter: dateLabel(periods.length > 1 && Date.parse(periods[periods.length - 1]) - Date.parse(periods[0]) > 400 * 86400000) },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      name: t("hours"),
      nameTextStyle: { color: muted, align: "left" },
      axisLabel: { color: muted },
      splitLine: { lineStyle: { color: token("grid") } },
    },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) =>
        `<b>${esc(shortDate(items[0].axisValue))}</b><br>` +
        items
          .filter((i) => i.value > 0)
          .map((i) => `${i.marker}${esc(i.seriesName)} ${esc(hoursMinutes(i.value * 3600))}`)
          .join("<br>"),
    },
    series: used.map((name) => ({
      name: t(name),
      type: "bar",
      stack: "time",
      barMaxWidth: 28,
      data: periods.map((p) => Math.round(hours[name][p] * 100) / 100),
      itemStyle: { color: series(names.indexOf(name) + 1) },
    })),
  });
}

function volumeTable(totals) {
  const rows = {};
  for (const [sport, t] of Object.entries(totals)) {
    const r = (rows[family(sport)] ||= { count: 0, moving_s: 0, distance_m: 0, ascent_m: 0 });
    for (const k of Object.keys(r)) r[k] += t[k] || 0;
  }
  const sorted = Object.entries(rows).sort((a, b) => b[1].moving_s - a[1].moving_s);
  if (!sorted.length) return `<div class="empty">${t("No activities in this period.")}</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>${t("Sport")}</th><th>${t("Activities")}</th><th>${t("Time")}</th><th>${t("Distance")}</th><th>${t("Climb")}</th></tr></thead><tbody>${sorted
    .map(
      ([name, r]) =>
        `<tr><td>${esc(t(name))}</td><td>${number(r.count)}</td><td>${esc(hoursMinutes(r.moving_s))}</td><td>${r.distance_m ? esc(km(r.distance_m)) : "–"}</td><td>${r.ascent_m ? `${number(r.ascent_m)} m` : "–"}</td></tr>`,
    )
    .join("")}</tbody></table></div>`;
}

// Zones and fitness -------------------------------------------------------------------

const ZONE_KINDS = ["hr", "pace", "power"];

function zoneKindLabel(kind, sport) {
  if (kind === "pace") return t(speedUnit(sport) === "km/h" ? "Speed" : "Pace");
  if (kind === "power") return t("Power");
  return t("Heart rate");
}

function zonesCard(kinds, sport) {
  const options = ZONE_KINDS.map((kind) => `<option value="${kind}">${esc(zoneKindLabel(kind, sport))}</option>`).join("");
  const panels = ZONE_KINDS.map((kind) => {
    const dist = kinds[kind] || { total_seconds_by_zone: {}, coaching_seconds_by_zone: {}, weekly_seconds_by_zone: {}, activities_with_zones: 0 };
    return `<div data-zone-panel="${kind}"${kind === "hr" ? "" : " hidden"}>${zonePanel(dist, kind, sport)}</div>`;
  }).join("");
  return `<section class="card" data-zones>
    <div class="page-head"><h2 data-zone-heading>${esc(zoneTitle("hr", sport))}</h2>
      <label>${t("Measurement")} <select class="btn" data-zone-kind>${options}</select></label></div>
    <p class="sub">${esc(t("A zone number means a different effort for heart rate, pace and power."))}</p>
    ${panels}
  </section>`;
}

function zonePanel(z, kind, sport) {
  const totals = z.total_seconds_by_zone || {};
  const total = Object.values(totals).reduce((a, b) => a + b, 0);
  if (!total) return `<div class="empty">${t("No recorded zone data in this period.")}</div>`;
  const rows = Object.entries(totals).map(([zone, seconds]) => ({ zone: Number(zone), seconds }));
  const share = kind === "hr" ? z.three_zone_share : null;
  const split = share
    ? `<p class="sub">${esc(t("Easy (zones 1–2) {low}% · moderate (zone 3) {moderate}% · hard (zones 4–5) {high}%", { low: Math.round(share.low * 100), moderate: Math.round(share.moderate * 100), high: Math.round(share.high * 100) }))}${isNum(z.polarisation_index) ? ` · ${esc(t("polarisation index {n}", { n: number(z.polarisation_index, 2) }))}` : ""}</p>`
    : "";
  const weekly = z.weekly_seconds_by_zone || {};
  const weeks = Object.keys(weekly).sort();
  const weekBlock = weeks.length ? `${zoneWeekLegend(weekly)}${zoneWeekChartSlot()}${zoneWeekTable(weekly)}` : "";
  const coaching = Object.values(z.coaching_seconds_by_zone || {}).reduce((sum, value) => sum + value, 0);
  return `${zoneRows(rows, { kind, sport })}${zoneTable(rows, { kind, sport })}${split}${weekBlock}
    <p class="sub">${esc(t("Coaching zone time: {time}", { time: hoursMinutes(coaching) }))}</p>
    <p class="sub">${esc(t("From {n} activities with recorded zone data.", { n: number(z.activities_with_zones) }))}</p>`;
}

function zoneIds(weekly) {
  return [...new Set(Object.values(weekly).flatMap((row) => Object.keys(row)))].map(Number).sort((a, b) => a - b);
}

function zoneWeekLegend(weekly) {
  return `<div class="legend zone-legend">${zoneIds(weekly).map((zone) => zoneBadge(zone)).join("")}</div>`;
}

function zoneWeekChartSlot() {
  return `<div class="chart zone-week" role="img" aria-label="${esc(t("Time in zones by week"))}"></div>`;
}

function zoneWeekTable(weekly) {
  const weeks = Object.keys(weekly).sort();
  const ids = zoneIds(weekly);
  const caption = t("Time in zones by week");
  return `<div class="table-wrap zone-table" tabindex="0" role="region" aria-label="${esc(caption)}"><table>
    <caption class="visually-hidden">${esc(caption)}</caption>
    <thead><tr><th scope="col">${esc(t("Week"))}</th>${ids.map((zone) => `<th scope="col">${zoneBadge(zone)}</th>`).join("")}</tr></thead>
    <tbody>${weeks.map((week) => `<tr><th scope="row">${esc(shortDate(week))}</th>${ids.map((zone) => {
      const seconds = weekly[week][String(zone)] ?? weekly[week][zone] ?? 0;
      return `<td class="num">${seconds ? esc(hoursMinutes(seconds)) : "–"}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function bindZoneKind(view, sport, kinds) {
  const select = view.querySelector("[data-zone-kind]");
  const heading = view.querySelector("[data-zone-heading]");
  if (!select) return;
  const show = (kind) => {
    view.querySelectorAll("[data-zone-panel]").forEach((panel) => {
      const on = panel.dataset.zonePanel === kind;
      panel.hidden = !on;
      if (on) paintZoneChart(panel, kinds[kind]);
    });
    if (heading) heading.textContent = zoneTitle(kind, sport);
  };
  select.addEventListener("change", () => show(select.value));
  show(select.value);
}

function paintZoneChart(panel, dist) {
  const el = panel.querySelector(".zone-week");
  if (!el || el.dataset.ready) return;
  const weekly = dist?.weekly_seconds_by_zone || {};
  const weeks = Object.keys(weekly).sort();
  const ids = zoneIds(weekly);
  if (!weeks.length || !ids.length) return;
  el.dataset.ready = "1";
  const muted = token("muted");
  mount(el, {
    grid: { left: 44, right: 16, top: 12, bottom: 28 },
    xAxis: {
      type: "category",
      data: weeks,
      axisLabel: { color: muted, hideOverlap: true, formatter: (v) => shortDate(v) },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      min: 0,
      name: t("hours"),
      nameTextStyle: { color: muted, align: "left" },
      axisLabel: { color: muted },
      splitLine: { lineStyle: { color: token("grid") } },
    },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) => {
        const head = `<b>${esc(shortDate(items[0].axisValue))}</b>`;
        const lines = items
          .filter((item) => item.value > 0)
          .map((item) => `${item.marker}${esc(item.seriesName)} ${esc(hoursMinutes(item.value * 3600))}`);
        return [head, ...lines].join("<br>");
      },
    },
    series: ids.map((zone) => ({
      name: zoneLabel(zone),
      type: "bar",
      stack: "zones",
      barMaxWidth: 28,
      data: weeks.map((week) => {
        const seconds = weekly[week][String(zone)] ?? weekly[week][zone] ?? 0;
        return Math.round((seconds / 3600) * 100) / 100;
      }),
      itemStyle: { color: token(zoneToken(zone)) },
    })),
  });
}

const RACES = [
  ["fitness.race_pred.5k", "5 km"],
  ["fitness.race_pred.10k", "10 km"],
  ["fitness.race_pred.half", N("Half marathon")],
  ["fitness.race_pred.marathon", N("Marathon")],
];

function latest(changes) {
  return changes?.length ? changes[changes.length - 1] : null;
}

// A trend chart only says something once the values cover more than a month.
function spansMonth(changes) {
  if (!changes || changes.length < 2) return false;
  return (Date.parse(changes[changes.length - 1].date) - Date.parse(changes[0].date)) / 86400000 > 31;
}

function fitnessHtml(f) {
  const m = f.markers;
  const vo2 = latest(m["fitness.vo2max.run"]);
  const ride = latest(m["fitness.vo2max.ride"]);
  const age = latest(m["fitness.fitness_age"]);
  const races = RACES.filter(([id]) => latest(m[id]));
  if (!vo2 && !ride && !age && !races.length) {
    return `<h2>${t("Fitness markers")}</h2><div class="empty">${t("No VO2max or race predictions in this period.")}</div>`;
  }
  const facts = [
    vo2 && [t("VO2max (running)"), `${number(vo2.value, 1)}`],
    ride && [t("VO2max (cycling)"), `${number(ride.value, 1)}`],
    age && [t("Fitness age"), t("{n} years", { n: number(age.value, 1) })],
    ...races.map(([id, label]) => [t(label), clock(latest(m[id]).value)]),
  ].filter(Boolean);
  return `<h2>${t("Fitness markers")}</h2>
    <div class="facts">${facts.map(([label, value]) => `<div>${esc(label)}<b>${esc(value)}</b></div>`).join("")}</div>
    ${spansMonth(m["fitness.vo2max.run"]) ? `<div class="chart vo2" role="img" aria-label="${esc(t("Running VO2max over the period"))}"></div>` : ""}
    <p class="sub">${t("Latest values from Garmin in this period.")}</p>`;
}

function vo2Chart(el, changes, period) {
  // Change points -> a step line that holds each value until the next one.
  const points = changes.map((c) => [c.date, c.value]);
  points.push([period.end, changes[changes.length - 1].value]);
  const muted = token("muted");
  mount(el, {
    grid: { left: 36, right: 12, top: 12, bottom: 24 },
    xAxis: { type: "time", axisLabel: { color: muted, hideOverlap: true }, axisLine: { lineStyle: { color: token("axis") } } },
    yAxis: { type: "value", scale: true, axisLabel: { color: muted, formatter: (v) => number(v, 1) }, splitLine: { lineStyle: { color: token("grid") } } },
    tooltip: { ...tooltipBase(), trigger: "axis", valueFormatter: (v) => number(v, 1) },
    series: [{ type: "line", step: "end", data: points, showSymbol: false, lineStyle: { width: 2, color: series(1) } }],
  });
}

// Power curve -------------------------------------------------------------------------

function duration(seconds) {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${seconds / 60} min`;
  return `${seconds / 3600} h`;
}

function curveHtml(el, data) {
  if (!data.curve.length) {
    el.innerHTML = `<div class="empty">${t("No activities with power in this period.")}</div>`;
    return;
  }
  el.innerHTML = `<div class="chart" role="img" aria-label="${esc(t("Best average power per duration"))}"></div>
    <div class="table-wrap"><table><thead><tr><th>${t("Duration")}</th><th>${t("Best power")}</th><th>${t("Activity")}</th></tr></thead><tbody>${data.curve
      .map(
        (p) =>
          `<tr><td>${esc(duration(p.seconds))}</td><td>${number(p.watts)} W</td><td><a href="#/activity/${encodeURIComponent(p.activity)}">${esc(p.name || sportName(p.sport))}</a> <span class="muted">${esc(shortDate(p.date))}</span></td></tr>`,
      )
      .join("")}</tbody></table></div>
    <p class="sub">${esc(t("From {n} activities with power data.", { n: number(data.activities_with_power) }))}</p>`;
  const muted = token("muted");
  mount(el.querySelector(".chart"), {
    grid: { left: 44, right: 16, top: 16, bottom: 28 },
    xAxis: {
      type: "category",
      data: data.curve.map((p) => duration(p.seconds)),
      axisLabel: { color: muted },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
    },
    yAxis: { type: "value", name: "W", nameTextStyle: { color: muted }, axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } } },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      formatter: (items) => {
        const p = data.curve[items[0].dataIndex];
        return `<b>${esc(duration(p.seconds))}: ${number(p.watts)} W}</b><br>${esc(p.name || sportName(p.sport))}, ${esc(shortDate(p.date))}`;
      },
    },
    series: [
      {
        type: "line",
        data: data.curve.map((p) => p.watts),
        symbolSize: 6,
        lineStyle: { width: 2, color: series(1) },
        itemStyle: { color: series(1) },
        areaStyle: { color: alpha(series(1), 0.1) },
      },
    ],
  });
}

function bestsHtml(el, data) {
  if (!data.curve.length) {
    el.innerHTML = `<div class="empty">${t("No runs with distance data in this period.")}</div>`;
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table><thead><tr><th>${t("Distance")}</th><th>${t("Time")}</th><th>${t("Pace")}</th><th>${t("Activity")}</th></tr></thead><tbody>${data.curve
    .map(
      (b) => `<tr><td>${esc(BEST_NAMES[b.distance_m] ? t(BEST_NAMES[b.distance_m]) : `${number(b.distance_m)} m`)}</td><td>${esc(clock(b.seconds))}</td>
        <td>${esc(speedText("running", b.distance_m / b.seconds))}</td>
        <td><a href="#/activity/${encodeURIComponent(b.activity)}">${esc(b.name || sportName(b.sport))}</a> <span class="muted">${esc(shortDate(b.date))}</span></td></tr>`,
    )
    .join("")}</tbody></table></div>
    <p class="sub">${esc(t("Based on {n} runs. Parts faster than running (bike, car) are left out.", { n: number(data.activities_with_distance) }))}</p>`;
}
