// The calendar section of the start page: races (with their priority), the agenda for planned
// items and a heatmap for any date range. Two independent period pickers; their choice is kept
// in the start page's address with section=calendar.
import { get, post } from "../api.js";
import { addDays, ago, esc, hoursMinutes, isNum, monthName, monthYear, number, shortDate, sourceName, timeOfDay } from "../format.js";
import { N, t } from "../i18n.js";
import {
  apiParams,
  bindPeriodPicker,
  formatPeriodRange,
  heatmapStorageValue,
  isoToday,
  mergePeriodValue,
  periodPickerHtml,
  resolvePeriod,
  saveStored,
  writePeriodQuery,
} from "../period_picker.js";
import { planKind } from "../plan_labels.js";
import { messageIcon } from "../symbols.js";

const METRICS = [
  ["activity.duration", N("Training time"), (v) => hoursMinutes(v)],
  ["load", N("Training load (heart rate)"), (v) => number(v)],
  ["sleep.score", N("Sleep score"), (v) => number(v)],
  ["sleep.duration", N("Time asleep"), (v) => hoursMinutes(v)],
  ["hrv.night_avg", N("Overnight HRV"), (v) => `${number(v)} ms`],
  ["hr.resting", N("Resting heart rate"), (v) => `${number(v)} bpm`],
  ["daily.steps", N("Steps"), (v) => number(v)],
  ["body.weight", N("Weight"), (v) => `${number(v, 1)} kg`],
];
const LEVELS = 5;
const DAY_MS = 86400000;
const HEATMAP_KEY = "vibesynergy.period.calendar-heatmap";
const AGENDA_KEY = "vibesynergy.period.calendar-agenda";
const AGENDA_MAX_DAYS = 366;
const AGENDA_URL = {
  param: { period: "agenda", start: "agenda_start", end: "agenda_end", legacyStart: "start", legacyEnd: "end" },
};

export async function mountCalendar(view, { query, meta, stale }, { hashBase = "#/dashboard" } = {}) {
  const today = meta?.data?.today || isoToday();
  const metric = query.get("m") || "activity.duration";
  const heatDefault = { period: today.slice(0, 4) };
  const agendaDefault = { period: "next_14d" };
  const heatValue = mergePeriodValue(query, HEATMAP_KEY, heatDefault, {
    heatmap: true,
    defaultValue: heatDefault,
    today,
  });
  const agendaValue = mergePeriodValue(query, AGENDA_KEY, agendaDefault, {
    defaultValue: agendaDefault,
    today,
    direction: "forward",
    ...AGENDA_URL,
  });
  const heatParams = { metric, ...heatmapRequestParams(heatValue, today), ...agendaRequestParams(agendaValue, today) };
  const reply = await get("calendar", heatParams);
  if (stale()) return;
  const d = reply.data;
  const agendaYears = [...new Set([...(d.years || []), Number(today.slice(0, 4)) + 1])].sort((a, b) => b - a);
  const span = d.span || { start: `${d.year}-01-01`, end: `${d.year}-12-31` };
  const periodLabel = formatPeriodRange(span.start, span.end);
  const [, name, fmt] = METRICS.find(([id]) => id === metric) || [metric, metric, (v) => number(v, 1)];
  const label = t(name);
  const values = Object.values(d.days).filter(isNum).sort((a, b) => a - b);
  const cuts = Array.from({ length: LEVELS - 1 }, (_, i) => values[Math.floor(((i + 1) * values.length) / LEVELS)]);
  const level = (v) => (isNum(v) ? 1 + cuts.filter((c) => v > c).length : 0);
  const events = Object.fromEntries(d.events.map((e) => [e.date, e]));

  // Races lead only when there are any; "no races" is one line in the agenda.
  const races = Array.isArray(d.races) ? d.races : null;
  view.innerHTML = `
    ${races?.length ? racesHtml(races) : ""}
    ${agendaHtml(d, agendaValue, today, agendaYears, races)}
    <section class="card cal-heatmap">
      <h3>${esc(label)}</h3>
      <form class="filters cal-heatmap-controls">
        <label>${t("Metric")} <select class="btn" name="m">${METRICS.map(([id, mname]) => `<option value="${esc(id)}"${id === metric ? " selected" : ""}>${esc(t(mname))}</option>`).join("")}</select></label>
        ${periodPickerHtml({
          id: "heatmap",
          value: heatValue,
          years: d.years,
          today,
          accessibleName: N("Heatmap period"),
        })}
      </form>
      <p class="sub">${esc(t("{n} days with data · {period}", { n: number(values.length), period: periodLabel }))}</p>
      <div class="cal-wrap">${rangeGrid(span, d.days, level, fmt, label, periodLabel, events)}</div>
      <p class="sub cal-scroll-hint" hidden>${t("The grid scrolls sideways; it opens at the current weeks.")}</p>
      <div class="legend cal-legend"><span>${t("Less")}</span>${Array.from({ length: LEVELS }, (_, i) => `<i class="cal-l${i + 1}"></i>`).join("")}<span>${t("More")}</span>
        <span><i class="cal-l0"></i>${t("no data")}</span><span><i class="cal-l0 cal-event"></i>${t("event")}</span></div>
    </section>`;

  const navigate = (heat, agenda, { storeHeat = false, storeAgenda = false } = {}) => {
    if (storeHeat) saveStored(HEATMAP_KEY, heatmapStorageValue(heat, today));
    if (storeAgenda) saveStored(AGENDA_KEY, agenda);
    const next = writePeriodQuery(new URLSearchParams(query), heat, {
      defaultValue: heatDefault,
      heatmap: true,
      omitDefault: false,
      today,
    });
    writePeriodQuery(next, agenda, { defaultValue: agendaDefault, omitDefault: true, today, ...AGENDA_URL });
    next.set("m", metric);
    next.set("section", "calendar");
    location.hash = `${hashBase}?${next}`;
  };

  bindGridKeys(view.querySelector(".cal-grid"), today);
  showCurrentWeeks(view.querySelector(".cal-wrap"), view.querySelector(".cal-scroll-hint"), today);

  bindPeriodPicker(view, {
    id: "heatmap",
    value: heatValue,
    years: d.years,
    today,
    accessibleName: N("Heatmap period"),
    onChange: (value) => navigate(value, agendaValue, { storeHeat: true }),
  });

  bindPeriodPicker(view, {
    id: "agenda",
    value: agendaValue,
    years: agendaYears,
    today,
    direction: "forward",
    accessibleName: N("Agenda period"),
    maxDays: AGENDA_MAX_DAYS,
    onChange: (value) => navigate(heatValue, value, { storeAgenda: true }),
  });

  const raceCard = view.querySelector("[data-races]");
  raceCard?.addEventListener("focusin", (event) => {
    if (event.target.name === "race_priority") event.target.dataset.previous = event.target.value;
  });
  raceCard?.addEventListener("change", async (event) => {
    const select = event.target;
    if (select.name !== "race_priority") return;
    const row = select.closest("[data-source]");
    const status = view.querySelector("[data-race-status]");
    if (status) status.textContent = "";
    try {
      const saved = await post("race-priority", {
        source: row.dataset.source,
        source_id: row.dataset.sourceId,
        priority: select.value || null,
      });
      select.value = saved.data.priority || "";
      if (status) status.textContent = t("Saved.");
    } catch {
      if (select.dataset.previous !== undefined) select.value = select.dataset.previous;
      if (status) status.textContent = t("Could not save the priority.");
    }
  });

  view.querySelector("[name=m]")?.addEventListener("change", (event) => {
    const next = new URLSearchParams(query);
    next.set("m", event.target.value);
    writePeriodQuery(next, heatValue, { defaultValue: heatDefault, heatmap: true, omitDefault: false, today });
    writePeriodQuery(next, agendaValue, { defaultValue: agendaDefault, omitDefault: true, today, ...AGENDA_URL });
    next.set("section", "calendar");
    location.hash = `${hashBase}?${next}`;
  });
}

// The year grid is one stop in the tab order (a page of 365 links would bury what follows).
// Arrow keys move a day or a week, Home and End go to the first and last day, Enter opens it.
function bindGridKeys(grid, today) {
  if (!grid) return;
  const cells = [...grid.querySelectorAll("a[data-day]")];
  if (!cells.length) return;
  const start = cells.findLast((cell) => cell.dataset.day <= today) || cells[0];
  start.tabIndex = 0;
  grid.addEventListener("keydown", (event) => {
    const index = cells.indexOf(document.activeElement);
    if (index < 0) return;
    const step = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1 }[event.key];
    let next = null;
    if (step) next = cells[Math.max(0, Math.min(cells.length - 1, index + step))];
    if (event.key === "Home") next = cells[0];
    if (event.key === "End") next = cells[cells.length - 1];
    if (!next) return;
    event.preventDefault();
    cells[index].tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  });
}

// A year grid is wider than most screens. Open it at the current weeks (where the data is)
// instead of January, and say that it scrolls.
function showCurrentWeeks(wrap, hint, today) {
  if (!wrap) return;
  const cells = [...wrap.querySelectorAll("a[data-day]")];
  const current = cells.findLast((cell) => cell.dataset.day <= today) || cells.at(-1);
  if (!current) return;
  if (!wrap.clientWidth && typeof ResizeObserver === "function") {
    // Mounted while hidden (a closed section): place it the first time it gets a width.
    const observer = new ResizeObserver(() => {
      if (!wrap.clientWidth) return;
      observer.disconnect();
      showCurrentWeeks(wrap, hint, today);
    });
    observer.observe(wrap);
    return;
  }
  const overflow = wrap.scrollWidth > wrap.clientWidth + 1;
  if (hint) hint.hidden = !overflow;
  if (!overflow) return;
  const right = current.offsetLeft + current.offsetWidth;
  wrap.scrollLeft = Math.max(0, right - wrap.clientWidth + 24);
}

function heatmapRequestParams(value, today) {
  const p = apiParams(value, today);
  if (p.start) return { from: p.start, to: p.end };
  if (p.period && /^\d{4}$/.test(p.period)) return { year: p.period };
  return { period: p.period };
}

function racesHtml(races) {
  return `<section class="card" data-races><h3>${t("Races")}</h3><ul class="ds-icon-list">${races.map(raceRow).join("")}</ul><p class="sub" data-race-status role="status" aria-live="polite"></p></section>`;
}

function raceRow(race) {
  const name = race.name || race.title || t("Race");
  const current = race.priority || "";
  const options = [["", t("Not set")], ["A", "A"], ["B", "B"], ["C", "C"]];
  const choices = options.map(([value, label]) => `<option value="${esc(value)}"${value === current ? " selected" : ""}>${esc(label)}</option>`).join("");
  const priority = race.editable === false
    ? `<span class="sub">${esc(race.priority ? t("{letter} race", { letter: race.priority }) : t("Not set"))} · ${esc(t("set in intervals.icu"))}</span>`
    : `<label>${esc(t("Priority for {name}", { name }))} <select class="btn" name="race_priority">${choices}</select></label>`;
  return `<li class="ds-icon-item ds-icon-item--race" data-source="${esc(race.source)}" data-source-id="${esc(race.source_id)}">${messageIcon("race")}<span><a href="#/dashboard/${esc(race.date)}">${esc(shortDate(race.date))}</a> ${esc(name)} ${priority}</span></li>`;
}

function agendaRequestParams(value, today) {
  if (value.period && !value.start) return { agenda: value.period };
  const span = resolvePeriod(value, today);
  return { agenda_start: span.start, agenda_end: span.end };
}

function rangeGrid(span, days, level, fmt, label, periodLabel, events) {
  const rangeStart = parseIso(span.start);
  const rangeEnd = parseIso(span.end);
  const gridStart = new Date(rangeStart.getTime() - ((rangeStart.getUTCDay() + 6) % 7) * DAY_MS);
  const gridEnd = new Date(rangeEnd.getTime() + (6 - ((rangeEnd.getUTCDay() + 6) % 7)) * DAY_MS);
  const cells = [];
  const months = [];
  let week = 0;
  let labeled = new Set();
  const multiYear = span.start.slice(0, 4) !== span.end.slice(0, 4);
  for (let ms = gridStart.getTime(); ms <= gridEnd.getTime(); ms += DAY_MS) {
    const day = new Date(ms);
    const iso = formatIso(day);
    const row = (day.getUTCDay() + 6) % 7;
    if (row === 0 && ms !== gridStart.getTime()) week += 1;
    const inRange = ms >= rangeStart.getTime() && ms <= rangeEnd.getTime();
    if (!inRange) {
      cells.push(`<span class="cal-day cal-out" style="grid-column:${week + 2};grid-row:${row + 2}"></span>`);
      continue;
    }
    const monthKey = `${day.getUTCFullYear()}-${day.getUTCMonth()}`;
    if (!labeled.has(monthKey) && (day.getUTCDate() === 1 || iso === span.start)) {
      labeled.add(monthKey);
      const monthLabel = multiYear ? monthYear(iso, "short") : monthName(iso, "short");
      months.push(`<span class="cal-month" style="grid-column:${week + 2}">${monthLabel}</span>`);
    }
    const v = days[iso];
    const event = events[iso];
    const title = `${shortDate(iso)}: ${isNum(v) ? fmt(v) : t("no data")}${event ? ` · ${event.title}` : ""}`;
    cells.push(
      `<a class="cal-day cal-l${level(v)}${event ? " cal-event" : ""}" style="grid-column:${week + 2};grid-row:${row + 2}" href="#/dashboard/${iso}" title="${esc(title)}" aria-label="${esc(title)}" data-day="${iso}" tabindex="-1"></a>`,
    );
  }
  const weekdays = [t("Mon"), "", t("Wed"), "", t("Fri"), "", t("Sun")]
    .map((n, i) => `<span class="cal-wd" style="grid-row:${i + 2}">${n}</span>`)
    .join("");
  return `<div class="cal-grid" role="group" aria-roledescription="${esc(t("calendar grid"))}" aria-label="${esc(t("{metric} per day · {period}", { metric: label, period: periodLabel }))}" title="${esc(t("Arrow keys move between days"))}">${months.join("")}${weekdays}${cells.join("")}</div>`;
}

function mirrorNote(data) {
  const lines = [];
  if (data.mirror_active) {
    lines.push(t("Garmin workouts are copied here. Your own appointments stay as they are."));
  }
  if (data.calendar_mirror?.counts?.uncertain) {
    lines.push(t("Some copied workouts still need checking. They will stay as they are for now."));
  }
  if (data.mirror_active && data.schedule_sync?.problems?.length) {
    lines.push(t("The Garmin schedule is not up to date, so copied workouts were left as they are."));
  }
  if (data.calendar_mirror?.counts?.conflict) {
    lines.push(t("A copied workout was changed on the calendar, so it was left as it is."));
  }
  return lines.map((line) => `<p class="sub">${esc(line)}</p>`).join("");
}

function parseIso(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

function formatIso(date) {
  return date.toISOString().slice(0, 10);
}

// Planned training is in "Plan and done", the week strip and the Training plan page, so the
// agenda keeps appointments and events.
const TRAINING_KINDS = new Set(["workout", "plan_workout"]);

function agendaHtml(data, agendaValue, today, years, races = null) {
  if (!Array.isArray(data.agenda)) return "";
  const rows = data.agenda.filter((item) => !TRAINING_KINDS.has(item.kind)).map((item) => {
    const extra = item.extra || {};
    const timing = extra.start && !extra.all_day ? ` · ${timeOfDay(extra.start)}` : "";
    const ending = extra.all_day ? ` · ${t("All day")}${extra.end && extra.end > addDays(item.date, 1) ? ` · ${esc(t("Through {date}", { date: shortDate(addDays(extra.end, -1)) }))}` : ""}`
      : extra.end ? ` · ${t("Ends")} ${esc(shortDate(extra.end.slice(0, 10)))} ${esc(timeOfDay(extra.end))}` : "";
    const fresh = extra.snapshot_at ? ` · ${t("Received")} ${ago(extra.snapshot_at)}` : "";
    const merged = item.mirrors?.length ? ` · ${t("Includes verified calendar mirror")}` : "";
    return `<li class="ds-icon-item ds-icon-item--planned">${messageIcon("planned")}<div><a href="#/dashboard/${encodeURIComponent(item.date)}">${esc(shortDate(item.date))}</a>${esc(timing)} · <b>${esc(item.title || planKind(item))}</b>
      <div class="sub">${esc(planKind(item))} · ${esc(sourceName(item.source))}${ending}${esc(fresh)}${esc(merged)}</div></div></li>`;
  }).join("");
  const sync = data.calendar_sync;
  const failed = sync?.problems?.length;
  const state = sync?.datasets?.[0];
  const note = failed ? t("Calendar refresh failed; showing the last saved context.")
    : state?.last_success_at ? t("Calendar last collected {time}", { time: ago(state.last_success_at) })
    : state?.processing?.last_received_at ? t("Calendar last received {time}", { time: ago(state.processing.last_received_at) })
    : t("Personal calendars are not connected yet.");
  return `<section class="card"><h3>${t("Agenda")}</h3>
    <p class="sub">${t("Appointments are context, not completed exercise.")} <a href="#/planning">${t("Planned training is in your training plan.")}</a></p>
    <div class="filters">
      ${periodPickerHtml({
        id: "agenda",
        value: agendaValue,
        years,
        today,
        direction: "forward",
        accessibleName: N("Agenda period"),
        maxDays: AGENDA_MAX_DAYS,
      })}
    </div>
    <p class="${failed ? "notice warning" : "sub"}">${esc(note)}</p>
    ${mirrorNote(data)}
    ${rows ? `<ul class="ds-icon-list">${rows}</ul>` : `<p class="empty">${t("Nothing planned in this period.")}</p>`}
    ${races && !races.length ? `<p class="sub">${t("No upcoming races.")}</p>` : ""}
  </section>`;
}
