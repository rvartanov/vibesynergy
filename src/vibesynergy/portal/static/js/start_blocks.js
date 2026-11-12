// Start page blocks added with body and measurements (weight with its trend, body
// composition, blood pressure), what is coming up (with the full calendar behind a
// disclosure), the best running efforts over a period you choose, and the editor that orders
// and hides the blocks. pages/dashboard.js places them; the numbers come from the portal API
// (vibesynergy.query.start_page).
import { get, post } from "./api.js";
import { sparkline } from "./charts.js";
import { addDays, dayMonth, esc, isNum, metricLabel, number, shortDate, sportName, value, weekday } from "./format.js";
import { N, t } from "./i18n.js";
import { withSportIcon } from "./symbols.js";
import { measurementsHtml } from "./pages/day.js";
import { DEFAULT_ORDER, moveVisible, orderChange, toggleHidden, togglePinned } from "./start_layout.js";

// Body and measurements --------------------------------------------------------------------

const TREND = {
  down: N("{kg} kg lighter than a month ago."),
  up: N("{kg} kg heavier than a month ago."),
  stable: N("Stable over the last month."),
};

/** Whether the Body block has nothing to show: no weight, composition or blood pressure in
 * the summary and nothing measured on the day itself. */
export function bodyEmpty(summary, measurements = []) {
  if (!summary) return false; // not known: draw the block, it reports what went wrong
  const bp = summary.blood_pressure;
  return !measurements.length && !summary.weight && !summary.composition?.length && !(bp && (bp.systolic || bp.diastolic));
}

/** Fill the Body and measurements block for `date`; `measurements` are that day's readings.
 * `summary` is the body-summary reply's data when the page already has it. */
export async function mountBody(host, { date, today, meta, stale, measurements = [], timezone, share, summary = null }) {
  let data = summary;
  try {
    if (!data) data = (await get("body-summary", { date })).data;
  } catch (error) {
    if (!stale() && host.isConnected) host.innerHTML = `<p class="notice error" role="alert">${esc(t("This part could not load"))}: ${esc(error.message)}</p>`;
    return;
  }
  if (stale() || !host.isConnected) return;
  const catalogue = meta.data.metrics;
  const parts = [weightHtml(data.weight, date), compositionHtml(data.composition, catalogue, date), pressureHtml(data.blood_pressure, date)].filter(Boolean);
  const table = measurementsHtml(measurements, catalogue, timezone);
  const day = table
    ? `<details class="collapsible dash-body-day"><summary>${esc(date === today ? t("Measured today ({n})", { n: number(measurements.length) }) : t("Measured on this day ({n})", { n: number(measurements.length) }))}</summary>${table}</details>`
    : "";
  const link = !share || share.pages.includes("body") ? `<p class="sub"><a href="#/body">${t("Body")}</a></p>` : "";
  host.innerHTML = parts.length || day
    ? `${parts.join("")}${day}${link}`
    : `<div class="empty">${t("No weight or other body measurements in the last 90 days.")}</div>${link}`;
  const spark = host.querySelector("[data-weight-spark]");
  if (spark && data.weight?.points?.length >= 2) {
    sparkline(spark, data.weight.points, (v) => value(v, "kg"));
  }
}

function measuredOn(day, date) {
  return day === date ? "" : t("measured {date}", { date: shortDate(day) });
}

function weightHtml(weight, date) {
  if (!weight) return "";
  const kg = (x) => number(Math.abs(x), 1);
  const trend = weight.trend ? t(TREND[weight.trend], { kg: kg(weight.change_30d) }) : "";
  const when = measuredOn(weight.date, date);
  const longer = isNum(weight.change_90d)
    ? t("{sign}{kg} kg over 90 days", { sign: weight.change_90d > 0 ? "+" : weight.change_90d < 0 ? "−" : "", kg: kg(weight.change_90d) })
    : "";
  const label = t("Weight");
  return `<div class="dash-body-weight dash-keep-line">
      <div>
        <div class="label">${esc(label)}</div>
        <div class="value">${esc(number(weight.value, 1))}<span class="unit">kg</span></div>
        ${trend ? `<p class="dash-body-trend">${esc(trend)}</p>` : ""}
        ${[when, longer].filter(Boolean).length ? `<p class="sub">${esc([when, longer].filter(Boolean).join(" · "))}</p>` : ""}
      </div>
      <div class="spark" data-weight-spark role="img" aria-label="${esc(t("{name}, last 90 days", { name: label }))}"></div>
    </div>`;
}

function compositionHtml(rows, catalogue, date) {
  if (!rows?.length) return "";
  const body = rows.map((row) => `<tr>
      <th scope="row">${esc(metricLabel(row.metric, catalogue))}</th>
      <td class="num">${esc(value(row.value, row.unit))}</td>
      <td class="muted">${esc(row.date === date ? "" : shortDate(row.date))}</td>
    </tr>`).join("");
  return `<h3>${t("Body composition")}</h3><div class="table-wrap"><table aria-label="${esc(t("Body composition"))}"><tbody>${body}</tbody></table></div>`;
}

function pressureHtml(bp, date) {
  if (!bp || (!bp.systolic && !bp.diastolic)) return "";
  const reading = [bp.systolic, bp.diastolic].map((row) => (row ? number(row.value) : "–")).join("/");
  const day = (bp.systolic || bp.diastolic).date;
  const pulse = bp.pulse ? ` · ${t("pulse {n}", { n: number(bp.pulse.value) })}` : "";
  const when = measuredOn(day, date);
  return `<h3>${t("Blood pressure")}</h3><p><b>${esc(reading)}</b> mmHg${esc(pulse)}${when ? ` <span class="muted">· ${esc(when)}</span>` : ""}</p>`;
}

// Coming up ---------------------------------------------------------------------------------

function dayName(date, today) {
  if (date === today) return t("Today");
  if (date === addDays(today, 1)) return t("Tomorrow");
  return `${weekday(date)} ${dayMonth(date)}`;
}

/** The next seven days of planned workouts and races. */
// `skipToday`: the advice block already lists today's plan (each value once).
export async function mountUpcoming(host, { today, stale, linkActivities = true, skipToday = false }) {
  let data;
  try {
    data = (await get("upcoming")).data;
  } catch (error) {
    if (!stale() && host.isConnected) host.innerHTML = `<p class="notice error" role="alert">${esc(t("This part could not load"))}: ${esc(error.message)}</p>`;
    return;
  }
  if (stale() || !host.isConnected) return;
  const days = data.days.filter((day) => day.items.length && !(skipToday && day.date === today));
  if (!days.length) {
    host.innerHTML = `<p class="sub">${t("Nothing planned for the coming week.")}</p>`;
    return;
  }
  // Folded, the first coming day stays visible.
  host.innerHTML = `<ul class="dash-upcoming">${days.map((day, i) => `<li${i === 0 ? ' class="dash-keep-line"' : ""}>
      <span class="dash-upcoming-day">${esc(dayName(day.date, today))}</span>
      <ul>${day.items.map((item) => `<li>${itemHtml(item, linkActivities)}</li>`).join("")}</ul>
    </li>`).join("")}</ul>`;
}

function itemHtml(item, linkActivities) {
  const title = esc(item.title || sportName(item.sport) || t("Workout"));
  if (item.kind === "race") return `<span class="chip race">${t("race")}</span> <b>${title}</b>`;
  if (item.kind === "event") return `<span class="chip info">${t("event")}</span> ${title}`;
  const done = item.status === "done" || item.status === "changed";
  const chip = done ? ` <span class="chip good">${t(item.status === "done" ? "done" : "≈ changed")}</span>` : item.status === "missed" ? ` <span class="chip warning">${t("missed")}</span>` : "";
  const link = item.activity_id && linkActivities ? ` · <a href="#/activity/${encodeURIComponent(item.activity_id)}">${t("Open activity")}</a>` : "";
  return `${withSportIcon(item.sport, title)}${chip}${link}`;
}

// Best efforts: the block lives in bests_block.js (also used by Fitness).
export { BEST_PERIODS, BEST_SPORTS, mountBests } from "./bests_block.js";

// Arranging the blocks ----------------------------------------------------------------------

/**
 * The editor for the start page's blocks: a checkbox per block to show or hide it, and Move
 * up / Move down buttons (no dragging needed), plus a reset. Each change saves at once to the
 * portal prefs; `onChange(prefs, change)` then lays out the page. `blocks` is what
 * arrangedBlocks() returned for this view. A block without data (`noData`) is left out of
 * the page and unticked here; ticking it pins it, so it stays even while it is empty.
 */
export function mountArrange(host, { blocks, prefs, onChange }) {
  let current = { ...prefs };
  let list = blocks.map((block) => ({ ...block }));
  const visible = () => list.map((block) => block.id);
  const draw = (focus) => {
    host.innerHTML = `
      <h2 id="dash-arrange-title">${t("Arrange the start page")}</h2>
      <p class="sub">${t("Choose which blocks show and in what order. Saved at once, the same on every device.")}</p>
      <ol class="menu-editor dash-arrange-list">${list.map((block, index) => `<li><div class="menu-row">
        <label class="check"><input type="checkbox" data-show="${esc(block.id)}"${block.hidden || block.noData ? "" : " checked"}> ${esc(t(block.label))}${block.noData && !block.hidden ? ` <span class="sub">(${esc(t("no data yet"))})</span>` : ""}</label>
        <span class="menu-move">
          <button class="btn small" type="button" data-move="-1" data-id="${esc(block.id)}" aria-label="${esc(t("Move {name} up", { name: t(block.label) }))}"${index === 0 ? " disabled" : ""}>↑</button>
          <button class="btn small" type="button" data-move="1" data-id="${esc(block.id)}" aria-label="${esc(t("Move {name} down", { name: t(block.label) }))}"${index === list.length - 1 ? " disabled" : ""}>↓</button>
        </span></div></li>`).join("")}</ol>
      <div class="save-row"><button class="btn" type="button" data-arrange-reset>${t("Back to the default layout")}</button>
        <span class="sub" data-arrange-status role="status" aria-live="polite"></span></div>`;
    if (focus) {
      const target = host.querySelector(focus.selector);
      // At the top or bottom the button just used is disabled: keep focus on its partner.
      (target && !target.disabled ? target : host.querySelector(focus.fallback))?.focus();
    }
  };
  const status = (text) => {
    const el = host.querySelector("[data-arrange-status]");
    if (el) el.textContent = text;
  };
  const save = async (changes, announce, focus) => {
    try {
      const reply = await post("prefs", { changes });
      current = reply.data;
      onChange(current, { changes });
      draw(focus);
      status(announce);
    } catch (error) {
      status(t("Not saved: {error}", { error: error.message }));
    }
  };
  host.addEventListener("click", (event) => {
    if (event.target.closest("[data-arrange-reset]")) {
      list = list
        .map((block) => ({ ...block, hidden: false }))
        .sort((a, b) => DEFAULT_ORDER.indexOf(a.id) - DEFAULT_ORDER.indexOf(b.id));
      save({ dashboard_order: null, dashboard_hidden: null, dashboard_pinned: null }, t("Back to the default layout."), { selector: "[data-arrange-reset]" });
      return;
    }
    const button = event.target.closest("button[data-move]");
    if (!button || button.disabled) return;
    const { id } = button.dataset;
    const step = Number(button.dataset.move);
    const order = moveVisible(current.dashboard_order, visible(), id, step);
    const at = list.findIndex((block) => block.id === id);
    [list[at], list[at + step]] = [list[at + step], list[at]];
    const block = list[at + step];
    const where = t("{name}: place {n} of {total}", { name: t(block.label), n: number(at + step + 1), total: number(list.length) });
    save({ dashboard_order: orderChange(order) }, where, {
      selector: `button[data-move="${step}"][data-id="${id}"]`,
      fallback: `button[data-move="${-step}"][data-id="${id}"]`,
    });
  });
  host.addEventListener("change", (event) => {
    const box = event.target.closest("input[data-show]");
    if (!box) return;
    const id = box.dataset.show;
    const block = list.find((item) => item.id === id);
    // A block left out for lack of data is unticked: ticking it pins it, so it shows
    // although it is empty. Any other block is simply shown or hidden.
    const pin = Boolean(block.noData) && box.checked;
    const hidden = toggleHidden(current.dashboard_hidden, id, box.checked);
    const pinned = pin ? togglePinned(current.dashboard_pinned, id, true) : null;
    block.hidden = !box.checked;
    if (pin) block.noData = false;
    const name = t(block.label);
    save(
      { dashboard_hidden: hidden.length ? hidden : null, ...(pinned ? { dashboard_pinned: pinned } : {}) },
      box.checked ? t("{name} is shown.", { name }) : t("{name} is hidden.", { name }),
      { selector: `input[data-show="${id}"]` },
    );
  });
  draw();
}

// Folding a block ------------------------------------------------------------------

/**
 * Every block on the start page folds to its heading: the heading becomes a disclosure
 * button. What you folded is remembered per user (prefs.dashboard_collapsed); a shared view
 * folds for the moment only. A line marked .dash-keep-line stays visible when folded.
 */
export function bindCollapse(container, prefs, { canSave }) {
  if (!container) return;
  const collapsed = new Set(Array.isArray(prefs.dashboard_collapsed) ? prefs.dashboard_collapsed : []);
  for (const block of container.querySelectorAll(".dash-block[data-block]")) {
    const heading = block.querySelector("h2");
    if (!heading || heading.querySelector("[data-collapse]")) continue;
    const id = block.dataset.block;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "dash-collapse";
    button.dataset.collapse = id;
    button.append(...heading.childNodes);
    heading.append(button);
    const show = (folded) => {
      block.toggleAttribute("data-collapsed", folded);
      button.setAttribute("aria-expanded", String(!folded));
    };
    show(collapsed.has(id));
    button.addEventListener("click", async () => {
      const folded = !block.hasAttribute("data-collapsed");
      show(folded);
      if (folded) collapsed.add(id);
      else collapsed.delete(id);
      if (!canSave) return;
      const list = DEFAULT_ORDER.filter((known) => collapsed.has(known));
      prefs.dashboard_collapsed = list;
      try {
        await post("prefs", { changes: { dashboard_collapsed: list.length ? list : null } });
      } catch {
        // folding still works on this page; it is just not remembered
      }
    });
  }
}
