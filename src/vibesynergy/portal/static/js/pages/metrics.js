// Metrics explorer: any metrics over any period. Each metric gets its own panel on a shared
// time axis (one crosshair), or all metrics share one chart indexed to their period average
// (100 = average), so different units never need a second axis. Optional smoothing and a
// comparison period; summary statistics and a data table below.
import { get } from "../api.js";
import { alpha, mount, series, token, tooltipBase } from "../charts.js";
import { clock, esc, isNum, metricLabel, monthYear, number, parseDay, shortDate, userDate, value } from "../format.js";
import { locale, N, t } from "../i18n.js";
import {
  apiParams,
  bindPeriodPicker,
  isoToday,
  mergePeriodValue,
  periodPickerHtml,
  saveStored,
  writePeriodQuery,
} from "../period_picker.js";

const STORAGE = "vibesynergy.period.metrics";
const DEFAULT = { period: "last_3m" };
const EXTRA = [["all", N("All time")]];
const GRANULARITIES = [
  ["", N("Automatic")],
  ["day", N("Days")],
  ["week", N("Weeks")],
  ["month", N("Months")],
  ["year", N("Years")],
];
const SMOOTHING = [
  ["0", N("Off")],
  ["7", N("7-day average")],
  ["28", N("28-day average")],
];
const COMPARES = [
  ["", N("Nothing")],
  ["previous", N("Previous period")],
  ["same_last_year", N("Same period last year")],
];
const DOMAIN_NAMES = {
  sleep: N("Sleep"),
  hrv: N("HRV"),
  hr: N("Heart rate"),
  recovery: N("Recovery"),
  daily: N("Daily activity"),
  activity: N("Training"),
  body: N("Body"),
  bp: N("Blood pressure"),
  fitness: N("Fitness"),
};
const PER = { day: N("per day"), week: N("per week"), month: N("per month"), year: N("per year") };
const DEFAULT_METRICS = ["hrv.night_avg", "hr.resting", "sleep.score"];
const MAX_METRICS = 4; // the categorical colours stay distinguishable up to four

export async function render(view, { query, meta, stale }) {
  const today = meta?.data?.today || isoToday();
  const periodValue = mergePeriodValue(query, STORAGE, DEFAULT, { today });
  const state = {
    metrics: query.getAll("m").length ? query.getAll("m").slice(0, MAX_METRICS) : DEFAULT_METRICS,
    granularity: query.get("g") || "",
    smooth: query.get("smooth") || "0",
    compare: query.get("compare") || "",
    layout: query.get("layout") || "panels",
  };
  const params = {
    metrics: state.metrics,
    ...apiParams(periodValue, today),
    granularity: state.granularity,
    compare: state.compare,
  };
  const [cat, reply] = await Promise.all([get("catalog"), get("series", params)]);
  if (stale()) return;
  document.title = `${t("Metrics")} · VibeSynergy`;
  const catalogue = Object.fromEntries(cat.data.metrics.map((m) => [m.id, m]));
  const { data } = reply;
  const granularity = data.series.granularity;
  const smoothDays = granularity === "day" ? Number(state.smooth) : 0;

  view.innerHTML = `
    <div class="page-head"><h1>${t("Metrics")}</h1><div class="sub">${esc(shortDate(reply.meta.period.start))} – ${esc(shortDate(reply.meta.period.end))} · ${esc(t(PER[granularity] || granularity))}</div></div>
    <form class="filters explorer">
      <div class="picked">${state.metrics
        .map(
          (m, i) => `<span class="pick"><i style="background:var(--s${i + 1})"></i>${esc(metricLabel(m, catalogue))}
            <button type="button" data-remove="${esc(m)}" aria-label="${esc(t("Remove {name}", { name: metricLabel(m, catalogue) }))}">×</button></span>`,
        )
        .join("")}
        ${state.metrics.length < MAX_METRICS ? `<label>${t("Add a metric")} ${metricSelect(cat.data.metrics, state.metrics, catalogue)}</label>` : ""}
      </div>
      ${periodPickerHtml({ id: "period", value: periodValue, extra: EXTRA, today })}
      <label>${t("Group by")} ${select("g", GRANULARITIES, state.granularity)}</label>
      <label>${t("Smoothing")} ${select("smooth", SMOOTHING, state.smooth, granularity !== "day")}</label>
      <label>${t("Compare with")} ${select("compare", COMPARES, state.compare)}</label>
      <label>${t("Layout")} ${select("layout", [["panels", N("One panel per metric")], ["indexed", N("One chart, indexed")]], state.layout)}</label>
    </form>
    <section class="card">
      ${state.layout === "panels" && (smoothDays || data.compare) ? keyHtml(smoothDays, data.compare?.kind) : ""}
      <div class="explorer-chart" role="img" aria-label="${esc(state.metrics.map((m) => metricLabel(m, catalogue)).join(", "))} over time"></div>
      ${state.layout === "indexed" ? `<p class="sub">${t("Each metric as a percentage of its own average (100 = average), so different units fit on one scale.")}</p>` : ""}
    </section>
    <section class="card">${statsHtml(state.metrics, data, catalogue)}</section>
    <details class="card"><summary>${t("Data table")}</summary>${tableHtml(state.metrics, data, catalogue)}</details>`;

  bind(view, state, periodValue, today);
  const el = view.querySelector(".explorer-chart");
  const input = prepare(state.metrics, data, smoothDays);
  const span = [time(reply.meta.period.start), time(reply.meta.period.end)];
  if (state.layout === "indexed") indexedChart(el, state.metrics, input, catalogue, granularity, span);
  else panelChart(el, state.metrics, input, catalogue, granularity, data.series.agg, span);
}

// What the line styles mean in the panels layout.
function keyHtml(smoothDays, compareKind) {
  const items = [];
  if (smoothDays) {
    items.push(`<span><i class="faint"></i>${t("Daily values")}</span>`, `<span><i></i>${esc(t("{n}-day average", { n: smoothDays }))}</span>`);
  } else {
    items.push(`<span><i></i>${t("This period")}</span>`);
  }
  if (compareKind) {
    items.push(`<span><i class="compare"></i>${compareKind === "same_last_year" ? t("Same period last year, shifted onto this one") : t("Previous period, shifted onto this one")}</span>`);
  }
  return `<div class="legend key">${items.join("")}</div>`;
}

// Controls ---------------------------------------------------------------------------

function select(name, options, current, disabled = false) {
  return `<select class="btn" name="${name}"${disabled ? " disabled" : ""}>${options
    .map(([v, label]) => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(t(label))}</option>`)
    .join("")}</select>`;
}

function metricSelect(metrics, chosen, catalogue) {
  const groups = {};
  for (const m of metrics) {
    if (chosen.includes(m.id)) continue;
    if (m.coverage === undefined && !m.id.startsWith("activity.")) continue; // no data yet
    (groups[m.domain] ??= []).push(m);
  }
  const options = Object.entries(groups)
    .sort(([a], [b]) => t(DOMAIN_NAMES[a] || a).localeCompare(t(DOMAIN_NAMES[b] || b)))
    .map(
      ([domain, list]) =>
        `<optgroup label="${esc(t(DOMAIN_NAMES[domain] || domain))}">${list
          .map((m) => `<option value="${esc(m.id)}">${esc(metricLabel(m.id, catalogue))}</option>`)
          .join("")}</optgroup>`,
    )
    .join("");
  return `<select class="btn" name="add"><option value="">${t("Choose…")}</option>${options}</select>`;
}

function bind(view, state, periodValue, today) {
  const form = view.querySelector("form");
  const go = (period, metrics = state.metrics) => {
    const p = new URLSearchParams();
    metrics.forEach((m) => p.append("m", m));
    writePeriodQuery(p, period, { defaultValue: DEFAULT });
    if (state.granularity) p.set("g", state.granularity);
    if (state.smooth !== "0") p.set("smooth", state.smooth);
    if (state.compare) p.set("compare", state.compare);
    if (state.layout !== "panels") p.set("layout", state.layout);
    location.hash = `#/metrics?${p}`;
  };
  bindPeriodPicker(view, {
    id: "period",
    value: periodValue,
    extra: EXTRA,
    today,
    onChange: (next) => {
      saveStored(STORAGE, next);
      go(next);
    },
  });
  form.addEventListener("change", (event) => {
    if (event.target.name?.startsWith("period-")) return;
    const f = new FormData(form);
    state.granularity = f.get("g") || "";
    state.smooth = f.get("smooth") || state.smooth;
    state.compare = f.get("compare") || "";
    state.layout = f.get("layout") || "panels";
    if (event.target.name === "add" && event.target.value) state.metrics = [...state.metrics, event.target.value];
    go(periodValue);
  });
  form.addEventListener("click", (event) => {
    const metric = event.target.dataset?.remove;
    if (!metric) return;
    const remaining = state.metrics.filter((m) => m !== metric);
    go(periodValue, remaining.length ? remaining : DEFAULT_METRICS.slice(0, 1));
  });
}

// Data -------------------------------------------------------------------------------

// Per metric: [[isoDate, value]] (raw and, when asked, smoothed) and the comparison.
function prepare(metrics, data, smoothDays) {
  const pick = (result) => {
    if (!result) return {};
    return Object.fromEntries(
      metrics.map((m) => {
        const i = result.columns.indexOf(m);
        return [m, result.rows.map((row) => [row[0], row[i]]).filter(([, v]) => isNum(v))];
      }),
    );
  };
  const raw = pick(data.series);
  const compare = pick(data.compare?.series);
  const smooth = smoothDays ? Object.fromEntries(metrics.map((m) => [m, rolling(raw[m], smoothDays)])) : null;
  return { raw, smooth, compare, compareKind: data.compare?.kind };
}

// Trailing mean over the last n calendar days that have a value (at least half of them).
function rolling(points, n) {
  const out = [];
  let lo = 0;
  let sum = 0;
  for (let hi = 0; hi < points.length; hi++) {
    sum += points[hi][1];
    const edge = parseDay(points[hi][0]).getTime() - (n - 1) * 86400000;
    while (parseDay(points[lo][0]).getTime() < edge) sum -= points[lo++][1];
    const count = hi - lo + 1;
    out.push([points[hi][0], count >= Math.ceil(n / 2) ? sum / count : null]);
  }
  return out;
}

function axisValue(unit) {
  if (unit === "s") return (v) => clock(v / 60); // hours:minutes
  if (unit === "m") return (v) => (Math.abs(v) >= 1000 ? `${number(v / 1000)} km` : `${number(v)} m`);
  return (v) => number(v, Math.abs(v) < 10 && !Number.isInteger(v) ? 1 : 0);
}

const time = (d) => parseDay(d).getTime();

function dateLabel(granularity) {
  return (ms) => {
    const iso = new Date(ms).toISOString().slice(0, 10);
    if (granularity === "year") return String(parseDay(iso).getUTCFullYear());
    if (granularity === "month") {
      return monthYear(iso, "long");
    }
    return userDate(iso);
  };
}

function periodTitle(iso, granularity) {
  if (granularity === "week") return t("Week of {date}", { date: shortDate(iso) });
  if (granularity === "month") return monthYear(iso, "long");
  if (granularity === "year") return iso.slice(0, 4);
  return shortDate(iso);
}

// Charts -----------------------------------------------------------------------------

function panelChart(el, metrics, input, catalogue, granularity, aggs, span) {
  const height = 150;
  const gap = 40;
  el.style.height = `${24 + metrics.length * (height + gap)}px`;
  const muted = token("muted");
  const grid = token("grid");
  const compareColor = token("muted");
  const out = { grid: [], xAxis: [], yAxis: [], series: [], graphic: [] };
  metrics.forEach((m, i) => {
    const color = series(i + 1); // the metric keeps its chip colour
    const unit = catalogue[m]?.unit;
    const top = 24 + i * (height + gap);
    const asBars = aggs[m] === "sum" && granularity !== "day";
    out.grid.push({ left: 56, right: 16, top, height });
    out.xAxis.push({
      type: "time",
      gridIndex: i,
      min: span[0],
      max: span[1],
      axisLabel: { show: i === metrics.length - 1, color: muted, formatter: dateLabel(granularity), hideOverlap: true },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
      splitLine: { show: false },
    });
    out.yAxis.push({
      type: "value",
      gridIndex: i,
      scale: !asBars,
      splitNumber: 3,
      axisLabel: { color: muted, fontSize: 11, formatter: axisValue(unit) },
      splitLine: { lineStyle: { color: grid } },
    });
    out.graphic.push({
      type: "text",
      left: 56,
      top: top - 18,
      style: { text: metricLabel(m, catalogue), fill: token("ink-2"), fontSize: 12, fontWeight: 600 },
    });
    const main = input.raw[m].map(([d, v]) => [time(d), v]);
    if (asBars) {
      out.series.push({ type: "bar", name: m, xAxisIndex: i, yAxisIndex: i, data: main, barMaxWidth: 24, itemStyle: { color, borderRadius: [4, 4, 0, 0] } });
    } else {
      const smoothed = input.smooth?.[m];
      out.series.push({
        type: "line",
        name: m,
        xAxisIndex: i,
        yAxisIndex: i,
        data: main,
        showSymbol: main.length < 60,
        symbolSize: 5,
        lineStyle: { width: smoothed ? 1 : 2, color: smoothed ? alpha(color, 0.35) : color },
        itemStyle: { color: smoothed ? alpha(color, 0.35) : color },
      });
      if (smoothed) {
        out.series.push({
          type: "line",
          name: `${m} (average)`,
          xAxisIndex: i,
          yAxisIndex: i,
          data: smoothed.map(([d, v]) => [time(d), v]),
          showSymbol: false,
          connectNulls: false,
          lineStyle: { width: 2.5, color },
          itemStyle: { color },
        });
      }
    }
    if (input.compare[m]?.length) {
      out.series.push({
        type: "line",
        name: `${m} (compare)`,
        xAxisIndex: i,
        yAxisIndex: i,
        data: input.compare[m].map(([d, v]) => [time(d), v]),
        showSymbol: false,
        lineStyle: { width: 1.5, color: alpha(compareColor, 0.8) },
        itemStyle: { color: compareColor },
      });
    }
  });
  const compareName = input.compareKind === "same_last_year" ? t("Same period last year") : t("Previous period");
  mount(el, {
    ...out,
    axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: token("axis") } },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      formatter: (items) => {
        if (!items.length) return "";
        const iso = new Date(items[0].axisValue).toISOString().slice(0, 10);
        const lines = [`<span style="color:${token("ink-2")}">${esc(periodTitle(iso, granularity))}</span>`];
        for (const item of items) {
          const [metric, suffix] = item.seriesName.split(" (");
          const v = item.value[1];
          if (!isNum(v)) continue;
          const label = suffix === "compare)" ? compareName : suffix === "average)" ? `${metricLabel(metric, catalogue)}, average` : metricLabel(metric, catalogue);
          lines.push(`${esc(label)}: <b>${esc(value(v, catalogue[metric]?.unit))}</b>`);
        }
        return lines.join("<br>");
      },
    },
  });
}

function indexedChart(el, metrics, input, catalogue, granularity, span) {
  el.style.height = "380px";
  const muted = token("muted");
  const source = input.smooth || input.raw;
  const indexed = {};
  for (const m of metrics) {
    const values = input.raw[m].map(([, v]) => v);
    const mean = values.reduce((a, b) => a + b, 0) / (values.length || 1);
    indexed[m] = { mean, points: source[m].map(([d, v]) => [time(d), isNum(v) && mean ? (v / mean) * 100 : null]) };
  }
  mount(el, {
    grid: { left: 48, right: 16, top: 44, bottom: 32 },
    legend: {
      top: 0,
      left: 40,
      icon: "roundRect",
      itemWidth: 14,
      itemHeight: 4,
      textStyle: { color: token("ink-2") },
      data: metrics.map((m) => ({ name: metricLabel(m, catalogue) })),
    },
    xAxis: {
      type: "time",
      min: span[0],
      max: span[1],
      axisLabel: { color: muted, formatter: dateLabel(granularity), hideOverlap: true },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      scale: true,
      axisLabel: { color: muted, formatter: (v) => `${number(v)}` },
      splitLine: { lineStyle: { color: token("grid") } },
    },
    series: metrics.map((m, i) => ({
      type: "line",
      name: metricLabel(m, catalogue),
      data: indexed[m].points,
      showSymbol: false,
      connectNulls: false,
      lineStyle: { width: 2, color: series(i + 1) },
      itemStyle: { color: series(i + 1) },
      markLine: i === 0 ? { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: token("axis"), type: "solid", width: 1 }, data: [{ yAxis: 100 }] } : undefined,
    })),
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      formatter: (items) => {
        if (!items.length) return "";
        const iso = new Date(items[0].axisValue).toISOString().slice(0, 10);
        const lines = [`<span style="color:${token("ink-2")}">${esc(periodTitle(iso, granularity))}</span>`];
        items.forEach((item) => {
          const m = metrics[item.seriesIndex];
          const v = item.value[1];
          if (!isNum(v)) return;
          const actual = (v / 100) * indexed[m].mean;
          lines.push(`${item.marker}${esc(metricLabel(m, catalogue))}: <b>${esc(value(actual, catalogue[m]?.unit))}</b> <span style="color:${token("ink-2")}">(${number(v)})</span>`);
        });
        return lines.join("<br>");
      },
    },
  });
}

// Tables -----------------------------------------------------------------------------

function statsHtml(metrics, data, catalogue) {
  const compare = data.compare;
  const head = `<tr><th>${t("Metric")}</th><th class="num">${t("Days")}</th><th class="num">${t("Average")}</th><th class="num">${t("Lowest")}</th><th class="num">${t("Highest")}</th>${compare ? `<th class="num">${t("Average before")}</th><th class="num">${t("Difference")}</th>` : ""}</tr>`;
  const rows = metrics
    .map((m) => {
      const s = data.stats[m] || {};
      const unit = catalogue[m]?.unit;
      const b = compare?.stats[m] || {};
      let diff = "";
      if (compare) {
        const d = isNum(s.mean) && isNum(b.mean) ? s.mean - b.mean : null;
        const pct = isNum(d) && b.mean ? ` (${d >= 0 ? "+" : ""}${number((d / b.mean) * 100, 1)}%)` : "";
        diff = `<td class="num">${isNum(b.mean) ? esc(value(b.mean, unit)) : "–"}</td><td class="num">${isNum(d) ? `${d >= 0 ? "+" : "−"}${esc(value(Math.abs(d), unit))}${pct}` : "–"}</td>`;
      }
      return `<tr><th scope="row">${esc(metricLabel(m, catalogue))}</th><td class="num">${s.n ?? 0}</td>
        <td class="num">${esc(value(s.mean, unit))}</td><td class="num">${esc(value(s.min, unit))}</td><td class="num">${esc(value(s.max, unit))}</td>${diff}</tr>`;
    })
    .join("");
  const note = compare
    ? `<p class="sub">${esc(t("Compared with {from} – {until}. Averages are over days with a value.", { from: shortDate(compare.period.start), until: shortDate(compare.period.end) }))}</p>`
    : `<p class="sub">${t("Averages, lowest and highest are over days with a value.")}</p>`;
  return `<h2>${t("Summary")}</h2><div class="table-wrap"><table><thead>${head}</thead><tbody>${rows}</tbody></table></div>${note}`;
}

function tableHtml(metrics, data, catalogue) {
  const s = data.series;
  const rows = s.rows
    .slice()
    .reverse()
    .map((row) => `<tr><th scope="row">${esc(periodTitle(row[0], s.granularity))}</th>${metrics.map((m) => `<td class="num">${esc(value(row[s.columns.indexOf(m)], catalogue[m]?.unit))}</td>`).join("")}</tr>`)
    .join("");
  return `<div class="table-wrap"><table><thead><tr><th>${t("Period")}</th>${metrics.map((m) => `<th class="num">${esc(metricLabel(m, catalogue))}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
}
