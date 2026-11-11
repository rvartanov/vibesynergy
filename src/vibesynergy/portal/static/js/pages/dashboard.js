// The start page (#/dashboard, #/dashboard/<date>): advice and the plan for the day,
// check-in and complaints, recovery and the week, what is coming up, the day in numbers,
// body and measurements, best runs and the diary, as blocks you can reorder and hide.
// #/today, #/day/<date>, #/diary and #/calendar land here.
import { captureViewState, restoreViewState } from "../refresh_state.js";
import { get, post } from "../api.js";
import { savedAdviceRole } from "../coaching_presentation.js";
import { dashboardCoachingHtml, dashboardCoachingPlan, dayCoachingHtml, hasSavedAdvice } from "../daily_review.js";
import { addDays, esc, hoursMinutes, isNum, km, longDate, number, sourceName, sportName, userDateTime, weekday } from "../format.js";
import { bindEffort, effortHtml } from "../effort.js";
import { N, t } from "../i18n.js";
import { mountQuickLog } from "../quicklog.js";
import { sportIcon, sportKey, withSportIcon } from "../symbols.js";
import { weekProgressHtml } from "../week_progress.js";
import { mountCalendar } from "./calendar.js";
import { dayJournalHtml, mountDayDetails } from "./day.js";
import { mountDiary } from "./diary.js";
import { bindCollapse, mountArrange, mountBests, mountBody, mountUpcoming } from "../start_blocks.js";
import { arrangedBlocks } from "../start_layout.js";

const PHASES = { build: N("Build"), peak: N("Peak"), taper: N("Taper"), recovery: N("Recovery") };
const CHIP = {
  done: ["good", N("done")],
  changed: ["warning", N("≈ changed")],
  missed: ["warning", N("missed")],
  uncertain: ["info", N("match uncertain")],
  today: ["planned", N("today")],
  upcoming: ["planned", N("upcoming")],
  rest: ["", N("Rest")],
};
const CHIP_MARK = { done: "✓", missed: "✗" };
const TRAINING = new Set(["workout", "plan_workout"]);
const RECOVERY_ROWS = [
  ["hrv.night_avg", N("HRV"), "ms"],
  ["sleep.duration", N("Sleep"), "s"],
  ["hr.resting", N("Resting heart rate"), "bpm"],
];

const COACHING_TIMEOUT_MS = 15000;

function errorHtml() {
  return `<p class="notice error">${esc(t("This part could not load"))} · <button type="button" class="btn" data-block-retry>${t("Retry")}</button></p>`;
}

function failed(block) {
  return Boolean(block && block.error);
}

async function fetchDailyCoaching(date) {
  try {
    const row = await get("daily-coaching", { date }, { timeoutMs: COACHING_TIMEOUT_MS });
    return { ok: true, data: row.data };
  } catch {
    return { ok: false };
  }
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function dayHref(date, today) {
  return date === today ? "#/dashboard" : `#/dashboard/${date}`;
}

export async function render(view, { args, query, meta, stale }) {
  const today = meta.data.today;
  const date = args?.[0] && ISO_DAY.test(args[0]) && args[0] <= today ? args[0] : today;
  const isToday = date === today;
  const share = meta.data.share ? new Set(meta.data.share.pages) : null;
  const can = (...pages) => !share || pages.some((page) => share.has(page));
  const showBoard = isToday && can("dashboard");
  const showDay = can("today", "day");
  const showDiary = can("diary");
  const showCalendar = can("calendar");
  const timelinePromise = showDay ? get("day", { date }) : null; // slower: drawn when it arrives
  timelinePromise?.catch(() => {}); // reported where it is drawn
  const [boardReply, dayReply, prefs] = await Promise.all([
    showBoard ? get("dashboard") : null,
    showDay ? get("today", { date }) : null,
    // Block order and hidden blocks; a shared view has no prefs and gets the defaults.
    share ? {} : get("prefs").then((reply) => reply.data || {}, () => ({})),
  ]);
  if (stale()) return;
  document.title = `${isToday ? t("Today") : longDate(date)} · VibeSynergy`;
  const d = boardReply?.data || { date };
  const zone = (boardReply || dayReply)?.meta?.timezone;
  const viewState = captureViewState(view);

  if (d.onboarding) {
    view.innerHTML = `
      <div class="page-head"><h1>${t("Today")}</h1></div>
      <section class="card" aria-labelledby="dash-connect-title">
        <h2 id="dash-connect-title">${t("Connect Garmin")}</h2>
        <p>${t("Connect Garmin to see what to do today.")}</p>
        <p><a class="btn primary" href="#/settings/data/sources">${t("Connect Garmin")}</a></p>
      </section>`;
    return;
  }

  const canDiary = isToday && (!share || (share.has("diary") && meta.data.share.can_write));
  const complaints = showBoard ? complaintsHtml(d.injuries) : "";
  const journal = dayReply ? dayJournalHtml(dayReply.data.day.journal, { checkin: !isToday, injuries: !isToday, linkActivities: can("activities") }) : "";
  const showUpcoming = showBoard;
  // one start page: its blocks in the order you chose, hidden ones left out. Each
  // piece of information has one place: recovery signs are not repeated as key numbers,
  // activities sit in "Plan and done", weight in Body and measurements, the next race only
  // in the header.
  const html = {};
  if (showBoard) {
    html.advice = `<section class="card dash-today verdict ${esc(failed(d.verdict) ? "" : d.verdict?.level || "")}" aria-labelledby="dash-today-title">
          <h2 id="dash-today-title" class="dash-kicker">${t("Advice for today")}</h2>
          <div data-dash-advice>${todayAdviceHtml(d)}</div>
          <div class="dash-plan">
            ${planHtml(failed(d.plan) ? null : d.plan?.today, failed(d.plan) ? null : (d.plan?.not_in_plan || []).filter((row) => row.date === date), share)}
            ${completedHtml(d.completed?.activities, d.completed, share, { editable: true })}
          </div>
          ${!can("coach") ? "" : `<div class="dash-coach" data-dash-coaching><p class="sub" role="status">${esc(t("Loading…"))}</p></div>`}
        </section>`;
  } else if (dayReply) {
    html.advice = `<div class="dash-block-group">
        <section class="card dash-today" aria-labelledby="dash-plan-title">
          <h2 id="dash-plan-title">${t("Plan and done")}</h2>
          <div class="dash-plan dash-plan-first">
            ${planHtml(dayReply.data.plan.filter((row) => TRAINING.has(row.kind)), dayReply.data.not_in_plan || [], share)}
            ${completedHtml(dayReply.data.day.activities, null, share)}
          </div>
        </section>
        ${!can("coach") ? "" : `<section class="card" aria-labelledby="day-coaching"><div data-day-coaching><h2 id="day-coaching">${t("Coaching")}</h2><p class="sub" role="status">${t("Loading…")}</p></div></section>`}
      </div>`;
  }
  if (canDiary) html.checkin = `<section class="card dash-checkin" aria-labelledby="dash-checkin-title"><h2 id="dash-checkin-title">${t("Check-in")}</h2><div data-dash-checkin></div></section>`;
  if (complaints) html.complaints = `<section class="card dash-complaints" aria-labelledby="dash-complaints-title">${complaints}</section>`;
  if (journal || (!isToday && dayReply)) {
    html.noted = `<section class="card day-diary" aria-labelledby="day-journal-title"><h2 id="day-journal-title">${isToday ? t("Noted today") : t("This day in your diary")}</h2>${journal || `<p class="sub">${t("Nothing noted for this day.")}</p>`}</section>`;
  }
  if (showBoard) {
    html.recovery = `<section class="card dash-recovery" aria-labelledby="dash-recovery-title">${recoveryHtml(d.recovery, d.load)}</section>`;
    html.week = `<section class="card dash-week" aria-labelledby="dash-week-title">${weekHtml(d.week, today, d.week_progress, share)}</section>`;
  }
  if (showUpcoming || showCalendar) {
    // The calendar section used to fill a screen with races, an agenda of every planned item
    // and a heatmap. What helps on the start page is what is coming: the next seven days of
    // workouts and races. The full calendar (race priorities, any period, the heatmap) stays
    // one click away.
    html.upcoming = `<section class="card dash-upcoming-card" id="calendar" aria-labelledby="calendar-title">
        <h2 id="calendar-title">${showUpcoming ? t("Coming up") : t("Calendar")}</h2>
        ${showUpcoming ? `<div data-upcoming><p class="sub" role="status">${t("Loading…")}</p></div>` : ""}
        ${showCalendar ? `<details class="collapsible start-diary" data-calendar-details${query?.get("section") === "calendar" || !showUpcoming ? " open" : ""}>
          <summary>${t("Full calendar: races, agenda and heatmap")}</summary><div data-calendar></div>
        </details>` : ""}
        ${showUpcoming ? `<p class="sub"><a href="#/plan">${t("Training plan")}</a></p>` : ""}
      </section>`;
  }
  if (showDay) {
    html.numbers = `<section class="dash-block-plain" aria-labelledby="day-details-title"><h2 id="day-details-title" class="dash-block-title">${t("Key numbers")}</h2><div data-day-numbers></div></section>`;
    html.sleep = `<section class="card" aria-labelledby="day-sleep-title"><h2 id="day-sleep-title">${isToday ? t("Last night") : t("Sleep")}</h2><div data-day-sleep></div></section>`;
    html.body = `<section class="card dash-body" aria-labelledby="day-body-title"><h2 id="day-body-title">${t("Body and measurements")}</h2><div data-day-body><p class="sub" role="status">${t("Loading…")}</p></div></section>`;
    html.timeline = `<section class="card" aria-labelledby="day-timeline-title"><h2 id="day-timeline-title">${t("Timeline")}</h2><div data-timeline></div></section>`;
    // Collapsed by default: the summary names what is inside once it has loaded.
    html.other = `<section class="dash-block-plain" aria-labelledby="day-other-title"><h2 id="day-other-title" class="dash-block-title">${t("Other measurements")}</h2>
      <details class="collapsible" data-other-details><summary data-other-summary>${t("Show the measurements")}</summary><div data-day-other></div></details></section>`;
  }
  if (showBoard) {
    html.bests = `<section class="card dash-bests" aria-labelledby="dash-bests-title"><h2 id="dash-bests-title">${t("Best efforts")}</h2><div data-bests></div></section>`;
  }
  if (showDiary) {
    html.diary = `<section class="card" id="diary" aria-labelledby="diary-title">
      <h2 id="diary-title">${t("Diary")}</h2>
      <details class="collapsible start-diary" data-diary${query?.get("section") === "diary" ? " open" : ""}>
        <summary>${t("History, complaints and more ways to add")}</summary>
        <p class="sub">${t("Your notes, how you feel, and what hurts, day by day. Add a note with dates and tags, or mark pain on the body map.")}</p>
        <div data-diary-panel></div>
      </details>
    </section>`;
  }
  const blocks = arrangedBlocks(prefs, Object.keys(html));
  const shownIds = new Set(blocks.filter((block) => !block.hidden).map((block) => block.id));
  const canArrange = !share;
  view.innerHTML = `
    ${headerHtml(d, zone, { date, today, showBoard })}
    <div class="dash-blocks" data-dash-blocks>${layoutHtml(blocks, html)}</div>
    ${canArrange ? `<div class="dash-arrange-foot">
      <button class="btn" type="button" data-arrange-toggle aria-controls="dash-arrange" aria-expanded="${arrangeOpen}">${t("Arrange the start page")}</button>
      <section class="card dash-arrange" id="dash-arrange" aria-labelledby="dash-arrange-title"${arrangeOpen ? "" : " hidden"}></section>
    </div>` : ""}`;

  bindRefresh(view, { args, query, meta, stale });
  bindDayPicker(view, today);
  for (const button of view.querySelectorAll("[data-block-retry]")) {
    button.addEventListener("click", () => {
      if (!stale()) render(view, { args, query, meta, stale });
    });
  }
  bindCollapse(view.querySelector("[data-dash-blocks]"), prefs, { canSave: canArrange });
  if (canArrange) bindArrange(view, { blocks, prefs, ctx: { args, query, meta, stale } });
  const checkin = view.querySelector("[data-dash-checkin]");
  if (checkin) mountQuickLog(checkin, meta, stale, { variant: "dashboard" });
  bindComplaints(view, { args, query, meta, stale });
  bindDoneEffort(view);
  if (shownIds.has("advice") && showBoard && can("coach")) {
    const coachingHost = view.querySelector("[data-dash-coaching]");
    const adviceHost = view.querySelector("[data-dash-advice]");
    Promise.all([
      fetchDailyCoaching(d.date),
      fetchDailyCoaching(addDays(d.date, -1)),
    ]).then(([todayReply, yesterdayReply]) => {
      fillCoaching(coachingHost, adviceHost, d, todayReply, yesterdayReply, stale);
    });
  }
  if (shownIds.has("advice") && !isToday && can("coach")) {
    fetchDailyCoaching(date).then((reply) => {
      const host = view.querySelector("[data-day-coaching]");
      if (stale() || !host?.isConnected) return;
      host.innerHTML = reply.ok
        ? dayCoachingHtml(reply.data)
        : `<h2 id="day-coaching">${t("Coaching")}</h2><p role="alert">${t("Morning and evening coaching is unavailable.")}</p>`;
    });
  }
  const work = [];
  const hosts = {
    numbers: view.querySelector("[data-day-numbers]"),
    sleep: view.querySelector("[data-day-sleep]"),
    timeline: view.querySelector("[data-timeline]"),
    other: view.querySelector("[data-day-other]"),
  };
  if (dayReply && Object.values(hosts).some(Boolean)) {
    // What another shown block already has is not repeated as a key number.
    const shown = [
      ...(shownIds.has("recovery") ? RECOVERY_ROWS.map(([metric]) => metric) : []),
      ...(shownIds.has("body") ? ["body.weight"] : []),
    ];
    work.push(
      mountDayDetails(hosts, { date, today, meta, stale, reply: dayReply, timelinePromise, shown }).then(() => {
        const summary = view.querySelector("[data-other-summary]");
        const names = [...view.querySelectorAll("[data-day-other] .domains > .card > h3")].map((h) => h.textContent.trim());
        if (summary && names.length) summary.textContent = t("{n} groups: {names}", { n: names.length, names: names.join(", ") });
      }),
    );
  }
  const body = view.querySelector("[data-day-body]");
  if (body) {
    work.push(mountBody(body, {
      date,
      today,
      meta,
      stale,
      measurements: dayReply?.data.day.body_measurements || [],
      timezone: dayReply?.meta.timezone,
      share: meta.data.share,
    }));
  }
  const upcoming = view.querySelector("[data-upcoming]");
  if (upcoming) mountUpcoming(upcoming, { today, stale, linkActivities: can("activities"), skipToday: shownIds.has("advice") && showBoard });
  const bests = view.querySelector("[data-bests]");
  if (bests) mountBests(bests, { period: prefs.best_runs_period, sport: prefs.best_sport, canSave: !share, stale, linkActivities: can("activities") });
  const calendarDetails = view.querySelector("[data-calendar-details]");
  if (calendarDetails) {
    const calendar = calendarDetails.querySelector("[data-calendar]");
    const mount = () => {
      if (calendarDetails.dataset.mounted) return null;
      calendarDetails.dataset.mounted = "1";
      return mountCalendar(calendar, { query: query || new URLSearchParams(), meta, stale }, { hashBase: dayHref(date, today) }).catch((error) => {
        if (!stale()) calendar.innerHTML = `<div class="notice error" role="alert">${esc(t("Could not load this page: {error}", { error: error.message }))}</div>`;
      });
    };
    if (calendarDetails.open) work.push(mount());
    calendarDetails.addEventListener("toggle", () => calendarDetails.open && mount());
  }
  const diary = view.querySelector("[data-diary]");
  if (diary) {
    const mount = () => {
      if (diary.dataset.mounted) return;
      diary.dataset.mounted = "1";
      const host = diary.querySelector("[data-diary-panel]");
      mountDiary(host, { query: query || new URLSearchParams(), meta, stale }, {
        embedded: true,
        skipHeading: true,
        hashBase: dayHref(date, today),
        section: "diary",
        entryForms: canDiary || (!share && isToday) ? { checkin: false } : false,
      }).catch((error) => {
        if (!stale()) host.innerHTML = `<div class="notice error" role="alert">${esc(t("Could not load the diary: {error}", { error: error.message }))}</div>`;
      });
    };
    if (diary.open) mount();
    diary.addEventListener("toggle", () => diary.open && mount());
  }
  await Promise.all(work);
  if (stale()) return;
  const section = query?.get("section");
  if (pendingFocus) {
    view.querySelector(pendingFocus)?.focus();
    pendingFocus = null;
  } else if (section === "calendar" || section === "diary") {
    view.querySelector(`#${section}`)?.scrollIntoView({ block: "start" });
  } else {
    restoreViewState(view, viewState);
  }
}

// Whether the arrange panel is open, and what to focus after a redraw it caused; kept across
// the page's own reloads (refresh, a block shown again).
let arrangeOpen = false;
let pendingFocus = null;

// Blocks in order: wide ones span the page; the others flow two columns wide on a large
// screen (top to bottom, then the next column), so the reading order is the chosen order.
function layoutHtml(blocks, html) {
  let out = "";
  let flow = "";
  const close = () => {
    if (flow) out += `<div class="dash-flow">${flow}</div>`;
    flow = "";
  };
  for (const block of blocks) {
    if (block.hidden) continue;
    const piece = `<div class="dash-block" data-block="${esc(block.id)}">${html[block.id]}</div>`;
    if (block.wide) {
      close();
      out += piece;
    } else {
      flow += piece;
    }
  }
  close();
  return out || `<p class="empty">${t("Every block is hidden. Choose Arrange the start page to show them again.")}</p>`;
}

// Put the blocks already on the page in a new order, without loading anything again.
function relayout(container, blocks) {
  const byId = Object.fromEntries([...container.querySelectorAll("[data-block]")].map((el) => [el.dataset.block, el]));
  const fragment = document.createDocumentFragment();
  let flow = null;
  for (const block of blocks) {
    const el = byId[block.id];
    if (block.hidden || !el) continue;
    if (block.wide) {
      flow = null;
      fragment.append(el);
    } else {
      if (!flow) {
        flow = document.createElement("div");
        flow.className = "dash-flow";
        fragment.append(flow);
      }
      flow.append(el);
    }
  }
  container.replaceChildren(fragment);
  if (!container.children.length) container.innerHTML = `<p class="empty">${t("Every block is hidden. Choose Arrange the start page to show them again.")}</p>`;
}

function bindArrange(view, { blocks, prefs, ctx }) {
  const panel = view.querySelector("#dash-arrange");
  const toggle = view.querySelector("[data-arrange-toggle]");
  const container = view.querySelector("[data-dash-blocks]");
  if (!panel || !toggle) return;
  const available = blocks.map((block) => block.id);
  mountArrange(panel, {
    blocks,
    prefs,
    onChange: (next) => {
      const arranged = arrangedBlocks(next, available);
      const missing = arranged.some((block) => !block.hidden && !container.querySelector(`[data-block="${block.id}"]`));
      if (missing) {
        // A block shown again has to load: draw the page again, focus back where it was.
        const active = document.activeElement;
        pendingFocus = active?.dataset?.show ? `#dash-arrange input[data-show="${active.dataset.show}"]` : "#dash-arrange [data-arrange-reset]";
        if (!ctx.stale()) render(view, ctx);
        return;
      }
      relayout(container, arranged);
    },
  });
  toggle.addEventListener("click", () => {
    arrangeOpen = panel.hidden;
    panel.hidden = !arrangeOpen;
    toggle.setAttribute("aria-expanded", String(arrangeOpen));
    if (arrangeOpen) panel.querySelector("input, button")?.focus();
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    arrangeOpen = false;
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.focus();
  });
}

function bindRefresh(view, ctx) {
  view.querySelector("[data-dashboard-refresh]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const beforeRefresh = captureViewState(view);
    button.disabled = true;
    try {
      await render(view, ctx);
      if (!ctx.stale()) restoreViewState(view, beforeRefresh);
    } catch (error) {
      if (!ctx.stale() && button.isConnected) {
        button.disabled = false;
        let failure = view.querySelector("[data-refresh-error]");
        if (!failure) {
          failure = document.createElement("p");
          failure.dataset.refreshError = "";
          failure.className = "notice error";
          button.after(failure);
        }
        failure.textContent = t("Refresh failed: {error}. The previous result is still shown.", { error: error.message });
      }
    }
  });
}

function bindDayPicker(view, today) {
  view.querySelector("[data-day-picker]")?.addEventListener("change", (event) => {
    const value = event.target.value;
    if (value && ISO_DAY.test(value)) location.hash = dayHref(value > today ? today : value, today);
  });
}

function headerHtml(d, zone, { date, today, showBoard }) {
  const isToday = date === today;
  const warning = showBoard ? syncWarning(d.delivery) : "";
  const tune = showBoard ? tuneUpHtml(d.supporting) : "";
  const race = showBoard ? raceLine(d) : "";
  const updated = showBoard && d.updated_at
    ? `<p class="sub dash-updated">${esc(t("Updated {when}", { when: userDateTime(d.updated_at, zone) }))}</p>`
    : "";
  const title = capitalise(isToday ? longDate(date).replace(/\s\d{4}$/, "") : longDate(date));
  const next = isToday
    ? `<span class="btn" aria-disabled="true" aria-label="${esc(t("Next day"))}">›</span>`
    : `<a class="btn" href="${dayHref(addDays(date, 1), today)}" aria-label="${esc(t("Next day"))}">›</a>`;
  return `<header class="dash-head">
    <div class="dash-head-text">
      <h1>${esc(title)}</h1>
      ${race ? `<p class="dash-race">${esc(race)}</p>` : ""}
      ${tune}
      ${updated}
    </div>
    <div class="dash-head-status">
      <nav class="stepper" aria-label="${esc(t("Days"))}">
        <a class="btn" href="${dayHref(addDays(date, -1), today)}" aria-label="${esc(t("Previous day"))}">‹</a>
        <input class="btn" type="date" value="${esc(date)}" max="${esc(today)}" aria-label="${esc(t("Choose a date"))}" data-day-picker>
        ${next}
        ${isToday ? "" : `<a class="btn" href="#/dashboard">${t("Today")}</a>`}
      </nav>
      ${showBoard ? `<button class="btn" data-refresh-key="dashboard-refresh" data-dashboard-refresh>${t("Refresh")}</button>` : ""}
    </div>
    ${warning}
  </header>`;
}

function capitalise(text) {
  return text ? text[0].toLocaleUpperCase() + text.slice(1) : text;
}

function raceLine(d) {
  const race = d.next_race;
  if (!race?.name) return "";
  const phase = d.plan_phase?.phase;
  const phaseLabel = phase ? t(PHASES[phase] || phase) : "";
  const n = Number(race.days_to_go);
  const raceBit = n === 0
    ? t("{name} is today", { name: race.name })
    : n === 1
      ? t("{name} in {n} day", { name: race.name, n: number(n) })
      : t("{name} in {n} days", { name: race.name, n: number(n) });
  return phaseLabel ? `${raceBit} · ${phaseLabel}` : raceBit;
}

function tuneUpHtml(supporting) {
  const event = Array.isArray(supporting) ? supporting[0] : null;
  if (!event) return "";
  const name = event.name || event.title || "";
  const days = Number(event.days_to_go);
  if (!name || !Number.isFinite(days)) return "";
  const key = days === 1 ? "Tune-up: {name} in {n} day" : "Tune-up: {name} in {n} days";
  return `<p class="sub">${esc(t(key, { name, n: number(days) }))}</p>`;
}

function syncWarning(delivery) {
  if (!delivery || failed(delivery)) return "";
  if (delivery.activity_sync === "failed") {
    return `<p class="notice error">${esc(t("A workout did not arrive. The list shows what is already here."))}</p>`;
  }
  const late = (delivery.sources || []).filter((row) => {
    if (!row.last_collected_at) return false;
    return Date.now() - new Date(row.last_collected_at).getTime() > 24 * 60 * 60 * 1000;
  });
  if (!late.length) return "";
  const names = late.map((row) => sourceName(row.source)).join(", ");
  return `<p class="notice error">${esc(t("Sync is late: {sources}.", { sources: names }))}</p>`;
}

function todayAdviceHtml(d) {
  if (failed(d.verdict)) return errorHtml();
  const advice = d.verdict?.advice || "";
  const reasons = d.verdict?.reasons || [];
  const details = reasons.length
    ? `<details data-refresh-key="dashboard-guidance-details"><summary>${t("Why this advice")}</summary><p>${esc(reasons.join(". "))}.</p></details>`
    : "";
  return `<p class="verdict-advice dash-keep-line" data-dash-main>${esc(advice)}</p>${details}`;
}

function safetyLine(verdict) {
  if (!verdict || failed(verdict) || verdict.safety_level === "ok" || verdict.level === "green") return "";
  const reason = (verdict.reasons || [])[0];
  if (!reason) return "";
  return `<p class="safety-line">${esc(reason)}</p>`;
}

function fillCoaching(host, adviceHost, dashboardData, todayReply, yesterdayReply, stale) {
  if (!host?.isConnected || stale()) return;
  if (!todayReply?.ok) {
    host.innerHTML = "";
    return;
  }
  const plan = dashboardCoachingPlan(todayReply.data, yesterdayReply?.ok ? yesterdayReply.data : {});
  const current = plan.primary && savedAdviceRole(plan.primary.entry) === "current" && hasSavedAdvice(plan.primary.entry);
  if (current && adviceHost?.isConnected) {
    adviceHost.innerHTML = `${safetyLine(dashboardData.verdict)}`;
    adviceHost.after(host);
    host.classList.remove("dash-coach");
  }
  if (plan.empty && !plan.safetyText) {
    host.innerHTML = "";
    return;
  }
  host.innerHTML = dashboardCoachingHtml(plan);
}

function planHtml(sessions, outside, shared) {
  if (sessions === null) return errorHtml();
  // A planned brick: the brick run and the ride with it as one multisport entry.
  const list = sessions || [];
  const isBrickRun = (x) => sportKey(x.sport) === "run" && ((x.extra || {}).brick_part === "loop" || /brick/i.test(x.title || ""));
  const run = list.find(isBrickRun);
  const rides = list.filter((x) => sportKey(x.sport) === "bike");
  const ride = run && (rides.find((x) => (x.extra || {}).brick_part === "fiets") || rides[0]);
  const one = (session) => {
    const title = session.title || sportName(session.sport) || t("Workout");
    const [tone, label] = CHIP[session.status] || ["", ""];
    const chip = label && session.status !== "upcoming" && session.status !== "today"
      ? ` <span class="chip ${tone}">${esc(t(label))}</span>`
      : "";
    const id = session.done_by?.[0]?.id;
    const link = id && (!shared || shared.has("activities"))
      ? ` · <a href="#/activity/${encodeURIComponent(id)}">${t("Open activity")}</a>`
      : "";
    return `<li>${withSportIcon(session.sport, esc(title))}${chip}${link}</li>`;
  };
  const brick = run && ride
    ? `<li>${withSportIcon("brick", esc(`${ride.title} + ${run.title}`))} <span class="sub">${esc(t("Multisport"))}</span><ul class="trends done-parts">${one(ride)}${one(run)}</ul></li>`
    : "";
  const rows = brick + list.filter((x) => !(brick && (x === run || x === ride))).map(one).join("");
  const plan = rows
    ? `<h3>${t("Planned")}</h3><ul class="trends">${rows}</ul>`
    : `<p class="sub">${t("Nothing planned for this day.")}</p>`;
  const others = (outside || []).map((item) => `<li>${withSportIcon(item.sport, esc(item.title || sportName(item.sport)))} <span class="chip info">${t("Garmin only")}</span></li>`).join("");
  const extra = others
    ? `<h3>${t("Not in your plan")}</h3><p class="sub">${t("On your Garmin calendar, but not in the plan. It does not count as planned.")}</p><ul class="trends">${others}</ul>`
    : "";
  return `${plan}${extra}`;
}

function activityTitle(activity) {
  const sport = sportName(activity.sport);
  const given = String(activity.name || "").trim();
  const raw = String(activity.sport || "").replaceAll("_", " ").trim();
  const named = given
    && given.localeCompare(sport, undefined, { sensitivity: "base" }) !== 0
    && given.localeCompare(raw, undefined, { sensitivity: "base" }) !== 0;
  if (named) return given;
  const seconds = isNum(activity.moving_s) ? activity.moving_s : activity.elapsed_s;
  const parts = [sport];
  if (isNum(seconds) && seconds > 0) parts.push(compactDuration(seconds));
  if (isNum(activity.distance_m) && activity.distance_m > 0) {
    parts.push(`${number(activity.distance_m / 1000, 1)} km`);
  }
  return parts.join(" · ");
}

function compactDuration(seconds) {
  const total = Math.round(seconds / 60);
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  if (!hours) return t("{m} min", { m: minutes });
  return t("{h} h {m}", { h: hours, m: String(minutes).padStart(2, "0") });
}

// Done: each activity with its RPE and Feel. Today you can fill in what no platform
// has; a past day and a shared view only show what is there.
function completedHtml(activities, block, shared, { editable = false } = {}) {
  if (block && failed(block)) return "";
  const all = activities || [];
  if (!all.length) return "";
  // A multisport is one entry with its parts inside.
  const byId = new Map(all.map((a) => [a.group_id, a]));
  const items = all.filter((a) => !(a.part_of && byId.has(a.part_of)));
  const one = (activity, index) => {
    const id = activity.sources?.[0] ?? activity.group_id;
    const name = activityTitle(activity);
    const href = `#/activity/${encodeURIComponent(id)}`;
    const label = shared && !shared.has("activities") ? esc(name) : `<a href="${esc(href)}">${esc(name)}</a>`;
    const effort = shared ? "" : effortHtml(activity.effort, `done-${index}`, { readOnly: !editable });
    const parts = (activity.parts || []).map((pid) => byId.get(pid)).filter(Boolean);
    const inner = parts.length
      ? `<details class="collapsible" open><summary>${esc(t("Multisport"))} · ${esc(t("{n} parts", { n: parts.length }))}</summary><ul class="done-list done-parts">${parts.map((part, k) => one(part, `${index}-${k}`)).join("")}</ul></details>`
      : "";
    return `<li>${label}${effort ? `<div data-effort-host data-effort-id="${esc(id)}" data-effort-key="done-${index}">${effort}</div>` : ""}${inner}</li>`;
  };
  const rows = items.map((activity, index) => one(activity, index)).join("");
  return `<h3>${t("Done")}</h3><ul class="done-list">${rows}</ul>`;
}

function bindDoneEffort(view) {
  for (const host of view.querySelectorAll("[data-effort-host]")) {
    bindEffort(host, host.dataset.effortId);
  }
}

function complaintsHtml(injuries) {
  if (failed(injuries)) {
    return `<h2 id="dash-complaints-title">${t("Complaints")}</h2>${errorHtml()}`;
  }
  if (!injuries?.length) return "";
  const rows = injuries.map((injury) => {
    const rising = (injury.flags || []).some((flag) => flag.rule === "worsening");
    const pain = isNum(injury.latest_pain)
      ? t("pain {n}/10", { n: number(injury.latest_pain) })
      : t("no pain logged");
    const trend = rising ? ` · ${t("rising")}` : "";
    const options = Array.from({ length: 11 }, (_, n) => `<option value="${n}">${n}</option>`).join("");
    return `<li><span>${esc(injury.title)} · ${esc(pain)}${esc(trend)}</span>
      <label class="sub">${t("Pain today")} <select class="btn" data-pain="${esc(injury.id)}"><option value="">–</option>${options}</select></label></li>`;
  }).join("");
  return `<h2 id="dash-complaints-title">${t("Complaints")}</h2><ul class="trends dash-complaints-list">${rows}</ul>`;
}

function bindComplaints(view, ctx) {
  const { stale } = ctx;
  for (const select of view.querySelectorAll("[data-pain]")) {
    select.addEventListener("change", async () => {
      if (select.value === "") return;
      try {
        await post("diary/injury", { injury_id: select.dataset.pain, pain: Number(select.value) });
        if (!stale()) render(view, ctx);
      } catch (error) {
        select.insertAdjacentHTML("afterend", `<span class="error-text">${esc(error.message)}</span>`);
      }
    });
  }
}

const FORM_MEANING = {
  fresh: N("Training load: rested, ready for a hard session."),
  neutral: N("Training load: in balance."),
  "optimal training": N("Training load: building fitness; some tiredness is normal."),
  "overreaching risk": N("Training load: very tired; plan rest soon."),
};

function recoveryHtml(recovery, load) {
  if (failed(recovery)) return `<h2 id="dash-recovery-title">${t("Recovery")}</h2>${errorHtml()}`;
  const rows = RECOVERY_ROWS.map(([metric, label]) => {
    const block = recovery?.[metric];
    if (!block) return "";
    const meaning = recoveryMeaning(metric, block);
    return `<tr><th scope="row"><a href="#/metrics?m=${encodeURIComponent(metric)}">${t(label)}</a></th>
      <td><span class="chip ${meaning.tone}">${esc(meaning.text)}</span></td></tr>`;
  }).join("");
  // Training load as meaning, not a number: the figures are on Planning & progress.
  const formText = !failed(load) && isNum(load?.tsb) ? FORM_MEANING[load.form] : null;
  const form = formText
    ? `<p class="sub">${esc(t(formText))} <a href="#/planning">${t("Planning & progress")}</a></p>`
    : "";
  const body = rows
    ? `<div class="table-wrap"><table aria-label="${esc(t("Recovery"))}"><tbody>${rows}</tbody></table></div>`
    : `<div class="empty">${t("No recovery data today yet.")}</div>`;
  return `<h2 id="dash-recovery-title">${t("Recovery")}</h2>${rows ? recoverySummary(recovery) : ""}${body}${form}`;
}

function recoverySummary(recovery) {
  const shown = RECOVERY_ROWS.map(([metric]) => [metric, recovery?.[metric]]).filter(([, block]) => block);
  const off = shown.filter(([metric, block]) => recoveryMeaning(metric, block).tone === "warning").length;
  const text = off
    ? t("{n} of {total} signs are off your usual. Take it a little easier.", { n: number(off), total: number(shown.length) })
    : t("You look recovered as usual.");
  return `<p class="dash-recovery-summary dash-keep-line">${esc(text)}</p>`;
}

function recoveryMeaning(metric, block) {
  const rounded = (value) => Math.round(value * 10) / 10;
  const value = rounded(block.value);
  const known = isNum(block.baseline_std);
  const low = known ? rounded(block.baseline_mean - block.baseline_std) : null;
  const high = known ? rounded(block.baseline_mean + block.baseline_std) : null;
  const below = known && value < low;
  const above = known && value > high;
  if (metric === "sleep.duration") {
    const time = hoursMinutes(block.value);
    if (below) return { tone: "warning", text: t("short ({time})", { time }) };
    if (above) return { tone: "", text: t("long ({time})", { time }) };
    return { tone: "", text: t("normal") };
  }
  if (metric === "hr.resting") {
    if (above) return { tone: "warning", text: t("higher than usual") };
    if (below) return { tone: "", text: t("lower than usual") };
    return { tone: "", text: t("normal") };
  }
  if (below) return { tone: "warning", text: t("below your usual") };
  if (above) return { tone: "", text: t("above your usual") };
  return { tone: "", text: t("normal") };
}

function weekHtml(week, today, progress, shared) {
  if (failed(week)) return `<h2 id="dash-week-title">${t("This week")}</h2>${errorHtml()}`;
  // With a plan this week: per sport, goal against done and what is still planned.
  if (progress && !failed(progress) && progress.has_plan) {
    return `<h2 id="dash-week-title">${t("This week against your plan")}</h2>
      ${weekProgressHtml(progress, { linkActivities: !shared || shared.has("activities") })}
      ${weekStripHtml(week, today)}
      <p class="sub"><a href="#/planning">${t("Planning & progress")}</a></p>`;
  }
  const plan = week?.compare === "plan";
  const title = plan ? t("This week against your plan") : t("This week against your usual week");
  const actual = week?.actual || week?.this || {};
  const target = week?.target || week?.usual_week || {};
  // Without a target (a plan with no distance, a first week) there is nothing to fill:
  // show what was done, not a bar against zero.
  // A sport's bar takes the sport's colour and icon; the name is always written.
  const bar = (label, now, goal, fmt, sport = null) => {
    const key = sport === null ? "" : sportKey(sport);
    const attrs = key ? ` data-sport="${key}"` : "";
    const name = key ? `<span class="sport-icon">${sportIcon(key)}</span>${esc(label)}` : esc(label);
    if (!goal) {
      return `<div class="zone"${attrs}><span class="zlabel">${name}</span><span></span>
      <span class="zvalue">${esc(fmt(now))}</span></div>`;
    }
    const pct = Math.min(100, (now / goal) * 100);
    const fill = key ? "" : ";background:var(--s1)";
    return `<div class="zone"${attrs}><span class="zlabel">${name}</span>
      <span class="ztrack"><span style="width:${pct}%${fill}"></span></span>
      <span class="zvalue">${esc(fmt(now))} <span class="muted">${esc(t("of {total}", { total: fmt(goal) }))}</span></span></div>`;
  };
  // The plan's weekly time per sport, when it has one, says more than one total.
  const targets = week?.targets || [];
  const rows = targets.length
    ? targets.map((goal) => bar(sportName(goal.sport), goal.done_s || 0, goal.planned_s || 0, (value) => hoursMinutes(value), goal.sport || "")).join("")
    : `${bar(t("Distance"), actual.distance_m || 0, target.distance_m || 0, (value) => km(value))}
      ${bar(t("Time"), actual.moving_s || 0, target.moving_s || 0, (value) => hoursMinutes(value))}
      ${bar(t("Climb"), actual.ascent_m || 0, target.ascent_m || 0, (value) => `${number(value)} m`)}`;
  const note = week?.note?.title ? `<p class="sub">${esc(week.note.title)}</p>` : "";
  return `<h2 id="dash-week-title">${esc(title)}</h2>
    ${note}
    <div class="zones">${rows}</div>
    ${weekStripHtml(week, today)}
    <p class="sub"><a href="#/planning">${t("Planning & progress")}</a></p>`;
}

// Monday to Sunday as links to each day, with what the plan says about it.
function weekStripHtml(week, today) {
  if (!week?.start) return "";
  const chips = Object.fromEntries((week.chips || []).map((chip) => [chip.date, chip]));
  const days = Array.from({ length: 7 }, (_, offset) => addDays(week.start, offset)).map((date) => {
    const chip = chips[date];
    const [tone, label] = chip ? CHIP[chip.status] || ["", ""] : ["", ""];
    const mark = chip ? CHIP_MARK[chip.status] || "" : "";
    const word = label ? t(label) : "";
    const name = [longDate(date), chip?.title, word].filter(Boolean).join(", ");
    const current = date === today ? ' aria-current="date"' : "";
    return `<li><a class="week-day ${tone}" href="${dayHref(date, today)}"${current} aria-label="${esc(name)}" title="${esc(name)}"><span aria-hidden="true">${esc(weekday(date))}</span><span class="week-day-mark" aria-hidden="true">${esc(mark || (chip ? "•" : ""))}</span></a></li>`;
  }).join("");
  return `<nav aria-label="${esc(t("Days this week"))}"><ul class="week-strip">${days}</ul></nav>`;
}
