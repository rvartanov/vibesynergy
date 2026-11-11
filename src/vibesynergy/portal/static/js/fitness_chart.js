// Fitness, fatigue and form: one chart for Planning & progress and Training analysis,
// so both look the same. Top: daily load (bars), fitness and fatigue. Bottom: form, with the
// heavy-fatigue band below -30. Days VibeSynergy estimated (no intervals.icu figure) are dashed;
// an optional projection continues dashed after today, with race days marked.
// rows: [date, load, ctl, atl, tsb, …, …, source] as the training-load query returns them.
import { alpha, mount, series, token, tooltipBase } from "./charts.js";
import { dayMonth, esc, monthYear, number, shortDate } from "./format.js";
import { t } from "./i18n.js";

export function fitnessLegendHtml({ projection = false } = {}) {
  return `<div class="legend key"><span><i style="background:var(--s1)"></i>${t("Fitness (42-day load)")}</span><span><i style="background:var(--s2)"></i>${t("Fatigue (7-day load)")}</span><span><i class="faint"></i>${t("Daily load")}</span><span><i style="background:var(--s3)"></i>${t("Form = fitness − fatigue")}</span>${projection ? `<span><i class="faint"></i>${t("Projection (dashed)")}</span>` : ""}</div>`;
}

export function fitnessChart(el, rows, { projection = null, zoneText = () => "" } = {}) {
  const ahead = projection?.rows || [];
  const n = rows.length;
  const dates = [...rows.map((r) => r[0]), ...ahead.map((r) => r.date)];
  const muted = token("muted");
  const narrow = el.clientWidth < 520;
  const label = (v) => (dates.length > 400 ? monthYear(v) : narrow ? dayMonth(v) : shortDate(v));
  const axis = (i, show) => ({
    type: "category",
    gridIndex: i,
    data: dates,
    boundaryGap: false,
    axisLabel: { show, color: muted, hideOverlap: true, formatter: label },
    axisLine: { lineStyle: { color: token("axis") } },
    axisTick: { show: false },
  });
  const yAxis = (i, name) => ({
    type: "value",
    gridIndex: i,
    name,
    nameTextStyle: { color: muted, align: "left" },
    axisLabel: { color: muted },
    splitLine: { lineStyle: { color: token("grid") } },
  });
  const pad = ahead.map(() => null);
  // Estimated stretches (no intervals.icu figure that day) are dashed, never passed off as its.
  const estimated = rows.map((r) => r[7] === "computed");
  const mixed = estimated.some(Boolean) && estimated.some((e) => !e);
  const line = (name, i, color, axisIndex = 0) => ({
    name,
    type: "line",
    xAxisIndex: axisIndex,
    yAxisIndex: axisIndex,
    data: [...rows.map((r, j) => (mixed && estimated[j] ? null : r[i])), ...pad],
    showSymbol: false,
    connectNulls: false,
    lineStyle: { width: 2, color },
    itemStyle: { color },
  });
  const dashedEstimate = (name, i, color, axisIndex = 0) => ({
    name,
    type: "line",
    xAxisIndex: axisIndex,
    yAxisIndex: axisIndex,
    data: [...rows.map((r, j) => (estimated[j] || estimated[j - 1] || estimated[j + 1] ? r[i] : null)), ...pad],
    showSymbol: false,
    connectNulls: false,
    lineStyle: { width: 2, color, type: "dashed" },
    itemStyle: { color },
    tooltip: { show: false },
  });
  const key = { 2: "ctl", 3: "atl", 4: "tsb" };
  const projected = (name, i, color, axisIndex = 0) => ({
    name: `${name} (${t("projection")})`,
    type: "line",
    xAxisIndex: axisIndex,
    yAxisIndex: axisIndex,
    // Starts at today's value, so the dashed line joins the solid one.
    data: [...rows.map((r, j) => (j === n - 1 ? r[i] : null)), ...ahead.map((r) => r[key[i]])],
    showSymbol: false,
    lineStyle: { width: 2, color, type: "dashed", opacity: 0.8 },
    itemStyle: { color },
  });
  const races = (projection?.races || []).filter((r) => dates.includes(r.date));
  mount(el, {
    grid: [
      { left: 8, right: 8, top: 24, height: "50%", containLabel: true },
      { left: 8, right: 8, top: "70%", bottom: 8, containLabel: true },
    ],
    xAxis: [axis(0, false), axis(1, true)],
    yAxis: [yAxis(0, t("Load")), yAxis(1, t("Form"))],
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      formatter: (items) => {
        const i = items[0].dataIndex;
        if (i < n) {
          const r = rows[i];
          const estimate = r[7] === "computed" ? `<br><i>${esc(t("Estimated by VibeSynergy"))}</i>` : "";
          return `<b>${esc(shortDate(r[0]))}</b><br>${t("Fitness")} ${number(r[2])} · ${t("Fatigue")} ${number(r[3])}<br>${t("Form")} ${number(r[4])} · ${t("Load that day")} ${number(r[1])}${estimate}`;
        }
        const r = ahead[i - n];
        const zone = zoneText(r.form_zone);
        return `<b>${esc(shortDate(r.date))}</b> · ${esc(t("projection"))}<br>${t("Fitness")} ${number(r.ctl)} · ${t("Fatigue")} ${number(r.atl)}<br>${t("Form")} ${number(r.tsb)}${zone ? ` (${esc(zone)})` : ""}`;
      },
    },
    series: [
      {
        name: t("Daily load"),
        type: "bar",
        data: [...rows.map((r) => r[1] || null), ...pad],
        itemStyle: { color: alpha(token("ink-2"), 0.25) },
        barMaxWidth: 6,
      },
      {
        ...line(t("Fitness"), 2, series(1)),
        markLine: races.length
          ? {
              silent: true,
              symbol: "none",
              lineStyle: { color: token("axis"), type: "dotted" },
              // The race's letter (A/B/C); its name is in the race list.
              label: { color: muted, position: "insideEndTop", formatter: (p) => p.name },
              data: races.map((r) => ({ xAxis: r.date, name: r.priority || t("Race") })),
            }
          : undefined,
      },
      line(t("Fatigue"), 3, series(2)),
      {
        ...line(t("Form"), 4, series(3), 1),
        areaStyle: { color: alpha(series(3), 0.12) },
        markArea: { silent: true, itemStyle: { color: alpha(token("msg-danger"), 0.06) }, data: [[{ yAxis: -1000 }, { yAxis: -30 }]] },
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: token("axis"), type: "dashed" },
          label: { show: false },
          data: [{ yAxis: 0 }, { yAxis: -30 }],
        },
      },
      ...(mixed ? [dashedEstimate(t("Fitness"), 2, series(1)), dashedEstimate(t("Fatigue"), 3, series(2)), dashedEstimate(t("Form"), 4, series(3), 1)] : []),
      ...(ahead.length ? [projected(t("Fitness"), 2, series(1)), projected(t("Fatigue"), 3, series(2)), projected(t("Form"), 4, series(3), 1)] : []),
    ],
  });
}
