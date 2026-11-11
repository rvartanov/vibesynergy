// The Coach page: Advice (coach notes, the active profile, daily and weekly reviews)
// and the tabs Knowledge, Methods & rules, Memory, Change log and Settings.
import { get, post } from "../api.js";
import { captureViewState, restoreViewState } from "../refresh_state.js";
import { esc, longDate, userDateTime } from "../format.js";
import { coachMemory, coachSettings } from "../coach_settings.js";
import { coachShell, coachTabsHtml } from "../coach_tabs.js";
import { coachRunnerSettings } from "../coach_runner_settings.js";
import { agentLinkSettings } from "../agent_links.js";
import { bindReplies, repliesHtml } from "../coach_replies.js";
import {
  dailyKindHtml,
} from "../daily_review.js";
import { N, t } from "../i18n.js";
import { entryTone, sessionsHtml, splitCurrent } from "../coach_message.js";
import { isoToday } from "../period_picker.js";

const COACH_TIMEOUT_MS = 15000;

function sectionLoading() {
  return `<p class="sub" role="status" aria-busy="true">${esc(t("Loading…"))}</p>`;
}

function sectionErrorCard(sectionId) {
  return `<div class="notice error" role="alert">
    <p>${esc(t("This part could not load right now."))}</p>
    <button type="button" class="btn" data-section-retry="${esc(sectionId)}">${t("Try again")}</button>
  </div>`;
}

async function loadSection(el, stale, sectionId, loader, beforeRetry) {
  if (!el?.isConnected) return;
  el.innerHTML = sectionLoading();
  try {
    await loader(el);
  } catch {
    if (!stale() && el.isConnected) {
      el.innerHTML = sectionErrorCard(sectionId);
      el.querySelector("[data-section-retry]")?.addEventListener("click", () => {
        beforeRetry?.();
        loadSection(el, stale, sectionId, loader, beforeRetry);
      });
    }
  }
}
const STYLE_LABELS = {
  language: { auto: N("Follow the question"), en: "English", nl: "Nederlands" },
  detail: { concise: N("Concise"), standard: N("Standard"), detailed: N("Detailed") },
  tone: { direct: N("Direct"), supportive: N("Supportive") },
};

// What the coach reads about you, read-only here; it changes on the Settings tab.
async function profile(el, stale) {
  const { data } = await get("coach", {}, { timeoutMs: COACH_TIMEOUT_MS });
  if (stale() || !el.isConnected) return;
  const p = { ...(data.catalogue?.policy_defaults || {}), ...(data.policy?.active?.payload || {}) };
  const remembered = (data.context?.active?.payload?.constraints || []).filter((entry) => entry.active !== false);
  const style = ["language", "detail", "tone"]
    .map((key) => STYLE_LABELS[key][p[key]])
    .filter(Boolean)
    .map((label) => t(label))
    .join(" · ");
  el.innerHTML = `${style ? `<p>${esc(style)}</p>` : ""}
    ${p.instructions ? `<h3>${t("Notes for your coach")}</h3><p class="coach-notes">${esc(p.instructions)}</p>` : ""}
    ${remembered.length ? `<h3>${t("What to remember")}</h3><ul>${remembered.map((entry) => `<li>${esc(entry.text)}</li>`).join("")}</ul>` : ""}
    <p><a href="#/coach/memory">${t("Change this under Memory")}</a></p>`;
}

// Settings tab, everything about the coach in one place: its own settings, running the
// coach on the API (/#133), and the links (webhooks) that start a coach routine.
async function fillSettings(el, stale) {
  el.innerHTML = `<div data-coach-settings><p class="sub" role="status" aria-busy="true">${esc(t("Loading…"))}</p></div>
    <section class="card" data-coach-runner aria-labelledby="coach-runner-title"><p class="sub" role="status" aria-busy="true">${esc(t("Loading…"))}</p></section>
    <section class="card" data-sync-agents aria-labelledby="coach-agents-title">
      <h2 id="coach-agents-title">${esc(t("AI coach after a workout"))}</h2>
      <p class="sub">${esc(t("VibeSynergy tells your AI coach when you make an activity public on Strava, so its insight is there right away. It only sends which activity it is; the AI reads the rest itself."))}</p>
      <div data-agent-links></div>
    </section>`;
  const settingsEl = el.querySelector("[data-coach-settings]");
  const runnerEl = el.querySelector("[data-coach-runner]");
  const agentsEl = el.querySelector("[data-agent-links]");
  const failed = (target) => {
    if (!stale() && target.isConnected) {
      target.innerHTML = `<div class="notice error" role="alert"><p>${esc(t("This part could not load right now."))}</p></div>`;
    }
  };
  await Promise.all([
    coachSettings(settingsEl, stale).catch(() => failed(settingsEl)),
    coachRunnerSettings(runnerEl, stale).catch(() => failed(runnerEl)),
    agentLinkSettings(agentsEl, { stale }).catch(() => failed(agentsEl)),
  ]);
}

// One report with its replies: where the report mail links to.
function fillReport(id) {
  return async (el, stale) => {
    const { data } = await get(`coach/report/${encodeURIComponent(id)}`, {}, { timeoutMs: COACH_TIMEOUT_MS });
    if (stale() || !el.isConnected) return;
    const entry = { ...data.entry, replies: data.replies };
    el.innerHTML = `<section class="card coach-section" aria-label="${esc(t("Report"))}"><div class="coach-feed">${coachEntryHtml(entry, {})}</div>
      <p><a href="#/coach">${esc(t("All messages from your coach"))}</a></p></section>`;
    bindReplies(el, () => entry);
  };
}

const SCHEDULES = { daily: N("Every day"), weekly: N("Every Sunday"), per_activity: N("After each activity") };

async function fillTasks(el, stale) {
  const { data } = await get("coach/tasks", {}, { timeoutMs: COACH_TIMEOUT_MS });
  if (stale() || !el.isConnected) return;
  const rows = data.tasks.map((task) => {
    const when = t(SCHEDULES[task.schedule] || task.schedule) + (task.due ? ` · ${t("before {time}", { time: task.due })}` : "");
    const last = `${t("Last done")}: ${task.last_run ? userDateTime(task.last_run) : t("Not yet")}`;
    const missing = task.missing.length
      ? `<p class="safety-line">${esc(t("Skipped: {days}", { days: task.missing.map((d) => longDate(d)).join(", ") }))}</p>`
      : "";
    return `<li><h3>${esc(t(task.title))}</h3><p class="sub">${esc(when)} · ${esc(last)}</p>${missing}</li>`;
  });
  el.innerHTML = `<section class="card coach-section" aria-labelledby="coach-tasks-title"><h2 id="coach-tasks-title">${esc(t("Tasks"))}</h2>
    <ul class="kb-list">${rows.join("")}</ul></section>`;
}

async function renderSection(view, stale, tab, intro, fill, heading) {
  const el = coachShell(view, tab, { intro, heading });
  el.innerHTML = `<p class="sub" role="status" aria-busy="true">${esc(t("Loading…"))}</p>`;
  try {
    await fill(el, stale);
  } catch (error) {
    if (!stale() && el.isConnected) {
      el.innerHTML = `<div class="notice error" role="alert"><p>${esc(t(error.message || "This part could not load right now."))}</p>
        <p><a href="#/coach">${esc(t("All messages from your coach"))}</a></p></div>`;
    }
  }
}

const SUBPAGES = {
  knowledge: async (view, ctx) => (await import("../coach_knowledge.js")).renderKnowledge(view, ctx),
  methods: async (view, ctx) => (await import("../coach_knowledge.js")).renderMethods(view, ctx),
  log: async (view, ctx) => (await import("../coach_log.js")).renderLog(view, ctx),
  memory: (view, { stale }) =>
    renderSection(view, stale, "memory", t("What your coach keeps about you. It reads this before every piece of advice; only you can change it."), coachMemory),
  tasks: (view, { stale }) =>
    renderSection(view, stale, "tasks", t("What your coach does, when, and when it last did it."), fillTasks),
  settings: (view, { stale }) => renderSection(view, stale, "settings", "", fillSettings),
  report: (view, { args, stale }) =>
    args[0] ? renderSection(view, stale, "", "", fillReport(args[0]), t("Report")) : render(view, { stale }),
};

export async function render(view, { args = [], query, meta, stale }) {
  const share = meta?.data?.share || null;
  const sub = SUBPAGES[args[0]];
  if (sub) return sub(view, { args: args.slice(1), query, stale, share });
  document.title = `${t("Coach")} · VibeSynergy`;
  view.innerHTML = `<div class="page-head"><h1>${t("Coach")}</h1></div>
    ${coachTabsHtml("")}
    <section class="card coach-section" aria-labelledby="coach-feed-title"><h2 id="coach-feed-title">${t("Messages from your coach")}</h2>
      <div data-coach-feed></div></section>
    <section class="card coach-section" aria-labelledby="coach-goals"><h2 id="coach-goals">${t("What your coach knows about you")}</h2><div data-profile></div></section>
    <section class="card coach-section" aria-labelledby="coach-rhythm"><h2 id="coach-rhythm">${t("Week and training block")}</h2><div data-rhythm></div></section>`;

  let dailyCoachingPromise = null;
  const dailyCoachingWaiters = new Set();
  const dailyCoaching = () => {
    if (!dailyCoachingPromise) {
      dailyCoachingPromise = get("daily-coaching", {}, { timeoutMs: COACH_TIMEOUT_MS });
      const promise = dailyCoachingPromise;
      for (const waiter of dailyCoachingWaiters) waiter(promise);
    }
    return dailyCoachingPromise;
  };
  const resetDailyCoaching = () => {
    dailyCoachingPromise = null;
  };
  const subscribeDailyCoaching = (waiter) => {
    dailyCoachingWaiters.add(waiter);
    if (dailyCoachingPromise) waiter(dailyCoachingPromise);
    return () => dailyCoachingWaiters.delete(waiter);
  };

  get("coach", {}, { timeoutMs: COACH_TIMEOUT_MS })
    .then(({ data }) => {
      if (stale() || !view.isConnected) return;
      const feedEl = view.querySelector("[data-coach-feed]");
      if (feedEl) {
        loadSection(
          feedEl,
          stale,
          "feed",
          (el) =>
            coachFeed(el, stale, {
              narrativeOn: false,
              dailyCoaching,
              resetDailyCoaching,
              subscribeDailyCoaching,
            }),
          resetDailyCoaching,
        );
      }
      // Today's morning and evening advice show once, at the top of the feed; this
      // section keeps the week and block reviews.
      loadSection(
        view.querySelector("[data-rhythm]"),
        stale,
        "daily",
        (el) => daily(el, stale, { dailyCoaching, resetDailyCoaching }),
        resetDailyCoaching,
      );
    })
    .catch(() => {
      loadSection(
        view.querySelector("[data-coach-feed]"),
        stale,
        "feed",
        (el) =>
          coachFeed(el, stale, {
            narrativeOn: false,
            dailyCoaching,
            resetDailyCoaching,
            subscribeDailyCoaching,
          }),
        resetDailyCoaching,
      );
      loadSection(
        view.querySelector("[data-rhythm]"),
        stale,
        "daily",
        (el) => daily(el, stale, { dailyCoaching, resetDailyCoaching }),
        resetDailyCoaching,
      );
    });

  loadSection(view.querySelector("[data-profile]"), stale, "profile", (el) => profile(el, stale));
  const rhythmPlaceholder = view.querySelector("[data-rhythm]");
  if (rhythmPlaceholder) rhythmPlaceholder.innerHTML = sectionLoading();
}

// Today's morning decision and evening review: the advice first, at most one caveat, numbers
// already turned into meaning by the model. A manual request works even when automatic
// reviews are off; a failed/interrupted/paused one needs an explicit retry.
function rhythmHtml(data) {
  const previous = data.weekly_previous
    ? dailyKindHtml("weekly", data.weekly_previous, {
        heading: t("Last week's review"),
        headingId: "daily-weekly-previous",
        showRequestButtons: false,
        showRefresh: false,
      })
    : "";
  return `${previous}${dailyKindHtml("weekly", data.weekly, { showRequestButtons: false, showRefresh: false })}${dailyKindHtml("block", data.block, { showRequestButtons: false, showRefresh: false })}`;
}

// Week and block reviews. Without an active coaching profile, the way to set it up.
async function daily(rhythmEl, stale, { dailyCoaching } = {}) {
  const { data } = await (dailyCoaching ? dailyCoaching() : get("daily-coaching", {}, { timeoutMs: COACH_TIMEOUT_MS }));
  if (stale() || !rhythmEl?.isConnected) return;
  if (!data.profile_active) {
    rhythmEl.innerHTML = `<p>${t("Set up your coaching approach in Settings.")} <a href="#/coach/memory">${t("Set up coach")}</a></p>`;
    return;
  }
  const rhythmState = captureViewState(rhythmEl);
  rhythmEl.innerHTML = rhythmHtml(data);
  restoreViewState(rhythmEl, rhythmState);
}

const COACH_ENTRY_TYPES = {
  activity_assessment: N("Activity review"),
  morning: N("Morning decision"),
  evening: N("Evening review"),
  weekly: N("Week review"),
  block: N("Block review"),
  note: N("Note"),
  plan_change: N("Plan change"),
  threshold_change: N("Threshold change"),
  data_check: N("Data check"),
};
const COACH_AUTHORS = {
  ai_coach: N("AI coach"),
  user: N("You"),
  legacy: N("Coach (before identities)"),
  system: N("System"),
  "client:shared": N("Shared link"),
  "client:coach-runner": N("AI coach (API)"),
};

const COACH_FRESHNESS = {
  current: N("Current"),
  stale: N("Out of date"),
  needs_revision: N("Needs refresh"),
};
const CONFIRM_THRESHOLD = N("Confirm threshold");
const COACH_ADVICE_LABEL = N("AI coach advice");

function coachSummary(entry) {
  const advice = entry.advice_text || (entry.text || "").split("\n")[0] || "";
  const caveat = entry.caveat || (entry.text || "").split("\n")[1] || "";
  return { advice, caveat };
}

function coachDetailsHtml(entry, names) {
  const meta = entry.meta || {};
  const bits = [];
  const author = authorLabel(entry, names);
  if (author) bits.push(`${t("Written by")}: ${esc(author)}`);
  const freshKey = entry.freshness || "current";
  if (freshKey !== "current") bits.push(esc(t(COACH_FRESHNESS[freshKey] || freshKey)));
  if (meta.model_id) bits.push(`${t("Model")}: ${esc(meta.model_id)}`);
  for (const key of ["prompt_version", "context_version", "policy_version", "evidence_version"]) {
    if (meta[key]) bits.push(`${esc(key)}: ${esc(String(meta[key]))}`);
  }
  const refs = meta.evidence_refs;
  if (refs?.length) bits.push(`${t("Evidence")}: ${esc(refs.join(", "))}`);
  if (meta.details) bits.push(esc(meta.details));
  if (entry.advice_mode === "safety" && (meta.coach_text || entry.entry_text)) {
    bits.push(`${t(COACH_ADVICE_LABEL)}: ${esc((meta.coach_text || entry.entry_text).split("\n")[0])}`);
  }
  if (!bits.length) return "";
  return `<details><summary>${t("Details")}</summary><p class="sub">${bits.join("<br>")}</p></details>`;
}

function authorLabel(entry, names) {
  const known = COACH_AUTHORS[entry.author];
  if (known) return t(known);
  if (names && names[entry.author]) return names[entry.author];
  const stored = entry.meta && entry.meta.author_name;
  if (stored) return stored;
  return entry.author || "";
}

function coachEntryHtml(entry, names) {
  const { advice, caveat } = coachSummary(entry);
  const type = t(COACH_ENTRY_TYPES[entry.entry_type] || entry.entry_type);
  const when = entry.at ? userDateTime(entry.at) : "";
  // Only "out of date" is worth a chip; the rest of the bookkeeping sits in Details.
  const stale = entry.freshness === "stale" ? ` <span class="chip warning">${esc(t(COACH_FRESHNESS.stale))}</span>` : "";
  const confirm =
    entry.entry_type === "threshold_change" && entry.meta?.status === "proposed"
      ? `<button class="btn" type="button" data-threshold-confirm="${esc(String(entry.id))}">${t(CONFIRM_THRESHOLD)}</button>`
      : "";
  const safety = entry.safety_line
    ? `<p class="safety-line">${esc(t(entry.safety_line))}</p>`
    : "";
  const coach = `<p>${esc(advice)}</p>${caveat ? `<p class="sub">${esc(caveat)}</p>` : ""}${sessionsHtml(entry.meta?.sessions)}`;
  const body = entry.advice_mode === "safety_override"
    ? `${safety}<details class="coach-note"><summary>${t("Coach's note")}</summary>${coach}</details>`
    : `${safety}${coach}`;
  return `<article class="coach-entry" data-entry-type="${esc(entry.entry_type || "")}">
    <header><span class="chip ${entryTone(entry.entry_type)}">${esc(type)}</span> <span class="sub">${esc(when)}</span>${stale}</header>
    ${body}
    ${confirm}
    ${coachDetailsHtml(entry, names)}
    ${repliesHtml(entry)}
  </article>`;
}

async function coachFeed(
  el,
  stale,
  { narrativeOn = true, dailyCoaching, resetDailyCoaching, subscribeDailyCoaching } = {},
) {
  const reload = () => {
    resetDailyCoaching?.();
    return coachFeed(el, stale, {
      narrativeOn,
      dailyCoaching,
      resetDailyCoaching,
      subscribeDailyCoaching,
    });
  };
  const [feedReply, clientReply] = await Promise.all([
    get("coach/feed", {}, { timeoutMs: COACH_TIMEOUT_MS }),
    get("mcp_clients").catch(() => ({ data: { names: {} } })),
  ]);
  const data = feedReply.data;
  const names = clientReply.data?.names || {};
    if (stale() || !el.isConnected) return;
    const entries = data.entries || [];
    // What is current first; older messages in a collapsed archive with a type filter.
    const { current, archive } = splitCurrent(entries, isoToday());
    const byDay = (list) => {
      const groups = new Map();
      for (const entry of list) {
        const day = entry.for_date || (entry.at ? entry.at.slice(0, 10) : "");
        if (!groups.has(day)) groups.set(day, []);
        groups.get(day).push(entry);
      }
      return [...groups.keys()].sort((a, b) => b.localeCompare(a)).map((day) => `<section class="coach-day"><h3 class="sub">${esc(longDate(day))}</h3>
          ${groups.get(day).map((entry) => coachEntryHtml(entry, names)).join("")}</section>`).join("");
    };
    const types = [...new Set(archive.map((e) => e.entry_type))].filter(Boolean);
    const filter = types.length > 1
      ? `<label class="sub">${esc(t("Show"))} <select class="btn" data-archive-type><option value="">${esc(t("All messages"))}</option>${types.map((type) => `<option value="${esc(type)}">${esc(t(COACH_ENTRY_TYPES[type] || type))}</option>`).join("")}</select></label>`
      : "";
    const feed = entries.length
      ? `${current.length ? byDay(current) : `<p class="sub">${esc(t("Nothing new from your coach today or yesterday."))}</p>`}
        ${archive.length ? `<details class="collapsible coach-archive"><summary>${esc(t("Earlier messages ({n})", { n: archive.length }))}</summary>${filter}<div data-archive-list>${byDay(archive)}</div></details>` : ""}`
      : `<p class="empty">${t("No advice from your AI coach yet for this period.")}</p>`;
    el.innerHTML = `<div class="coach-today" data-coach-today hidden></div><p class="sub" data-coach-status role="status" aria-live="polite"></p>
      <div class="coach-feed">${feed}</div>`;
    el.querySelector("[data-archive-type]")?.addEventListener("change", (event) => {
      const wanted = event.target.value;
      for (const article of el.querySelectorAll("[data-archive-list] .coach-entry")) {
        article.hidden = Boolean(wanted) && article.dataset.entryType !== wanted;
      }
      for (const day of el.querySelectorAll("[data-archive-list] .coach-day")) {
        day.hidden = ![...day.querySelectorAll(".coach-entry")].some((a) => !a.hidden);
      }
    });
    if (!narrativeOn) {
      const todaySlot = el.querySelector("[data-coach-today]");
      let watch = 0;
      const paintToday = (promise) => {
        const generation = ++watch;
        promise
          .then((dailyRes) => {
            if (generation !== watch || stale() || !el.isConnected || !todaySlot?.isConnected) return;
            todaySlot.hidden = false;
            todaySlot.innerHTML = ["morning", "evening"]
              .map((kind) =>
                dailyKindHtml(kind, dailyRes.data[kind], {
                  // Today's advice appears only here; the ids stay distinct from
                  // the day page's own daily-* headings.
                  headingId: `coach-today-${kind}`,
                  showRequestButtons: false,
                  showRefresh: false,
                }),
              )
              .join("");
          })
          .catch(() => {
            // A later successful retry can still fill this slot. Do not fail the feed.
            if (generation !== watch || !todaySlot?.isConnected) return;
            todaySlot.hidden = true;
          });
      };
      el._stopDailyWatch?.();
      if (subscribeDailyCoaching) {
        el._stopDailyWatch = subscribeDailyCoaching(paintToday);
        dailyCoaching?.();
      } else {
        paintToday(
          dailyCoaching
            ? dailyCoaching()
            : get("daily-coaching", {}, { timeoutMs: COACH_TIMEOUT_MS }),
        );
      }
    }
    bindReplies(el, (id) => entries.find((entry) => entry.id === id));
    const status = el.querySelector("[data-coach-status]");
    for (const button of el.querySelectorAll("[data-threshold-confirm]")) {
      button.addEventListener("click", async () => {
        button.disabled = true;
        status.textContent = "";
        try {
          await post(`coach/threshold-confirm/${button.dataset.thresholdConfirm}`);
          if (!stale() && el.isConnected) status.textContent = t("Saved.");
          await reload();
        } catch (error) {
          if (!stale() && el.isConnected) status.textContent = error.message;
          button.disabled = false;
        }
      });
    }
}
