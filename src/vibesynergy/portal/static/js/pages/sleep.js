// Sleep: nightly time asleep per stage with the sleep score, when you sleep (bedtime to
// wake time per night, and how regular that is), a hypnogram per night for periods up to a
// month, and the other nightly measurements. Nights come from the best sleep source.
import { get } from "../api.js";
import { alpha, mount, series, token, tooltipBase } from "../charts.js";
import { esc, hoursMinutes, isNum, number, shortDate, value } from "../format.js";
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

const STORAGE = "vibesynergy.period.sleep";
const DEFAULT = { period: "last_1m" };
const EXTRA = [["all", N("All time")]];
// Same colours as the Day page's sleep chart.
const STAGES = [
  ["deep", "sleep.deep", N("Deep"), "s7"],
  ["light", "sleep.light", N("Light"), "s1"],
  ["rem", "sleep.rem", N("REM"), "s3"],
  ["awake", "sleep.awake", N("Awake"), "s2"],
];
const OTHER = [
  ["sleep.avg_hrv", N("HRV asleep"), "ms"],
  ["sleep.avg_hr", N("Heart rate asleep"), "bpm"],
  ["sleep.avg_respiration", N("Breathing rate asleep"), "brpm"],
];
// Clock window of the hypnograms and the bedtime chart: 18:00 to 14:00, as minutes from noon.
const FROM = 6 * 60;
const TO = 26 * 60;

export async function render(view, { query, meta, stale }) {
  const today = meta?.data?.today || isoToday();
  const value = mergePeriodValue(query, STORAGE, DEFAULT, { today });
  const reply = await get("sleep", apiParams(value, today));
  if (stale()) return;
  document.title = `${t("Sleep")} · VibeSynergy`;
  const { data, meta: replyMeta } = reply;
  const tz = replyMeta.timezone;
  const nights = data.nights;
  // Missing is never drawn as an empty frame: only the parts that have values.
  const hasStages = STAGES.some(([, metric]) => column(data.series, metric).some(isNum));
  const hasScore = column(data.series, "sleep.score").some(isNum);

  view.innerHTML = `
    <div class="page-head"><h1>${t("Sleep")}</h1>
      <div class="sub">${esc(shortDate(replyMeta.period.start))} – ${esc(shortDate(replyMeta.period.end))} · ${esc(t("{n} nights", { n: number(nights.length) }))}</div></div>
    <form class="filters">${periodPickerHtml({ id: "period", value, extra: EXTRA, today })}</form>
    ${nights.length ? "" : `<div class="notice info">${t("No sleep recorded in this period.")}</div>`}
    ${tilesHtml(data)}
    <section class="card">
      <h2>${t("Sleep per night")}</h2>
      ${perNightHtml(hasStages, hasScore)}
    </section>
    <section class="card">
      <h2>${t("When you sleep")}</h2>
      ${nights.length ? `<div class="chart sleep-times" role="img" aria-label="${esc(t("Bedtime to wake time per night"))}"></div>
      ${regularityText(data.regularity)}` : `<p class="empty">${t("No bedtimes recorded in this period.")}</p>`}
    </section>
    ${nights.some((n) => n.stages?.length) ? `<section class="card"><h2>${t("Hypnograms")}</h2>${hypnogramsHtml(nights, tz)}</section>` : ""}
    <section class="card">${otherHtml(data.series)}</section>`;

  bindPeriodPicker(view, {
    id: "period",
    value,
    extra: EXTRA,
    today,
    onChange: (next) => {
      saveStored(STORAGE, next);
      const params = writePeriodQuery(new URLSearchParams(), next, { defaultValue: DEFAULT });
      const qs = params.toString();
      location.hash = `#/sleep${qs ? `?${qs}` : ""}`;
    },
  });
  if (nights.length) {
    const perNight = view.querySelector(".sleep-stages");
    if (perNight) stagesChart(perNight, data.series, hasStages);
    timesChart(view.querySelector(".sleep-times"), nights, tz);
  }
}

// Local clock time as minutes from noon (so a night doesn't wrap around midnight).
function fromNoon(iso, tz) {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso));
  const hour = Number(parts.find((p) => p.type === "hour").value);
  const minute = Number(parts.find((p) => p.type === "minute").value);
  return (hour * 60 + minute - 12 * 60 + 24 * 60) % (24 * 60);
}

function clockText(minutesFromNoon) {
  const total = Math.round(minutesFromNoon + 12 * 60) % (24 * 60);
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

// Tiles -------------------------------------------------------------------------------

function tilesHtml(data) {
  const s = data.summary || {};
  const r = data.regularity;
  const tile = (label, main, context) =>
    `<div class="card tile"><div class="label">${esc(label)}</div><div class="value">${main}</div><div class="context">${context}</div></div>`;
  const tiles = [];
  if (s["sleep.duration"]) {
    const d = s["sleep.duration"];
    tiles.push(tile(t("Time asleep"), esc(hoursMinutes(d.mean)), esc(t("average · {min} to {max}", { min: hoursMinutes(d.min), max: hoursMinutes(d.max) }))));
  }
  if (s["sleep.score"]) {
    const d = s["sleep.score"];
    tiles.push(tile(t("Sleep score"), esc(number(d.mean)), esc(t("average · {min} to {max}", { min: number(d.min), max: number(d.max) }))));
  }
  if (r) {
    tiles.push(tile(t("Bedtime"), esc(r.bedtime.mean), esc(t("average · varies ± {time}", { time: minutesText(r.bedtime.sd_min) }))));
    tiles.push(tile(t("Wake time"), esc(r.wake.mean), esc(t("average · varies ± {time}", { time: minutesText(r.wake.sd_min) }))));
  }
  return tiles.length ? `<section class="grid tiles">${tiles.join("")}</section>` : "";
}

function minutesText(m) {
  return m >= 60 ? hoursMinutes(m * 60) : t("{m} min", { m });
}

function regularityText(r) {
  if (!r) return "";
  return `<p class="sub">${esc(t("You sleep around {time}, give or take {sd} over {n} nights. Under about 30 minutes is regular.", { time: r.midpoint.mean, sd: minutesText(r.midpoint.sd_min), n: number(r.nights) }))}</p>`;
}

// Charts ------------------------------------------------------------------------------

// Stage bars with the score line under them; the score alone when no stages were recorded;
// a plain message when neither is there.
function perNightHtml(hasStages, hasScore) {
  if (!hasStages && !hasScore) return `<p class="empty">${t("No sleep stages or sleep score recorded in this period.")}</p>`;
  const score = `<span><i style="background:var(--ink-2)"></i>${t("Sleep score")}</span>`;
  if (!hasStages) {
    return `<div class="legend">${score}</div>
      <div class="chart sleep-stages sleep-score-only" role="img" aria-label="${esc(t("Sleep score per night"))}"></div>
      <p class="sub">${t("No sleep stages recorded in this period.")}</p>`;
  }
  return `<div class="legend">${STAGES.map(([, , label, color]) => `<span><i style="background:var(--${color})"></i>${t(label)}</span>`).join("")}${hasScore ? score : ""}</div>
      <div class="chart sleep-stages" role="img" aria-label="${esc(hasScore ? t("Time in each sleep stage per night, with the sleep score") : t("Time in each sleep stage per night"))}"></div>`;
}

function column(seriesData, metric) {
  const i = seriesData.columns.indexOf(metric);
  return seriesData.rows.map((row) => (i < 0 ? null : row[i]));
}

function stagesChart(el, seriesData, hasStages = true) {
  const periods = seriesData.rows.map((row) => row[0]);
  const muted = token("muted");
  const perWeek = seriesData.granularity !== "day";
  const periodLabel = (i) => {
    const day = shortDate(periods[i]);
    return perWeek ? t("Week of {date}", { date: day }) : day;
  };
  if (!hasStages) {
    mount(el, {
      grid: { left: 44, right: 16, top: 16, bottom: 28 },
      xAxis: { type: "category", data: periods, axisLabel: { color: muted, formatter: (d) => shortDate(d) }, axisLine: { lineStyle: { color: token("axis") } }, axisTick: { show: false } },
      yAxis: { type: "value", scale: true, splitNumber: 3, axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } } },
      tooltip: {
        ...tooltipBase(),
        trigger: "axis",
        formatter: (items) => {
          const i = items[0].dataIndex;
          const score = column(seriesData, "sleep.score")[i];
          const asleep = column(seriesData, "sleep.duration")[i];
          return `<b>${esc(periodLabel(i))}</b><br>${isNum(asleep) ? `${esc(t("Asleep"))} ${esc(hoursMinutes(asleep))} · ` : ""}${isNum(score) ? esc(t("score {n}", { n: number(score) })) : ""}`;
        },
      },
      series: [{
        name: t("Sleep score"),
        type: "line",
        data: column(seriesData, "sleep.score"),
        connectNulls: true,
        symbolSize: 5,
        lineStyle: { width: 2, color: token("ink-2") },
        itemStyle: { color: token("ink-2") },
      }],
    });
    return;
  }
  mount(el, {
    grid: [
      { left: 44, right: 16, top: 16, height: "58%" },
      { left: 44, right: 16, top: "76%", bottom: 28 },
    ],
    xAxis: [0, 1].map((i) => ({
      type: "category",
      gridIndex: i,
      data: periods,
      axisLabel: { show: i === 1, color: muted, formatter: (d) => shortDate(d) },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
    })),
    yAxis: [
      { type: "value", gridIndex: 0, axisLabel: { color: muted, formatter: (v) => `${v} h` }, splitLine: { lineStyle: { color: token("grid") } } },
      { type: "value", gridIndex: 1, scale: true, splitNumber: 2, axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } } },
    ],
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) => {
        const i = items[0].dataIndex;
        const lines = STAGES.map(([, metric, label]) => {
          const v = column(seriesData, metric)[i];
          return isNum(v) ? `${t(label)} ${esc(hoursMinutes(v))}` : null;
        }).filter(Boolean);
        const score = column(seriesData, "sleep.score")[i];
        const asleep = column(seriesData, "sleep.duration")[i];
        const day = shortDate(periods[i]);
        return `<b>${esc(perWeek ? t("Week of {date}", { date: day }) : day)}</b><br>${esc(t("Asleep"))} ${esc(hoursMinutes(asleep))}${isNum(score) ? ` · ${esc(t("score {n}", { n: number(score) }))}` : ""}<br>${lines.join(" · ")}`;
      },
    },
    series: [
      ...STAGES.map(([, metric, label, color]) => ({
        name: t(label),
        type: "bar",
        stack: "night",
        barMaxWidth: 22,
        data: column(seriesData, metric).map((v) => (isNum(v) ? Math.round((v / 3600) * 100) / 100 : null)),
        itemStyle: { color: token(color) },
      })),
      {
        name: t("Sleep score"),
        type: "line",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: column(seriesData, "sleep.score"),
        connectNulls: true,
        symbolSize: 5,
        lineStyle: { width: 2, color: token("ink-2") },
        itemStyle: { color: token("ink-2") },
      },
    ],
  });
}

function timesChart(el, nights, tz) {
  const rows = nights
    .filter((n) => n.start_utc && n.end_utc)
    .map((n) => {
      const bed = fromNoon(n.start_utc, tz);
      let wake = fromNoon(n.end_utc, tz);
      if (wake < bed) wake += 24 * 60;
      return [n.local_date, bed, wake];
    });
  const muted = token("muted");
  const color = series(1);
  mount(el, {
    grid: { left: 52, right: 16, top: 12, bottom: 28 },
    xAxis: {
      type: "category",
      data: rows.map((r) => r[0]),
      axisLabel: { color: muted, formatter: (d) => shortDate(d) },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      inverse: true,
      min: (range) => Math.max(0, Math.floor((range.min - 30) / 60) * 60),
      max: (range) => Math.min(TO + 6 * 60, Math.ceil((range.max + 30) / 60) * 60),
      interval: 120,
      axisLabel: { color: muted, formatter: (v) => clockText(v) },
      splitLine: { lineStyle: { color: token("grid") } },
    },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) => {
        const r = rows[items[0].dataIndex];
        return `<b>${esc(shortDate(r[0]))}</b><br>${esc(t("{from} to {until}", { from: clockText(r[1]), until: clockText(r[2]) }))}`;
      },
    },
    series: [
      // An invisible bar up to bedtime, then the night itself: a floating bar per night.
      { type: "bar", stack: "t", data: rows.map((r) => r[1]), itemStyle: { color: "transparent" }, barMaxWidth: 18, silent: true },
      { type: "bar", stack: "t", data: rows.map((r) => r[2] - r[1]), itemStyle: { color: alpha(color, 0.75), borderRadius: 3 }, barMaxWidth: 18 },
    ],
  });
}

// Hypnograms: one row per night, stages placed on the same 18:00-14:00 clock window.
function hypnogramsHtml(nights, tz) {
  const span = TO - FROM;
  const pos = (iso) => {
    let m = fromNoon(iso, tz);
    if (m < FROM) m += 24 * 60;
    return Math.max(0, Math.min(100, ((m - FROM) / span) * 100));
  };
  const color = Object.fromEntries(STAGES.map(([stage, , , c]) => [stage, c]));
  const ticks = [];
  for (let m = FROM; m <= TO; m += 120) ticks.push(`<span style="left:${((m - FROM) / span) * 100}%">${clockText(m)}</span>`);
  const rows = [...nights]
    .reverse()
    .filter((n) => n.stages?.length)
    .map((n) => {
      const segments = n.stages
        .map((s) => {
          const left = pos(s.start_utc);
          const width = Math.max(pos(s.end_utc) - left, 0.15);
          return `<i class="${esc(s.stage)}" style="left:${left}%;width:${width}%;background:var(--${color[s.stage] || "muted"})"></i>`;
        })
        .join("");
      return `<div class="hyp"><a href="#/dashboard/${esc(n.local_date)}">${esc(shortDate(n.local_date))}</a><div class="hyp-track">${segments}</div><span class="muted">${esc(hoursMinutes(n.duration_s))}</span></div>`;
    })
    .join("");
  return `<div class="legend">${STAGES.map(([, , label, c]) => `<span><i style="background:var(--${c})"></i>${t(label)}</span>`).join("")}</div>
    <div class="hyps"><div class="hyp hyp-axis"><span></span><div class="hyp-ticks">${ticks.join("")}</div><span></span></div>${rows}</div>`;
}

// Nightly measurements: period averages from the series.
function otherHtml(seriesData) {
  const rows = OTHER.map(([metric, label, unit]) => {
    const values = column(seriesData, metric).filter(isNum);
    if (!values.length) return null;
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    return `<tr><th scope="row">${esc(t(label))}</th><td>${esc(value(mean, unit))}</td><td>${esc(value(Math.min(...values), unit))} – ${esc(value(Math.max(...values), unit))}</td></tr>`;
  }).filter(Boolean);
  if (!rows.length) return `<h2>${t("During sleep")}</h2><div class="empty">${t("No heart rate, HRV or breathing data for these nights.")}</div>`;
  return `<h2>${t("During sleep")}</h2><div class="table-wrap"><table><thead><tr><th>${t("Measurement")}</th><th>${t("Average")}</th><th>${t("Range")}</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>
    <p class="sub">${t("Per night; open Metrics for their trends.")}</p>`;
}
