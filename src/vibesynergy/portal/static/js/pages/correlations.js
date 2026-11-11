// Correlations: do two daily metrics move together, the same day or with y some days later
// (e.g. training load today and HRV tomorrow)? A scatter of the day pairs with a fitted line,
// the correlation per lag 0-7 days, and a plain reading of the strength.
import { get } from "../api.js";
import { alpha, mount, series, token, tooltipBase } from "../charts.js";
import { esc, isNum, metricLabel, number, shortDate, value } from "../format.js";
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

const STORAGE = "vibesynergy.period.correlations";
const DEFAULT = { period: "last_12m" };
const EXTRA = [["last_12m", N("12 months")], ["all", N("All time")]];
const METHODS = [
  ["spearman", N("Spearman (ranks)")],
  ["pearson", N("Pearson (linear)")],
];
const MAX_LAG = 7;
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
  resp: N("Breathing"),
  spo2: N("Blood oxygen"),
  checkin: N("Diary check-in"),
  pain: N("Diary pain"),
};
const DEFAULTS = { x: "sleep.duration", y: "hrv.night_avg", period: "last_12m", lag: "0", method: "spearman", sick: "keep" };

export async function render(view, { query, meta, stale }) {
  const today = meta?.data?.today || isoToday();
  const periodValue = mergePeriodValue(query, STORAGE, { period: DEFAULTS.period }, { today });
  const state = Object.fromEntries(Object.entries(DEFAULTS).map(([k, v]) => [k, query.get(k) || v]));
  const [cat, reply] = await Promise.all([
    get("catalog"),
    get("correlate", {
      x: state.x,
      y: state.y,
      ...apiParams(periodValue, today),
      lag: state.lag,
      method: state.method,
      exclude_tags: state.sick === "skip" ? "sick" : undefined,
    }),
  ]);
  if (stale()) return;
  document.title = `${t("Correlations")} · VibeSynergy`;
  const catalogue = Object.fromEntries(cat.data.metrics.map((m) => [m.id, m]));
  const { data, meta: replyMeta } = reply;
  const lag = Number(state.lag);
  const chosen = data.by_lag.find((r) => r.lag_days === lag) || {};
  const xLabel = metricLabel(state.x, catalogue);
  const yLabel = metricLabel(state.y, catalogue);
  // Draw only what has a value: a scatter once the chosen lag has a correlation,
  // the lag bars once any lag has one. The headline says how many days are missing.
  const showScatter = data.points.length > 0 && isNum(chosen.r);
  const showLags = data.by_lag.some((r) => isNum(r.r));

  view.innerHTML = `
    <div class="page-head"><h1>${t("Correlations")}</h1>
      <div class="sub">${esc(shortDate(replyMeta.period.start))} – ${esc(shortDate(replyMeta.period.end))}</div></div>
    <form class="filters">
      <label>X ${metricSelect("x", cat.data.metrics, state.x, catalogue)}</label>
      <label>Y ${metricSelect("y", cat.data.metrics, state.y, catalogue)}</label>
      <label>${t("Y is")} ${select("lag", Array.from({ length: MAX_LAG + 1 }, (_, i) => [String(i), lagText(i)]), state.lag)}</label>
      ${periodPickerHtml({ id: "period", value: periodValue, extra: EXTRA, today })}
      <label>${t("Method")} ${select("method", METHODS, state.method)}</label>
      <label>${t("Sick days")} ${select("sick", [["keep", N("Included")], ["skip", N("Left out (diary)")]], state.sick)}</label>
    </form>
    <section class="card">
      <h2>${esc(t("{y} {lag} vs {x}", { y: yLabel, lag: lagText(lag), x: xLabel }))}</h2>
      <p class="headline">${headline(chosen, xLabel, yLabel)}</p>
      ${showScatter ? `<div class="chart corr-scatter" role="img" aria-label="${esc(t("{x} against {y}, one dot per day", { x: xLabel, y: yLabel }))}"></div>` : data.points.length ? "" : `<div class="empty">${t("No days with both values in this period.")}</div>`}
    </section>
    <section class="card">
      <h2>${t("By lag")}</h2>
      ${showLags ? `<div class="chart corr-lags" role="img" aria-label="${esc(t("Correlation for each lag from 0 to {n} days", { n: MAX_LAG }))}"></div>
      <p class="sub">${t("The strongest lag can be chance, especially with fewer than about 30 days.")}</p>` : `<div class="empty">${t("Not enough days with both values for any lag yet. Pick a longer period.")}</div>`}
    </section>`;

  const navigate = (period) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(state)) if (value !== DEFAULTS[key]) params.set(key, value);
    writePeriodQuery(params, period, { defaultValue: { period: DEFAULTS.period } });
    const qs = params.toString();
    location.hash = `#/correlations${qs ? `?${qs}` : ""}`;
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
  view.querySelectorAll("select").forEach((el) => {
    if (el.name?.startsWith("period-")) return;
    el.addEventListener("change", () => {
      state[el.name] = el.value;
      navigate(periodValue);
    });
  });
  const units = replyMeta.units || {};
  if (showScatter) scatter(view.querySelector(".corr-scatter"), data.points, xLabel, yLabel, units[state.x], units[state.y]);
  if (showLags) lagChart(view.querySelector(".corr-lags"), data.by_lag, lag);
}

function select(name, options, current) {
  return `<select class="btn" name="${name}">${options
    .map(([v, label]) => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(t(label))}</option>`)
    .join("")}</select>`;
}

function metricSelect(name, metrics, current, catalogue) {
  const groups = {};
  for (const m of metrics) {
    if (m.coverage === undefined && !m.id.startsWith("activity.") && m.id !== current) continue; // no data yet
    (groups[m.domain] ??= []).push(m);
  }
  const options = Object.entries(groups)
    .sort(([a], [b]) => t(DOMAIN_NAMES[a] || a).localeCompare(t(DOMAIN_NAMES[b] || b)))
    .map(
      ([domain, list]) =>
        `<optgroup label="${esc(t(DOMAIN_NAMES[domain] || domain))}">${list
          .map((m) => `<option value="${esc(m.id)}"${m.id === current ? " selected" : ""}>${esc(metricLabel(m.id, catalogue))}</option>`)
          .join("")}</optgroup>`,
    )
    .join("");
  return `<select class="btn" name="${name}">${options}</select>`;
}

function lagText(days) {
  if (days === 0) return t("the same day");
  return days === 1 ? t("the next day") : t("{n} days later", { n: days });
}

// Cohen's rough bands for |r|, with the direction: the whole phrase, so it translates.
function strength(r) {
  const a = Math.abs(r);
  if (a < 0.1) return "";
  const up = r > 0;
  if (a < 0.3) return up ? t("a weak positive relationship") : t("a weak negative relationship");
  if (a < 0.5) return up ? t("a moderate positive relationship") : t("a moderate negative relationship");
  return up ? t("a strong positive relationship") : t("a strong negative relationship");
}

function headline(c, xLabel, yLabel) {
  if (!isNum(c.r)) return esc(t("Not enough days with both values ({n}; at least 5 needed).", { n: number(c.n || 0) }));
  const kind = strength(c.r);
  const p = isNum(c.p) ? (c.p < 0.001 ? "p < 0.001" : `p = ${number(c.p, c.p < 0.01 ? 3 : 2)}`) : "";
  const r = `<b>r = ${number(c.r, 2)}</b>`;
  const days = number(c.n);
  // Too few days or too weak to tell apart from chance: say so instead of naming a strength.
  if (!isNum(c.p) || c.p >= 0.05) {
    const text = c.n < 30
      ? t("over {n} days{p}: this could well be chance; more days are needed.", { n: days, p: p ? ` (${p})` : "" })
      : t("over {n} days{p}: this could well be chance.", { n: days, p: p ? ` (${p})` : "" });
    return `${r} ${esc(text)}`;
  }
  if (!kind) return `${r}: ${esc(t("no relationship over {n} days ({p}).", { n: days, p }))}`;
  const meaning = c.r > 0
    ? t("Higher {x} tends to go with higher {y}.", { x: xLabel, y: yLabel })
    : t("Higher {x} tends to go with lower {y}.", { x: xLabel, y: yLabel });
  return `${r}: ${esc(t("{kind} over {n} days ({p}).", { kind, n: days, p }))} ${esc(meaning)}`;
}

// Least-squares line through the points, drawn across the x range.
function fit(points) {
  const n = points.length;
  const mx = points.reduce((s, p) => s + p[1], 0) / n;
  const my = points.reduce((s, p) => s + p[2], 0) / n;
  let sxy = 0;
  let sxx = 0;
  for (const [, x, y] of points) {
    sxy += (x - mx) * (y - my);
    sxx += (x - mx) ** 2;
  }
  if (!sxx) return null;
  const slope = sxy / sxx;
  const xs = points.map((p) => p[1]);
  const lo = Math.min(...xs);
  const hi = Math.max(...xs);
  return [
    [lo, my + slope * (lo - mx)],
    [hi, my + slope * (hi - mx)],
  ];
}

function scatter(el, points, xLabel, yLabel, xUnit, yUnit) {
  const muted = token("muted");
  const color = series(1);
  const line = fit(points);
  const axis = (name, unit) => ({
    type: "value",
    scale: true,
    name,
    nameLocation: "middle",
    nameGap: 30,
    nameTextStyle: { color: muted },
    axisLabel: { color: muted, formatter: (v) => value(v, unit) },
    splitLine: { lineStyle: { color: token("grid") } },
  });
  mount(el, {
    grid: { left: 64, right: 20, top: 16, bottom: 48 },
    xAxis: axis(xLabel, xUnit),
    yAxis: { ...axis(yLabel, yUnit), nameGap: 52 },
    tooltip: {
      ...tooltipBase(),
      trigger: "item",
      formatter: (item) => {
        if (item.seriesType !== "scatter") return "";
        const [day, x, y] = points[item.dataIndex];
        return `<b>${esc(shortDate(day))}</b><br>${esc(xLabel)} ${esc(value(x, xUnit))}<br>${esc(yLabel)} ${esc(value(y, yUnit))}`;
      },
    },
    series: [
      { type: "scatter", data: points.map((p) => [p[1], p[2]]), symbolSize: 6, itemStyle: { color: alpha(color, 0.55) } },
      ...(line ? [{ type: "line", data: line, showSymbol: false, silent: true, lineStyle: { width: 2, color: token("ink-2"), type: "dashed" } }] : []),
    ],
  });
}

function lagChart(el, byLag, chosen) {
  const muted = token("muted");
  mount(el, {
    grid: { left: 44, right: 16, top: 16, bottom: 28 },
    xAxis: {
      type: "category",
      data: byLag.map((r) => (r.lag_days === 0 ? t("same day") : t("+{n} d", { n: r.lag_days }))),
      axisLabel: { color: muted },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
    },
    yAxis: { type: "value", min: -1, max: 1, interval: 0.5, axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } } },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) => {
        const r = byLag[items[0].dataIndex];
        return `<b>${esc(lagText(r.lag_days))}</b><br>${isNum(r.r) ? `r = ${number(r.r, 2)} · ${esc(t("{n} days", { n: number(r.n) }))}` : t("not enough days")}`;
      },
    },
    series: [
      {
        type: "bar",
        barMaxWidth: 36,
        data: byLag.map((r) => ({
          value: r.r,
          itemStyle: { color: r.lag_days === chosen ? series(1) : alpha(series(1), 0.35) },
        })),
      },
    ],
  });
}
