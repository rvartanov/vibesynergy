// Body: every weigh-in with a smoothed trend, body composition over time, and blood pressure
// readings. Weight and composition come from the scale, blood pressure from the monitor; one
// source per measurement (the best one), so mirrored copies aren't counted twice.
import { get } from "../api.js";
import { alpha, mount, series, token, tooltipBase } from "../charts.js";
import { esc, isNum, number, shortDate, userDateTime } from "../format.js";
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

const STORAGE = "vibesynergy.period.body";
const DEFAULT = { period: "last_12m" };
const EXTRA = [["last_12m", N("12 months")], ["all", N("All time")]];
const TREND_DAYS = 7; // the weight trend: average of the weigh-ins in the last 7 days
const COMPOSITION = [
  ["body.fat_mass", N("Fat mass"), "s2"],
  ["body.muscle_mass", N("Muscle mass"), "s1"],
];
const DAY_MS = 86400000;

export async function render(view, { query, meta, stale }) {
  const today = meta?.data?.today || isoToday();
  const value = mergePeriodValue(query, STORAGE, DEFAULT, { today });
  const reply = await get("body", apiParams(value, today));
  if (stale()) return;
  document.title = `${t("Body")} · VibeSynergy`;
  const { data, meta: replyMeta } = reply;
  const tz = replyMeta.timezone;
  const readings = (metric) => data.measurements.filter((r) => r.metric === metric);
  const weights = readings("body.weight");
  const bp = pairs(readings("bp.systolic"), readings("bp.diastolic"), readings("bp.pulse"));
  // An empty frame is never shown for missing data.
  const hasComposition = [...COMPOSITION.map(([metric]) => metric), "body.fat_pct"].some((metric) => {
    const i = data.trend.columns.indexOf(metric);
    return i >= 0 && data.trend.rows.some((row) => isNum(row[i]));
  });

  view.innerHTML = `
    <div class="page-head"><h1>${t("Body")}</h1>
      <div class="sub">${esc(shortDate(replyMeta.period.start))} – ${esc(shortDate(replyMeta.period.end))}</div></div>
    <form class="filters">${periodPickerHtml({ id: "period", value, extra: EXTRA, today })}</form>
    ${tilesHtml(data, bp, tz)}
    <section class="card">
      <h2>${t("Weight")}</h2>
      ${weights.length ? `<div class="legend"><span><i style="background:${alpha(series(1), 0.5)}"></i>${t("Weigh-in")}</span><span><i style="background:var(--s1)"></i>${esc(t("{n}-day trend", { n: TREND_DAYS }))}</span></div>
        <div class="chart body-weight" role="img" aria-label="${esc(t("Weigh-ins and weight trend"))}"></div>` : `<div class="empty">${t("No weigh-ins in this period.")}</div>`}
    </section>
    <section class="card">
      <h2>${t("Body composition")}</h2>
      ${hasComposition ? `<div class="legend">${COMPOSITION.map(([, label, c]) => `<span><i style="background:var(--${c})"></i>${t(label)} (kg)</span>`).join("")}<span><i style="background:var(--s3)"></i>${t("Body fat")} (%)</span></div>
        <div class="chart body-comp" role="img" aria-label="${esc(t("Fat mass, muscle mass and body fat percentage over time"))}"></div>` : `<div class="empty">${t("No body composition measured in this period. A scale that measures body fat fills this in.")}</div>`}
    </section>
    <section class="card">
      <h2>${t("Blood pressure")}</h2>
      ${bp.length ? `<div class="legend"><span><i style="background:var(--s8)"></i>${t("Systolic")}</span><span><i style="background:var(--s1)"></i>${t("Diastolic")}</span></div>
        <div class="chart body-bp" role="img" aria-label="${esc(t("Blood pressure readings"))}"></div>${bpTable(bp, tz)}` : `<div class="empty">${t("No blood pressure readings in this period.")}</div>`}
    </section>`;

  bindPeriodPicker(view, {
    id: "period",
    value,
    extra: EXTRA,
    today,
    onChange: (next) => {
      saveStored(STORAGE, next);
      const params = writePeriodQuery(new URLSearchParams(), next, { defaultValue: DEFAULT });
      const qs = params.toString();
      location.hash = `#/body${qs ? `?${qs}` : ""}`;
    },
  });
  if (weights.length) weightChart(view.querySelector(".body-weight"), weights, tz);
  if (hasComposition) compositionChart(view.querySelector(".body-comp"), data.trend);
  if (bp.length) bpChart(view.querySelector(".body-bp"), bp, tz);
}

function select(name, options, current) {
  return `<select class="btn" name="${name}">${options
    .map(([v, label]) => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(t(label))}</option>`)
    .join("")}</select>`;
}

// Systolic, diastolic and pulse taken together share a timestamp.
function pairs(sys, dia, pulse) {
  const byTime = (rows) => Object.fromEntries(rows.map((r) => [r.measured_at, r.value]));
  const d = byTime(dia);
  const p = byTime(pulse);
  return sys.map((r) => ({ at: r.measured_at, sys: r.value, dia: d[r.measured_at], pulse: p[r.measured_at] }));
}

// Tiles -------------------------------------------------------------------------------

function tilesHtml(data, bp, tz) {
  const tile = (label, main, context) =>
    `<div class="card tile"><div class="label">${esc(label)}</div><div class="value">${main}</div><div class="context">${context}</div></div>`;
  const latest = data.latest;
  const tiles = [];
  const w = latest["body.weight"];
  if (w) {
    const s = data.stats["body.weight"];
    const change = s && isNum(s.change) ? t("{change} kg over the period", { change: `${s.change > 0 ? "+" : ""}${number(s.change, 1)}` }) : "";
    tiles.push(tile(t("Weight"), `${esc(number(w.value, 1))}<span class="unit">kg</span>`, esc(change || userDateTime(w.measured_at, tz))));
  }
  const fat = latest["body.fat_pct"];
  if (fat) tiles.push(tile(t("Body fat"), `${esc(number(fat.value, 1))}<span class="unit">%</span>`, esc(t("latest · average {value}", { value: `${number(data.stats["body.fat_pct"]?.mean, 1)}%` }))));
  const muscle = latest["body.muscle_mass"];
  if (muscle) tiles.push(tile(t("Muscle mass"), `${esc(number(muscle.value, 1))}<span class="unit">kg</span>`, esc(t("latest · average {value}", { value: `${number(data.stats["body.muscle_mass"]?.mean, 1)} kg` }))));
  if (bp.length) {
    const last = bp[bp.length - 1];
    tiles.push(tile(t("Blood pressure"), `${number(last.sys)}/${number(last.dia)}<span class="unit">mmHg</span>`, esc(t("latest, {when}", { when: userDateTime(last.at, tz) }))));
  }
  return tiles.length ? `<section class="grid tiles">${tiles.join("")}</section>` : "";
}

// Charts ------------------------------------------------------------------------------

function axisStyle() {
  const muted = token("muted");
  return {
    x: { type: "time", axisLabel: { color: muted, hideOverlap: true }, axisLine: { lineStyle: { color: token("axis") } }, splitLine: { show: false } },
    y: (extra = {}) => ({ type: "value", scale: true, axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } }, ...extra }),
  };
}

// Trend: the mean of the weigh-ins in the TREND_DAYS days up to each weigh-in.
function trend(points) {
  return points.map(([t], i) => {
    const window = [];
    for (let j = i; j >= 0 && t - points[j][0] < TREND_DAYS * DAY_MS; j--) window.push(points[j][1]);
    return [t, Math.round((window.reduce((a, b) => a + b, 0) / window.length) * 10) / 10];
  });
}

function weightChart(el, weights, tz) {
  const points = weights.map((r) => [Date.parse(r.measured_at), r.value]);
  const { x, y } = axisStyle();
  const color = series(1);
  mount(el, {
    grid: { left: 44, right: 16, top: 12, bottom: 28 },
    xAxis: x,
    yAxis: y({ axisLabel: { color: token("muted"), formatter: (v) => `${v} kg` } }),
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      formatter: (items) => {
        const at = items[0].value[0];
        const lines = items.map((i) => `${i.seriesName} ${number(i.value[1], 1)} kg`);
        return `<b>${esc(userDateTime(new Date(at).toISOString(), tz))}</b><br>${lines.join("<br>")}`;
      },
    },
    series: [
      { name: t("Weigh-in"), type: "scatter", data: points, symbolSize: 5, itemStyle: { color: alpha(color, 0.45) } },
      { name: t("Trend"), type: "line", data: trend(points), showSymbol: false, lineStyle: { width: 2, color }, itemStyle: { color } },
    ],
  });
}

function compositionChart(el, trendData) {
  const col = (metric) => {
    const i = trendData.columns.indexOf(metric);
    return trendData.rows.map((row) => [row[0], i < 0 ? null : row[i]]);
  };
  const { x, y } = axisStyle();
  const line = (name, data, color, axisIndex) => ({
    name,
    type: "line",
    data,
    xAxisIndex: axisIndex,
    yAxisIndex: axisIndex,
    connectNulls: true,
    showSymbol: false,
    lineStyle: { width: 2, color: token(color) },
    itemStyle: { color: token(color) },
  });
  // One panel each, so a kilo of fat isn't flattened next to 75 kg of muscle.
  const panels = [...COMPOSITION.map(([, , , unit]) => unit || "kg"), "%"];
  const height = 100 / panels.length;
  mount(el, {
    grid: panels.map((_, i) => ({ left: 44, right: 16, top: `${i * height + 3}%`, height: `${height - 11}%` })),
    xAxis: panels.map((_, i) => ({ ...x, gridIndex: i, axisLabel: { ...x.axisLabel, show: i === panels.length - 1 } })),
    yAxis: panels.map((unit, i) =>
      y({ gridIndex: i, splitNumber: 2, axisLabel: { color: token("muted"), formatter: (v) => (unit === "%" ? `${v}%` : `${v} kg`) } }),
    ),
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      formatter: (items) =>
        `<b>${esc(shortDate(items[0].value[0]))}</b><br>` +
        items.filter((i) => isNum(i.value[1])).map((i) => `${i.marker}${esc(i.seriesName)} ${number(i.value[1], 1)}${i.seriesName === t("Body fat") ? "%" : " kg"}`).join("<br>"),
    },
    series: [
      ...COMPOSITION.map(([metric, label, color], i) => line(t(label), col(metric), color, i)),
      line(t("Body fat"), col("body.fat_pct"), "s3", COMPOSITION.length),
    ],
  });
}

function bpChart(el, bp, tz) {
  const { x, y } = axisStyle();
  const point = (key) => bp.map((r) => [Date.parse(r.at), r[key]]);
  const dots = (name, key, color) => ({
    name,
    type: "line",
    data: point(key),
    symbolSize: 5,
    lineStyle: { width: 1, color: alpha(token(color), 0.5) },
    itemStyle: { color: token(color) },
  });
  const threshold = (value) => ({
    silent: true,
    symbol: "none",
    lineStyle: { color: token("msg-danger"), type: "dashed", opacity: 0.6 },
    label: { color: token("muted"), fontSize: 11, position: "insideEndTop", formatter: t("high ≥ {value}", { value }) },
    data: [{ yAxis: value }],
  });
  mount(el, {
    grid: { left: 44, right: 16, top: 12, bottom: 28 },
    xAxis: x,
    yAxis: y({ min: (r) => Math.min(50, Math.floor(r.min / 10) * 10), max: (r) => Math.max(160, Math.ceil(r.max / 10) * 10) }),
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      formatter: (items) => {
        const r = bp[items[0].dataIndex];
        return `<b>${esc(userDateTime(r.at, tz))}</b><br>${number(r.sys)}/${number(r.dia)} mmHg${isNum(r.pulse) ? ` · ${t("pulse {n}", { n: number(r.pulse) })}` : ""}`;
      },
    },
    series: [
      {
        ...dots(t("Systolic"), "sys", "s8"),
        markLine: threshold(140),
      },
      {
        ...dots(t("Diastolic"), "dia", "s1"),
        markLine: threshold(90),
      },
    ],
  });
}

function bpTable(bp, tz) {
  const recent = bp.slice(-8).reverse();
  return `<div class="table-wrap"><table><thead><tr><th>${t("Taken")}</th><th>${t("Blood pressure")}</th><th>${t("Pulse")}</th></tr></thead><tbody>${recent
    .map((r) => `<tr><th scope="row">${esc(userDateTime(r.at, tz))}</th><td>${number(r.sys)}/${number(r.dia)} mmHg</td><td>${isNum(r.pulse) ? `${number(r.pulse)} bpm` : "–"}</td></tr>`)
    .join("")}</tbody></table></div>
    <p class="sub">${esc(t("The latest {n} of {total} readings in this period. Dashed lines: high blood pressure starts at 140 systolic or 90 diastolic.", { n: recent.length, total: number(bp.length) }))}</p>`;
}
