// Shared rendering for morning decisions and evening reviews (Coach, Dashboard, Day).
import { earlierAdviceStatus, savedAdviceRole } from "./coaching_presentation.js";
import { esc, longDate, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";

export const DAILY_LABELS = {
  morning: N("Morning decision"),
  evening: N("Evening review"),
  weekly: N("Week review"),
  block: N("Block review"),
};
export const DAILY_WORKING = {
  yes: N("This plan is working."),
  partly: N("This plan is only partly working."),
  no: N("This plan is not working."),
};
export const DAILY_BLOCK = {
  improved: N("The test improved."),
  held: N("The test held steady."),
  short: N("The test came up short."),
  single: N("There is a test, but nothing earlier in this block to compare it with."),
  no_test: N("There is no test to compare yet."),
};
export const DAILY_DECISIONS = {
  keep_plan: N("Keep the planned session"),
  take_it_easier: N("Take it easier"),
  rest: N("Rest today"),
  free_choice: N("Free choice today"),
};
export const DAILY_STATES = {
  missing: N("No review yet today"),
  pending: N("Preparing…"),
  running: N("Preparing…"),
  generated: N("Almost ready"),
  waiting_inputs: N("Waiting for new data"),
  ready: N("Up to date"),
  stale: N("New data came in. This review is out of date."),
  failed: N("The review failed"),
  interrupted: N("The request was interrupted. Try again."),
  paused: N("Automatic reviews are off"),
  superseded: N("New data came in. Request a new review."),
  no_coach: N("No advice from your AI coach for today."),
};
export const COACH_NEEDS_REVISION = N(
  "Something changed since this advice; your AI coach should refresh it.",
);
export const DAILY_RETRY = new Set(["failed", "interrupted", "paused"]);
export const DAILY_REQUESTABLE = new Set([
  "missing",
  "stale",
  "superseded",
  "failed",
  "interrupted",
  "paused",
]);

export function hasSavedAdvice(entry) {
  return Boolean(entry?.advice);
}

export function archiveStatusHtml(display) {
  if (!display?.archive) return "";
  return `<p class="sub archive-label">${esc(
    t("Archive, advice for {date}", { date: longDate(display.target_date) }),
  )}</p>`;
}

export function statusText(kind, entry) {
  if (entry?.state === "no_coach") return t(DAILY_STATES.no_coach);
  if (entry?.reason === "no_block") return t("No training block to review yet.");
  if (entry?.reason === "not_yet") {
    return t("This training block is still running. The review follows when it ends.");
  }
  if (kind === "weekly" && entry?.state === "missing") return t("No review for this week yet");
  if (kind === "block" && entry?.state === "missing") return t("No block review yet");
  return t(DAILY_STATES[entry?.state] || entry?.state || "");
}

function safetyLineHtml(text) {
  if (!text) return "";
  return `<p class="safety-line">${esc(t(text))}</p>`;
}

function adviceBodyHtml(kind, entry) {
  const display = entry.display;
  const advice = entry.advice?.interpretation;
  const safetyText = display?.mode === "safety" && display.safety_text ? display.safety_text : "";
  const line = safetyLineHtml(safetyText);
  if (!advice?.summary) return line;
  const translated = safetyText ? t(safetyText) : "";
  const coachIsSafety = advice.summary === safetyText || advice.summary === translated;
  const working = advice.working ? DAILY_WORKING[advice.working] : "";
  const block = advice.block_result ? DAILY_BLOCK[advice.block_result] : "";
  const coach = `<p class="assessment-summary">${esc(advice.summary)}</p>
    ${kind === "evening" && advice.tomorrow ? `<p>${esc(advice.tomorrow)}</p>` : ""}
    ${kind === "weekly" && advice.next_week ? `<p>${esc(advice.next_week)}</p>` : ""}
    ${working ? `<p>${esc(t(working))}</p>` : ""}
    ${block ? `<p>${esc(t(block))}</p>` : ""}
    ${advice.caveat ? `<p class="sub">${esc(advice.caveat)}</p>` : ""}
    ${advice.question ? `<p class="sub">${esc(advice.question)}</p>` : ""}`;
  if (!safetyText || coachIsSafety) return line || coach;
  if (entry.safety?.level === "refuse") {
    return `${line}<details class="coach-note"><summary>${t("Coach's note")}</summary>${coach}</details>`;
  }
  return `${line}${coach}`;
}

function freshnessHtml(entry) {
  if (entry.needs_revision) {
    return `<p class="sub">${esc(t(COACH_NEEDS_REVISION))}</p>`;
  }
  if (entry.state === "stale") {
    return `<p class="sub">${esc(t(DAILY_STATES.stale))}</p>`;
  }
  if (entry.advice && !entry.advice.current) {
    const msg = entry.display?.archive
      ? "Made before later data came in."
      : "New data came in; this is updating.";
    return `<p class="sub">${esc(t(msg))}</p>`;
  }
  return "";
}

function detailsHtml(kind, entry, key = kind) {
  if (entry.display?.mode === "safety") return "";
  const advice = entry.advice?.interpretation;
  if (!advice) return "";
  const period = entry.period?.start && entry.period?.end
    ? kind === "block"
      ? `<p>${esc(t("Training block from {start} to {end}.", { start: longDate(entry.period.start), end: longDate(entry.period.end) }))}</p>`
      : `<p>${esc(t("Week of {start} to {end}.", { start: longDate(entry.period.start), end: longDate(entry.period.end) }))}</p>`
    : "";
  return `<details data-refresh-key="daily-details-${key}"><summary>${t("Details")}</summary>
    ${kind === "morning" && advice.decision ? `<p>${t("Decision")}: ${t(DAILY_DECISIONS[advice.decision] || advice.decision)}</p>` : ""}
    ${period}
    <p>${t("Saved")}: ${esc(userDateTime(entry.advice.completed_at))}</p>
  </details>`;
}

function reviewBody(kind, entry, key = kind, { earlierInline = false } = {}) {
  if (entry?.state === "no_coach") {
    const safety = safetyLineHtml(
      entry.display?.mode === "safety" ? entry.display.safety_text : "",
    );
    return `${safety}<p role="status">${esc(statusText(kind, entry))}</p>`;
  }
  const role = savedAdviceRole(entry);
  if (role === "earlier") {
    const status = earlierAdviceStatus(entry);
    const text = status === "preparing"
      ? t("New advice is still being prepared.")
      : status === "failed"
        ? t(DAILY_STATES[entry.state] || entry.state)
        : t("This advice was saved before newer data arrived.");
    // The dashboard puts the whole earlier block behind one disclosure (proposal).
    if (earlierInline) return `<p class="sub" role="status">${esc(text)}</p>${adviceBodyHtml(kind, entry)}`;
    return `<p role="status">${esc(text)}</p>
      <details data-refresh-key="daily-earlier-${key}"><summary>${t("Earlier advice")}</summary>
        ${adviceBodyHtml(kind, entry)}
      </details>`;
  }
  if (role === "status") {
    return `<p role="status">${esc(statusText(kind, entry))}</p>`;
  }
  const failedNote = role === "current" && (entry.state === "failed" || entry.state === "interrupted")
    ? `<p class="sub" role="status">${esc(t(DAILY_STATES[entry.state]))}</p>`
    : "";
  return `${archiveStatusHtml(entry.display)}${adviceBodyHtml(kind, entry)}${failedNote}${freshnessHtml(entry)}${detailsHtml(kind, entry, key)}`;
}

/** One morning or evening block; options control headings, collapse, and Coach actions. */
export function dailyKindHtml(kind, entry, options = {}) {
  const {
    heading = t(DAILY_LABELS[kind]),
    headingId = `daily-${kind}`,
    showRequestButtons = false,
    showRefresh = false,
    wrapSection = true,
    extraClass = "",
    hideHeading = false,
    earlierInline = false,
  } = options;
  const canRequest = showRequestButtons && DAILY_REQUESTABLE.has(entry.state);
  const retry = DAILY_RETRY.has(entry.state);
  const requestLabels = {
    morning: t("Get today's decision"),
    evening: t("Get today's review"),
    weekly: t("Get this week's review"),
    block: t("Review this block"),
  };
  const requestLabel = requestLabels[kind] || t("Get today's review");
  const following = entry.active_until
    ? `<p class="sub">${esc(t("The next training block runs until {date}.", { date: longDate(entry.active_until) }))}</p>`
    : "";
  const body = reviewBody(kind, entry, headingId, { earlierInline });
  const actions =
    showRequestButtons || showRefresh
      ? `<div class="settings-save">
      ${canRequest ? `<button class="btn" data-daily-request="${kind}">${retry ? t("Try again") : requestLabel}</button>` : ""}
      ${showRefresh ? `<button class="btn" data-daily-refresh="${kind}">${t("Refresh")}</button>` : ""}
    </div>
    <p data-daily-error="${kind}" role="alert"></p>`
      : "";
  const inner = `${hideHeading ? "" : `<h3 id="${headingId}">${esc(heading)}</h3>`}${body}${following}${actions}`;
  if (!wrapSection) return `<div class="daily-review ${extraClass}">${inner}</div>`;
  return `<section class="daily-review ${extraClass}" aria-labelledby="${headingId}">${inner}</section>`;
}

/** Which coaching to show on the dashboard (no AI buttons here). */
export function dashboardCoachingPlan(today, yesterday) {
  if (hasSavedAdvice(today?.evening)) {
    const secondary = hasSavedAdvice(today?.morning)
      ? { kind: "morning", entry: today.morning, heading: t("This morning's decision"), collapsed: true }
      : null;
    return {
      primary: { kind: "evening", entry: today.evening, heading: t("Evening review") },
      secondary,
    };
  }
  if (hasSavedAdvice(today?.morning)) {
    return { primary: { kind: "morning", entry: today.morning, heading: t(DAILY_LABELS.morning) } };
  }
  if (hasSavedAdvice(yesterday?.evening)) {
    return {
      primary: {
        kind: "evening",
        entry: yesterday.evening,
        heading: t("Yesterday evening's review"),
      },
    };
  }
  const safetyText = unsavedSafetyText(today);
  return safetyText ? { empty: true, safetyText } : { empty: true };
}

function unsavedSafetyText(today) {
  for (const kind of ["morning", "evening"]) {
    const entry = today?.[kind];
    if (!entry || hasSavedAdvice(entry)) continue;
    if (entry.display?.mode === "safety" && entry.display.safety_text) return entry.display.safety_text;
  }
  return "";
}

export function dashboardCoachingHtml(plan) {
  if (plan.empty) {
    if (plan.safetyText) return `<p class="safety-line">${esc(t(plan.safetyText))}</p>`;
    return `<p class="empty">${t("No coaching saved for today yet.")} <a href="#/coach">${t("Coach")}</a></p>`;
  }
  // An outdated coach message is context, not today's advice: one closed disclosure whose
  // summary says what it is; the "saved before newer data" note sits inside it.
  const earlier = savedAdviceRole(plan.primary.entry) === "earlier";
  const primary = earlier
    ? `<details class="daily-review-collapsed" data-refresh-key="dash-coaching-earlier">
        <summary>${esc(t("Earlier advice"))}: ${esc(plan.primary.heading)}</summary>
        ${dailyKindHtml(plan.primary.kind, plan.primary.entry, {
          headingId: "dash-coaching-primary",
          wrapSection: false,
          hideHeading: true,
          earlierInline: true,
        })}
      </details>`
    : dailyKindHtml(plan.primary.kind, plan.primary.entry, {
      heading: plan.primary.heading,
      headingId: "dash-coaching-primary",
      wrapSection: false,
    });
  const secondary = plan.secondary
    ? `<details class="daily-review-collapsed">
        <summary>${esc(plan.secondary.heading)}</summary>
        ${dailyKindHtml(plan.secondary.kind, plan.secondary.entry, {
          headingId: "dash-coaching-secondary",
          wrapSection: false,
          hideHeading: true,
        })}
      </details>`
    : "";
  return `${primary}${secondary}`;
}

export function dayCoachingHtml(data) {
  const emptyMsg = {
    morning: t("No morning decision saved for this day."),
    evening: t("No evening review saved for this day."),
  };
  const blocks = ["morning", "evening"].map((kind) => {
    const entry = data[kind];
    const headingId = `day-coaching-${kind}`;
    if (!hasSavedAdvice(entry)) {
      return `<div class="daily-review"><h3 id="${headingId}">${t(DAILY_LABELS[kind])}</h3><p>${emptyMsg[kind]}</p></div>`;
    }
    return dailyKindHtml(kind, entry, { heading: t(DAILY_LABELS[kind]), headingId });
  });
  return `<h2 id="day-coaching">${t("Coaching")}</h2>${blocks.join("")}`;
}
