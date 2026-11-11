// Recovery: how rested you are. HRV against your own normal band (the band is the
// signal, one night is not), resting heart rate, Garmin's readiness and Body Battery per
// day, and your morning check-ins. Missing data is never drawn as zero.
import { get } from "../api.js";
import { alpha, mount, token, tooltipBase } from "../charts.js";
import { esc, isNum, number, shortDate } from "../format.js";
import { N, t } from "../i18n.js";
import {
  apiParams,
  bindPeriodPicker,
  isoToday,
  mergePeriodValue,
  periodPickerHtml,
  saveStored,
  writePeriodQuery,
} from "../period_picker.js";

const STORAGE = "vibesynergy.period.recovery";
const DEFAULT = { period: "last_1m" };
const EXTRA = [["last_12m", N("12 months")]];
const CHECKIN = [
  ["sleep_quality", N("Sleep")],
  ["soreness", N("Soreness")],
  ["mood", N("Mood")],
  ["stress", N("Stress")],
  ["motivation", N("Motivation")],
];

function column(data, metric) {
  const i = data.columns.indexOf(metric);
  return i < 0 ? data.rows.map(() => null) : data.rows.map((row) => row[i]);
}

function last(values) {
  for (let i = values.length - 1; i >= 0; i -= 1) if (isNum(values[i])) return { value: values[i], index: i };
  return null;
}

function tilesHtml(data) {
  const tile = (label, main, context) =>
    `<div class="card tile"><div class="label">${esc(label)}</div><div class="value">${main}</div><div class="context">${esc(context)}</div></div>`;
  const tiles = [];
  const hrv = last(column(data, "hrv.night_avg"));
  if (hrv) {
    const low = column(data, "hrv.baseline_low")[hrv.index];
    const high = column(data, "hrv.baseline_high")[hrv.index];
    const where = !isNum(low) || !isNum(high)
      ? t("no normal band yet")
      : hrv.value < low ? t("below your normal band ({low}–{high})", { low: number(low), high: number(high) })
      : hrv.value > high ? t("above your normal band ({low}–{high})", { low: number(low), high: number(high) })
      : t("within your normal band ({low}–{high})", { low: number(low), high: number(high) });
    tiles.push(tile(t("HRV last night"), `${esc(number(hrv.value))} ms`, where));
  }
  const rhr = last(column(data, "hr.resting"));
  if (rhr) {
    const values = column(data, "hr.resting").filter(isNum);
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    tiles.push(tile(t("Resting heart rate"), `${esc(number(rhr.value))} bpm`, t("average in this period {n} bpm", { n: number(mean) })));
  }
  const ready = last(column(data, "recovery.readiness"));
  if (ready) tiles.push(tile(t("Readiness (Garmin)"), esc(number(ready.value)), shortDate(data.rows[ready.index][0])));
  return tiles.length ? `<section class="grid tiles">${tiles.join("")}</section>` : "";
}

function lineChart(el, data, lines, { band = null } = {}) {
  const days = data.rows.map((row) => row[0]);
  const muted = token("muted");
  const series = lines.map(([metric, label, colour]) => ({
    name: t(label),
    type: "line",
    data: column(data, metric),
    connectNulls: false,
    symbolSize: 5,
    lineStyle: { width: 2, color: token(colour) },
    itemStyle: { color: token(colour) },
  }));
  if (band) {
    const low = column(data, band[0]);
    const high = column(data, band[1]);
    // The normal band as a shaded area: an invisible base plus the band's height.
    series.unshift(
      { name: "low", type: "line", data: low, stack: "band", lineStyle: { opacity: 0 }, symbol: "none", tooltip: { show: false } },
      {
        name: t("Normal band"),
        type: "line",
        data: high.map((h, i) => (isNum(h) && isNum(low[i]) ? h - low[i] : null)),
        stack: "band",
        lineStyle: { opacity: 0 },
        symbol: "none",
        areaStyle: { color: alpha(token("s1"), 0.18) },
        tooltip: { show: false },
      },
    );
  }
  mount(el, {
    grid: { left: 44, right: 16, top: 16, bottom: 28 },
    xAxis: { type: "category", data: days, axisLabel: { color: muted, formatter: (d) => shortDate(d) }, axisLine: { lineStyle: { color: token("axis") } }, axisTick: { show: false } },
    yAxis: { type: "value", scale: true, splitNumber: 3, axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } } },
    tooltip: { ...tooltipBase(), trigger: "axis" },
    series,
  });
}

function checkinsHtml(checkins) {
  if (!checkins.length) return `<p class="empty">${esc(t("No check-ins in this period."))}</p>`;
  const head = CHECKIN.map(([, label]) => `<th scope="col" class="num">${esc(t(label))}</th>`).join("");
  const rows = checkins.map((c) => `<tr><th scope="row">${esc(shortDate(c.date))}</th>${CHECKIN.map(([field]) => `<td class="num">${isNum(c[field]) ? esc(number(c[field])) : "—"}</td>`).join("")}<td>${esc(c.note || "")}</td></tr>`).join("");
  return `<div class="table-wrap"><table aria-label="${esc(t("Morning check-ins"))}"><thead><tr><th scope="col">${esc(t("Day"))}</th>${head}<th scope="col">${esc(t("Note"))}</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="sub">${esc(t("1 to 5; for soreness and stress, 1 is none."))}</p>`;
}

export async function render(view, { query, meta, stale }) {
  const today = meta?.data?.today || isoToday();
  const value = mergePeriodValue(query, STORAGE, DEFAULT, { today });
  const reply = await get("recovery", apiParams(value, today));
  if (stale()) return;
  document.title = `${t("Recovery")} · VibeSynergy`;
  const { data, meta: replyMeta } = reply;
  const s = data.series;
  const has = (metric) => column(s, metric).some(isNum);
  view.innerHTML = `
    <div class="page-head"><h1>${t("Recovery")}</h1>
      <div class="sub">${esc(shortDate(replyMeta.period.start))} – ${esc(shortDate(replyMeta.period.end))}</div></div>
    <form class="filters">${periodPickerHtml({ id: "period", value, extra: EXTRA, today })}</form>
    ${tilesHtml(s)}
    <section class="card"><h2>${t("HRV and your normal band")}</h2>
      ${has("hrv.night_avg") ? `<div class="chart rec-hrv" role="img" aria-label="${esc(t("HRV per night with your normal band"))}"></div>
      <p class="sub">${esc(t("A night outside the band is not a signal on its own; several nights in a row are."))}</p>` : `<p class="empty">${esc(t("No HRV in this period."))}</p>`}</section>
    <section class="card"><h2>${t("Resting heart rate")}</h2>
      ${has("hr.resting") ? `<div class="chart rec-rhr" role="img" aria-label="${esc(t("Resting heart rate per day"))}"></div>` : `<p class="empty">${esc(t("No resting heart rate in this period."))}</p>`}</section>
    <section class="card"><h2>${t("Readiness and Body Battery")}</h2>
      ${has("recovery.readiness") || has("recovery.body_battery.max") ? `<div class="chart rec-ready" role="img" aria-label="${esc(t("Readiness and highest Body Battery per day"))}"></div>` : `<p class="empty">${esc(t("No readiness in this period."))}</p>`}</section>
    <details class="card collapsible"><summary>${esc(t("Morning check-ins ({n})", { n: data.checkins.length }))}</summary>${checkinsHtml(data.checkins)}</details>`;
  bindPeriodPicker(view, {
    id: "period",
    value,
    extra: EXTRA,
    today,
    onChange: (next) => {
      saveStored(STORAGE, next);
      const params = writePeriodQuery(new URLSearchParams(), next, { defaultValue: DEFAULT });
      const qs = params.toString();
      location.hash = `#/recovery${qs ? `?${qs}` : ""}`;
    },
  });
  const hrv = view.querySelector(".rec-hrv");
  if (hrv) lineChart(hrv, s, [["hrv.night_avg", N("HRV"), "s1"]], { band: ["hrv.baseline_low", "hrv.baseline_high"] });
  const rhr = view.querySelector(".rec-rhr");
  if (rhr) lineChart(rhr, s, [["hr.resting", N("Resting heart rate"), "s2"]]);
  const ready = view.querySelector(".rec-ready");
  if (ready) lineChart(ready, s, [["recovery.readiness", N("Readiness"), "s3"], ["recovery.body_battery.max", N("Body Battery (highest)"), "s4"]]);
}
