// Training → Calendar: a compact month or week grid, each session a bar in its sport's
// colour (solid when done, dashed when still planned) with its title where there is room
// (the week view: title, time and distance; the month view from 900px wide: a short title),
// and below it the day you pick:
// recovery, the weather, what you did and what is planned. Arrow keys move the selected day
// (← → a day, ↑ ↓ a week); the day stays in the address so a reload keeps it. Weather comes
// from Garmin or Open-Meteo, not intervals.icu: per outdoor session at its start and hour,
// and a forecast for the coming days.
import { get, post } from "../api.js";
import { addDays, esc, hoursMinutes, isNum, isoDay, longDate, monthYear, number, parseDay, weekday } from "../format.js";
import { N, t } from "../i18n.js";
import { sportIcon, sportKey, sportLabel } from "../symbols.js";
import { complianceChip } from "../compliance.js";
import { workoutHtml } from "../workout_text.js";
import { setWeatherUnits } from "../weather_units.js";
import { weatherHtml } from "../weather_day.js";

const STATUS = {
  done: ["success", N("Done")],
  changed: ["advice", N("Changed")],
  missed: ["warning", N("Missed")],
  today: ["planned", N("Today")],
  upcoming: ["planned", N("Planned")],
};
const VIEWS = [
  ["month", N("Month")],
  ["week", N("Week")],
];
const MAX_BARS = 4;

function monday(day) {
  const back = (parseDay(day).getUTCDay() + 6) % 7;
  return addDays(day, -back);
}

function rangeFor(view, anchor) {
  if (view === "week") {
    const start = monday(anchor);
    return { start, end: addDays(start, 6) };
  }
  const first = `${anchor.slice(0, 7)}-01`;
  const d = parseDay(first);
  const last = isoDay(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)));
  return { start: monday(first), end: addDays(monday(last), 6) };
}

function km(m) {
  return isNum(m) && m > 0 ? `${number(m / 1000, 1)} km` : "";
}

// One entry per session: planned sessions that were done count as done. Each has what the
// cell can write next to its bar: a title, the time and the distance.
function bars(day, races) {
  const out = races.filter((r) => r.date === day.date).map((r) => ({ state: "race", sport: "other", title: r.title || t("Race") }));
  for (const p of day.planned) {
    const state = p.status === "done" || p.status === "changed" ? "done" : p.status === "missed" ? "missed" : "planned";
    const did = (p.done || [])[0];
    out.push({ state, sport: sportKey(p.sport), title: p.title || did?.name || sportLabel(p.sport), seconds: did?.moving_s ?? p.planned_s, distance: did?.distance_m ?? p.distance_m });
  }
  for (const d of day.done) out.push({ state: "done", sport: sportKey(d.sport), title: d.name || sportLabel(d.sport), seconds: d.moving_s, distance: d.distance_m });
  return out;
}

const STATE_WORD = { done: N("done"), planned: N("planned"), missed: N("missed"), race: N("race") };

// A session in a cell: its bar and, where the view has room (the week, and the month on a
// wide screen; see .sched-item in design.css), its title, time and distance. The full
// title is in the tooltip and in the cell's name.
function itemHtml(b) {
  const time = isNum(b.seconds) && b.seconds > 0 ? hoursMinutes(b.seconds) : "";
  const dist = km(b.distance);
  const tip = [b.title, time, dist, t(STATE_WORD[b.state])].filter(Boolean).join(" · ");
  return `<span class="sched-item" data-sport="${esc(b.sport)}" data-state="${b.state}" title="${esc(tip)}"><span class="sched-bar" data-sport="${esc(b.sport)}" data-state="${b.state}"></span><span class="sched-title">${esc(b.title)}</span>${time || dist ? `<span class="sched-meta">${esc(time)}${time && dist ? `<span class="sched-dist"> · ${esc(dist)}</span>` : esc(dist)}</span>` : ""}</span>`;
}

function cellHtml(day, { today, month, selected, races }) {
  const items = bars(day, races);
  const outside = month && day.date.slice(0, 7) !== month;
  const shown = items.slice(0, MAX_BARS).map(itemHtml).join("");
  const more = items.length > MAX_BARS ? `<span class="sched-more">+${items.length - MAX_BARS}</span>` : "";
  const done = items.filter((b) => b.state === "done").length;
  const planned = items.filter((b) => b.state === "planned").length;
  const label = [
    longDate(day.date),
    done ? t("{n} done", { n: done }) : "",
    planned ? t("{n} planned", { n: planned }) : "",
    items.some((b) => b.state === "race") ? t("race") : "",
    ...items.map((b) => b.title),
  ].filter(Boolean).join(", ");
  const cls = ["sched-cell", day.date === today ? "is-today" : "", outside ? "is-outside" : ""].filter(Boolean).join(" ");
  return `<button type="button" class="${cls}" data-day="${day.date}" aria-pressed="${day.date === selected}" aria-label="${esc(label)}" tabindex="${day.date === selected ? 0 : -1}"${day.date === today ? ' aria-current="date"' : ""}>
    <span class="sched-num"><span class="sched-wd">${esc(weekday(day.date))} </span>${esc(String(Number(day.date.slice(8))))}</span>
    <span class="sched-bars">${shown}${more}</span></button>`;
}

function weekSummaryHtml(start, data) {
  const s = data.week_summary?.[start];
  const week = data.weeks.find((w) => w.start === start);
  const days = data.days.filter((d) => d.date >= start && d.date <= addDays(start, 6));
  const target = (week?.targets || []).reduce((sum, x) => sum + (isNum(x.time_s) ? x.time_s : 0), 0);
  const planned = days.flatMap((d) => d.planned).reduce((sum, p) => sum + (isNum(p.planned_s) ? p.planned_s : 0), 0);
  const goal = target || planned;
  const facts = [
    [t("Time"), hoursMinutes(s?.time_s || 0) + (goal ? ` / ${hoursMinutes(goal)}` : "")],
    // Unknown load (no power, pace or heart rate to count it from) is a dash, never a zero.
    [t("Load"), isNum(s?.load) ? number(s.load) : "—"],
    [t("Activities"), number(s?.count || 0)],
  ];
  // Per discipline: done, and what is planned or the week's target.
  const plan = data.week_planned?.[start] || {};
  const families = [...new Set([...Object.keys(s?.sports || {}), ...Object.keys(plan)])];
  const sports = families.map((fam) => {
    const x = (s?.sports || {})[fam] || { time_s: 0, distance_m: 0 };
    const p = plan[fam];
    const goal = p ? (isNum(p.target_s) ? p.target_s : p.time_s) : 0;
    const time = hoursMinutes(x.time_s) + (goal ? ` / ${hoursMinutes(goal)}` : "");
    const distText = p?.distance_m
      ? t("{done} of {planned} planned", { done: km(x.distance_m || 0), planned: km(p.distance_m) })
      : x.distance_m ? km(x.distance_m) : "";
    return `<li data-sport="${esc(sportKey(fam))}"><span class="sport-icon">${sportIcon(sportKey(fam))}</span>${esc(sportLabel(fam))} ${esc(time)}${distText ? ` · ${esc(distText)}` : ""}</li>`;
  }).join("");
  return `<section class="card sched-summary" aria-labelledby="sched-week-title">
    <h2 id="sched-week-title">${esc(week?.title || t("Week of {date}", { date: longDate(start) }))}</h2>
    <dl class="sched-facts">${facts.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    ${sports ? `<ul class="plain sched-sports">${sports}</ul>` : ""}
    ${week?.note ? `<details class="collapsible sched-note"><summary>${esc(t("Week note"))}</summary><div class="plan-note">${esc(week.note).replaceAll("\n", "<br>")}</div></details>` : ""}</section>`;
}

function wellnessHtml(w) {
  if (!w) return "";
  const tiles = [
    [t("Readiness"), isNum(w.readiness) ? number(w.readiness) : "–"],
    [t("HRV"), isNum(w.hrv_ms) ? `${number(w.hrv_ms)} ms` : "–"],
    [t("Sleep"), isNum(w.sleep_s) ? hoursMinutes(w.sleep_s) : "–"],
    [t("Resting heart rate"), isNum(w.rhr_bpm) ? `${number(w.rhr_bpm)} bpm` : "–"],
  ];
  const more = [
    [t("Heart rate"), isNum(w.rhr_bpm) && isNum(w.hr_max_bpm) ? `${number(w.rhr_bpm)}–${number(w.hr_max_bpm)} bpm` : ""],
    [t("Stress"), isNum(w.stress) ? number(w.stress) : ""],
    [t("Body Battery"), isNum(w.bb_charged) || isNum(w.bb_drained) ? `+${number(w.bb_charged || 0)} / −${number(w.bb_drained || 0)}` : ""],
    [t("Steps"), isNum(w.steps) ? number(w.steps) : ""],
  ].filter(([, v]) => v);
  const list = (rows) => rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
  return `<dl class="sched-facts sched-wellness">${list(tiles)}</dl>${more.length ? `<details class="collapsible"><summary>${esc(t("More about the day"))}</summary><dl class="sched-facts">${list(more)}</dl></details>` : ""}`;
}

function statsLine(x) {
  return [isNum(x.moving_s) ? hoursMinutes(x.moving_s) : "", km(x.distance_m), isNum(x.load) ? t("load {n}", { n: number(x.load) }) : "", isNum(x.avg_hr) ? `${number(x.avg_hr)} bpm` : ""].filter(Boolean).join(" · ");
}

function doneCard(x, plan = null) {
  const line = statsLine(x);
  const planned = plan ? `<div class="sub">${esc(t("Planned: {title}", { title: plan.title || "" }))} ${complianceChip(x.compliance, x.compliance_source)}${plan.status === "changed" ? ` <span class="chip advice">${esc(t("Changed"))}</span>` : ""}</div>` : "";
  return `<li class="sched-entry" data-sport="${esc(sportKey(x.sport))}"><span class="sport-icon">${sportIcon(sportKey(x.sport))}</span>
    <div><a href="#/activity/${encodeURIComponent(x.id)}">${esc(x.name || plan?.title || "")}</a>${line ? `<div class="sub">${esc(line)}</div>` : ""}${planned}<div class="sub sched-act-weather" data-weather-for="${esc(x.id)}"></div></div></li>`;
}

// The holder of a brick or triathlon: one entry, the parts folded inside. The parts
// count for their own sport; the holder adds nothing to the totals.
function multisportCard(h, partsHtml) {
  const facts = [isNum(h.moving_s) ? hoursMinutes(h.moving_s) : "", t("{n} parts", { n: h.parts.length })].filter(Boolean).join(" · ");
  return `<li class="sched-entry is-multisport" data-sport="${esc(sportKey(h.sport))}"><span class="sport-icon">${sportIcon(sportKey(h.sport))}</span>
    <div><details class="collapsible sched-multi" open><summary><b>${esc(h.name || t("Multisport"))}</b> <span class="sub">${esc(t("Multisport"))} · ${esc(facts)}</span></summary>
      <ul class="plain sched-list sched-parts">${partsHtml}</ul></details>
      ${h.id ? `<a class="sub" href="#/activity/${encodeURIComponent(h.id)}">${esc(t("Open the multisport"))}</a>` : ""}</div></li>`;
}

// Move or skip a planned session, while VibeSynergy leads the plan.
function editHtml(p, day, today) {
  return `<details class="collapsible sched-edit"><summary>${esc(t("Change"))}</summary>
    <div class="filters">
      <label>${esc(t("Move to"))} <input class="btn" type="date" value="${esc(day)}" min="${esc(today)}" data-move-date></label>
      <button class="btn" type="button" data-move="${esc(p.session_id)}">${esc(t("Move"))}</button>
      <button class="btn" type="button" data-skip="${esc(p.session_id)}">${esc(t("Skip this session"))}</button>
    </div><p class="sub" role="status" data-edit-status></p></details>`;
}

function plannedCard(p, edit = null) {
  const [tone, label] = STATUS[p.status] || ["", ""];
  const facts = [isNum(p.planned_s) ? hoursMinutes(p.planned_s) : "", km(p.distance_m)].filter(Boolean).join(" · ");
  const details = p.description ? `<details class="collapsible sched-details"><summary>${esc(t("Workout details"))}</summary>${workoutHtml(p.description)}</details>` : "";
  return `<li class="sched-entry is-planned" data-sport="${esc(sportKey(p.sport))}"><span class="sport-icon">${sportIcon(sportKey(p.sport))}</span>
    <div><b>${esc(p.title || "")}</b>${label ? ` <span class="chip ${tone}">${esc(t(label))}</span>` : ""}${facts ? `<div class="sub">${esc(facts)}</div>` : ""}${details}${edit || ""}</div></li>`;
}

function dayPanelHtml(day, { data, weather, canEdit = false }) {
  if (!day) return "";
  const races = data.races.filter((r) => r.date === day.date).map((r) => `<p><span class="chip race">${esc(r.priority ? `${r.priority} · ${r.title}` : r.title)}</span></p>`).join("");
  // A planned session that was done shows once: the activity, with what was planned.
  // A multisport is one holder with its parts inside; a part keeps what was planned.
  const entries = [...day.planned.flatMap((p) => (p.done || []).map((d) => ({ d: { ...d, sport: d.sport || p.sport }, p }))), ...day.done.map((d) => ({ d, p: null }))];
  const holders = day.multisport || [];
  const inHolder = new Set(holders.flatMap((h) => h.parts));
  const partCard = new Map(entries.filter(({ d }) => inHolder.has(d.id)).map(({ d, p }) => [d.id, doneCard(d, p)]));
  const done = [
    ...holders.map((h) => multisportCard(h, h.parts.map((id) => partCard.get(id) || "").join(""))),
    ...entries.filter(({ d }) => !inHolder.has(d.id)).map(({ d, p }) => doneCard(d, p)),
  ];
  const editable = (p) => canEdit && data.editable && p.session_id && day.date >= data.today && p.status !== "done";
  // A planned brick is one holder with its ride and run, while neither is done yet.
  const brickParts = new Set();
  const brickCards = (day.planned_multisport || []).filter((b) => b.parts.every((i) => !(day.planned[i].done || []).length)).map((b) => {
    b.parts.forEach((i) => brickParts.add(i));
    const inner = b.parts.map((i) => plannedCard(day.planned[i], editable(day.planned[i]) ? editHtml(day.planned[i], day.date, data.today) : null)).join("");
    return `<li class="sched-entry is-planned is-multisport" data-sport="brick"><span class="sport-icon">${sportIcon("brick")}</span>
      <div><details class="collapsible sched-multi" open><summary><b>${esc(b.title)}</b> <span class="sub">${esc(t("Multisport"))} · ${esc(hoursMinutes(b.planned_s))}</span></summary>
        <ul class="plain sched-list sched-parts">${inner}</ul></details></div></li>`;
  });
  const planned = [...brickCards, ...day.planned.filter((p, i) => !brickParts.has(i) && !(p.done || []).length).map((p) => plannedCard(p, editable(p) ? editHtml(p, day.date, data.today) : null))];
  return `<section class="card sched-panel" aria-labelledby="sched-day-title" aria-live="polite">
    <div class="sched-panel-head"><h2 id="sched-day-title">${esc(longDate(day.date))}</h2>
      <a href="#/dashboard/${day.date}">${esc(t("Open the day"))}</a></div>
    ${wellnessHtml(day.wellness)}
    ${day.date >= data.today ? weatherHtml((weather?.days || []).find((w) => w.date === day.date), weather?.location) : ""}
    ${races}
    ${done.length ? `<h3>${esc(t("Done"))}</h3><ul class="plain sched-list">${done.join("")}</ul>` : ""}
    ${planned.length ? `<h3>${esc(t("Planned"))}</h3><ul class="plain sched-list">${planned.join("")}</ul>` : ""}
    ${!done.length && !planned.length && !races ? `<p class="sub">${esc(t("Rest"))}</p>` : ""}
  </section>`;
}

// The weather of the day's outdoor sessions, fetched when you open the day (kept per day).
const activityWeather = new Map();
async function fillActivityWeather(view, date) {
  const slots = view.querySelectorAll("[data-weather-for]");
  if (!slots.length) return;
  if (!activityWeather.has(date)) {
    activityWeather.set(date, get("schedule/weather", { date }).then((r) => r.data.activities || [], () => []));
  }
  const found = await activityWeather.get(date);
  for (const slot of slots) {
    if (!slot.isConnected) continue;
    const hit = found.find((x) => x.id === slot.dataset.weatherFor);
    const label = hit?.place ? t("Weather in {place}", { place: hit.place }) : t("Weather");
    slot.textContent = hit ? `${label}: ${hit.line}` : "";
  }
}

export async function render(view, { query, meta, stale }) {
  const today = meta?.data?.today || isoDay(new Date());
  setWeatherUnits(meta?.data?.weather_units);
  const mode = VIEWS.some(([id]) => id === query.get("view")) ? query.get("view") : "month";
  const anchor = /^\d{4}-\d{2}-\d{2}$/.test(query.get("date") || "") ? query.get("date") : today;
  const { start, end } = rangeFor(mode, anchor);
  document.title = `${t("Calendar")} · VibeSynergy`;
  const [{ data }, weather] = await Promise.all([
    get("schedule", { start, end }),
    get("weather").then((r) => r.data, () => null),
  ]);
  if (stale()) return;
  let selected = anchor;
  const month = mode === "month" ? anchor.slice(0, 7) : null;
  const prev = mode === "week" ? addDays(anchor, -7) : isoDay(new Date(Date.UTC(Number(anchor.slice(0, 4)), Number(anchor.slice(5, 7)) - 2, 1)));
  const next = mode === "week" ? addDays(anchor, 7) : isoDay(new Date(Date.UTC(Number(anchor.slice(0, 4)), Number(anchor.slice(5, 7)), 1)));
  const href = (date, v = mode) => `#/schedule?view=${v}&date=${date}`;
  const title = mode === "month" ? monthYear(anchor, "long") : t("Week of {date}", { date: longDate(start) });
  const cells = data.days.map((d) => cellHtml(d, { today, month, selected, races: data.races })).join("");
  view.innerHTML = `<div class="page-head"><div><h1>${esc(t("Calendar"))}</h1><p class="sub">${esc(title)}</p></div></div>
    <nav class="sched-nav" aria-label="${esc(t("Calendar"))}">
      <a class="btn" href="${href(prev)}" aria-label="${esc(t("Previous"))}">‹</a>
      <a class="btn" href="${href(today)}">${esc(t("Today"))}</a>
      <a class="btn" href="${href(next)}" aria-label="${esc(t("Next"))}">›</a>
      <span class="sched-views">${VIEWS.map(([id, label]) => `<a class="btn" href="${href(anchor, id)}"${id === mode ? ' aria-current="page"' : ""}>${esc(t(label))}</a>`).join("")}</span>
    </nav>
    <div class="sched-grid-wrap">
      <div class="sched-grid sched-head${mode === "week" ? " is-week" : ""}" aria-hidden="true">${data.days.slice(0, 7).map((d) => `<span>${esc(weekday(d.date))}</span>`).join("")}</div>
      <div class="sched-grid${mode === "week" ? " is-week" : ""}" role="group" aria-label="${esc(title)}">${cells}</div>
      <p class="sub sched-legend"><span class="sched-bar" data-state="done" data-sport="run"></span> ${esc(t("done"))} <span class="sched-bar" data-state="planned" data-sport="run"></span> ${esc(t("planned"))} <span class="sched-bar" data-state="missed" data-sport="run"></span> ${esc(t("missed"))}</p>
    </div>
    <div data-sched-week></div>
    <div data-sched-day></div>`;
  const buttons = [...view.querySelectorAll(".sched-cell")];
  const show = (date, { focus = false } = {}) => {
    selected = date;
    for (const b of buttons) {
      const on = b.dataset.day === date;
      b.setAttribute("aria-pressed", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    }
    view.querySelector("[data-sched-week]").innerHTML = weekSummaryHtml(monday(date), data);
    view.querySelector("[data-sched-day]").innerHTML = dayPanelHtml(data.days.find((d) => d.date === date), { data, weather, canEdit: !meta?.data?.share });
    history.replaceState(null, "", href(date));
    fillActivityWeather(view, date);
  };
  if (!buttons.some((b) => b.dataset.day === selected)) selected = buttons[0]?.dataset.day;
  show(selected);
  view.querySelector(".sched-grid[role=group]").addEventListener("click", (event) => {
    const button = event.target.closest(".sched-cell");
    if (button) show(button.dataset.day);
  });
  view.querySelector("[data-sched-day]").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-move], [data-skip]");
    if (!button) return;
    const box = button.closest(".sched-edit");
    const status = box.querySelector("[data-edit-status]");
    button.disabled = true;
    try {
      if (button.dataset.move) {
        const date = box.querySelector("[data-move-date]").value;
        await post("plan/session/move", { session_id: button.dataset.move, date });
        status.textContent = t("Moved. The calendar, intervals.icu and Garmin show it within minutes.");
      } else {
        if (!window.confirm(t("Skip this session? It leaves the plan, Garmin and intervals.icu."))) return;
        await post("plan/session/skip", { session_id: button.dataset.skip });
        location.hash = `${href(selected)}&changed=${Date.now()}`;
      }
    } catch (error) {
      status.textContent = t("Not saved: {error}", { error: t(error.message) });
    } finally {
      button.disabled = false;
    }
  });
  view.querySelector(".sched-grid[role=group]").addEventListener("keydown", (event) => {
    // The week is a list of days on a narrow screen: up and down then move one day.
    const column = mode === "week" && window.matchMedia("(max-width: 899px)").matches;
    const move = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: column ? 1 : 7, ArrowUp: column ? -1 : -7 }[event.key];
    if (!move) return;
    const i = buttons.findIndex((b) => b.dataset.day === selected);
    const target = buttons[i + move];
    if (target) {
      event.preventDefault();
      show(target.dataset.day, { focus: true });
    }
  });
}
