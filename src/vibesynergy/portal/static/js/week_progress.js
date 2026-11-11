// One week against the plan, per sport: the weekly goal, what is done and what is
// still planned, and per workout planned against actual. The start page shows this week;
// the planning page can use the same pieces for any week. Data: query/week_progress.py.
import { effortLabel } from "./effort.js";
import { esc, hoursMinutes, isNum, km, longDate, number, sportName, weekday } from "./format.js";
import { N, t } from "./i18n.js";
import { complianceChip } from "./compliance.js";
import { sportIcon, sportKey } from "./symbols.js";
import { workoutHtml } from "./workout_text.js";

// Sport per family, so a row reads "Swimming", not "Lap swimming".
const FAMILY_SPORT = { swim: "swimming", ride: "cycling", run: "running", strength: "strength_training", walk: "walking" };
// Status: the chip's tone (colour and icon) and a word, never colour alone.
const STATUS = {
  done: ["success", N("done")],
  changed: ["warning", N("changed")],
  missed: ["warning", N("missed")],
  optional: ["planned", N("option")],
  uncertain: ["info", N("match uncertain")],
  today: ["planned", N("today")],
  upcoming: ["planned", N("upcoming")],
  extra: ["info", N("not in plan")],
};
// Done plus still planned below this share of the target: the sport falls short this week.
const SHORT = 0.85;

function familyName(family) {
  return family ? sportName(FAMILY_SPORT[family] || family) : t("Other");
}

function count(n, one, many) {
  return n === 1 ? t(one, { n: number(n) }) : t(many, { n: number(n) });
}

function chip(status) {
  const [tone, word] = STATUS[status] || ["", status];
  return `<span class="chip ${tone}">${esc(t(word))}</span>`;
}

// Advice first: what the week so far means and what to do now.
export function weekSummary(progress) {
  const counts = progress.counts || {};
  const missed = counts.missed || 0;
  const changed = counts.changed || 0;
  const left = (progress.sports || []).reduce((sum, sport) => sum + (sport.remaining || []).length, 0);
  const short = (progress.sports || [])
    .filter((sport) => isNum(sport.goal_s) && sport.goal_s > 0 && (sport.done_s + sport.remaining_s) < SHORT * sport.goal_s)
    .map((sport) => familyName(sport.family));
  if (progress.days_left === 0 && !left) {
    if (missed) return count(missed, N("{n} session missed this week. Let it go and start the new week fresh."), N("{n} sessions missed this week. Let them go and start the new week fresh."));
    if (short.length) return t("The planned workouts are done. Below this week's goal: {sports}. No need to make up for it.", { sports: short.join(", ") });
    if (changed) return t("A good week: everything planned is done, some of it differently.");
    return t("A good week: everything planned is done.");
  }
  if (missed) return count(missed, N("{n} session missed. Don't make it up; carry on with the plan."), N("{n} sessions missed. Don't make them up; carry on with the plan."));
  if (short.length) return t("On track, but below this week's goal: {sports}. No need to make up for it.", { sports: short.join(", ") });
  if (left) {
    return t("On track. Still to do: {sessions}, {time}.", {
      sessions: count(left, N("{n} session"), N("{n} sessions")),
      time: hoursMinutes(progress.totals?.remaining_s || 0),
    });
  }
  return t("On track: everything planned so far is done.");
}

function contextHtml(progress) {
  const bits = [progress.season?.name, progress.note?.title].filter(Boolean);
  const context = bits.length ? `<p class="wp-context">${esc(bits.join(" · "))}</p>` : "";
  const focus = progress.note?.focus ? `<p class="sub wp-focus">${esc(t("Focus: {text}", { text: progress.note.focus }))}</p>` : "";
  return context + focus;
}

function barHtml(sport) {
  const done = sport.done_s || 0;
  const goal = sport.goal_s || 0;
  const value = goal
    ? `${esc(hoursMinutes(done))} <span class="muted">${esc(t("of {total}", { total: hoursMinutes(goal) }))}</span>`
    : esc(hoursMinutes(done));
  if (!goal) return `<span class="wp-bar" aria-hidden="true"></span><span class="zvalue">${value}</span>`;
  const pct = Math.min(100, (done / goal) * 100);
  const label = t("{done} of {total} done", { done: hoursMinutes(done), total: hoursMinutes(goal) });
  return `<span class="ztrack wp-bar" role="img" aria-label="${esc(label)}"><span style="width:${pct.toFixed(1)}%"></span></span>
    <span class="zvalue">${value}</span>`;
}

function leftLine(sport) {
  const left = (sport.remaining || []).length;
  if (left) {
    return t("{sessions} still planned, {time}", {
      sessions: count(left, N("{n} session"), N("{n} sessions")),
      time: hoursMinutes(sport.remaining_s || 0),
    });
  }
  const goal = sport.goal_s || 0;
  if (goal && (sport.done_s || 0) >= goal) return t("Goal reached");
  if (goal) return t("{time} short of the goal", { time: hoursMinutes(goal - (sport.done_s || 0)) });
  if (sport.planned_count) return t("Nothing left on the plan this week");
  return t("Not in this week's plan");
}

function actualText(session) {
  const plan = session.planned || {};
  const done = session.actual || {};
  const parts = [];
  if (session.status === "extra") {
    if (isNum(done.duration_s)) parts.push(hoursMinutes(done.duration_s));
  } else if (session.actual) {
    if (isNum(plan.duration_s) || isNum(done.duration_s)) {
      parts.push(t("{planned} planned, {done} done", { planned: hoursMinutes(plan.duration_s), done: hoursMinutes(done.duration_s) }));
    }
  } else if (isNum(plan.duration_s)) {
    parts.push(t("{time} planned", { time: hoursMinutes(plan.duration_s) }));
  }
  if (isNum(done.distance_m) && done.distance_m > 0) parts.push(km(done.distance_m));
  if (isNum(done.load)) {
    parts.push(isNum(plan.load) ? t("load {done} (plan {planned})", { done: number(done.load), planned: number(plan.load) }) : t("load {done}", { done: number(done.load) }));
  }
  if (isNum(session.rpe)) parts.push(t("RPE {n} ({label})", { n: number(session.rpe), label: effortLabel("rpe", session.rpe) }));
  if (isNum(session.feel)) parts.push(t("feel: {label}", { label: effortLabel("feel", session.feel) }));
  if (done.coaching) parts.push(t("training you gave"));
  return parts.join(" · ");
}

function sessionHtml(session, { linkActivities }) {
  const day = weekday(session.date);
  const title = esc(session.title || t("Activity"));
  const id = session.actual?.id;
  const name = id && linkActivities ? `<a href="#/activity/${esc(encodeURIComponent(id))}">${title}</a>` : title;
  const text = actualText(session);
  // What the planned workout is, folded open on demand (8-11).
  const details = session.description && session.in_plan
    ? `<details class="collapsible wp-details"><summary>${esc(t("Workout details"))}</summary>${workoutHtml(session.description)}</details>`
    : "";
  const fit = session.in_plan && session.actual ? ` ${complianceChip(session.compliance, session.compliance_source)}` : "";
  return `<li class="wp-session">${chip(session.status)}${fit} <span><span class="wp-day" title="${esc(longDate(session.date))}">${esc(day)}</span> ${name}${text ? `<span class="sub wp-actual">${esc(text)}</span>` : ""}${details}</span></li>`;
}

function sportHtml(sport, sessions, options) {
  const mine = sessions.filter((s) => s.family === sport.family);
  const past = mine.filter((s) => !s.remaining);
  const ahead = mine.filter((s) => s.remaining);
  const done = past.length
    ? `<h3>${t("So far")}</h3><ul class="wp-sessions">${past.map((s) => sessionHtml(s, options)).join("")}</ul>`
    : "";
  const planned = ahead.length
    ? `<h3>${t("Still planned")}</h3><ul class="wp-sessions">${ahead.map((s) => sessionHtml(s, options)).join("")}</ul>`
    : "";
  const body = done || planned ? `${done}${planned}` : `<p class="sub">${t("Nothing this week yet.")}</p>`;
  return `<li class="wp-sport" data-family="${esc(sport.family || "other")}" data-sport="${sportKey(sport.family)}">
    <details>
      <summary>
        <span class="wp-name"><span class="sport-icon">${sportIcon(sport.family)}</span>${esc(familyName(sport.family))}</span>
        ${barHtml(sport)}
        <span class="sub wp-left">${esc(leftLine(sport))}</span>
      </summary>
      <div class="wp-detail">${body}</div>
    </details>
  </li>`;
}

function nextWeekHtml(progress) {
  const next = progress.next_week;
  if (!next || progress.days_left > 1) return "";
  const goals = (next.targets || []).map((goal) => `${familyName(goal.family)} ${hoursMinutes(goal.time_s)}`).join(" · ");
  const text = [next.title, goals].filter(Boolean).join(" — ");
  return text ? `<p class="sub wp-next"><b>${t("Next week")}:</b> ${esc(text)}</p>` : "";
}

function detailsHtml(progress) {
  const rows = (progress.sports || []).map((sport) => {
    const basis = sport.basis === "target"
      ? t("goal: the plan's weekly time ({title})", { title: sport.target_title || hoursMinutes(sport.target_s) })
      : sport.basis === "planned"
        ? t("goal: the planned workouts added up")
        : t("no goal this week");
    const bits = [basis];
    if (isNum(sport.planned_s)) bits.push(t("{workouts} planned, {time}", { workouts: count(sport.planned_count, N("{n} workout"), N("{n} workouts")), time: hoursMinutes(sport.planned_s) }));
    if (isNum(sport.done_distance_m)) bits.push(km(sport.done_distance_m));
    if (isNum(sport.done_load)) bits.push(t("load {done}", { done: number(sport.done_load) }));
    return `<li><b>${esc(familyName(sport.family))}</b>: ${esc(bits.join(" · "))}</li>`;
  }).join("");
  // The whole week note, when it says more than the focus line above.
  const text = progress.note?.text;
  const note = text && text !== progress.note?.focus ? `<p class="plan-note wp-note">${esc(text)}</p>` : "";
  return `<details class="wp-more">
    <summary>${t("Details")}</summary>
    ${note}
    <ul class="wp-basis">${rows}</ul>
    <p class="sub">${t("A workout counts as done when an activity of that sport took place that day, or when intervals.icu paired them; then intervals.icu's score is shown. Changed means more than a quarter longer or shorter, or another intensity.")}</p>
  </details>`;
}

// The week card's body (after its heading).
export function weekProgressHtml(progress, { linkActivities = true } = {}) {
  const sessions = progress.sessions || [];
  const sports = (progress.sports || []).map((sport) => sportHtml(sport, sessions, { linkActivities })).join("");
  return `${contextHtml(progress)}
    <p class="wp-summary dash-keep-line">${esc(weekSummary(progress))}</p>
    <ul class="wp-sports">${sports}</ul>
    ${nextWeekHtml(progress)}
    ${detailsHtml(progress)}`;
}
