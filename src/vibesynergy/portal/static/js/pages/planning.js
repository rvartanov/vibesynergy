// Planning & progress: one page for the plan and how it goes. A week picker on top;
// the chosen week against the plan per sport and per workout (the start page's week card,
// query/week_progress.py); plan against done over the weeks and phases; where form stands,
// with a link to Fitness → Form for the chart and the projection; the next nine
// days, the remaining phases and the races with a countdown. The
// "Training analysis" tab holds the former Training page (load model, volume, zones, power
// curve). Read-only: the plan changes in intervals.icu or through the coach.
import { get, url } from "../api.js";
import { mount, series, token, tooltipBase } from "../charts.js";
import { addDays, dayMonth, esc, hoursMinutes, isNum, km, longDate, number, shortDate, sportName } from "../format.js";
import { N, language, t } from "../i18n.js";
import { workoutHtml } from "../workout_text.js";
import { formLinkHtml, zoneText } from "../form_block.js";
import { sportIcon, sportKey, withSportIcon } from "../symbols.js";
import { weekProgressHtml } from "../week_progress.js";

const TABS = [
  ["", N("Plan and progress")],
  ["analysis", N("Training analysis")],
  ["builder", N("Build a plan")],
];
const KINDS = {
  load: N("Load week"),
  recovery: N("Recovery week"),
  race: N("Race week"),
  taper: N("Taper week"),
};
// A mark next to the colour, so a status never rests on colour alone.
const STATUS = {
  done: ["success", N("done")],
  changed: ["warning", N("≈ changed")],
  missed: ["warning", N("missed")],
  optional: ["planned", N("option")],
  uncertain: ["info", N("match uncertain")],
  today: ["planned", N("today")],
  upcoming: ["planned", N("upcoming")],
};
const FAMILY_SPORT = { swim: "swimming", ride: "cycling", run: "running", strength: "strength_training", walk: "walking" };

function tabsHtml(current) {
  return `<nav class="tabs settings-nav" aria-label="${esc(t("Planning & progress"))}">${TABS.map(([id, label]) => `<a class="btn" href="#/planning${id ? `/${id}` : ""}"${id === current ? ' aria-current="page"' : ""}>${esc(t(label))}</a>`).join("")}</nav>`;
}

export async function render(view, ctx) {
  const { args = [], query, stale } = ctx;
  if (args[0] === "analysis") return renderAnalysis(view, ctx);
  if (args[0] === "builder") return renderBuilder(view, ctx);
  const week = query.get("week") || "";
  const reply = await get("planning", week ? { week } : {});
  if (stale()) return;
  document.title = `${t("Planning & progress")} · VibeSynergy`;
  const d = reply.data;
  const plan = d.plan || {};
  const weeks = plan.weeks || [];
  const progress = d.week;
  view.innerHTML = `
    <div class="page-head"><div><h1>${t("Planning & progress")}</h1>
      <p class="sub">${esc(d.plan_source === "vibesynergy" ? t("Your coach changes the plan; you can move or skip a session in the calendar.") : t("Read-only. The plan is changed in intervals.icu or through your coach."))} ${esc(t("Download the plan:"))} <a href="${esc(url(`plan/export.xlsx?lang=${language()}`))}" download>Excel</a> · <a href="${esc(url(`plan/export.pdf?lang=${language()}`))}" download>PDF</a> · <a href="${esc(url("plan/export.csv"))}" download>CSV</a> · <a href="${esc(url("plan/booklet"))}" target="_blank" rel="noopener">${esc(t("Coach booklet (print)"))}</a></p></div>
      ${pickerHtml(plan.phases || [], weeks, progress.start, d.today)}</div>
    ${tabsHtml("")}
    ${weeks.length || plan.races?.length || progress.has_plan ? "" : noPlanHtml()}
    <div class="plan-layout">
      <div class="plan-main">
        ${weekCardHtml(progress, weeks, d.today)}
        ${garminHtml(d.horizon)}
        ${trendHtml(d.trend)}
        ${formLinkHtml(d.fitness)}
        ${upcomingHtml(plan.upcoming)}
      </div>
      <div class="plan-side">
        ${racesHtml(d.projection, plan.races || [])}
        ${phasesHtml(plan.phases || [], weeks, d.today)}
      </div>
    </div>`;
  const go = (changes) => {
    const params = new URLSearchParams({ ...(week ? { week } : {}), ...changes });
    for (const [k, v] of [...params]) if (!v) params.delete(k);
    location.hash = `#/planning${params.toString() ? `?${params}` : ""}`;
  };
  view.querySelector("#plan-week-picker")?.addEventListener("change", (event) => go({ week: event.target.value }));
  const trendEl = view.querySelector(".planning-trend");
  if (trendEl) trendChart(trendEl, d.trend.weeks);
}

// Build a plan: the settings a new plan is built from.
async function renderBuilder(view, ctx) {
  document.title = `${t("Build a plan")} · ${t("Planning & progress")} · VibeSynergy`;
  view.innerHTML = `<div class="page-head"><h1>${t("Planning & progress")}</h1></div>
    ${tabsHtml("builder")}
    <p class="sub">${esc(t("The plan builder uses these settings. They start from your current plan; every change saves at once."))}</p>
    <div data-plan-builder><p class="sub" role="status" aria-busy="true">${esc(t("Loading…"))}</p></div>`;
  const { mountBuilder } = await import("../plan_builder.js");
  if (ctx.stale()) return;
  await mountBuilder(view.querySelector("[data-plan-builder]"), ctx.stale);
}

// The former Training page, below the same heading and tabs.
async function renderAnalysis(view, ctx) {
  document.title = `${t("Training analysis")} · ${t("Planning & progress")} · VibeSynergy`;
  view.innerHTML = `<div class="page-head"><h1>${t("Planning & progress")}</h1></div>
    ${tabsHtml("analysis")}
    <div data-planning-analysis><p class="sub" role="status" aria-busy="true">${esc(t("Loading…"))}</p></div>`;
  const { render: renderTraining } = await import("./training.js");
  if (ctx.stale()) return;
  await renderTraining(view.querySelector("[data-planning-analysis]"), ctx);
}

// No plan yet: say so once, with the next step.
function noPlanHtml() {
  return `<section class="card empty-state" aria-labelledby="plan-empty-title">
    <h2 id="plan-empty-title">${esc(t("No plan yet"))}</h2>
    <p>${esc(t("Build a plan here, or set up your coach and let it plan with you. Your weeks, phases and races then show on this page."))}</p>
    <p class="actions"><a class="btn primary" href="#/planning/builder">${esc(t("Build a plan"))}</a> <a class="btn" href="#/coach/settings">${esc(t("Set up your coach"))}</a></p>
  </section>`;
}

// On your Garmin: the dashboard's horizon warnings point here. Null without a plan or
// without a connected Garmin (the server leaves it out): then there is nothing to check.
function garminHtml(h) {
  if (!h || h.has_plan === false) return "";
  const rows = [
    ...h.missing.map((i) => t("Not on your Garmin yet: {title} ({day}).", { title: i.title, day: longDate(i.date) })),
    ...h.duplicates.map((i) => t("Twice on your Garmin: {title} ({day}).", { title: i.title, day: longDate(i.date) })),
    ...h.outdated.map((i) => t("Old version on your Garmin: {title} ({day}).", { title: i.title, day: longDate(i.date) })),
    ...(h.fill?.behind
      ? [h.fill.planned_through
        ? t("Your plan runs only to {day}. Ask your coach to fill it up to {end}.", { day: longDate(h.fill.planned_through), end: longDate(h.through) })
        : t("Nothing is planned for the coming days. Ask your coach to fill your plan up to {end}.", { end: longDate(h.through) })]
      : []),
  ];
  const ok = !rows.length;
  return `<section class="card" aria-labelledby="plan-garmin-title">
    <h2 id="plan-garmin-title">${esc(t("Are your workouts on your watch?"))} <span class="chip ${ok ? "success" : "warning"}">${esc(ok ? t("Yes") : t("{n} to check", { n: rows.length }))}</span></h2>
    <p class="sub">${esc(t("VibeSynergy puts your planned workouts on your Garmin. This shows whether that worked for the coming days."))}</p>
    ${ok ? `<p>${esc(t("Everything planned up to {day} is on your Garmin.", { day: longDate(h.through) }))}</p>` : `<ul class="plain">${rows.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`}
  </section>`;
}

// Week picker ---------------------------------------------------------------------------

function weekName(week) {
  return isNum(week.number) ? t("Week {n}", { n: number(week.number) }) : t("Week of {date}", { date: shortDate(week.start) });
}

function kindLabel(kind) {
  return kind && KINDS[kind] ? t(KINDS[kind]) : "";
}

function pickerHtml(phases, weeks, chosen, today) {
  const option = (w) => {
    const parts = [weekName(w), `${shortDate(w.start)} – ${shortDate(w.end)}`];
    const kind = kindLabel(w.kind);
    if (kind) parts.push(kind);
    if (w.when === "current") parts.push(t("this week"));
    return `<option value="${esc(w.start)}"${w.start === chosen ? " selected" : ""}>${esc(parts.join(" · "))}</option>`;
  };
  const used = new Set();
  const groups = phases.map((phase) => {
    const inPhase = weeks.filter((w) => w.start >= phase.start && w.end <= phase.end);
    inPhase.forEach((w) => used.add(w));
    return inPhase.length ? `<optgroup label="${esc(phase.phase || t("Other"))}">${inPhase.map(option).join("")}</optgroup>` : "";
  }).join("");
  const rest = weeks.filter((w) => !used.has(w)).map(option).join("");
  // A week outside the plan (before it started) still reads as the chosen one.
  const outside = weeks.some((w) => w.start === chosen)
    ? ""
    : `<option value="${esc(chosen)}" selected>${esc(t("Week of {date}", { date: shortDate(chosen) }))}${chosen <= today && addDays(chosen, 6) >= today ? ` · ${esc(t("this week"))}` : ""}</option>`;
  return `<div class="plan-picker field"><label for="plan-week-picker">${t("Week")}</label>
    <select id="plan-week-picker">${outside}${groups}${rest}</select></div>`;
}

// The chosen week -----------------------------------------------------------------------

function weekCardHtml(progress, weeks, today) {
  const planWeek = weeks.find((w) => w.start === progress.start);
  const meta = [planWeek?.phase, kindLabel(planWeek?.kind)].filter(Boolean).join(" · ");
  const current = progress.start <= today && today <= progress.end ? ` <span class="chip planned">${t("This week")}</span>` : "";
  const link = (start, label, rel) => `<a class="btn" href="#/planning?week=${esc(start)}" rel="${rel}">${esc(label)}</a>`;
  const title = planWeek ? weekName(planWeek) : t("Week of {date}", { date: shortDate(progress.start) });
  return `<section class="card plan-week${current ? " plan-current" : ""}" aria-labelledby="plan-week-title">
    <div class="plan-week-head">
      <div>
        <h2 id="plan-week-title">${esc(title)}${current}</h2>
        <p class="sub">${esc(`${dayMonth(progress.start)} – ${dayMonth(progress.end)}`)}${meta ? ` · ${esc(meta)}` : ""}</p>
      </div>
      <nav class="stepper" aria-label="${esc(t("Weeks"))}">
        ${link(addDays(progress.start, -7), t("Previous week"), "prev")}
        ${link(addDays(progress.start, 7), t("Next week"), "next")}
      </nav>
    </div>
    ${progress.has_plan || (progress.sessions || []).length ? weekProgressHtml(progress) : `<p class="empty">${t("Nothing planned or done this week.")}</p>`}
  </section>`;
}

// Plan against done over the weeks --------------------------------------------------------

function pct(value) {
  return isNum(value) ? `${number(value)}%` : "–";
}

// Advice first: how the last weeks went against the plan, in one sentence.
function trendSummary(trend) {
  const totals = trend.totals || {};
  const share = totals.done_pct;
  if (!isNum(share)) return t("No plan in these weeks to compare with.");
  if (share > 120) return t("More than the plan: {pct} of the planned time in the weeks with a plan.", { pct: pct(share) });
  if (share >= 90) return t("You do what the plan asks: {pct} of the planned time in these weeks.", { pct: pct(share) });
  if (share >= 70) return t("Mostly on plan: {pct} of the planned time in these weeks.", { pct: pct(share) });
  return t("Well below the plan: {pct} of the planned time in these weeks. Talk to your coach about a plan that fits.", { pct: pct(share) });
}

function trendHtml(trend) {
  const weeks = (trend?.weeks || []).filter((w) => w.has_plan || w.done_s);
  if (!weeks.length) return "";
  const rows = weeks.map((w) => `<tr${w.when === "current" ? ' aria-current="date"' : ""}>
      <th scope="row"><a href="#/planning?week=${esc(w.start)}">${esc(isNum(w.number) ? weekName(w) : shortDate(w.start))}</a>${w.phase ? `<div class="sub">${esc(w.phase)}</div>` : ""}</th>
      <td>${isNum(w.goal_s) ? esc(hoursMinutes(w.goal_s)) : "–"}</td>
      <td>${esc(hoursMinutes(w.done_s || 0))}</td>
      <td>${esc(pct(w.done_pct))}</td>
      <td>${esc(pct(w.sessions_done_pct))}</td>
      <td>${esc(pct(w.compliance_avg))}</td>
    </tr>`).join("");
  const phases = (trend.phases || []).filter((p) => p.phase);
  const phaseRows = phases.map((p) => `<tr><th scope="row">${esc(p.phase)}<div class="sub">${esc(`${shortDate(p.start)} – ${shortDate(p.end)}`)}</div></th>
      <td>${isNum(p.goal_s) ? esc(hoursMinutes(p.goal_s)) : "–"}</td><td>${esc(hoursMinutes(p.done_s || 0))}</td>
      <td>${esc(pct(p.done_pct))}</td><td>${esc(pct(p.sessions_done_pct))}</td><td>${esc(pct(p.compliance_avg))}</td></tr>`).join("");
  const head = `<thead><tr><th scope="col">${t("Week")}</th><th scope="col">${t("Planned")}</th><th scope="col">${t("Done")}</th><th scope="col">${t("Of plan")}</th><th scope="col">${t("Workouts as planned")}</th><th scope="col">${t("intervals.icu compliance")}</th></tr></thead>`;
  const phaseHead = head.replace(`>${t("Week")}<`, `>${t("Phase")}<`);
  return `<section class="card" aria-labelledby="plan-trend-title">
    <h2 id="plan-trend-title">${t("Plan against done")}</h2>
    <p class="wp-summary">${esc(trendSummary(trend))}</p>
    <div class="chart planning-trend" role="img" aria-label="${esc(t("Planned and done training time per week"))}"></div>
    <details class="details"><summary>${t("Per week and phase")}</summary>
      <div class="table-wrap"><table>${head}<tbody>${rows}</tbody></table></div>
      ${phaseRows ? `<h3>${t("Per phase")}</h3><div class="table-wrap"><table>${phaseHead}<tbody>${phaseRows}</tbody></table></div>` : ""}
      <p class="sub">${esc(t("Workouts as planned: of the planned workouts whose day has passed, the share done as planned. intervals.icu compliance: its own score for workouts it paired with an activity."))}</p>
    </details>
  </section>`;
}

function trendChart(el, weeks) {
  const shown = weeks.filter((w) => w.has_plan || w.done_s);
  const labels = shown.map((w) => w.start);
  const muted = token("muted");
  // Done per sport in the sport's own colour; the plan as an outline behind it.
  const families = [...new Set(shown.flatMap((w) => (w.sports || []).map((s) => s.family || "other")))];
  const sportSeries = families.map((fam) => {
    const key = sportKey(FAMILY_SPORT[fam] || fam);
    return {
      name: sportName(FAMILY_SPORT[fam] || fam),
      type: "bar",
      stack: "done",
      barMaxWidth: 26,
      data: shown.map((w) => {
        const s = (w.sports || []).find((x) => (x.family || "other") === fam);
        return s ? Math.round((s.done_s / 3600) * 100) / 100 : 0;
      }),
      itemStyle: { color: token(`sport-${key}`) || series(1) },
    };
  });
  // Phone: the legend goes under the chart (it covered the axis names), long dates
  // become day-month, and labels that would still touch are left out.
  const narrow = el.clientWidth < 520;
  mount(el, {
    grid: { left: 8, right: 8, top: 28, bottom: narrow ? 56 : 40, containLabel: true },
    legend: { type: "scroll", bottom: 0, textStyle: { color: token("ink-2") }, itemWidth: 10, itemHeight: 10 },
    xAxis: {
      type: "category",
      data: labels,
      axisLabel: { color: muted, hideOverlap: true, formatter: (v) => (narrow ? dayMonth(v) : shortDate(v)) },
      axisLine: { lineStyle: { color: token("axis") } },
      axisTick: { show: false },
    },
    yAxis: [
      { type: "value", name: t("hours"), nameTextStyle: { color: muted, align: "left" }, axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } } },
      { type: "value", name: "%", max: (v) => Math.max(120, Math.ceil(v.max / 10) * 10), axisLabel: { color: muted }, splitLine: { show: false } },
    ],
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) => {
        const w = shown[items[0].dataIndex];
        return `<b>${esc(shortDate(w.start))}</b><br>${esc(t("Planned"))} ${esc(isNum(w.goal_s) ? hoursMinutes(w.goal_s) : "–")} · ${esc(t("Done"))} ${esc(hoursMinutes(w.done_s || 0))}<br>${esc(t("Of plan"))} ${esc(pct(w.done_pct))}`;
      },
    },
    series: [
      {
        name: t("Planned"),
        type: "bar",
        barGap: "-100%",
        barMaxWidth: 26,
        data: shown.map((w) => (isNum(w.goal_s) ? Math.round((w.goal_s / 3600) * 100) / 100 : null)),
        itemStyle: { color: "transparent", borderColor: token("ink-2"), borderType: "dashed", borderWidth: 1 },
        z: 1,
      },
      ...sportSeries,
      {
        name: t("Of plan"),
        type: "line",
        yAxisIndex: 1,
        data: shown.map((w) => w.done_pct),
        showSymbol: true,
        symbol: "diamond",
        connectNulls: false,
        lineStyle: { width: 2, color: series(3) },
        itemStyle: { color: series(3) },
      },
    ],
  });
}

// Races with countdown and projected race-day form ----------------------------------------

function racesHtml(projection, races) {
  const projected = Object.fromEntries((projection?.races || []).map((r) => [`${r.date}|${r.title}`, r]));
  const rows = races.map((race) => {
    const days = Number(race.days_away);
    const countdown = days === 0 ? t("Today") : days === 1 ? t("{n} day to go", { n: number(days) }) : t("{n} days to go", { n: number(days) });
    const letter = race.priority ? `<span class="chip race plan-priority">${esc(t("{letter} race", { letter: race.priority }))}</span>` : "";
    const p = projected[`${race.date}|${race.title}`];
    const form = p && isNum(p.tsb)
      ? `<div class="sub">${esc(t("Projected race-day form: {zone} (fitness {ctl}, form {tsb})", { zone: zoneText(p.form_zone), ctl: number(p.ctl), tsb: `${p.tsb > 0 ? "+" : ""}${number(p.tsb)}` }))}</div>`
      : "";
    return `<li><div class="race-days">${esc(countdown)}</div>
      <div><b>${esc(race.title || t("Race"))}</b> ${letter}<div class="sub">${esc(longDate(race.date))}</div>${form}</div></li>`;
  }).join("");
  const note = rows && projection?.races?.some((r) => isNum(r.tsb))
    ? `<p class="sub">${esc(t("Race-day form is a projection from the plan as it stands now."))}</p>`
    : "";
  return `<section class="card" aria-labelledby="plan-races-title"><h2 id="plan-races-title">${t("Races")}</h2>
    ${rows ? `<ul class="plan-races">${rows}</ul>${note}` : `<p class="empty">${t("No upcoming races.")}</p>`}</section>`;
}

// Remaining phases ----------------------------------------------------------------------

function phasesHtml(phases, weeks, today) {
  const ahead = phases.filter((p) => p.end >= today);
  if (!ahead.length) return "";
  const rows = ahead.map((p) => {
    const inPhase = weeks.filter((w) => w.start >= p.start && w.end <= p.end);
    const hours = inPhase.reduce((sum, w) => sum + (w.time_s || 0), 0);
    const now = p.start <= today && today <= p.end ? ` <span class="chip planned">${t("now")}</span>` : "";
    const facts = [`${dayMonth(p.start)} – ${dayMonth(p.end)}`, inPhase.length === 1 ? t("{n} week", { n: 1 }) : t("{n} weeks", { n: number(inPhase.length) })];
    if (hours) facts.push(t("{total} planned", { total: hoursMinutes(hours) }));
    return `<li><b>${esc(p.phase || t("Other"))}</b>${now}<div class="sub">${esc(facts.join(" · "))}</div></li>`;
  }).join("");
  return `<section class="card" aria-labelledby="plan-phases-title"><h2 id="plan-phases-title">${t("Phases still to come")}</h2>
    <ul class="trends">${rows}</ul></section>`;
}

// Next nine days ------------------------------------------------------------------------

function upcomingHtml(upcoming) {
  const items = upcoming?.items || [];
  const outside = upcoming?.not_in_plan || [];
  const byDay = new Map();
  for (const item of items) {
    if (!byDay.has(item.date)) byDay.set(item.date, []);
    byDay.get(item.date).push(item);
  }
  const days = [...byDay.entries()].map(([date, rows]) => `<li class="plan-day">
      <h3>${esc(longDate(date).replace(/\s\d{4}$/, ""))}</h3>
      ${rows.map(sessionHtml).join("")}
    </li>`).join("");
  const others = outside.length
    ? `<h3>${t("Only on your Garmin calendar")}</h3><p class="sub">${t("Your plan comes from intervals.icu. The workouts below are on your Garmin calendar but not in intervals.icu, so they do not count in planned versus done. If one belongs to your plan, add it in intervals.icu.")}</p><ul class="trends">${outside.map((item) => `<li>${esc(shortDate(item.date))} · ${withSportIcon(item.sport, esc(item.title || sportName(item.sport)))}</li>`).join("")}</ul>`
    : "";
  return `<section class="card" aria-labelledby="plan-next-title">
    <h2 id="plan-next-title">${t("Next 9 days")}</h2>
    ${days ? `<ol class="plan-days">${days}</ol>` : `<p class="empty">${t("Nothing planned in the next 9 days.")}</p>`}
    ${others}
  </section>`;
}

function sessionHtml(item) {
  const [tone, label] = STATUS[item.status] || ["", ""];
  const facts = [sportName(item.sport)];
  if (isNum(item.duration_s)) facts.push(hoursMinutes(item.duration_s));
  if (isNum(item.distance_m)) facts.push(km(item.distance_m));
  const chip = label && item.status !== "upcoming" ? ` <span class="chip ${tone}">${esc(t(label))}</span>` : "";
  const text = item.description
    ? `<details class="collapsible"><summary>${t("Workout details")}</summary>${workoutHtml(item.description)}</details>`
    : "";
  return `<div class="plan-session" data-sport="${sportKey(item.sport)}"><p><b><span class="sport-icon">${sportIcon(sportKey(item.sport))}</span>${esc(item.title || sportName(item.sport))}</b>${chip}</p>
    <p class="sub">${esc(facts.join(" · "))}</p>${text}</div>`;
}
