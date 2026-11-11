// SQL notebook: one read-only SELECT at a time over the documented tables (the same runner as
// the MCP tool vibesynergy_sql), results as a table and optionally a chart. Saved queries live in
// this browser; the examples show the tables' typical use.
import { get, post } from "../api.js";
import { alpha, mount, series, token, tooltipBase } from "../charts.js";
import { esc, isNum, number } from "../format.js";
import { N, t } from "../i18n.js";

const EXAMPLES = [
  [
    N("Running distance per month"),
    `SELECT substr(local_date, 1, 7) AS month,
       round(sum(distance_m) / 1000, 1) AS km,
       count(*) AS runs
FROM activity_unified
WHERE sport LIKE '%run%'
GROUP BY month
ORDER BY month`,
  ],
  [
    N("Resting heart rate and HRV per week"),
    `SELECT strftime('%Y-%W', date) AS week,
       round(avg(CASE WHEN metric = 'hr.resting' THEN value END), 1) AS resting_hr,
       round(avg(CASE WHEN metric = 'hrv.night_avg' THEN value END), 1) AS hrv
FROM daily_unified
WHERE metric IN ('hr.resting', 'hrv.night_avg')
GROUP BY week
ORDER BY week`,
  ],
  [
    N("Longest activities"),
    `SELECT local_date, sport, name,
       round(coalesce(moving_s, elapsed_s) / 3600.0, 2) AS hours,
       round(distance_m / 1000, 1) AS km, avg_hr
FROM activity_unified
ORDER BY coalesce(moving_s, elapsed_s) DESC
LIMIT 20`,
  ],
  [
    N("Weight per month (scale)"),
    `SELECT substr(measured_at, 1, 7) AS month, round(avg(value), 1) AS kg, count(*) AS weigh_ins
FROM body_measurement
WHERE metric = 'body.weight' AND source = 'withings'
GROUP BY month
ORDER BY month`,
  ],
];
const LIMITS = ["100", "500", "1000", "5000"];
const SAVED_KEY = "vibesynergy-sql-saved";
const LAST_KEY = "vibesynergy-sql-last";

// Browser storage can be unavailable (private windows); the page works without it.
function load(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}
function store(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // not saved; the notebook still works
  }
}

export async function render(view, { stale }) {
  const schema = await get("sql/schema");
  if (stale()) return;
  document.title = "SQL · VibeSynergy";
  const state = { query: load(LAST_KEY, EXAMPLES[0][1]), limit: "500", result: null };

  view.innerHTML = `
    <div class="page-head"><h1>SQL</h1><div class="sub">${esc(t("Read-only · {dialect} · one SELECT, up to 10 seconds", { dialect: schema.data.dialect }))}</div></div>
    <section class="card sql-editor">
      <div class="filters">
        <label>${t("Example")} <select class="btn" name="example"><option value="">${t("Choose…")}</option>${EXAMPLES.map(([name], i) => `<option value="${i}">${esc(t(name))}</option>`).join("")}</select></label>
        <label>${t("Saved")} <select class="btn" name="saved"></select></label>
        <label>${t("Rows")} <select class="btn" name="limit">${LIMITS.map((l) => `<option${l === state.limit ? " selected" : ""}>${l}</option>`).join("")}</select></label>
      </div>
      <textarea class="sql-text" spellcheck="false" aria-label="SQL query">${esc(state.query)}</textarea>
      <div class="sql-actions">
        <button class="btn primary" type="button" data-run>${t("Run")}</button>
        <span class="sub">Ctrl+Enter</span>
        <span class="grow"></span>
        <button class="btn" type="button" data-save>${t("Save…")}</button>
        <button class="btn" type="button" data-delete hidden>${t("Delete saved")}</button>
      </div>
      <form class="inline-name sql-save-name" hidden>
        <label>${t("Name for this query")}
          <input class="btn" name="saved-name" type="text" required autocomplete="off" maxlength="80">
        </label>
        <button class="btn primary" type="submit">${t("Save")}</button>
        <button class="btn" type="button" data-save-cancel>${t("Cancel")}</button>
      </form>
    </section>
    <section class="card sql-result"><div class="empty">${t("Run a query to see results.")}</div></section>
    <details class="card"><summary>${t("Tables")}</summary>${schemaHtml(schema.data.tables)}</details>`;

  const text = view.querySelector(".sql-text");
  const savedSelect = view.querySelector("[name=saved]");
  const deleteButton = view.querySelector("[data-delete]");
  const result = view.querySelector(".sql-result");

  const refreshSaved = (selected = "") => {
    const saved = load(SAVED_KEY, []);
    savedSelect.innerHTML = `<option value="">${saved.length ? t("Choose…") : t("None yet")}</option>${saved
      .map((s, i) => `<option value="${i}"${String(i) === selected ? " selected" : ""}>${esc(s.name)}</option>`)
      .join("")}`;
    deleteButton.hidden = selected === "";
  };
  refreshSaved();

  const run = async () => {
    state.query = text.value;
    store(LAST_KEY, state.query);
    result.innerHTML = `<div class="loading" role="status">${t("Running…")}</div>`;
    try {
      const reply = await post("sql", { query: state.query, limit: Number(state.limit) });
      if (stale()) return;
      state.result = reply.data;
      showResult(result, reply.data);
    } catch (error) {
      if (stale()) return;
      result.innerHTML = `<div class="notice error" role="alert">${esc(error.message)}</div>`;
    }
  };

  view.querySelector("[data-run]").addEventListener("click", run);
  text.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      run();
    }
  });
  view.querySelector("[name=limit]").addEventListener("change", (event) => {
    state.limit = event.target.value;
  });
  view.querySelector("[name=example]").addEventListener("change", (event) => {
    if (event.target.value === "") return;
    text.value = EXAMPLES[Number(event.target.value)][1];
    event.target.value = "";
    run();
  });
  savedSelect.addEventListener("change", () => {
    const saved = load(SAVED_KEY, []);
    const pick = saved[Number(savedSelect.value)];
    deleteButton.hidden = !pick;
    if (!pick) return;
    text.value = pick.query;
    run();
  });
  const saveForm = view.querySelector(".sql-save-name");
  const saveName = saveForm.querySelector("[name=saved-name]");
  view.querySelector("[data-save]").addEventListener("click", () => {
    saveForm.hidden = false;
    saveName.value = "";
    saveName.focus();
  });
  saveForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = saveName.value.trim();
    if (!name) return;
    const saved = load(SAVED_KEY, []).filter((s) => s.name !== name);
    saved.push({ name, query: text.value });
    saved.sort((a, b) => a.name.localeCompare(b.name));
    store(SAVED_KEY, saved);
    refreshSaved(String(saved.findIndex((s) => s.name === name)));
    saveForm.hidden = true;
    view.querySelector("[data-save]").focus();
  });
  view.querySelector("[data-save-cancel]").addEventListener("click", () => {
    saveForm.hidden = true;
    view.querySelector("[data-save]").focus();
  });
  deleteButton.addEventListener("click", () => {
    const saved = load(SAVED_KEY, []);
    const index = Number(savedSelect.value);
    if (!saved[index] || !window.confirm(t("Delete the saved query “{name}”?", { name: saved[index].name }))) return;
    saved.splice(index, 1);
    store(SAVED_KEY, saved);
    refreshSaved();
  });

  run();
}

// Results -----------------------------------------------------------------------------

function showResult(el, data) {
  const { columns, rows } = data;
  if (!columns.length) {
    el.innerHTML = `<div class="empty">${t("The query returned no columns.")}</div>`;
    return;
  }
  const numeric = columns.map((_, i) => rows.length > 0 && rows.every((r) => r[i] === null || isNum(r[i])));
  const yChoices = columns.filter((_, i) => i > 0 && numeric[i]);
  el.innerHTML = `
    <div class="sub" role="status">${esc(rows.length === 1 ? t("1 row") : t("{n} rows", { n: number(rows.length) }))}${data.truncated ? ` ${t("(more rows exist: raise Rows or narrow the query)")}` : ""} · ${number(data.seconds * 1000)} ms</div>
    ${yChoices.length ? `<div class="filters sql-chart-controls">
        <label>${t("Chart")} <select class="btn" name="kind"><option value="">${t("No chart")}</option><option value="line">${t("Line")}</option><option value="bar">${t("Bars")}</option><option value="scatter">${t("Dots")}</option></select></label>
        <label>X <select class="btn" name="x">${columns.map((c, i) => `<option value="${i}">${esc(c)}</option>`).join("")}</select></label>
        <span class="sub">Y: ${yChoices.map((c) => `<label class="check"><input type="checkbox" name="y" value="${esc(c)}" checked> ${esc(c)}</label>`).join(" ")}</span>
      </div><div class="chart sql-chart" hidden></div>` : ""}
    <div class="table-wrap sql-table"><table><thead><tr>${columns.map((c, i) => `<th${numeric[i] ? ' class="num"' : ""}>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${r.map((v, i) => `<td${numeric[i] ? ' class="num"' : ""}>${cell(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;

  const controls = el.querySelector(".sql-chart-controls");
  if (!controls) return;
  const chartEl = el.querySelector(".sql-chart");
  let chart = null;
  const draw = () => {
    const kind = controls.querySelector("[name=kind]").value;
    chartEl.hidden = !kind; // visible before drawing, or the chart measures zero size
    if (!kind) return;
    const x = Number(controls.querySelector("[name=x]").value);
    const ys = [...controls.querySelectorAll("[name=y]:checked")].map((c) => columns.indexOf(c.value)).filter((i) => i !== x);
    const option = chartOption(kind, columns, rows, x, ys, numeric[x]);
    if (chart) {
      chart.setOption({ animation: false, ...option }, true);
      chart.resize();
    } else {
      chart = mount(chartEl, option);
    }
  };
  controls.addEventListener("change", draw);
  // A first chart when the result looks like a series: a label column and numbers.
  if (rows.length > 1 && !numeric[0]) {
    controls.querySelector("[name=kind]").value = rows.length > 40 ? "line" : "bar";
    draw();
  }
}

function cell(v) {
  if (v === null) return `<span class="muted">null</span>`;
  if (isNum(v)) return esc(Number.isInteger(v) ? String(v) : number(v, Math.abs(v) < 100 ? 2 : 1));
  return esc(String(v));
}

function chartOption(kind, columns, rows, x, ys, xNumeric) {
  const muted = token("muted");
  const category = !(kind === "scatter" && xNumeric);
  return {
    grid: { left: 56, right: 16, top: 30, bottom: 36 },
    legend: { top: 0, textStyle: { color: token("ink-2") } },
    xAxis: {
      type: category ? "category" : "value",
      scale: !category,
      data: category ? rows.map((r) => String(r[x])) : undefined,
      name: columns[x],
      nameLocation: "middle",
      nameGap: 24,
      nameTextStyle: { color: muted },
      axisLabel: { color: muted, hideOverlap: true },
      axisLine: { lineStyle: { color: token("axis") } },
      splitLine: { show: !category, lineStyle: { color: token("grid") } },
    },
    yAxis: { type: "value", scale: kind !== "bar", axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } } },
    tooltip: { ...tooltipBase(), trigger: kind === "scatter" ? "item" : "axis" },
    series: ys.map((i, n) => ({
      name: columns[i],
      type: kind,
      data: category ? rows.map((r) => r[i]) : rows.map((r) => [r[x], r[i]]),
      showSymbol: kind !== "line" || rows.length < 60,
      symbolSize: 6,
      barMaxWidth: 28,
      itemStyle: { color: kind === "scatter" ? alpha(series(n + 1), 0.6) : series(n + 1) },
      lineStyle: { width: 2, color: series(n + 1) },
    })),
  };
}

function schemaHtml(tables) {
  return `<div class="table-wrap"><table><thead><tr><th>${t("Table")}</th><th>${t("What's in it")}</th></tr></thead><tbody>${Object.entries(tables)
    .map(([name, doc]) => `<tr><td><code>${esc(name)}</code></td><td>${esc(doc)}</td></tr>`)
    .join("")}</tbody></table></div>
    <p class="sub">${t("Dates are local (YYYY-MM-DD); timestamps are UTC. Values are SI units: seconds, metres, kilograms, m/s. Metric ids are listed on the Metrics page.")}</p>`;
}
