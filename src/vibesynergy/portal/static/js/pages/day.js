// The day's own numbers for the start page (#/dashboard/<date>): key numbers, last night,
// the timeline and the remaining measurements, plus the day's diary entries. The start page
// (pages/dashboard.js) composes these with the advice, the plan and the calendar.
import { alpha, mount, series, sparkline, token, tooltipBase } from "../charts.js";
import {
  addDays,
  clock,
  esc,
  hoursMinutes,
  isNum,
  metricLabel,
  shortDate,
  sourceName,
  sportName,
  timeOfDay,
  value,
  valueParts,
} from "../format.js";
import { N, t } from "../i18n.js";

// Each tile opens where its number is explained: a page of its own, or else the Metrics
// explorer with that metric chosen.
const TILES = [
  { metric: "recovery.readiness", label: N("Training readiness") },
  { metric: "sleep.score", label: N("Sleep score"), page: "sleep" },
  { metric: "sleep.duration", label: N("Time asleep"), page: "sleep" },
  { metric: "hrv.night_avg", label: N("Overnight HRV") },
  { metric: "hr.resting", label: N("Resting heart rate") },
  { metric: "recovery.body_battery.max", label: N("Body Battery high"), low: "recovery.body_battery.min" },
  { metric: "recovery.stress.avg", label: N("Average stress") },
  { metric: "body.weight", label: N("Weight"), page: "body" },
  { metric: "daily.steps", label: N("Steps") },
  { metric: "activity.duration", label: N("Training time"), page: "training" },
];

const DOMAINS = [
  ["sleep", N("Sleep")],
  ["hrv", N("Heart rate variability")],
  ["hr", N("Heart rate")],
  ["recovery", N("Recovery")],
  ["daily", N("Activity")],
  ["body", N("Body")],
  ["bp", N("Blood pressure")],
  ["fitness", N("Fitness")],
];

// Stage rows of the hypnogram, top to bottom.
const HYPNOGRAM = [
  ["awake", N("Awake"), "s2"],
  ["rem", N("REM"), "s3"],
  ["light", N("Light"), "s1"],
  ["deep", N("Deep"), "s7"],
];

const PANELS = [
  ["hr.sample", N("Heart rate"), "bpm"],
  ["stress.sample", N("Stress"), ""],
  ["bodybattery.sample", N("Body Battery"), ""],
];

// The tile's link, or null when that page isn't part of a shared view.
function tileLink(tile, share) {
  const page = tile.page || "metrics";
  if (share && !share.pages.includes(page)) return null;
  return tile.page ? `#/${page}` : `#/metrics?m=${encodeURIComponent(tile.metric)}`;
}

/**
 * The day's own numbers on the start page, each in its own block: `hosts.numbers`
 * (key numbers), `hosts.sleep` (last night), `hosts.timeline` and `hosts.other` (the other
 * measurements by domain); a host left out is a block that is hidden. `shown` lists metrics
 * another block already shows (recovery, body), so they are not repeated here. `reply` is the
 * /today reply the page already has. Body and blood pressure have their own block
 * (start_blocks.js), so they are left out of the other measurements.
 */
export async function mountDayDetails(hosts, { date, today, meta, stale, reply, timelinePromise, shown = [] }) {
  const { data } = reply;
  const tz = reply.meta.timezone;
  const catalogue = meta.data.metrics;
  const days = dateRange(reply.meta.period.start, reply.meta.period.end);
  const trend = byMetric(data.trend, days);
  const values = Object.fromEntries(Object.keys(trend).map((m) => [m, trend[m][trend[m].length - 1][1]]));
  const hasAny = Object.values(values).some(isNum) || data.day.sleep || data.day.activities.length;
  // Sleep has its own block; training time is in "Plan and done".
  const skip = new Set([...shown, "sleep.score", "sleep.duration", "activity.duration"]);
  const tiles = TILES.filter((tile) => !skip.has(tile.metric));
  for (const tile of tiles) skip.add(tile.metric);
  if (hosts.numbers) {
    hosts.numbers.innerHTML = `
      ${hasAny ? "" : `<div class="notice info">${date === today ? t("Nothing recorded for this day yet. It shows up after your watch or scale syncs.") : t("Nothing recorded for this day.")}</div>`}
      <div class="grid tiles">${tiles.map((tile) => tileHtml(tile, values, data.baselines, catalogue, trend[tile.metric], tileLink(tile, meta.data.share))).join("")}</div>`;
    for (const tile of tiles) {
      const el = hosts.numbers.querySelector(`[data-spark="${tile.metric}"]`);
      const points = trend[tile.metric];
      if (el && points.filter(([, v]) => isNum(v)).length >= 2) {
        sparkline(el, points, (v) => value(v, catalogue[tile.metric]?.unit));
      }
    }
  }
  if (hosts.sleep) hosts.sleep.innerHTML = sleepHtml(data.day.sleep, tz);
  if (hosts.other) {
    const domains = domainsHtml(data.day, catalogue, skip, OWN_BLOCK_DOMAINS);
    hosts.other.innerHTML = domains
      ? `<div class="grid domains">${domains}</div>`
      : `<div class="card empty">${t("No other measurements for this day.")}</div>`;
  }
  const holder = hosts.timeline;
  if (!holder) return;
  holder.innerHTML = `<div class="loading" role="status">${t("Loading the timeline…")}</div>`;
  try {
    const full = (await timelinePromise).data;
    if (stale() || !holder.isConnected) return;
    if (full.timeline.rows.length || full.timeline.sleep_stages.length) {
      holder.innerHTML = `<div class="timeline" role="img" aria-label="${esc(t("Sleep stages, heart rate, stress and Body Battery through the day"))}"></div>`;
      timeline(holder.firstElementChild, full.timeline);
    } else {
      holder.innerHTML = `<div class="empty">${t("No all-day samples for this date.")}</div>`;
    }
  } catch (error) {
    if (!stale()) holder.innerHTML = `<div class="notice error" role="alert">${esc(t("Could not load the timeline: {error}", { error: error.message }))}</div>`;
  }
}

// Domains the start page shows in a block of their own (Body and measurements).
const OWN_BLOCK_DOMAINS = new Set(["body", "bp"]);

function dateRange(start, end) {
  const days = [];
  for (let d = start; d <= end; d = addDays(d, 1)) days.push(d);
  return days;
}

// series rows -> {metric: [[date, value|null] for every day]}
function byMetric(result, days) {
  const index = Object.fromEntries(result.rows.map((row) => [row[0], row]));
  const out = {};
  result.columns.slice(1).forEach((metric, i) => {
    out[metric] = days.map((d) => [d, index[d]?.[i + 1] ?? null]);
  });
  return out;
}

function tileHtml(tile, todayValues, baselines, catalogue, points, href) {
  const unit = catalogue[tile.metric]?.unit;
  const v = todayValues[tile.metric];
  const parts = valueParts(v, unit);
  const main = isNum(v)
    ? `<div class="value">${esc(parts.text)}${parts.unit ? `<span class="unit">${esc(parts.unit)}</span>` : ""}</div>`
    : `<div class="none" aria-label="${esc(t("no value"))}">–</div>`;
  let context = "";
  if (tile.low && isNum(todayValues[tile.low])) {
    context = esc(t("Low {value}", { value: value(todayValues[tile.low], unit) }));
  } else if (baselines[tile.metric]) {
    context = baselineHtml(baselines[tile.metric], unit, catalogue[tile.metric]?.higher_is_better);
  } else if (!isNum(v)) {
    const last = [...points].reverse().find(([, x]) => isNum(x));
    context = last
      ? `<span class="muted">${esc(t("Last {value} on {date}", { value: value(last[1], unit), date: shortDate(last[0]) }))}</span>`
      : `<span class="muted">${t("Nothing in the last 14 days")}</span>`;
  }
  const label = t(tile.label);
  const inner = `
      <div class="label">${esc(label)}</div>
      ${main}
      <div class="context">${context}</div>
      <div class="spark" data-spark="${esc(tile.metric)}" role="img" aria-label="${esc(t("{name}, last 14 days", { name: label }))}"></div>`;
  return href
    ? `<a class="card tile link" href="${esc(href)}" title="${esc(t("Open {name}", { name: label }))}">${inner}</a>`
    : `<div class="card tile">${inner}</div>`;
}

// "Normal 45–59 ms" plus a chip saying where today falls in the 60-day personal norm.
function baselineHtml(b, unit, higherIsBetter) {
  const low = Math.max(0, b.baseline_mean - b.baseline_std); // none of these can go below 0
  const high = b.baseline_mean + b.baseline_std;
  const range = rangeText(low, high, unit);
  let chip = `<span class="chip">${t("normal")}</span>`;
  // Compare at the precision the range is shown in, so "80" is never "below 80.0–90.3".
  const shown = (x) => roundShown(x, unit);
  const v = shown(b.value);
  if (v < shown(low) || v > shown(high)) {
    const up = v > shown(high);
    let tone = "";
    if (higherIsBetter === true) tone = up ? "good" : "warning";
    if (higherIsBetter === false) tone = up ? "warning" : "good";
    chip = `<span class="chip ${tone}">${up ? t("above normal") : t("below normal")}</span>`;
  }
  return `${chip} <span title="${esc(t("Your usual range: the 60 days before today, mean ± 1 standard deviation"))}">${esc(range)}</span>`;
}

// Round like valueParts/rangeText display it: whole minutes for durations, 0.1 kg, …
function roundShown(x, unit) {
  if (unit === "s") return Math.round(x / 60) * 60;
  if (unit === "m") return x >= 1000 ? Math.round(x / 100) * 100 : Math.round(x);
  const tenths = unit === "kg" || unit === "pct" || (!Number.isInteger(x) && Math.abs(x) < 100);
  return tenths ? Math.round(x * 10) / 10 : Math.round(x);
}

// "45–59 ms", "7:10–7:50 h" for durations, or "usually 71" when the norm barely varies.
function rangeText(low, high, unit) {
  const fmt = unit === "s" ? (x) => clock(x / 60) : (x) => valueParts(x, unit).text;
  const suffix = unit === "s" ? t("h") : valueParts(high, unit).unit;
  const [a, b] = [fmt(low), fmt(high)];
  const text = a === b ? t("usually {value}", { value: a }) : `${a}–${b}`;
  return suffix ? `${text} ${suffix}` : text;
}

const STAGES = [
  ["deep_s", N("Deep"), "s7"],
  ["light_s", N("Light"), "s1"],
  ["rem_s", N("REM"), "s3"],
  ["awake_s", N("Awake"), "s2"],
];

function sleepHtml(sleep, timeZone) {
  if (!sleep) return `<div class="empty">${t("No sleep recorded for this night.")}</div>`;
  const others = sleep.other_sources?.length
    ? ` · ${esc(t("also from {sources}", { sources: sleep.other_sources.map(sourceName).join(", ") }))}`
    : "";
  const total = STAGES.reduce((sum, [key]) => sum + (sleep[key] || 0), 0);
  const bar = total
    ? `<div class="stages" role="img" aria-label="${esc(t("Sleep stages"))}">${STAGES.filter(([key]) => sleep[key])
        .map(([key, label, color]) => `<span style="flex:${sleep[key]};background:var(--${color})" title="${t(label)}: ${hoursMinutes(sleep[key])}"></span>`)
        .join("")}</div>
       <div class="legend">${STAGES.filter(([key]) => sleep[key])
        .map(([key, label, color]) => `<span><i style="background:var(--${color})"></i>${t(label)} ${hoursMinutes(sleep[key])}</span>`)
        .join("")}</div>`
    : "";
  return `
    <div class="facts dash-keep-line">
      <div>${t("Asleep")}<b>${esc(hoursMinutes(sleep.duration_s))}</b></div>
      <div>${t("Score")}<b>${isNum(sleep.score) ? Math.round(sleep.score) : "–"}</b></div>
      <div>${t("Bedtime")}<b>${esc(timeOfDay(sleep.start_utc, timeZone))}</b></div>
      <div>${t("Woke up")}<b>${esc(timeOfDay(sleep.end_utc, timeZone))}</b></div>
    </div>
    ${bar}
    <div class="sub" style="margin-top:8px">${t("Source")}: ${esc(sourceName(sleep.source))}${others}</div>`;
}

// Timeline -------------------------------------------------------------------------------

function clockLabel(minutes) {
  const m = ((Math.round(minutes) % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

function timeline(el, tl) {
  const toMinutes = (hhmm) => {
    const [h, m] = hhmm.split(":").map(Number);
    return h * 60 + m;
  };
  const start = Math.min(0, ...tl.sleep_stages.map((s) => s.start ?? 0));
  const end = 1440;
  const panels = [{ key: "stages", title: t("Sleep"), height: 64 }];
  for (const [metric, title, unit] of PANELS) {
    const index = tl.columns.indexOf(metric);
    if (index < 0) continue;
    panels.push({
      key: metric,
      title: t(title),
      unit,
      height: 88,
      points: tl.rows.map((row) => [toMinutes(row[0]) + tl.minutes_per_sample / 2, row[index]]),
    });
  }
  if (!tl.sleep_stages.length) panels.shift();

  const gap = 34;
  let top = 18;
  const grids = panels.map((p) => {
    const grid = { left: 44, right: 16, top, height: p.height };
    top += p.height + gap;
    return grid;
  });
  el.style.height = `${top - gap + 30}px`;

  const ink2 = token("ink-2");
  const muted = token("muted");
  const gridColor = token("grid");
  const color = series(1);
  const bands = tl.activities.map((a) => [{ xAxis: a.start }, { xAxis: a.end }]);

  const xAxis = panels.map((_, i) => ({
    type: "value",
    gridIndex: i,
    min: start,
    max: end,
    interval: el.clientWidth < 600 ? 360 : 180,
    axisLabel: { show: i === panels.length - 1, color: muted, formatter: clockLabel },
    axisLine: { lineStyle: { color: token("axis") } },
    axisTick: { show: false },
    splitLine: { lineStyle: { color: gridColor } },
  }));

  const yAxis = panels.map((p, i) =>
    p.key === "stages"
      ? {
          type: "category",
          gridIndex: i,
          data: HYPNOGRAM.map(([, label]) => t(label)),
          inverse: false,
          axisLabel: { color: muted, fontSize: 11, interval: 0 }, // all four stages, never thinned
          axisLine: { show: false },
          axisTick: { show: false },
        }
      : {
          type: "value",
          gridIndex: i,
          scale: p.key === "hr.sample",
          min: p.key === "hr.sample" ? undefined : 0,
          max: p.key === "hr.sample" ? undefined : 100,
          splitNumber: 2,
          axisLabel: { color: muted, fontSize: 11 },
          splitLine: { lineStyle: { color: gridColor } },
        },
  );

  const seriesList = panels.map((p, i) => {
    if (p.key === "stages") {
      const rows = Object.fromEntries(HYPNOGRAM.map(([stage], index) => [stage, index]));
      return {
        type: "custom",
        name: t("Sleep"),
        xAxisIndex: i,
        yAxisIndex: i,
        encode: { x: [0, 1], y: 2 },
        data: tl.sleep_stages
          .filter((s) => s.stage in rows)
          .map((s) => [s.start, s.end, rows[s.stage], s.stage]),
        renderItem: (params, api) => {
          const a = api.coord([api.value(0), api.value(2)]);
          const b = api.coord([api.value(1), api.value(2)]);
          const h = api.size([0, 1])[1] * 0.7;
          const stage = HYPNOGRAM[api.value(2)];
          return {
            type: "rect",
            shape: { x: a[0], y: a[1] - h / 2, width: Math.max(1, b[0] - a[0]), height: h, r: 2 },
            style: { fill: token(stage[2]) },
          };
        },
      };
    }
    return {
      type: "line",
      name: p.title,
      xAxisIndex: i,
      yAxisIndex: i,
      data: p.points,
      showSymbol: false,
      connectNulls: false,
      lineStyle: { width: 2, color, cap: "round", join: "round" },
      itemStyle: { color },
      areaStyle: p.key === "bodybattery.sample" ? { color: alpha(color, 0.1) } : undefined,
      markArea: bands.length
        ? {
            silent: true,
            itemStyle: { color: token("wash") },
            data: bands,
          }
        : undefined,
    };
  });

  const titles = panels.map((p, i) => ({
    type: "text",
    left: 44,
    top: grids[i].top - 16,
    style: { text: p.unit ? `${p.title} (${p.unit})` : p.title, fill: ink2, fontSize: 12, fontWeight: 600 },
  }));

  mount(el, {
    grid: grids,
    xAxis,
    yAxis,
    series: seriesList,
    graphic: titles,
    axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: token("axis") } },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "line" },
      formatter: (items) => tooltipText(items, panels, tl),
    },
  });
}

function tooltipText(items, panels, tl) {
  if (!items.length) return "";
  const minute = items[0].axisValue;
  const lines = [`<span style="color:${token("ink-2")}">${clockLabel(minute)}</span>`];
  const stage = tl.sleep_stages.find((s) => s.start <= minute && minute < s.end);
  if (stage) lines.push(`${t("Sleep")}: <b>${esc(t(HYPNOGRAM.find(([k]) => k === stage.stage)?.[1] ?? stage.stage))}</b>`);
  for (const p of panels) {
    if (!p.points) continue;
    const nearest = p.points.reduce(
      (best, point) => (Math.abs(point[0] - minute) < Math.abs(best[0] - minute) ? point : best),
      [Infinity, null],
    );
    if (isNum(nearest[1]) && Math.abs(nearest[0] - minute) <= tl.minutes_per_sample) {
      lines.push(`${esc(p.title)}: <b>${Math.round(nearest[1])}${p.unit ? ` ${p.unit}` : ""}</b>`);
    }
  }
  const activity = tl.activities.find((a) => a.start <= minute && minute < a.end);
  if (activity) lines.push(`${esc(activity.name || sportName(activity.sport))}`);
  return lines.join("<br>");
}

// Cards ----------------------------------------------------------------------------------

function domainsHtml(day, catalogue, skip = new Set(), leave = new Set()) {
  const baselines = day.vs_baseline_60d || {};
  return DOMAINS.filter(([domain]) => day.metrics[domain] && !leave.has(domain))
    .map(([domain, title]) => {
      const rows = Object.entries(day.metrics[domain])
        .filter(([metric]) => !skip.has(metric))
        .map(([metric, m]) => {
          const b = baselines[metric];
          const z = b && Math.abs(b.z) >= 1 ? `<span class="chip">${b.z > 0 ? t("above normal") : t("below normal")}</span>` : "";
          return `<tr>
            <th scope="row">${esc(metricLabel(metric, catalogue))} ${z}</th>
            <td class="num">${esc(value(m.value, m.unit))}</td>
            <td class="muted">${esc(sourceName(m.source))}</td>
          </tr>`;
        })
        .join("");
      if (!rows) return "";
      return `<div class="card"><h3>${esc(t(title))}</h3><div class="table-wrap"><table aria-label="${esc(t(title))}"><tbody>${rows}</tbody></table></div></div>`;
    })
    .join("");
}

const CHECKIN_NAMES = { sleep_quality: N("Sleep quality"), soreness: N("Soreness"), mood: N("Mood"), stress: N("Stress"), motivation: N("Motivation") };
const CHECKIN_SCALE_ENDS = {
  sleep_quality: [N("poor"), N("good")],
  soreness: [N("fresh"), N("heavy")],
  mood: [N("sad"), N("cheerful")],
  stress: [N("calm"), N("stressed")],
  motivation: [N("low"), N("high")],
};

/**
 * Which workout a diary note belongs to: its date, sport and name, linked to the
 * activity. A text copied from Strava or Garmin reads as a loose remark without it.
 */
export function noteActivityHtml(n, { linkActivities = true } = {}) {
  if (!n.activity_id) return "";
  const a = n.activity;
  if (!a) return `<div class="sub note-activity">${t("About a workout that is no longer in your archive.")}</div>`;
  const sport = sportName(a.sport);
  const name = String(a.name || "").trim();
  const title = name && name.localeCompare(sport, undefined, { sensitivity: "base" }) !== 0 ? `${sport} · ${name}` : sport;
  const label = esc(t("{date}: {title}", { date: shortDate(a.date), title }));
  const what = linkActivities ? `<a href="#/activity/${encodeURIComponent(a.id)}">${label}</a>` : label;
  return `<div class="sub note-activity">${t("With workout")} ${what}</div>`;
}

/** The day's notes (and check-in and pain, unless the page shows them already) as a list. */
export function dayJournalHtml(journal, { injuries = true, checkin = true, linkActivities = true } = {}) {
  if (!journal) return "";
  const parts = [];
  for (const n of journal.notes) {
    const tags = n.tags.map((tag) => `<span class="chip">${esc(t(tag.replace(/_/g, " ")))}</span>`).join(" ");
    parts.push(`<li>${tags} ${esc(n.text)}${noteActivityHtml(n, { linkActivities })}</li>`);
  }
  const c = checkin ? journal.checkin : null;
  if (c) {
    const scores = Object.entries(CHECKIN_NAMES)
      .filter(([k]) => c[k] !== null && c[k] !== undefined)
      .map(([k, label]) => {
        const [low, high] = CHECKIN_SCALE_ENDS[k].map((key) => t(key));
        const scale = `${esc(t(label))} <b title="1 = ${esc(low)}, 5 = ${esc(high)}">${c[k]}</b>/5`;
        return scale;
      })
      .join(" · ");
    parts.push(`<li><span class="chip recovery">${t("check-in")}</span> ${scores}${c.note ? ` — ${esc(c.note)}` : ""}</li>`);
  }
  for (const i of injuries ? journal.injuries : []) {
    const p = i.pain;
    parts.push(`<li><span class="chip warning">${esc(i.title)}</span> ${p ? `${t("pain")} <b>${p.pain}</b>/10${p.affects_movement ? ` · ${t("changes movement")}` : ""}` : t("open, no pain logged this day")}</li>`);
  }
  return parts.length ? `<ul class="day-diary-list">${parts.join("")}</ul>` : "";
}

/** The day's measurements with their time (scale, blood pressure monitor), as a table. */
export function measurementsHtml(items, catalogue, tz) {
  if (!items.length) return "";
  const rows = items
    .map(
      (m) => `<tr>
        <td>${esc(timeOfDay(m.measured_at, tz))}</td>
        <th scope="row">${esc(metricLabel(m.metric, catalogue))}</th>
        <td class="num">${esc(value(m.value, m.unit))}</td>
        <td class="muted">${esc(sourceName(m.source))}</td>
      </tr>`,
    )
    .join("");
  return `<div class="table-wrap"><table aria-label="${esc(t("Measurements"))}">
    <thead><tr><th>${t("Time")}</th><th>${t("Measurement")}</th><th class="num">${t("Value")}</th><th>${t("Source")}</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

