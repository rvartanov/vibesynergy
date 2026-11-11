// Chart helpers on ECharts. Colours come from the CSS role tokens, so charts follow the
// light/dark theme; pages re-render on theme change.
import * as echarts from "../vendor/echarts/echarts.esm.min.mjs";
import { esc, shortDate } from "./format.js";

const live = new Set();

export function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();
}

export function series(n) {
  return token(`s${n}`);
}

// Hex -> rgba with alpha, for area washes.
export function alpha(hex, a) {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

export function mount(el, option) {
  const chart = echarts.init(el, null, { renderer: "svg" });
  chart.setOption({ animation: false, textStyle: { fontFamily: token("font") }, ...option });
  live.add(chart);
  return chart;
}

const cleanups = [];

// Run when the page is left (e.g. to remove a map).
export function onLeave(fn) {
  cleanups.push(fn);
}

// Dispose every chart (and map) of the page being left.
export function disposeAll() {
  for (const chart of live) chart.dispose();
  live.clear();
  while (cleanups.length) {
    try {
      cleanups.pop()();
    } catch {
      // a failed cleanup must not block the next page
    }
  }
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => live.forEach((c) => c.resize()), 100);
});

function tooltipBase() {
  return {
    backgroundColor: token("surface"),
    borderColor: token("ring"),
    borderWidth: 1,
    padding: [6, 10],
    textStyle: { color: token("ink"), fontSize: 13 },
    extraCssText: "box-shadow: 0 2px 10px rgba(0,0,0,.12); border-radius: 8px;",
    confine: true,
  };
}

// A small trend line for a stat tile: points = [[isoDate, value|null], …].
// Hover shows the date and the formatted value; the last value gets an end dot.
export function sparkline(el, points, formatValue) {
  const color = series(1);
  const last = [...points].reverse().find(([, v]) => v !== null);
  return mount(el, {
    grid: { left: 4, right: 8, top: 6, bottom: 4 },
    xAxis: { type: "category", data: points.map(([d]) => d), show: false, boundaryGap: false },
    yAxis: { type: "value", show: false, scale: true },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: token("axis"), width: 1 } },
      formatter: (items) => {
        const item = items[0];
        return `<span style="color:${token("ink-2")}">${esc(shortDate(item.axisValue))}</span><br><b>${esc(formatValue(item.value))}</b>`;
      },
    },
    series: [
      {
        type: "line",
        data: points.map(([, v]) => v),
        connectNulls: true,
        showSymbol: false,
        symbolSize: 8,
        lineStyle: { width: 2, color, cap: "round", join: "round" },
        itemStyle: { color, borderColor: token("surface"), borderWidth: 2 },
        areaStyle: { color: alpha(color, 0.1) },
        markPoint: last
          ? {
              symbol: "circle",
              symbolSize: 8,
              itemStyle: { color, borderColor: token("surface"), borderWidth: 2 },
              label: { show: false },
              data: [{ coord: [last[0], last[1]] }],
            }
          : undefined,
      },
    ],
  });
}

export { tooltipBase };
